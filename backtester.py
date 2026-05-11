"""
backtester.py  - PISTA 競輪版
過去レースデータに戦略を適用して回収率・的中率を計算する

払戻倍率:
  nishafuku: DBのnishafuku_payout（シグナル車が含まれるペア）
  wide:      DBのwide_payout（シグナル車が含まれるワイド）
"""

from __future__ import annotations
from dataclasses import dataclass, field
from feature_engine import group_by_race
from strategy_engine import StrategyConfig, BetSignal, apply_strategy

BET_AMOUNT = 100  # 1回の賭け金（円）


@dataclass
class BacktestResult:
    strategy_name:  str
    bet_type:       str
    total_bets:     int   = 0
    hits:           int   = 0
    total_invested: float = 0.0
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

    def summary(self) -> str:
        return (
            f"[{self.strategy_name}] "
            f"賭:{self.total_bets}回 "
            f"的中:{self.hits}回({self.hit_rate*100:.1f}%) "
            f"回収率:{self.recovery_rate*100:.1f}% "
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
    elif signal.bet_type == "wide":
        return pos <= 3, pos
    elif signal.bet_type == "sanrenfuku":
        return pos <= 3, pos
    return False, pos


def _estimate_return(
    signal: BetSignal,
    race_horses: list[dict],
    race_payouts: list[dict] | None = None,
) -> float:
    if not race_payouts:
        return 0.0

    car = signal.car_no

    if signal.bet_type == "nishafuku":
        for p in race_payouts:
            if p["bet_type"] == "nishafuku" and (p["car_no1"] == car or p["car_no2"] == car):
                return float(p["payout"])
        return 0.0

    if signal.bet_type == "wide":
        # ワイド: 3着以内に入る2頭の組み合わせ。シグナル車を含むワイドを合計
        total = 0.0
        for p in race_payouts:
            if p["bet_type"] == "wide" and (p["car_no1"] == car or p["car_no2"] == car):
                total = float(p["payout"])
                break  # 最初のワイド配当を使用
        return total

    if signal.bet_type == "tansho":
        for p in race_payouts:
            if p["bet_type"] == "tansho" and p["car_no1"] == car:
                return float(p["payout"])
        return 0.0

    if signal.bet_type in ("fukusho", "sanrenfuku"):
        for p in race_payouts:
            if p["bet_type"] == signal.bet_type and p["car_no1"] == car:
                return float(p["payout"])
        return 0.0

    return 0.0


def run_backtest(
    enriched_rows: list[dict],
    strategy: StrategyConfig,
    payouts_by_race: dict[str, list[dict]] | None = None,
) -> BacktestResult:
    races  = group_by_race(enriched_rows)
    result = BacktestResult(strategy_name=strategy.name, bet_type=strategy.bet_type)

    for race_id, horses in races.items():
        if not any(h.get("finish_pos") is not None for h in horses):
            continue

        signals = apply_strategy(horses, strategy)
        race_payouts = (payouts_by_race or {}).get(race_id, [])

        for sig in signals:
            hit, actual_pos = _is_hit(sig, horses)
            sig.actual_finish = actual_pos
            result.total_bets += 1
            result.total_invested += BET_AMOUNT
            if hit:
                result.hits += 1
                result.total_return += _estimate_return(sig, horses, race_payouts)
            result.signals.append(sig)

    return result


def is_live_ready(bt: BacktestResult) -> bool:
    """実運用基準: 賭回数 ≥ 50 かつ 回収率 ≥ 100%"""
    return bt.total_bets >= 50 and bt.recovery_rate >= 1.00
