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
            "name":       s.name,
            "bet_type":   s.bet_type,
            "params":     s.params,
            "hit_rate":   hit_rate_map.get(s.name, None),
            "avg_payout": getattr(s, "avg_payout", None),
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
                avg_payout=d.get("avg_payout"),
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

def cmd_fetch(years: int, specific_date: str | None, days: int | None = None):
    from data_fetcher import run_fetch
    if days is not None:
        logger.info(f"=== データ取得開始（直近{days}日分）===")
    elif years == 0:
        logger.info("=== データ取得開始（直近7日分）===")
    else:
        logger.info(f"=== データ取得開始（{years}年分）===")
    run_fetch(years=years, specific_date=specific_date, days=days)
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

    # 今日の出走表取得（days_ahead=0 → 今日分のみ。明日分は含めない）
    entry_rows, entry_lines = fetch_upcoming_entries(days_ahead=0)
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

    # picks 直後に照合実行（既に結果が出ているレースがあれば即照合）
    try:
        cmd_grade_signals()
    except Exception as e:
        logger.warning(f"picks 後の照合スキップ: {e}")


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
        hit_rate_map   = {s.name: s.hit_rate for s in live_strategies}
        avg_payout_map = {s.name: getattr(s, "avg_payout", None) for s in live_strategies}

        for race_id, horses in races.items():
            if not horses:
                continue
            h = horses[0]
            st = h.get("start_time", "")
            # racesテーブルのstart_timeが空なら更新（picks時点でkeirin.jpから取得済み）
            if st:
                c.execute(_db.sql("""
                    UPDATE races SET start_time = ?
                    WHERE race_id = ? AND (start_time IS NULL OR start_time = '')
                """), (st, race_id))
            for strategy in live_strategies:
                for sig in apply_strategy(horses, strategy):
                    ev_mark, _ = _ev_label(
                        sig.odds,
                        hit_rate_map.get(sig.strategy),
                        avg_payout=avg_payout_map.get(sig.strategy),
                    )
                    c.execute(_db.sql("""
                        INSERT OR IGNORE INTO signals
                        (date, race_id, venue, race_no, strategy, bet_type,
                         axis_car, racer_name, odds_at_pick, ev_mark, created_at,
                         start_time)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """), (
                        h.get("date", ""), race_id,
                        h.get("venue", ""), h.get("race_no", 0),
                        sig.strategy, sig.bet_type,
                        sig.car_no, sig.racer_name, sig.odds,
                        ev_mark, now,
                        st,
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


def cmd_retro(start_date: str, end_date: str):
    """指定期間の過去データに対してシミュレーション予想を実行し、結果照合まで行う。
    start_date / end_date: YYYY-MM-DD 形式
    """
    from data_fetcher import load_from_db, load_payouts_from_db
    from feature_engine import build_features, group_by_race
    from strategy_engine import apply_strategy
    from report_generator import _ev_label
    from datetime import datetime as _dt, date as _date, timedelta as _td
    import db as _db

    logger.info(f"=== レトロシミュレーション開始: {start_date} 〜 {end_date} ===")

    # 期間内のデータをロード（余裕を持って30日前まで読む）
    days_back = (_date.today() - _date.fromisoformat(start_date)).days + 30
    rows, lines_by_race = load_from_db(days=days_back)
    if not rows:
        logger.error("DBにデータがありません")
        return

    enriched = build_features(rows, lines_by_race)

    # 対象期間に絞り込む
    target = [r for r in enriched if start_date <= r.get("date", "") <= end_date]
    logger.info(f"対象期間のエントリ: {len(target)}件")

    live_strategies = load_live_strategies()
    payouts_by_race = load_payouts_from_db(days=days_back)

    conn = _db.get_connection()
    c    = _db.get_cursor(conn)
    now  = _dt.now().isoformat()
    hit_rate_map   = {s.name: s.hit_rate for s in live_strategies}
    avg_payout_map = {s.name: getattr(s, "avg_payout", None) for s in live_strategies}

    races = group_by_race(target)
    saved = 0
    for race_id, horses in races.items():
        if not horses:
            continue
        h = horses[0]
        payouts = payouts_by_race.get(race_id, [])
        payout_map = {(p["bet_type"].lower(), p["car_no1"]): p["payout"] for p in payouts}
        payout_map.update({(p["bet_type"].lower(), p["car_no2"]): p["payout"] for p in payouts if p.get("car_no2")})

        # 結果が取得済みかチェック
        c.execute(_db.sql(
            "SELECT COUNT(*) AS cnt FROM results WHERE race_id = ? AND finish_pos IS NOT NULL"
        ), (race_id,))
        res_row = c.fetchone()
        has_result = res_row and dict(res_row)["cnt"] > 0

        for strategy in live_strategies:
            for sig in apply_strategy(horses, strategy):
                ev_mark, _ = _ev_label(
                    sig.odds,
                    hit_rate_map.get(sig.strategy),
                    avg_payout=avg_payout_map.get(sig.strategy),
                )
                # 的中判定
                key = (sig.bet_type.lower(), sig.car_no)
                if has_result:
                    if key in payout_map:
                        is_hit = 1
                        actual_payout = payout_map[key]
                    else:
                        is_hit = 0
                        actual_payout = 0
                else:
                    is_hit = None
                    actual_payout = None

                c.execute(_db.sql("""
                    INSERT OR IGNORE INTO signals
                    (date, race_id, venue, race_no, strategy, bet_type,
                     axis_car, racer_name, odds_at_pick, ev_mark,
                     is_hit, actual_payout, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """), (
                    h.get("date", ""), race_id,
                    h.get("venue", ""), h.get("race_no", 0),
                    sig.strategy, sig.bet_type,
                    sig.car_no, sig.racer_name, sig.odds,
                    ev_mark, is_hit, actual_payout,
                    f"retro_{now}",
                ))
                saved += c.rowcount

    conn.commit()
    conn.close()

    # avg_payout を戦略ごとに計算して live_strategies.json を更新
    conn2 = _db.get_connection()
    c2    = _db.get_cursor(conn2)
    for strategy in live_strategies:
        c2.execute(_db.sql("""
            SELECT AVG(actual_payout) AS avg_p
            FROM signals
            WHERE strategy = ? AND is_hit = 1 AND actual_payout > 0
        """), (strategy.name,))
        row = c2.fetchone()
        if row:
            avg_p = dict(row).get("avg_p")
            if avg_p:
                strategy.avg_payout = round(avg_p, 1)
    conn2.close()
    save_live_strategies(live_strategies)

    logger.info(f"レトロシミュレーション完了: {saved}件保存")

    # サマリー表示
    conn3 = _db.get_connection()
    c3    = _db.get_cursor(conn3)
    c3.execute(_db.sql("""
        SELECT strategy, bet_type,
               COUNT(*) AS total,
               SUM(CASE WHEN is_hit=1 THEN 1 ELSE 0 END) AS hits,
               SUM(CASE WHEN is_hit=1 THEN actual_payout ELSE 0 END) AS payout_sum
        FROM signals
        WHERE created_at LIKE 'retro_%'
          AND date >= ? AND date <= ?
        GROUP BY strategy, bet_type
    """), (start_date, end_date))
    rows_summary = [dict(r) for r in c3.fetchall()]
    conn3.close()

    print(f"\n{'='*55}")
    print(f"  レトロシミュレーション結果  {start_date} 〜 {end_date}")
    print(f"{'='*55}")
    for r in rows_summary:
        total   = r["total"] or 0
        hits    = r["hits"] or 0
        paid    = r["payout_sum"] or 0
        invest  = total * 100
        roi     = (paid - invest) / invest * 100 if invest > 0 else 0
        hit_r   = hits / total * 100 if total > 0 else 0
        print(
            f"  {r['strategy']:12s} {r['bet_type']:10s}"
            f"  {total}件 的中{hits}件({hit_r:.0f}%)"
            f"  投資{invest:,}円 払戻{paid:,}円  ROI {roi:+.1f}%"
        )
    print(f"{'='*55}\n")


def main():
    parser = argparse.ArgumentParser(description="PISTA 競輪AI予想システム")
    parser.add_argument("--fetch",    action="store_true", help="データ取得")
    parser.add_argument("--optimize", action="store_true", help="バックテスト＆最適化（ルールベース）")
    parser.add_argument("--ml",       action="store_true", help="XGBoostによるML最適化")
    parser.add_argument("--picks",    action="store_true", help="今日の推奨車券")
    parser.add_argument("--retro",    action="store_true", help="レトロシミュレーション（--start〜--end）")
    parser.add_argument("--years",    type=int, default=2,   help="取得年数（--fetch 時）")
    parser.add_argument("--days",     type=int, default=None, help="取得日数（--fetch 時、--years より優先）")
    parser.add_argument("--date",     type=str, default=None, help="指定日 YYYYMMDD（--fetch 時）")
    parser.add_argument("--trials",   type=int, default=300, help="最適化試行数")
    parser.add_argument("--start",    type=str, default=None, help="レトロ開始日 YYYY-MM-DD")
    parser.add_argument("--end",      type=str, default=None, help="レトロ終了日 YYYY-MM-DD")
    args = parser.parse_args()

    if not any([args.fetch, args.optimize, args.ml, args.picks, args.retro]):
        parser.print_help()
        return

    live_strategies = None

    if args.fetch:
        cmd_fetch(years=args.years, specific_date=args.date, days=args.days)
        cmd_grade_signals()   # fetch後にシグナル結果を自動照合

    if args.optimize:
        live_strategies = cmd_optimize(n_trials=args.trials)

    if args.ml:
        cmd_ml()

    if args.retro:
        from datetime import date as _date, timedelta as _td
        start = args.start or (_date.today() - _td(days=7)).isoformat()
        end   = args.end   or _date.today().isoformat()
        cmd_retro(start, end)

    if args.picks:
        cmd_picks(live_strategies=live_strategies)


if __name__ == "__main__":
    main()
