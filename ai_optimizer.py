"""
ai_optimizer.py  - PISTA 競輪版
ランダムサーチで各戦略のパラメータを自動最適化する

Train/Test Split:
  - 古い70%でパラメータ最適化（学習）
  - 新しい30%で検証（テスト）
  - live_ready 判定はテストデータの結果のみで行う（過学習防止）
"""

from __future__ import annotations
import random
import logging
from dataclasses import dataclass

from strategy_engine import StrategyConfig
from backtester import run_backtest, BacktestResult, is_live_ready

logger = logging.getLogger(__name__)

# 戦略ごとのパラメータ探索空間
PARAM_SPACE: dict[str, dict] = {
    "LineLeader": {
        "min_racer_win_rate":   [0.05, 0.08, 0.10, 0.12, 0.15],
        "min_class_score":      [1, 2, 3, 4],
        "max_popularity":       [4, 5, 6, 7, 8],
        "min_line_size":        [1, 2, 3],
        "max_rival_line_class": [2.0, 3.0, 3.5, 4.0, 5.0],
    },
    "ClassValue": {
        "min_class_score":      [3, 4, 5],
        "min_popularity":       [2, 3, 4],
        "max_popularity":       [6, 7, 8, 9, 10],
        "min_line_class_edge":  [-1.0, -0.5, 0.0, 0.5, 1.0],
        "min_line_size":        [1, 2],
    },
    "GradeFilter": {
        "min_grade_score":    [3, 4, 5],
        "max_popularity":     [3, 4, 5, 6],
        "min_racer_win_rate": [0.08, 0.10, 0.12, 0.15, 0.18],
        "min_class_score":    [2, 3, 4],
    },
    "FormPeak": {
        "max_prev_finish":     [1, 2, 3],
        "max_days_since_last": [7, 14, 21, 28],
        "min_popularity":      [2, 3, 4, 5],
        "max_popularity":      [7, 8, 9, 10],
        "min_racer_win_rate":  [0.05, 0.08, 0.10, 0.12],
        # 三連単向け追加条件
        "min_line_size":       [1, 2, 3],    # 最低ライン人数（3=3車ライン必須）
        "require_line_leader": [0, 1],       # 1=ライン先頭のみ対象
    },
    "ValueHunt": {
        "min_popularity":    [3, 4, 5, 6],
        "max_popularity":    [8, 9, 10, 12],
        "max_line_position": [0, 1, 2],
        "min_racer_win_rate":[0.05, 0.08, 0.10],
        "max_prev_finish":   [3, 4, 5, 6],
        "min_line_size":     [1, 2],
    },
    "BankSpec": {
        "max_popularity":      [3, 4, 5, 6],
        "min_venue_win_rate":  [0.08, 0.10, 0.12, 0.15, 0.20],
        "min_class_score":     [2, 3, 4],
        "min_racer_win_rate":  [0.05, 0.08, 0.10, 0.12],
    },
}

BET_TYPE_MAP = {
    "LineLeader":  "nishafuku",
    "ClassValue":  "nishafuku",
    "GradeFilter": "nishafuku",
    "FormPeak":    "nishafuku",
    "ValueHunt":   "wide",
    "BankSpec":    "wide",
}


@dataclass
class OptimResult:
    strategy:      StrategyConfig
    backtest:      BacktestResult
    test_backtest: BacktestResult | None
    live_ready:    bool

    def summary(self) -> str:
        tag = "✅ 実運用OK" if self.live_ready else "❌ 未達"
        s = f"{tag}  {self.backtest.summary()}  params={self.strategy.params}"
        if self.test_backtest:
            s += f"\n         テスト: {self.test_backtest.summary()}"
        return s


def _split_rows(enriched_rows: list[dict], train_ratio: float = 0.7) -> tuple[list[dict], list[dict]]:
    """日付順で race_id 単位に train/test 分割"""
    race_dates: dict[str, str] = {}
    for row in enriched_rows:
        rid = row.get("race_id", "")
        if rid and rid not in race_dates:
            race_dates[rid] = row.get("date", "")

    sorted_races = sorted(race_dates.keys(), key=lambda r: race_dates[r])
    n_train = int(len(sorted_races) * train_ratio)
    train_ids = set(sorted_races[:n_train])
    test_ids  = set(sorted_races[n_train:])

    train_rows = [r for r in enriched_rows if r.get("race_id") in train_ids]
    test_rows  = [r for r in enriched_rows if r.get("race_id") in test_ids]

    if n_train > 0 and n_train < len(sorted_races):
        logger.info(
            f"データ分割: 学習={len(train_ids)}レース(〜{race_dates[sorted_races[n_train-1]]}), "
            f"テスト={len(test_ids)}レース({race_dates[sorted_races[n_train]]}〜)"
        )
    return train_rows, test_rows


def optimize(
    enriched_rows: list[dict],
    n_trials: int = 300,
    seed: int = 42,
    train_ratio: float = 0.7,
    payouts_by_race: dict | None = None,
    buy_mode: str = "line",   # "full"=全流し / "line"=ライン内流し（デフォルト）
) -> list[OptimResult]:
    random.seed(seed)
    train_rows, test_rows = _split_rows(enriched_rows, train_ratio)
    logger.info(f"学習: {len(train_rows)}行 / テスト: {len(test_rows)}行")

    # payouts を train/test に分割
    train_ids = {r["race_id"] for r in train_rows}
    test_ids  = {r["race_id"] for r in test_rows}
    pb = payouts_by_race or {}
    train_payouts = {k: v for k, v in pb.items() if k in train_ids}
    test_payouts  = {k: v for k, v in pb.items() if k in test_ids}

    results: list[OptimResult] = []

    for strategy_name, space in PARAM_SPACE.items():
        logger.info(f"最適化中: {strategy_name} ({n_trials}試行)")
        best_train_bt: BacktestResult | None = None
        best_strategy: StrategyConfig | None = None

        for _ in range(n_trials):
            params = {k: random.choice(v) for k, v in space.items()}

            # min/max 整合性
            if strategy_name in ("ClassValue", "FormPeak", "ValueHunt"):
                if params.get("min_popularity", 0) >= params.get("max_popularity", 99):
                    continue

            strategy = StrategyConfig(
                name=strategy_name,
                bet_type=BET_TYPE_MAP[strategy_name],
                params=params,
            )
            bt = run_backtest(train_rows, strategy, train_payouts, buy_mode=buy_mode)
            if bt.total_bets < 10:
                continue

            if best_train_bt is None or bt.recovery_rate > best_train_bt.recovery_rate:
                best_train_bt = bt
                best_strategy = strategy

        if best_strategy is None or best_train_bt is None:
            continue

        test_bt = run_backtest(test_rows, best_strategy, test_payouts, buy_mode=buy_mode)
        live    = is_live_ready(test_bt)

        optim = OptimResult(
            strategy=best_strategy,
            backtest=best_train_bt,
            test_backtest=test_bt,
            live_ready=live,
        )
        results.append(optim)

        label = "✅ 実運用OK" if live else "❌ 未達"
        logger.info(
            f"  学習: {best_train_bt.summary()}\n"
            f"  テスト: {test_bt.summary()} {label}"
        )

    results.sort(key=lambda r: (r.test_backtest.recovery_rate if r.test_backtest else 0), reverse=True)
    return results
