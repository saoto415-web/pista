"""
picks_fetcher.py  - PISTA 競輪版
今日・明日の出走表を取得して予測用データを構築する
"""

from __future__ import annotations
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

from data_fetcher import (
    fetch_race_ids_for_date, fetch_race_entry, VENUE_MAP,
    _make_race_id, init_db
)
from feature_engine import build_features

logger = logging.getLogger(__name__)

_FETCH_WORKERS = 4   # 並列リクエスト数（keirin.jp への同時接続数を抑制）


def _fetch_one_entry(args: tuple) -> tuple[str, dict | None]:
    """並列化用ヘルパー: (venue_code, date_str, race_no) → (race_id, entry_data)"""
    venue_code, date_str, race_no = args
    race_id    = _make_race_id(venue_code, date_str, race_no)
    entry_data = fetch_race_entry(venue_code, date_str, race_no)
    return race_id, entry_data


def fetch_upcoming_entries(days_ahead: int = 1) -> tuple[list[dict], dict[str, list[dict]]]:
    """
    今日と直近 days_ahead 日分の出走表を取得。
    複数レースを並列 HTTP リクエストで取得して高速化。
    戻り値: (entries_rows, lines_by_race)
    """
    init_db()
    target_dates = [date.today() + timedelta(days=i) for i in range(days_ahead + 1)]

    all_entries: list[dict] = []
    all_lines:   dict[str, list[dict]] = {}

    for target_date in target_dates:
        races = fetch_race_ids_for_date(target_date)
        if not races:
            logger.info(f"{target_date}: 開催なし or 取得失敗")
            continue

        logger.info(f"{target_date}: {len(races)}レース発見 → 並列取得開始（workers={_FETCH_WORKERS}）")

        # 並列で全レースの出走表を取得
        results_map: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=_FETCH_WORKERS) as exe:
            futures = {exe.submit(_fetch_one_entry, race): race for race in races}
            for fut in as_completed(futures):
                try:
                    race_id, entry_data = fut.result()
                    if entry_data:
                        results_map[race_id] = entry_data
                    else:
                        vc, ds, rno = futures[fut]
                        venue_name = VENUE_MAP.get(vc, (vc,))[0]
                        logger.warning(f"出走表取得失敗: {venue_name} R{rno}")
                except Exception as exc:
                    logger.warning(f"出走表取得例外: {exc}")

        # 元の races 順に並べて rows を構築（レース番号順）
        for venue_code, date_str, race_no in sorted(races, key=lambda x: (x[0], x[2])):
            race_id    = _make_race_id(venue_code, date_str, race_no)
            entry_data = results_map.get(race_id)
            if not entry_data:
                continue

            ri = entry_data["race_info"]
            for e in entry_data["entries"]:
                row = {
                    "race_id":    race_id,
                    "date":       ri["date"],
                    "venue":      ri["venue"],
                    "venue_code": ri["venue_code"],
                    "race_no":    ri["race_no"],
                    "race_name":  ri["race_name"],
                    "grade":      ri["grade"],
                    "num_racers": ri["num_racers"],
                    "bank_length": ri["bank_length"],
                    "start_time": ri.get("start_time", ""),
                    "finish_pos": None,   # 未来なのでNULL
                    "car_no":     e["car_no"],
                    "racer_no":   e.get("racer_no", ""),
                    "racer_name": e.get("racer_name", ""),
                    "class_rank": e.get("class_rank", ""),
                    "prefecture": e.get("prefecture", ""),
                    "age":        e.get("age", 0),
                    "racing_style": e.get("racing_style", ""),
                    "gear_ratio": e.get("gear_ratio", 0.0),
                    "time_sec":   None,
                    "odds":       e.get("odds", 0.0),
                    "popularity": e.get("popularity", 0),
                }
                all_entries.append(row)

            all_lines[race_id] = entry_data.get("lines", [])

    logger.info(f"出走表取得完了: {len(all_entries)}エントリ / {len(all_lines)}レース")
    return all_entries, all_lines


def build_picks_features(
    entry_rows: list[dict],
    lines_by_race: dict[str, list[dict]],
    history_rows: list[dict],
    history_lines: dict[str, list[dict]],
) -> list[dict]:
    """
    過去データ + 今日の出走表を結合して特徴量を構築する。
    過去データで選手履歴を積み上げてから今日分の特徴量を計算。
    """
    combined_rows  = history_rows + entry_rows
    combined_lines = {**history_lines, **lines_by_race}
    all_feat = build_features(combined_rows, combined_lines)

    # 今日分のレースIDセットで絞り込む
    today_race_ids = {r["race_id"] for r in entry_rows}
    return [f for f in all_feat if f["race_id"] in today_race_ids]
