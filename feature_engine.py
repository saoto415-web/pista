"""
feature_engine.py  - PISTA 競輪版
レース生データ + ライン情報から予測特徴量を計算する

競輪固有の核心特徴量:
  - ライン内位置（先頭/番手/三番手）
  - ライン規模・ライン平均クラス・ライン平均勝率
  - 対抗ラインの強さ
  - 選手クラススコア (S1=5 〜 A3=1)
  - 脚質コード
  - バンク長・グレード

汎用特徴量（PROVAから継承）:
  - 選手直近勝率・3着内率（90日・365日）
  - 前走成績・間隔
  - オッズの歪み（implied_prob / 実績の比率）
"""

from __future__ import annotations
from collections import defaultdict
from datetime import date, timedelta


# クラスを数値化 (高いほど強い)
CLASS_SCORE = {"S1": 5, "S2": 4, "A1": 3, "A2": 2, "A3": 1}

# 脚質コード
STYLE_CODE = {"逃": 0, "差": 1, "追": 2, "捲": 3, "自": 4}

# グレードスコア (高いほど上位グレード)
GRADE_SCORE = {"GP": 6, "G1": 5, "G2": 4, "G3": 3, "F1": 2, "F2": 1}


def build_features(
    rows: list[dict],
    lines_by_race: dict[str, list[dict]],
) -> list[dict]:
    """
    load_from_db() の出力を受け取り、各出走エントリに特徴量を付加して返す。

    Args:
        rows: races × results JOIN の行リスト
        lines_by_race: {race_id: [line_dict, ...]}
    Returns:
        特徴量付きの行リスト
    """
    rows = sorted(rows, key=lambda r: (r["date"], r["race_id"], r.get("car_no", 0)))

    # 選手別履歴バッファ: racer_name → [(date, finish_pos), ...]
    racer_hist:    dict[str, list[tuple[str, int]]] = defaultdict(list)
    venue_hist:    dict[tuple, list[tuple[str, int]]] = defaultdict(list)  # (racer, venue)
    grade_hist:    dict[tuple, list[tuple[str, int]]] = defaultdict(list)  # (racer, grade)

    WINDOW_90  = 90
    WINDOW_365 = 365

    def _win_place_rate(
        history: list[tuple[str, int]],
        current_date: str,
        window_days: int = 90,
        top_n: int = 3,      # n着内で「的中」
    ) -> tuple[float, float]:
        cutoff = (date.fromisoformat(current_date) - timedelta(days=window_days)).isoformat()
        recent = [pos for d, pos in history if d >= cutoff and pos is not None]
        if not recent:
            return 0.0, 0.0
        n = len(recent)
        win_r  = sum(1 for p in recent if p == 1) / n
        place_r = sum(1 for p in recent if p <= top_n) / n
        return win_r, place_r

    # 前走情報: racer_name → (date, finish_pos, grade)
    prev_info: dict[str, tuple[str, int | None, str]] = {}

    enriched = []

    for row in rows:
        race_date   = row.get("date", "")
        race_id     = row.get("race_id", "")
        racer_name  = row.get("racer_name", "")
        car_no      = row.get("car_no", 0)
        finish_pos  = row.get("finish_pos")
        class_rank  = row.get("class_rank", "")
        grade       = row.get("grade", "")
        venue       = row.get("venue", "")
        racing_style = row.get("racing_style", "")
        bank_length = row.get("bank_length", 400)
        num_racers  = row.get("num_racers", 9)
        odds        = row.get("odds", 0.0) or 0.0

        feat = dict(row)

        # --- 基本数値化 ---
        feat["class_score"]    = CLASS_SCORE.get(class_rank, 0)
        feat["grade_score"]    = GRADE_SCORE.get(grade, 0)
        feat["style_code"]     = STYLE_CODE.get(racing_style, -1)
        feat["is_333bank"]     = 1 if bank_length == 333 else 0
        feat["is_500bank"]     = 1 if bank_length == 500 else 0

        # --- 選手直近実績 ---
        feat["racer_win_rate"],   feat["racer_place_rate"]   = _win_place_rate(
            racer_hist[racer_name], race_date, WINDOW_90
        )
        feat["racer_win_r365"],   feat["racer_place_r365"]   = _win_place_rate(
            racer_hist[racer_name], race_date, WINDOW_365
        )

        # 競輪場別実績
        feat["venue_win_rate"],  feat["venue_place_rate"]  = _win_place_rate(
            venue_hist[(racer_name, venue)], race_date, WINDOW_365
        )

        # グレード別実績
        feat["grade_win_rate"],  feat["grade_place_rate"]  = _win_place_rate(
            grade_hist[(racer_name, grade)], race_date, WINDOW_365
        )

        # --- 前走情報 ---
        if racer_name in prev_info:
            prev_date, prev_pos, prev_grade = prev_info[racer_name]
            try:
                days = (date.fromisoformat(race_date) - date.fromisoformat(prev_date)).days
            except Exception:
                days = 999
            feat["days_since_last"]  = days
            feat["prev_finish_pos"]  = prev_pos if prev_pos is not None else 99
            feat["prev_grade_score"] = GRADE_SCORE.get(prev_grade, 0)
        else:
            feat["days_since_last"]  = 999
            feat["prev_finish_pos"]  = 99
            feat["prev_grade_score"] = 0

        # --- ライン特徴量（競輪の核心）---
        line_feats = _build_line_features(
            race_id, car_no, racer_name,
            lines_by_race.get(race_id, []),
            rows,                # 同一レースの全選手情報（クラス・勝率参照）
            racer_hist,
            race_date,
            WINDOW_365,
        )
        feat.update(line_feats)

        # --- オッズ歪み特徴量 ---
        implied_prob = 1.0 / odds if odds > 1.0 else 0.0
        feat["implied_prob"] = implied_prob

        rwr = feat["racer_win_rate"]
        if implied_prob > 0.01:
            feat["racer_value"]    = rwr / implied_prob
            # 期待値: 予測勝率 × オッズ
            feat["expected_value"] = rwr * odds
        else:
            feat["racer_value"]    = 0.0
            feat["expected_value"] = 0.0

        enriched.append(feat)

        # --- 履歴更新（この出走結果を記録）---
        if finish_pos is not None:
            racer_hist[racer_name].append((race_date, finish_pos))
            venue_hist[(racer_name, venue)].append((race_date, finish_pos))
            if grade:
                grade_hist[(racer_name, grade)].append((race_date, finish_pos))
            prev_info[racer_name] = (race_date, finish_pos, grade)

    # popularity=0 のレース（過去データ）に人気プロキシを補完
    # クラス×2 + 直近勝率×10 で降順ランク付け → popularity に設定
    race_groups: dict[str, list[dict]] = defaultdict(list)
    for feat in enriched:
        race_groups[feat["race_id"]].append(feat)

    for horses in race_groups.values():
        if all(h.get("popularity", 0) == 0 for h in horses):
            scored = sorted(
                horses,
                key=lambda h: h.get("class_score", 0) * 2 + h.get("racer_win_rate", 0) * 10,
                reverse=True,
            )
            for rank, h in enumerate(scored, 1):
                h["popularity"] = rank

    return enriched


def _build_line_features(
    race_id: str,
    car_no: int,
    racer_name: str,
    line_members: list[dict],       # この race_id のライン情報
    all_rows: list[dict],           # 全レース行（同一レースの選手を参照）
    racer_hist: dict[str, list],
    race_date: str,
    window_days: int,
) -> dict:
    """ライン特徴量を計算"""

    feats = {
        "line_no":           0,      # 所属ライン番号
        "line_position":     -1,     # ライン内位置 (0=先頭, 1=番手, 2=三番手)
        "line_size":         1,      # ライン人数
        "is_line_leader":    0,      # 先頭フラグ
        "line_avg_class":    0.0,    # ライン平均クラススコア
        "line_avg_win_rate": 0.0,    # ラインメンバー平均勝率
        "rival_line_size":   0.0,    # 最大対抗ライン人数
        "rival_line_class":  0.0,    # 最強対抗ライン平均クラス
        "line_class_edge":   0.0,    # 自ライン - 対抗ライン クラス差
    }

    if not line_members:
        return feats

    # 自分が所属するライン
    my_line = None
    my_pos  = -1
    for lm in line_members:
        if lm["car_no"] == car_no:
            my_line = lm["line_no"]
            my_pos  = lm["position"]
            break

    if my_line is None:
        return feats

    # 同一ライン・対抗ライン を分類
    line_groups: dict[int, list[dict]] = defaultdict(list)
    for lm in line_members:
        line_groups[lm["line_no"]].append(lm)

    my_members   = line_groups[my_line]
    rival_groups = [members for lno, members in line_groups.items() if lno != my_line]

    # 同一レース全選手の class_score と racer_win_rate を参照するためのマップ
    race_rows = [r for r in all_rows if r.get("race_id") == race_id]
    class_map = {r["car_no"]: CLASS_SCORE.get(r.get("class_rank", ""), 0) for r in race_rows}

    def avg_class(members):
        scores = [class_map.get(m["car_no"], 0) for m in members]
        return sum(scores) / len(scores) if scores else 0.0

    def avg_win_rate(members):
        """ラインメンバーの直近365日平均勝率"""
        rates = []
        for m in members:
            hist = racer_hist.get(m.get("racer_name", ""), [])
            cutoff = (date.fromisoformat(race_date) - timedelta(days=window_days)).isoformat()
            recent = [pos for d, pos in hist if d >= cutoff and pos is not None]
            if recent:
                rates.append(sum(1 for p in recent if p == 1) / len(recent))
        return sum(rates) / len(rates) if rates else 0.0

    my_avg_class = avg_class(my_members)
    my_avg_win   = avg_win_rate(my_members)

    # 最大ライン（人数）と最強ライン（クラス）
    rival_best_size  = max((len(g) for g in rival_groups), default=0)
    rival_best_class = max((avg_class(g) for g in rival_groups), default=0.0)

    feats["line_no"]           = my_line
    feats["line_position"]     = my_pos
    feats["line_size"]         = len(my_members)
    feats["is_line_leader"]    = 1 if my_pos == 0 else 0
    feats["line_avg_class"]    = my_avg_class
    feats["line_avg_win_rate"] = my_avg_win
    feats["rival_line_size"]   = rival_best_size
    feats["rival_line_class"]  = rival_best_class
    feats["line_class_edge"]   = my_avg_class - rival_best_class
    # 同一ライン内の他車番（買い目絞り込みに使用）
    feats["line_partner_cars"] = [m["car_no"] for m in my_members if m["car_no"] != car_no]

    return feats


def group_by_race(rows: list[dict]) -> dict[str, list[dict]]:
    """race_id をキーにしてレースごとにグループ化"""
    races: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        races[row["race_id"]].append(row)
    return dict(races)
