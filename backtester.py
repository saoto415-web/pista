"""
backtester.py  - PISTA 競輪版
過去レースデータに戦略を適用して回収率・的中率を計算する

買いモード (buy_mode) 一覧:
  ── 三連単 (sanrentan) ──
  "san_1fix_full"   : 1着固定 × 全流し        (n-1)×(n-2) 点
  "san_1fix_line"   : 1着固定 × ライン内流し   ライン内2・3着の順列
  "san_2fix_full"   : 1・2着固定 × 全流し      (n-2) 点
  "san_2fix_line"   : 1・2着固定 × ライン内    ライン番手固定、残り3着
  "san_box3"        : 3車ボックス              6点固定
  "san_box4"        : 4車ボックス              24点固定

  ── 三連複 (sanrenfuku) ──
  "sf_1jiku_full"   : 軸1頭 × 全流し          C(n-1, 2) 点
  "sf_1jiku_line"   : 軸1頭 × ライン内流し     C(ライン数, 2) 点
  "sf_2jiku_full"   : 軸2頭 × 全流し          (n-2) 点
  "sf_2jiku_line"   : 軸2頭 × ライン内        ライン番手固定、残り全員
  "sf_box3"         : 3車ボックス              1点固定
  "sf_box4"         : 4車ボックス              4点固定

  ── 2車複・ワイド（比較用）──
  "full"            : 軸1車 × 全流し（旧来のデフォルト）
  "line"            : 軸1車 × ライン内流し

信頼度:
  試行中   : サンプル <  50
  参考値   : サンプル  50〜199
  信頼可能 : サンプル 200〜999
  採用     : サンプル 1000以上 かつ 回収率 100%超
"""

from __future__ import annotations
import math
from dataclasses import dataclass, field
from feature_engine import group_by_race
from strategy_engine import StrategyConfig, BetSignal, apply_strategy

BET_UNIT = 100


# ── 信頼度 ─────────────────────────────────────────────────

def confidence_level(n_bets: int) -> str:
    if n_bets < 50:
        return "🔴 試行中"
    elif n_bets < 200:
        return "🟡 参考値"
    elif n_bets < 1000:
        return "🟢 信頼可能"
    else:
        return "✅ 採用候補"


def confidence_order(n_bets: int) -> int:
    """ソート用の数値（大きいほど信頼度高）"""
    if n_bets < 50:    return 0
    elif n_bets < 200: return 1
    elif n_bets < 1000: return 2
    else:               return 3


# ── 採用基準 ────────────────────────────────────────────────

def is_live_ready(bt: "BacktestResult") -> bool:
    """実運用基準: サンプル 1000回以上 かつ 回収率 100%超"""
    return bt.total_bets >= 1000 and bt.recovery_rate >= 1.00


# ── データクラス ─────────────────────────────────────────────

@dataclass
class BacktestResult:
    strategy_name:  str
    bet_type:       str
    buy_mode:       str
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

    @property
    def avg_cost(self) -> float:
        return self.total_invested / self.total_bets if self.total_bets else 0.0

    @property
    def confidence(self) -> str:
        return confidence_level(self.total_bets)

    def summary(self) -> str:
        return (
            f"[{self.strategy_name}|{self.buy_mode}] "
            f"賭:{self.total_bets}回 "
            f"的中:{self.hits}回({self.hit_rate*100:.1f}%) "
            f"平均コスト:{self.avg_cost:.0f}円 "
            f"回収率:{self.recovery_rate*100:.1f}%（流しコスト込み） "
            f"ROI:{self.roi*100:+.1f}% "
            f"信頼度:{self.confidence}"
        )


# ── コスト計算 ───────────────────────────────────────────────

def _calc_cost(sig: BetSignal, horses: list[dict], buy_mode: str) -> int:
    n = len(horses)
    partners = list(sig.line_partner_cars or [])
    p = len(partners)

    if buy_mode == "san_1fix_full":
        return BET_UNIT * max((n - 1) * (n - 2), 1)

    elif buy_mode == "san_1fix_line":
        return BET_UNIT * max(math.perm(p, 2) if p >= 2 else 1, 1)

    elif buy_mode == "san_2fix_full":
        return BET_UNIT * max(n - 2, 1)

    elif buy_mode == "san_2fix_line":
        # 番手を2着固定、残り全員3着 or ライン三番手のみ
        third_n = max(p - 1, 1)  # ライン番手を除いた残りのパートナー数
        return BET_UNIT * max(third_n, 1)

    elif buy_mode == "san_box3":
        return BET_UNIT * 6   # 3車ボックス = 3! = 6点

    elif buy_mode == "san_box4":
        return BET_UNIT * 24  # 4車ボックス = 4! = 24点

    elif buy_mode == "sf_1jiku_full":
        return BET_UNIT * max(math.comb(n - 1, 2), 1)

    elif buy_mode == "sf_1jiku_line":
        return BET_UNIT * max(math.comb(p, 2) if p >= 2 else 1, 1)

    elif buy_mode == "sf_2jiku_full":
        return BET_UNIT * max(n - 2, 1)

    elif buy_mode == "sf_2jiku_line":
        third_n = max(p - 1, 1)
        return BET_UNIT * max(third_n, 1)

    elif buy_mode == "sf_box3":
        return BET_UNIT * 1   # C(3,3) = 1点

    elif buy_mode == "sf_box4":
        return BET_UNIT * 4   # C(4,3) = 4点

    elif buy_mode == "line":
        return BET_UNIT * max(p, 1)

    else:  # "full"（デフォルト）
        return BET_UNIT * max(n - 1, 1)


# ── 払戻計算 ─────────────────────────────────────────────────

def _estimate_return(
    signal: BetSignal,
    horses: list[dict],
    race_payouts: list[dict] | None,
    buy_mode: str,
) -> float:
    if not race_payouts:
        return 0.0

    car      = signal.car_no
    partners = set(signal.line_partner_cars or [])
    bet_type = signal.bet_type

    # 2着として最もクラスの高いラインパートナー（2着固定系で使用）
    partner_list = sorted(partners)
    partner_2nd  = partner_list[0] if partner_list else None

    if bet_type == "sanrentan":
        total = 0.0
        for p in race_payouts:
            if p["bet_type"] != "sanrentan" or p.get("car_no1") != car:
                continue
            p2, p3 = p.get("car_no2"), p.get("car_no3")

            if buy_mode == "san_1fix_full":
                pass  # 全組み合わせ対象

            elif buy_mode == "san_1fix_line":
                if p2 not in partners or p3 not in partners:
                    continue

            elif buy_mode == "san_2fix_full":
                if p2 != partner_2nd:
                    continue  # 2着はラインの番手固定

            elif buy_mode == "san_2fix_line":
                if p2 != partner_2nd:
                    continue
                remaining = partners - {partner_2nd}
                if remaining and p3 not in remaining:
                    continue

            elif buy_mode in ("san_box3", "san_box4"):
                # ボックスは軸車を含む上位N車の全順列
                box_n = 3 if buy_mode == "san_box3" else 4
                top_cars = {h["car_no"] for h in sorted(
                    horses, key=lambda h: h.get("popularity", 99)
                )[:box_n]}
                if car not in top_cars or p2 not in top_cars or p3 not in top_cars:
                    continue

            total += float(p["payout"])
        return total

    elif bet_type == "sanrenfuku":
        total = 0.0
        for p in race_payouts:
            if p["bet_type"] != "sanrenfuku":
                continue
            involved = {p.get("car_no1"), p.get("car_no2"), p.get("car_no3")}
            if car not in involved:
                continue

            others = involved - {car}

            if buy_mode == "sf_1jiku_full":
                pass

            elif buy_mode == "sf_1jiku_line":
                if not others.issubset(partners):
                    continue

            elif buy_mode == "sf_2jiku_full":
                if partner_2nd not in involved:
                    continue

            elif buy_mode == "sf_2jiku_line":
                if partner_2nd not in involved:
                    continue
                remaining = partners - {partner_2nd}
                third = others - {partner_2nd}
                if remaining and not third.issubset(remaining):
                    continue

            elif buy_mode in ("sf_box3", "sf_box4"):
                box_n = 3 if buy_mode == "sf_box3" else 4
                top_cars = {h["car_no"] for h in sorted(
                    horses, key=lambda h: h.get("popularity", 99)
                )[:box_n]}
                if not involved.issubset(top_cars):
                    continue

            total += float(p["payout"])
        return total

    elif bet_type == "nishafuku":
        total = 0.0
        for p in race_payouts:
            if p["bet_type"] != "nishafuku":
                continue
            if not (p["car_no1"] == car or p["car_no2"] == car):
                continue
            if buy_mode == "line" and partners:
                other = p["car_no2"] if p["car_no1"] == car else p["car_no1"]
                if other not in partners:
                    continue
            total += float(p["payout"])
        return total

    elif bet_type == "wide":
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

    elif bet_type == "tansho":
        for p in race_payouts:
            if p["bet_type"] == "tansho" and p["car_no1"] == car:
                return float(p["payout"])

    elif bet_type == "fukusho":
        for p in race_payouts:
            if p["bet_type"] == "fukusho" and p["car_no1"] == car:
                return float(p["payout"])

    return 0.0


# ── 的中判定 ─────────────────────────────────────────────────

def _is_hit(signal: BetSignal, horses: list[dict], buy_mode: str) -> tuple[bool, int | None]:
    h = next((x for x in horses if x["car_no"] == signal.car_no), None)
    if not h:
        return False, None
    pos = h.get("finish_pos")
    if pos is None:
        return False, None

    partners = set(signal.line_partner_cars or [])
    partner_list = sorted(partners)
    partner_2nd  = partner_list[0] if partner_list else None

    if signal.bet_type == "sanrentan":
        if pos != 1:
            return False, pos
        # 2着・3着の着順を取得
        finishes = {x["car_no"]: x.get("finish_pos") for x in horses}
        p2_car = next((c for c, fp in finishes.items() if fp == 2), None)
        p3_car = next((c for c, fp in finishes.items() if fp == 3), None)

        if buy_mode == "san_1fix_full":
            return True, pos
        elif buy_mode == "san_1fix_line":
            return p2_car in partners and p3_car in partners, pos
        elif buy_mode in ("san_2fix_full", "san_2fix_line"):
            return p2_car == partner_2nd, pos
        elif buy_mode in ("san_box3", "san_box4"):
            box_n = 3 if buy_mode == "san_box3" else 4
            top_cars = {h2["car_no"] for h2 in sorted(
                horses, key=lambda h2: h2.get("popularity", 99)
            )[:box_n]}
            return p2_car in top_cars and p3_car in top_cars, pos

    elif signal.bet_type == "sanrenfuku":
        if pos > 3:
            return False, pos
        finishes = {x["car_no"]: x.get("finish_pos") for x in horses}
        top3 = {c for c, fp in finishes.items() if fp and fp <= 3}
        p2_car = next((c for c, fp in finishes.items() if fp == 2), None)

        if buy_mode == "sf_1jiku_full":
            return True, pos
        elif buy_mode == "sf_1jiku_line":
            others = top3 - {signal.car_no}
            return others.issubset(partners), pos
        elif buy_mode in ("sf_2jiku_full", "sf_2jiku_line"):
            return partner_2nd in top3, pos
        elif buy_mode in ("sf_box3", "sf_box4"):
            box_n = 3 if buy_mode == "sf_box3" else 4
            top_cars = {h2["car_no"] for h2 in sorted(
                horses, key=lambda h2: h2.get("popularity", 99)
            )[:box_n]}
            return top3.issubset(top_cars), pos

    elif signal.bet_type in ("fukusho", "nishafuku"):
        return pos <= 2, pos
    elif signal.bet_type in ("wide",):
        return pos <= 3, pos
    elif signal.bet_type == "tansho":
        return pos == 1, pos

    return False, pos


# ── メインのバックテスト関数 ─────────────────────────────────

def run_backtest(
    enriched_rows: list[dict],
    strategy: StrategyConfig,
    payouts_by_race: dict[str, list[dict]] | None = None,
    buy_mode: str = "full",
) -> BacktestResult:
    races  = group_by_race(enriched_rows)
    result = BacktestResult(
        strategy_name=strategy.name,
        bet_type=strategy.bet_type,
        buy_mode=buy_mode,
    )

    for race_id, horses in races.items():
        if not any(h.get("finish_pos") is not None for h in horses):
            continue

        signals      = apply_strategy(horses, strategy)
        race_payouts = (payouts_by_race or {}).get(race_id, [])

        for sig in signals:
            cost = _calc_cost(sig, horses, buy_mode)
            hit, actual_pos = _is_hit(sig, horses, buy_mode)
            sig.actual_finish = actual_pos
            result.total_bets     += 1
            result.total_invested += cost
            if hit:
                result.hits += 1
                result.total_return += _estimate_return(sig, horses, race_payouts, buy_mode)
            result.signals.append(sig)

    return result


# ── 買いモード定義 ────────────────────────────────────────────

# 賭け種 → 対応する買いモード一覧
BUY_MODES_FOR_BET_TYPE: dict[str, list[str]] = {
    "sanrentan":  [
        "san_1fix_full", "san_1fix_line",
        "san_2fix_full", "san_2fix_line",
        "san_box3",      "san_box4",
    ],
    "sanrenfuku": [
        "sf_1jiku_full", "sf_1jiku_line",
        "sf_2jiku_full", "sf_2jiku_line",
        "sf_box3",       "sf_box4",
    ],
    "nishafuku":  ["full", "line"],
    "wide":       ["full", "line"],
    "tansho":     ["full"],
    "fukusho":    ["full"],
}

BUY_MODE_LABEL: dict[str, str] = {
    "san_1fix_full":  "三連単①1着固定×全流し",
    "san_1fix_line":  "三連単②1着固定×ライン内",
    "san_2fix_full":  "三連単③1・2着固定×全流し",
    "san_2fix_line":  "三連単④1・2着固定×ライン内",
    "san_box3":       "三連単⑤3車BOX",
    "san_box4":       "三連単⑥4車BOX",
    "sf_1jiku_full":  "三連複①軸1頭×全流し",
    "sf_1jiku_line":  "三連複②軸1頭×ライン内",
    "sf_2jiku_full":  "三連複③軸2頭×全流し",
    "sf_2jiku_line":  "三連複④軸2頭×ライン内",
    "sf_box3":        "三連複⑤3車BOX",
    "sf_box4":        "三連複⑥4車BOX",
    "full":           "全流し",
    "line":           "ライン内流し",
}
