"""
ml_model.py  - PISTA 競輪版
XGBoostを使った車券期待値モデル

設計:
  - 各選手の特徴量 → XGBoostで「nishafuku/wideの的中確率」を予測
  - Isotonic Regressionでキャリブレーション
  - 予測確率 × 期待配当 > EV閾値 の選手だけ賭ける
  - Train/Testは時系列分割（70%/30%）
  - 配当はDBのpayouts_by_raceから実データを参照（charilotoにtansho/fukushoなし）
"""

from __future__ import annotations
import logging
from dataclasses import dataclass, field

import numpy as np
from xgboost import XGBClassifier
from sklearn.metrics import roc_auc_score
from sklearn.isotonic import IsotonicRegression

from backtester import BET_AMOUNT
from feature_engine import group_by_race

logger = logging.getLogger(__name__)

# nishafuku/wide の平均配当（DB実測値）/ 100円
AVG_PAYOUT = {
    "nishafuku": 13.19,
    "wide":       5.16,
}

FEATURE_COLS = [
    "popularity",
    "racer_win_rate",
    "racer_place_rate",
    "racer_win_r365",
    "racer_place_r365",
    "venue_win_rate",
    "venue_place_rate",
    "grade_win_rate",
    "grade_place_rate",
    "prev_finish_pos",
    "days_since_last",
    "prev_grade_score",
    "line_position",
    "line_size",
    "is_line_leader",
    "line_avg_class",
    "line_avg_win_rate",
    "rival_line_size",
    "rival_line_class",
    "line_class_edge",
    "class_score",
    "grade_score",
    "style_code",
    "is_333bank",
    "gear_ratio",
    "num_racers",
]


def _make_xy(rows: list[dict], target: str) -> tuple[np.ndarray, np.ndarray]:
    X, y = [], []
    for row in rows:
        pos = row.get("finish_pos")
        if pos is None:
            continue
        x = [float(row.get(col, 0) or 0) for col in FEATURE_COLS]
        if target == "nishafuku":
            label = 1 if pos <= 2 else 0
        elif target == "wide":
            label = 1 if pos <= 3 else 0
        else:
            label = 1 if pos == 1 else 0
        X.append(x)
        y.append(label)
    return np.array(X, dtype=np.float32), np.array(y, dtype=np.int8)


class CalibratedXGB:
    def __init__(self, model: XGBClassifier, calibrator: IsotonicRegression):
        self.model      = model
        self.calibrator = calibrator

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        raw = self.model.predict_proba(X)[:, 1]
        cal = self.calibrator.predict(raw)
        return np.column_stack([1 - cal, cal])


def train_model(
    train_rows: list[dict],
    target: str = "nishafuku",
    n_estimators: int = 300,
    max_depth: int = 4,
    learning_rate: float = 0.05,
) -> XGBClassifier | CalibratedXGB:
    X, y = _make_xy(train_rows, target)
    if len(X) == 0:
        raise ValueError("学習データが空です")

    pos_rate = y.mean()
    scale_pos_weight = (1 - pos_rate) / pos_rate if pos_rate > 0 else 1.0

    n_cal = max(int(len(X) * 0.2), 1)
    X_fit, X_cal = X[:-n_cal], X[-n_cal:]
    y_fit, y_cal = y[:-n_cal], y[-n_cal:]

    model = XGBClassifier(
        n_estimators=n_estimators,
        max_depth=max_depth,
        learning_rate=learning_rate,
        subsample=0.8,
        colsample_bytree=0.8,
        scale_pos_weight=scale_pos_weight,
        eval_metric="logloss",
        random_state=42,
        verbosity=0,
    )
    model.fit(X_fit, y_fit)

    train_auc = roc_auc_score(y_fit, model.predict_proba(X_fit)[:, 1])

    if len(X_cal) >= 50 and y_cal.sum() >= 3:
        raw_probs = model.predict_proba(X_cal)[:, 1]
        iso = IsotonicRegression(out_of_bounds="clip")
        iso.fit(raw_probs, y_cal)
        logger.info(
            f"学習完了 [{target}]: AUC={train_auc:.4f} "
            f"(fit={len(X_fit)}, cal={len(X_cal)}, pos_rate={pos_rate*100:.1f}%, calibrated)"
        )
        return CalibratedXGB(model, iso)

    logger.info(f"学習完了 [{target}]: AUC={train_auc:.4f} (n={len(X)}, pos_rate={pos_rate*100:.1f}%)")
    return model


def predict_proba(model, rows: list[dict]) -> list[float]:
    if not rows:
        return []
    X = np.array(
        [[float(row.get(col, 0) or 0) for col in FEATURE_COLS] for row in rows],
        dtype=np.float32
    )
    return model.predict_proba(X)[:, 1].tolist()


@dataclass
class MLBacktestResult:
    target:         str
    threshold:      float
    ev_threshold:   float
    total_bets:     int   = 0
    hits:           int   = 0
    total_invested: float = 0.0
    total_return:   float = 0.0
    test_auc:       float = 0.0

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
            f"[ML-{self.target}] "
            f"閾値={self.threshold:.2f}/EV>{self.ev_threshold:.1f} "
            f"賭:{self.total_bets}回 "
            f"的中:{self.hits}回({self.hit_rate*100:.1f}%) "
            f"回収率:{self.recovery_rate*100:.1f}% "
            f"ROI:{self.roi*100:+.1f}% "
            f"AUC:{self.test_auc:.4f}"
        )


def backtest_ml(
    model,
    test_rows: list[dict],
    target: str = "nishafuku",
    prob_threshold: float = 0.20,
    ev_threshold: float = 1.0,
    payouts_by_race: dict | None = None,
) -> MLBacktestResult:
    races  = group_by_race(test_rows)
    result = MLBacktestResult(target=target, threshold=prob_threshold, ev_threshold=ev_threshold)
    pb     = payouts_by_race or {}
    avg_payout_ratio = AVG_PAYOUT.get(target, 5.0)

    X_test, y_test = _make_xy(test_rows, target)
    if len(X_test) > 0 and y_test.sum() > 0:
        probs_all = model.predict_proba(X_test)[:, 1]
        result.test_auc = roc_auc_score(y_test, probs_all)

    for race_id, horses in races.items():
        valid = [h for h in horses if h.get("finish_pos") is not None]
        if not valid:
            continue

        probs       = predict_proba(model, valid)
        race_payouts = pb.get(race_id, [])
        candidates  = []

        for h, prob in zip(valid, probs):
            if prob < prob_threshold:
                continue
            # EV = predicted_prob × 平均配当倍率
            ev = prob * avg_payout_ratio
            if ev < ev_threshold:
                continue
            candidates.append((prob, ev, h))

        if not candidates:
            continue

        candidates.sort(key=lambda x: x[1], reverse=True)
        prob, ev, h = candidates[0]
        car = h["car_no"]
        pos = h.get("finish_pos")

        if target == "nishafuku":
            hit = pos is not None and pos <= 2
        elif target == "wide":
            hit = pos is not None and pos <= 3
        else:
            hit = pos == 1

        result.total_bets     += 1
        result.total_invested += BET_AMOUNT

        if hit:
            result.hits += 1
            # 実際の配当を検索
            actual_payout = next(
                (p["payout"] for p in race_payouts
                 if p["bet_type"] == target and (p["car_no1"] == car or p["car_no2"] == car)),
                0,
            )
            result.total_return += float(actual_payout) if actual_payout else BET_AMOUNT * avg_payout_ratio

    return result


def optimize_ml(
    train_rows: list[dict],
    test_rows: list[dict],
    payouts_by_race: dict | None = None,
) -> list[MLBacktestResult]:
    results = []

    for target in ["nishafuku", "wide"]:
        logger.info(f"ML最適化中: {target}")
        try:
            model = train_model(train_rows, target=target)
        except Exception as e:
            logger.warning(f"{target} 学習失敗: {e}")
            continue

        best: MLBacktestResult | None = None
        prob_thresholds = [0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50]
        ev_thresholds   = [0.80, 0.90, 1.00, 1.10, 1.20, 1.30, 1.40, 1.50]

        for prob_th in prob_thresholds:
            for ev_th in ev_thresholds:
                bt = backtest_ml(
                    model, test_rows,
                    target=target,
                    prob_threshold=prob_th,
                    ev_threshold=ev_th,
                    payouts_by_race=payouts_by_race,
                )
                if bt.total_bets < 20:
                    continue
                if best is None or bt.recovery_rate > best.recovery_rate:
                    best = bt

        if best:
            results.append(best)
            tag = "✅ 実運用OK" if best.recovery_rate >= 1.00 else "❌ 未達"
            logger.info(f"  最良: {best.summary()} {tag}")
        else:
            logger.info(f"  {target}: 賭回数不足")

    return results
