"""
strategy_engine.py  - PISTA 競輪版
車券戦略の定義と出走者リストへのシグナル適用

戦略タイプ:
  1. LineLeader    - ライン先頭選手の単勝（競輪最重要戦略）
  2. ClassValue    - 高クラスなのにオッズが歪んでいる選手
  3. GradeFilter   - 高グレードレースでの安定型複勝
  4. FormPeak      - 前走好走・短間隔の選手
  5. ValueHunt     - オッズ歪み（implied_probより実績が高い）を狙う
  6. BankSpec      - バンク特性に強い選手の複勝
"""

from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class StrategyConfig:
    name:     str
    bet_type: str          # "tansho" / "fukusho" / "nishafuku" / "sanrenfuku"
    params:   dict = field(default_factory=dict)
    hit_rate:   float | None = None  # バックテスト的中率（EV計算用）
    avg_payout: float | None = None  # 過去平均払戻額（暫定EV推計用）

    def default_params(self) -> dict:
        defaults = {
            "LineLeader": {
                "min_racer_win_rate":   0.08,
                "min_class_score":      2,
                "max_popularity":       6,
                "min_line_size":        2,
                "max_rival_line_class": 4.0,
            },
            "ClassValue": {
                "min_class_score":      4,       # S1/S2
                "min_popularity":       3,
                "max_popularity":       8,
                "min_racer_value":      0.8,     # 実績/市場 比率
                "min_line_size":        1,
            },
            "GradeFilter": {
                "min_grade_score":      4,       # G2以上
                "max_popularity":       4,
                "min_racer_win_rate":   0.12,
                "min_class_score":      3,       # A1以上
            },
            "FormPeak": {
                "max_prev_finish":      3,       # 前走3着以内
                "max_days_since_last":  14,      # 直近2週以内
                "min_popularity":       3,
                "max_popularity":       8,
                "min_racer_win_rate":   0.10,
            },
            "ValueHunt": {
                "min_popularity":       4,
                "max_popularity":       9,
                "min_racer_value":      1.2,     # 実績が市場より20%以上高い
                "min_racer_win_rate":   0.08,
                "max_prev_finish":      5,
                "min_line_size":        1,
            },
            "BankSpec": {
                "max_popularity":       5,
                "min_venue_win_rate":   0.12,    # バンク別勝率
                "min_class_score":      3,
                "min_racer_win_rate":   0.08,
            },
        }
        return {**defaults.get(self.name, {}), **self.params}


@dataclass
class BetSignal:
    race_id:    str
    race_date:  str
    venue:      str
    race_no:    int
    race_name:  str
    grade:      str
    car_no:     int
    racer_name: str
    class_rank: str
    popularity: int
    odds:       float
    bet_type:   str
    strategy:   str
    actual_finish:    int | None = None
    line_partner_cars: list = None   # 同一ライン内の他車番（ライン内流し用）

    def __post_init__(self):
        if self.line_partner_cars is None:
            self.line_partner_cars = []


def apply_strategy(race_horses: list[dict], strategy: StrategyConfig) -> list[BetSignal]:
    """
    1レース分の出走者リスト（特徴量付き）に戦略を適用してBetSignalを返す。
    複数頭ヒットしても実際に買うのは先頭1頭（最高人気優先）。
    """
    p = strategy.default_params()
    signals = []

    for h in race_horses:
        if not _passes(h, strategy.name, p):
            continue
        signals.append(BetSignal(
            race_id=h["race_id"],
            race_date=h["date"],
            venue=h.get("venue", ""),
            race_no=h.get("race_no", 0),
            race_name=h.get("race_name", ""),
            grade=h.get("grade", ""),
            car_no=h["car_no"],
            racer_name=h.get("racer_name", ""),
            class_rank=h.get("class_rank", ""),
            popularity=h.get("popularity", 99),
            odds=h.get("odds", 0.0),
            bet_type=strategy.bet_type,
            strategy=strategy.name,
            line_partner_cars=list(h.get("line_partner_cars") or []),
        ))

    if signals:
        signals.sort(key=lambda s: s.popularity)
        return [signals[0]]
    return []


def _passes(h: dict, name: str, p: dict) -> bool:
    pop = h.get("popularity", 99)
    if pop == 0:
        return False

    if name == "LineLeader":
        return (
            h.get("is_line_leader", 0) == 1
            and pop <= p["max_popularity"]
            and h.get("racer_win_rate", 0) >= p["min_racer_win_rate"]
            and h.get("class_score", 0) >= p["min_class_score"]
            and h.get("line_size", 1) >= p["min_line_size"]
            and h.get("rival_line_class", 99) <= p["max_rival_line_class"]
        )

    elif name == "ClassValue":
        return (
            h.get("class_score", 0) >= p["min_class_score"]
            and p["min_popularity"] <= pop <= p["max_popularity"]
            and h.get("line_size", 1) >= p["min_line_size"]
            and h.get("line_class_edge", -99) >= p.get("min_line_class_edge", -1.0)
        )

    elif name == "GradeFilter":
        return (
            h.get("grade_score", 0) >= p["min_grade_score"]
            and pop <= p["max_popularity"]
            and h.get("racer_win_rate", 0) >= p["min_racer_win_rate"]
            and h.get("class_score", 0) >= p["min_class_score"]
        )

    elif name == "FormPeak":
        return (
            h.get("prev_finish_pos", 99) <= p["max_prev_finish"]
            and h.get("days_since_last", 999) <= p["max_days_since_last"]
            and p["min_popularity"] <= pop <= p["max_popularity"]
            and h.get("racer_win_rate", 0) >= p["min_racer_win_rate"]
        )

    elif name == "ValueHunt":
        return (
            p["min_popularity"] <= pop <= p["max_popularity"]
            and h.get("line_position", -1) <= p.get("max_line_position", 1)
            and h.get("racer_win_rate", 0) >= p["min_racer_win_rate"]
            and h.get("prev_finish_pos", 99) <= p["max_prev_finish"]
            and h.get("line_size", 1) >= p["min_line_size"]
        )

    elif name == "BankSpec":
        return (
            pop <= p["max_popularity"]
            and h.get("venue_win_rate", 0) >= p["min_venue_win_rate"]
            and h.get("class_score", 0) >= p["min_class_score"]
            and h.get("racer_win_rate", 0) >= p["min_racer_win_rate"]
        )

    return False


def get_default_strategies() -> list[StrategyConfig]:
    return [
        StrategyConfig("LineLeader",  "tansho"),
        StrategyConfig("ClassValue",  "tansho"),
        StrategyConfig("GradeFilter", "fukusho"),
        StrategyConfig("FormPeak",    "tansho"),
        StrategyConfig("ValueHunt",   "tansho"),
        StrategyConfig("BankSpec",    "fukusho"),
    ]
