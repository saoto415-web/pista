"""
backtester.py  - PISTA 競輪版
過去レースデータに戦略を適用して回収率・的中率を計算する

買いモード (buy_mode):
  "full"  : 軸1車 全流し = (頭数-1) 点 … デフォルト
  "line"  : 軸1車 ライン内流し = ライン内の他車番のみ

コストモデル:
  full: (頭数-1) × 100円 / シグナル
  line: max(len(line_partner_cars), 1) × 100円 / シグナル

払戻:
  nishafuku full : 軸車を含む2車複の払戻（1件）
  nishafuku line : 軸車 + ライン内パートナーの2車複のみ集計
  wide full      : 軸車を含む全ワイドの合算
  wide line      : 軸車 + ライン内パートナーのワイドのみ合算
"""

from __future__ import annotations
from dataclasses import dataclass, field
from feature_engine import group_by_race
from strategy_engine import StrategyConfig, BetSignal, apply_strategy

BET_UNIT = 100  # 1点あたりの賭け金（円）


@dataclass
class BacktestResult:
    strategy_name:  str
    bet_type:       str
    total_bets:     int   = 0   # シグナル数（流し単位）
    hits:           int   = 0
    total_invested: float = 0.0  # 流しコスト込みの合計投資額
    total_return:   float = 0.0
    signals:        list[BetSignal] = field(default_factory=list)

    @property
    def hit_rate(self) -> float:
        return self.hits / self.total_bets if self.total_bets else 0.0

    @property
    def recovery_rate(self) -> float:
        return self.total_return / self.total_invested if self.total_invested else 0.0

    @property
    def roi(self) -> float:
        return (self.total_return - self.total_invested) / self.total_invested if self.total_invested else 0.0

    @property
    def avg_cost(self) -> float:
        """シグナル1件あたりの平均投資コスト（流し点数込み）"""
        return self.total_invested / self.total_bets if self.total_bets else 0.0

    def summary(self) -> str:
        return (
            f"[{self.strategy_name}] "
            f"賭:{self.total_bets}回 "
            f"的中:{self.hits}回({self.hit_rate*100:.1f}%) "
            f"平均コスト:{self.avg_cost:.0f}円 "
            f"回収率:{self.recovery_rate*100:.1f}%（流しコスト込み） "
            f"ROI:{self.roi*100:+.1f}%"
        )


def _is_hit(signal: BetSignal, race_horses: list[dict]) -> tuple[bool, int | None]:
    h = next((x for x in race_horses if x["car_no"] == signal.car_no), None)
    if not h:
        return False, None
    pos = h.get("finish_pos")
    if pos is None:
        return False, None

    if signal.bet_type == "tansho":
        return pos == 1, pos
    elif signal.bet_type in ("fukusho", "nishafuku"):
        return pos <= 2, pos
    elif signal.bet_type in ("wide", "sanrenfuku"):
        return pos <= 3, pos
    elif signal.bet_type == "sanrentan":
        return pos == 1, pos   # 軸1着固定で的中判定
    return False, pos


def _estimate_return(
    signal: BetSignal,
    race_horses: list[dict],
    race_payouts: list[dict] | None = None,
    buy_mode: str = "full",
) -> float:
    if not race_payouts:
        return 0.0

    car = signal.car_no
    partners = set(signal.line_partner_cars) if signal.line_partner_cars else set()

    if signal.bet_type == "nishafuku":
        total = 0.0
        for p in race_payouts:
            if p["bet_type"] != "nishafuku":
                continue
            if not (p["car_no1"] == car or p["car_no2"] == car):
                continue
            if buy_mode == "line" and partners:
                # ライン内流し: 相手がラインパートナーである組み合わせのみ
                other = p["car_no2"] if p["car_no1"] == car else p["car_no1"]
                if other not in partners:
                    continue
            total += float(p["payout"])
        return total

    if signal.bet_type == "wide":
        total = 0.0
        for p in race_payouts:
            if p["bet_type"] != "wide":
                continue
            if not (p["car_no1"] == car or p["car_no2"] == car):
                continue
            if buy_mode == "line" and partners:
                other = p["car_no2"] if p["car_no1"] == car else p["car_no1"]
                if other not in partners:
                    continue
            total += float(p["payout"])
        return total

    if signal.bet_type == "tansho":
        for p in race_payouts:
            if p["bet_type"] == "tansho" and p["car_no1"] == car:
                return float(p["payout"])
        return 0.0

    if signal.bet_type == "sanrenfuku":
        total = 0.0
        for p in race_payouts:
            if p["bet_type"] != "sanrenfuku":
                continue
            involved = {p.get("car_no1"), p.get("car_no2"), p.get("car_no3")}
            if car not in involved:
                continue
            if buy_mode == "line" and partners:
                if not partners.issubset(involved):
                    continue
            total += float(p["payout"])
        return total

    if signal.bet_type == "sanrentan":
        # 軸1着固定: car_no1 == axis
        total = 0.0
        for p in race_payouts:
            if p["bet_type"] != "sanrentan" or p.get("car_no1") != car:
                continue
            if buy_mode == "line" and partners:
                # ライン内2・3着の組み合わせのみ
                p2, p3 = p.get("car_no2"), p.get("car_no3")
                if p2 not in partners or p3 not in partners:
                    continue
            total += float(p["payout"])
        return total

    if signal.bet_type == "fukusho":
        for p in race_payouts:
            if p["bet_type"] == "fukusho" and p["car_no1"] == car:
                return float(p["payout"])
        return 0.0

    return 0.0


def run_backtest(
    enriched_rows: list[dict],
    strategy: StrategyConfig,
    payouts_by_race: dict[str, list[dict]] | None = None,
    buy_mode: str = "full",   # "full"=全流し / "line"=ライン内流し
) -> BacktestResult:
    """
    buy_mode:
      "full" : 軸1車 全流し（全頭を相手にする）
      "line" : 軸1車 ライン内流し（同一ライン内の選手だけを相手にする）
    """
    races  = group_by_race(enriched_rows)
    result = BacktestResult(
        strategy_name=f"{strategy.name}[{buy_mode}]",
        bet_type=strategy.bet_type,
    )

    for race_id, horses in races.items():
        if not any(h.get("finish_pos") is not None for h in horses):
            continue

        signals = apply_strategy(horses, strategy)
        race_payouts = (payouts_by_race or {}).get(race_id, [])

        for sig in signals:
            # 買いモードごとのコスト計算
            if buy_mode == "line" and sig.line_partner_cars:
                partners_n = len(sig.line_partner_cars)
                if sig.bet_type == "sanrentan":
                    # 軸1着 + ライン内2頭の順列 = P(partners, 2)
                    import math
                    n_combos = max(math.perm(partners_n, 2) if partners_n >= 2 else 1, 1)
                elif sig.bet_type == "sanrenfuku":
                    n_combos = max(partners_n, 1)   # ライン内の組み合わせ数
                else:
                    n_combos = max(partners_n, 1)   # 2車複/ワイドはライン相手数
            else:
                n_combos = max(len(horses) - 1, 1)     # 全流し（全頭 - 軸1頭）

            race_cost = BET_UNIT * n_combos

            hit, actual_pos = _is_hit(sig, horses)
            sig.actual_finish = actual_pos
            result.total_bets     += 1
            result.total_invested += race_cost
            if hit:
                result.hits += 1
                result.total_return += _estimate_return(sig, horses, race_payouts, buy_mode)
            result.signals.append(sig)

    return result


def is_live_ready(bt: BacktestResult) -> bool:
    """実運用基準（流しコスト込み）: 賭回数 ≥ 30 かつ 回収率 ≥ 100%"""
    return bt.total_bets >= 30 and bt.recovery_rate >= 1.00
