"""
ai_optimizer.py  - PISTA 競輪版
ランダムサーチで各戦略のパラメータを自動最適化する

設計:
  1. 各戦略について、PRIMARY買いモードでランダムサーチ（パラメータ最適化）
  2. 最良パラメータで全買いモード × 全賭け種を評価
  3. 結果を (strategy, bet_type, buy_mode) のマトリクスで保存

Train/Test Split:
  - 古い70%でパラメータ最適化（学習）
  - 新しい30%で検証（テスト）
  - 信頼度・採用基準はテストデータのサンプル数で判定
"""

from __future__ import annotations
import random
import logging
from dataclasses import dataclass, field

from strategy_engine import StrategyConfig
from backtester import (
    run_backtest, BacktestResult, is_live_ready,
    BUY_MODES_FOR_BET_TYPE, confidence_level,
)

logger = logging.getLogger(__name__)


# ── 戦略ごとのパラメータ探索空間 ─────────────────────────────

PARAM_SPACE: dict[str, dict] = {
    "LineLeader": {
        "min_racer_win_rate":   [0.05, 0.08, 0.10, 0.12, 0.15],
        "min_class_score":      [1, 2, 3, 4],
        "max_popularity":       [4, 5, 6, 7, 8],
        "min_line_size":        [1, 2, 3],
        "max_rival_line_class": [2.0, 3.0, 3.5, 4.0, 5.0],
    },
    "ClassValue": {
        "min_class_score":     [3, 4, 5],
        "min_popularity":      [2, 3, 4],
        "max_popularity":      [6, 7, 8, 9, 10],
        "min_line_class_edge": [-1.0, -0.5, 0.0, 0.5, 1.0],
        "min_line_size":       [1, 2],
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
        "min_line_size":       [1, 2, 3],
        "require_line_leader": [0, 1],
    },
    "ValueHunt": {
        "min_popularity":     [3, 4, 5, 6],
        "max_popularity":     [8, 9, 10, 12],
        "max_line_position":  [0, 1, 2],
        "min_racer_win_rate": [0.05, 0.08, 0.10],
        "max_prev_finish":    [3, 4, 5, 6],
        "min_line_size":      [1, 2],
    },
    "BankSpec": {
        "max_popularity":     [3, 4, 5, 6],
        "min_venue_win_rate": [0.08, 0.10, 0.12, 0.15, 0.20],
        "min_class_score":    [2, 3, 4],
        "min_racer_win_rate": [0.05, 0.08, 0.10, 0.12],
    },
}

# パラメータ探索時に使う賭け種（全買いモード評価の基準点）
PRIMARY_BET_TYPE: dict[str, str] = {
    "LineLeader":  "sanrentan",
    "ClassValue":  "sanrenfuku",
    "GradeFilter": "sanrenfuku",
    "FormPeak":    "sanrentan",
    "ValueHunt":   "sanrenfuku",
    "BankSpec":    "sanrenfuku",
}

# パラメータ探索時に使う買いモード
PRIMARY_BUY_MODE: dict[str, str] = {
    "sanrentan":  "san_1fix_full",
    "sanrenfuku": "sf_1jiku_full",
    "nishafuku":  "full",
    "wide":       "full",
}

# 各戦略で評価する賭け種一覧（三連単・三連複を主軸に、2車複・ワイドは比較用）
EVAL_BET_TYPES: dict[str, list[str]] = {
    "LineLeader":  ["sanrentan", "sanrenfuku", "nishafuku"],
    "ClassValue":  ["sanrentan", "sanrenfuku", "nishafuku"],
    "GradeFilter": ["sanrentan", "sanrenfuku", "nishafuku"],
    "FormPeak":    ["sanrentan", "sanrenfuku", "nishafuku", "wide"],
    "ValueHunt":   ["sanrentan", "sanrenfuku", "wide"],
    "BankSpec":    ["sanrentan", "sanrenfuku", "wide"],
}


# ── 結果クラス ────────────────────────────────────────────────

@dataclass
class BuyModeResult:
    """1つの (strategy, bet_type, buy_mode) 組み合わせの結果"""
    strategy_name: str
    bet_type:      str
    buy_mode:      str
    test_bt:       BacktestResult
    live_ready:    bool

    @property
    def confidence(self) -> str:
        return confidence_level(self.test_bt.total_bets, self.test_bt.recovery_rate)

    def summary(self) -> str:
        tag = "✅" if self.live_ready else "❌"
        return (
            f"{tag} [{self.strategy_name}|{self.bet_type}|{self.buy_mode}] "
            f"賭:{self.test_bt.total_bets}回 "
            f"的中:{self.test_bt.hits}回({self.test_bt.hit_rate*100:.1f}%) "
            f"回収率:{self.test_bt.recovery_rate*100:.1f}% "
            f"信頼度:{self.confidence}"
        )


@dataclass
class OptimResult:
    """1戦略の最適化結果（全買いモード × 賭け種を含む）"""
    strategy:      StrategyConfig
    train_bt:      BacktestResult        # 学習データでの最良バックテスト
    buy_results:   list[BuyModeResult] = field(default_factory=list)

    @property
    def live_ready(self) -> bool:
        return any(r.live_ready for r in self.buy_results)

    def best_result(self) -> BuyModeResult | None:
        if not self.buy_results:
            return None
        return max(self.buy_results, key=lambda r: r.test_bt.recovery_rate)

    def summary(self) -> str:
        best = self.best_result()
        if best:
            return f"[{self.strategy.name}] 最良: {best.summary()}"
        return f"[{self.strategy.name}] 結果なし"


# ── データ分割 ────────────────────────────────────────────────

def _split_rows(
    enriched_rows: list[dict],
    train_ratio: float = 0.7,
) -> tuple[list[dict], list[dict]]:
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


# ── メイン最適化関数 ──────────────────────────────────────────

def optimize(
    enriched_rows: list[dict],
    n_trials: int = 200,
    seed: int = 42,
    train_ratio: float = 0.7,
    payouts_by_race: dict | None = None,
) -> list[OptimResult]:
    """
    各戦略のパラメータをランダムサーチで最適化し、
    全買いモード × 賭け種の組み合わせを評価して返す。
    """
    random.seed(seed)
    train_rows, test_rows = _split_rows(enriched_rows, train_ratio)
    logger.info(f"学習: {len(train_rows)}行 / テスト: {len(test_rows)}行")

    pb = payouts_by_race or {}
    train_ids     = {r["race_id"] for r in train_rows}
    test_ids      = {r["race_id"] for r in test_rows}
    train_payouts = {k: v for k, v in pb.items() if k in train_ids}
    test_payouts  = {k: v for k, v in pb.items() if k in test_ids}

    results: list[OptimResult] = []

    for strategy_name, space in PARAM_SPACE.items():
        primary_bet  = PRIMARY_BET_TYPE[strategy_name]
        primary_mode = PRIMARY_BUY_MODE[primary_bet]

        logger.info(
            f"最適化中: {strategy_name} "
            f"({n_trials}試行 / 基準: {primary_bet}×{primary_mode})"
        )

        # ── Step1: ランダムサーチでパラメータ最適化 ──
        best_train_bt: BacktestResult | None = None
        best_strategy: StrategyConfig | None = None

        for _ in range(n_trials):
            params = {k: random.choice(v) for k, v in space.items()}

            if strategy_name in ("ClassValue", "FormPeak", "ValueHunt", "FormPeakSanrentan", "FormPeakSanrenfuku"):
                if params.get("min_popularity", 0) >= params.get("max_popularity", 99):
                    continue

            strategy = StrategyConfig(
                name=strategy_name,
                bet_type=primary_bet,
                params=params,
            )
            bt = run_backtest(
                train_rows, strategy, train_payouts,
                buy_mode=primary_mode,
            )
            if bt.total_bets < 10:
                continue

            if best_train_bt is None or bt.recovery_rate > best_train_bt.recovery_rate:
                best_train_bt = bt
                best_strategy = strategy

        if best_strategy is None or best_train_bt is None:
            logger.warning(f"  {strategy_name}: 有効な試行なし（スキップ）")
            continue

        logger.info(f"  最良学習: {best_train_bt.summary()}")

        # ── Step2: 全買いモード × 賭け種を評価 ──
        buy_results: list[BuyModeResult] = []
        eval_bet_types = EVAL_BET_TYPES.get(strategy_name, ["sanrentan", "sanrenfuku"])

        for bet_type in eval_bet_types:
            for buy_mode in BUY_MODES_FOR_BET_TYPE.get(bet_type, ["full"]):
                # 賭け種を切り替えた戦略を生成
                eval_strategy = StrategyConfig(
                    name=strategy_name,
                    bet_type=bet_type,
                    params=best_strategy.params,
                )
                test_bt = run_backtest(
                    test_rows, eval_strategy, test_payouts,
                    buy_mode=buy_mode,
                )
                live = is_live_ready(test_bt)
                br = BuyModeResult(
                    strategy_name=strategy_name,
                    bet_type=bet_type,
                    buy_mode=buy_mode,
                    test_bt=test_bt,
                    live_ready=live,
                )
                buy_results.append(br)
                logger.info(f"    {br.summary()}")

        optim = OptimResult(
            strategy=best_strategy,
            train_bt=best_train_bt,
            buy_results=buy_results,
        )
        results.append(optim)

    results.sort(
        key=lambda r: r.best_result().test_bt.recovery_rate if r.best_result() else 0,
        reverse=True,
    )
    return results
