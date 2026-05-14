"""
main.py  - PISTA 競輪AI 予想システム

使い方:
  # 1. データ取得（初回は --years 2 で2年分）
  python3 main.py --fetch --years 2

  # 2. バックテスト + ルールベース最適化
  python3 main.py --optimize

  # 3. XGBoostによるML最適化
  python3 main.py --ml

  # 4. 今日の推奨車券を出力
  python3 main.py --picks

  # 5. まとめて実行
  python3 main.py --fetch --optimize --ml --picks
"""

import argparse
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)
_LOG_DIR = Path(__file__).parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)
_handlers = [logging.StreamHandler()]
try:
    _handlers.append(logging.FileHandler(_LOG_DIR / "main.log", encoding="utf-8"))
except Exception:
    pass
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=_handlers,
)

LIVE_STRATEGIES_PATH = Path(__file__).parent / "reports" / "live_strategies.json"


def save_live_strategies(live_strategies, optim_results=None):
    hit_rate_map = {}
    if optim_results:
        for r in optim_results:
            if r.live_ready and r.test_backtest:
                hit_rate_map[r.strategy.name] = r.test_backtest.hit_rate
    data = [
        {
            "name":     s.name,
            "bet_type": s.bet_type,
            "params":   s.params,
            "hit_rate": hit_rate_map.get(s.name, None),
        }
        for s in live_strategies
    ]
    LIVE_STRATEGIES_PATH.parent.mkdir(exist_ok=True)
    LIVE_STRATEGIES_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"実運用戦略を保存: {LIVE_STRATEGIES_PATH} ({len(data)}戦略)")


def load_live_strategies():
    from strategy_engine import StrategyConfig, get_default_strategies
    if LIVE_STRATEGIES_PATH.exists():
        data = json.loads(LIVE_STRATEGIES_PATH.read_text(encoding="utf-8"))
        strategies = [
            StrategyConfig(
                name=d["name"], bet_type=d["bet_type"], params=d["params"],
                hit_rate=d.get("hit_rate"),
            )
            for d in data
        ]
        logger.info(f"保存済み戦略をロード: {len(strategies)}戦略")
        return strategies
    else:
        logger.warning("live_strategies.json が未作成。--optimize を先に実行してください。デフォルト戦略を使用します。")
        return get_default_strategies()


# ============================================================
# コマンド実装
# ============================================================

def cmd_fetch(years: int, specific_date: str | None):
    from data_fetcher import run_fetch
    logger.info(f"=== データ取得開始（{years}年分）===")
    run_fetch(years=years, specific_date=specific_date)
    logger.info("=== データ取得完了 ===")


def cmd_optimize(n_trials: int):
    from data_fetcher import load_from_db, load_payouts_from_db
    from feature_engine import build_features
    from ai_optimizer import optimize
    from report_generator import generate_backtest_report

    logger.info("=== バックテスト・最適化開始 ===")

    rows, lines_by_race = load_from_db(days=730)
    if not rows:
        logger.error("DBにデータがありません。先に --fetch を実行してください。")
        return []

    logger.info(f"読み込み: {len(rows)}行 / {len(lines_by_race)}レース")
    enriched = build_features(rows, lines_by_race)
    logger.info("特徴量生成完了")

    payouts_by_race = load_payouts_from_db(days=730)
    logger.info(f"払戻データ: {sum(len(v) for v in payouts_by_race.values())}件")

    results = optimize(enriched, n_trials=n_trials, payouts_by_race=payouts_by_race)

    report = generate_backtest_report(results)
    print("\n" + report)

    # DBにレポートを保存（Streamlitから参照できるように）
    try:
        import db as _db
        from datetime import datetime as _dt
        conn = _db.get_connection()
        c    = _db.get_cursor(conn)
        now  = _dt.now().isoformat()
        c.execute(_db.sql("""
            INSERT OR IGNORE INTO optimize_cache (id, report, updated_at) VALUES (?,?,?)
        """), ("latest", report, now))
        if c.rowcount == 0:
            c.execute(_db.sql(
                "UPDATE optimize_cache SET report=?, updated_at=? WHERE id=?"
            ), (report, now, "latest"))
        conn.commit()
        conn.close()
        logger.info("optimize_cache 保存完了")
    except Exception as e:
        logger.warning(f"optimize_cache 保存失敗（無視）: {e}")

    live_strategies = [r.strategy for r in results if r.live_ready]
    logger.info(f"実運用基準クリア: {len(live_strategies)}戦略")
    save_live_strategies(live_strategies, optim_results=results)

    return live_strategies


def cmd_ml():
    from data_fetcher import load_from_db, load_payouts_from_db
    from feature_engine import build_features
    from ai_optimizer import _split_rows
    from ml_model import optimize_ml

    logger.info("=== ML（XGBoost）最適化開始 ===")

    rows, lines_by_race = load_from_db(days=730)
    if not rows:
        logger.error("DBにデータがありません。先に --fetch を実行してください。")
        return

    logger.info(f"読み込み: {len(rows)}行")
    enriched = build_features(rows, lines_by_race)
    train_rows, test_rows = _split_rows(enriched, train_ratio=0.7)

    payouts_by_race = load_payouts_from_db(days=730)
    results = optimize_ml(train_rows, test_rows, payouts_by_race=payouts_by_race)

    print("\n# ML（XGBoost）バックテストレポート")
    print(f"学習期間: 古い70% / テスト期間: 新しい30%\n")
    live     = [r for r in results if r.recovery_rate >= 1.00]
    not_live = [r for r in results if r.recovery_rate < 1.00]

    print("## 実運用OK（テスト回収率≥100%）")
    if live:
        for r in live:
            print(f"  ✅ {r.summary()}")
    else:
        print("  なし")

    print("\n## 未達")
    for r in not_live:
        print(f"  ❌ {r.summary()}")


def cmd_picks(live_strategies=None):
    from data_fetcher import load_from_db
    from picks_fetcher import fetch_upcoming_entries, build_picks_features
    from report_generator import generate_picks_report

    logger.info("=== 今日の推奨車券生成 ===")

    # 過去データ（選手履歴構築用）
    # クラウド環境では90日に絞ってDB負荷・転送量を削減
    history_rows, history_lines = load_from_db(days=90)

    def _save_picks_cache(report: str):
        """picks_cache テーブルに保存（共通処理）"""
        try:
            import db as _db
            from datetime import date as _date, datetime as _datetime
            conn = _db.get_connection()
            c    = _db.get_cursor(conn)
            today = _date.today().isoformat()
            now   = _datetime.now().isoformat()
            c.execute(_db.sql("""
                INSERT OR IGNORE INTO picks_cache (date, report, updated_at)
                VALUES (?,?,?)
            """), (today, report, now))
            if c.rowcount == 0:
                c.execute(_db.sql(
                    "UPDATE picks_cache SET report=?, updated_at=? WHERE date=?"
                ), (report, now, today))
            conn.commit()
            conn.close()
            logger.info("picks_cache 保存完了")
        except Exception as e:
            logger.warning(f"picks_cache 保存失敗（無視）: {e}")

    # 今日の出走表取得
    entry_rows, entry_lines = fetch_upcoming_entries(days_ahead=1)
    if not entry_rows:
        msg = "出走表取得失敗（開催なし、または出走表未公開）\n（keirin.jp は毎朝8時頃に出走表を公開します）"
        logger.warning(msg)
        _save_picks_cache(msg)
        return

    odds_available = any(e.get("popularity", 0) > 0 for e in entry_rows)
    if not odds_available:
        logger.info("オッズ未確定（暫定ピックス）")

    feat_rows = build_picks_features(entry_rows, entry_lines, history_rows, history_lines)

    if live_strategies is None:
        live_strategies = load_live_strategies()

    report = generate_picks_report(feat_rows, live_strategies, odds_available=odds_available)
    print("\n" + report)

    picks_log = Path(__file__).parent / "logs" / "picks.log"
    picks_log.parent.mkdir(exist_ok=True)
    picks_log.write_text(report, encoding="utf-8")

    _save_picks_cache(report)

    # シグナルを signals テーブルに保存
    _save_signals(feat_rows, live_strategies)


def _save_signals(feat_rows: list, live_strategies: list):
    """picks のシグナルを signals テーブルに保存（重複は無視）"""
    try:
        import db as _db
        from strategy_engine import apply_strategy
        from feature_engine import group_by_race
        from report_generator import _ev_label
        from datetime import datetime as _dt

        conn = _db.get_connection()
        c    = _db.get_cursor(conn)
        now  = _dt.now().isoformat()

        races = group_by_race(feat_rows)
        hit_rate_map = {s.name: s.hit_rate for s in live_strategies}

        for race_id, horses in races.items():
            if not horses:
                continue
            h = horses[0]
            for strategy in live_strategies:
                for sig in apply_strategy(horses, strategy):
                    ev_mark, _ = _ev_label(sig.odds, hit_rate_map.get(sig.strategy))
                    c.execute(_db.sql("""
                        INSERT OR IGNORE INTO signals
                        (date, race_id, venue, race_no, strategy, bet_type,
                         axis_car, racer_name, odds_at_pick, ev_mark, created_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """), (
                        h.get("date", ""), race_id,
                        h.get("venue", ""), h.get("race_no", 0),
                        sig.strategy, sig.bet_type,
                        sig.car_no, sig.racer_name, sig.odds,
                        ev_mark, now,
                    ))
        conn.commit()
        conn.close()
        logger.info("signals 保存完了")
    except Exception as e:
        logger.warning(f"signals 保存失敗（無視）: {e}")


def cmd_grade_signals():
    """signals テーブルの未照合シグナルに結果（的中/外れ/払戻）を設定"""
    import db as _db

    conn = _db.get_connection()
    c    = _db.get_cursor(conn)

    # is_hit が NULL のシグナルを対象
    c.execute(_db.sql(
        "SELECT id, race_id, bet_type, axis_car FROM signals WHERE is_hit IS NULL"
    ))
    rows = [dict(r) for r in c.fetchall()]

    updated = 0
    for row in rows:
        race_id  = row["race_id"]
        bet_type = row["bet_type"].lower()
        axis     = row["axis_car"]

        # payouts テーブルで照合
        c.execute(_db.sql("""
            SELECT payout FROM payouts
            WHERE race_id = ?
              AND bet_type = ?
              AND (car_no1 = ? OR car_no2 = ?)
        """), (race_id, bet_type, axis, axis))
        payouts = [dict(r) for r in c.fetchall()]

        # results テーブルで着順を確認（レース結果が取得済みか）
        c.execute(_db.sql(
            "SELECT COUNT(*) AS cnt FROM results WHERE race_id = ? AND finish_pos IS NOT NULL"
        ), (race_id,))
        result_row = c.fetchone()
        if not result_row or dict(result_row)["cnt"] == 0:
            continue  # 結果未取得はスキップ

        if payouts:
            payout = payouts[0]["payout"]
            c.execute(_db.sql(
                "UPDATE signals SET is_hit=1, actual_payout=? WHERE id=?"
            ), (payout, row["id"]))
        else:
            c.execute(_db.sql(
                "UPDATE signals SET is_hit=0, actual_payout=0 WHERE id=?"
            ), (row["id"],))
        updated += 1

    conn.commit()
    conn.close()
    logger.info(f"signals 照合完了: {updated}件更新")


def main():
    parser = argparse.ArgumentParser(description="PISTA 競輪AI予想システム")
    parser.add_argument("--fetch",    action="store_true", help="データ取得")
    parser.add_argument("--optimize", action="store_true", help="バックテスト＆最適化（ルールベース）")
    parser.add_argument("--ml",       action="store_true", help="XGBoostによるML最適化")
    parser.add_argument("--picks",    action="store_true", help="今日の推奨車券")
    parser.add_argument("--years",    type=int, default=2,   help="取得年数（--fetch 時）")
    parser.add_argument("--date",     type=str, default=None, help="指定日 YYYYMMDD（--fetch 時）")
    parser.add_argument("--trials",   type=int, default=300, help="最適化試行数")
    args = parser.parse_args()

    if not any([args.fetch, args.optimize, args.ml, args.picks]):
        parser.print_help()
        return

    live_strategies = None

    if args.fetch:
        cmd_fetch(years=args.years, specific_date=args.date)
        cmd_grade_signals()   # fetch後にシグナル結果を自動照合

    if args.optimize:
        live_strategies = cmd_optimize(n_trials=args.trials)

    if args.ml:
        cmd_ml()

    if args.picks:
        cmd_picks(live_strategies=live_strategies)


if __name__ == "__main__":
    main()
