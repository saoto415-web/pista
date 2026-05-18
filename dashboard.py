"""
dashboard.py - PISTA 競輪AI ダッシュボード
起動: streamlit run dashboard.py
"""

import os
import re
import json
from pathlib import Path
from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import db as _db

BASE_DIR = Path(__file__).parent

# Streamlit Secrets から DATABASE_URL を OS 環境変数にも設定
# （subprocess で main.py を呼ぶ際に継承させるため）
if "DATABASE_URL" in st.secrets and not os.environ.get("DATABASE_URL"):
    os.environ["DATABASE_URL"] = st.secrets["DATABASE_URL"]
    # db モジュールも再初期化
    _db.DATABASE_URL = st.secrets["DATABASE_URL"]

st.set_page_config(page_title="PISTA 競輪AI", page_icon="🚴", layout="wide")

st.sidebar.title("🚴 PISTA 競輪AI")
page = st.sidebar.radio(
    "ページ選択",
    ["🏆 今日の買い目", "📈 成績・収支", "📊 戦略の成績", "🗃️ DB概要", "🔍 レース検索"],
)

# ──────────────────────────────────────────────
# ユーティリティ
# ──────────────────────────────────────────────

@st.cache_data(ttl=60)
def query_db(sql: str, params: tuple = ()) -> pd.DataFrame:
    conn = _db.get_connection()
    try:
        if _db.is_pg():
            # psycopg2: pd.read_sql_query との相性問題を避けてカーソルで取得
            c = _db.get_cursor(conn)
            c.execute(_db.sql(sql), params if params else None)
            rows = c.fetchall()
            if not rows:
                return pd.DataFrame()
            return pd.DataFrame([dict(r) for r in rows])
        else:
            return pd.read_sql_query(sql, conn, params=params if params else None)
    finally:
        conn.close()


def load_picks_from_db() -> str | None:
    """picks_cache テーブルから今日のピックスを取得"""
    try:
        conn = _db.get_connection()
        c    = _db.get_cursor(conn)
        c.execute(_db.sql("SELECT report FROM picks_cache WHERE date = ?"), (date.today().isoformat(),))
        row = c.fetchone()
        conn.close()
        return dict(row)["report"] if row else None
    except Exception:
        return None


def load_optimize_report() -> str | None:
    """optimize_cache テーブルから最新のレポートを取得"""
    try:
        conn = _db.get_connection()
        c    = _db.get_cursor(conn)
        c.execute(_db.sql("SELECT report FROM optimize_cache WHERE id = ?"), ("latest",))
        row = c.fetchone()
        conn.close()
        return dict(row)["report"] if row else None
    except Exception:
        return None


def parse_optimize_log() -> list[dict]:
    # まずDBから取得を試みる
    text = load_optimize_report()
    # DBになければローカルファイルを試みる（開発環境用）
    if not text:
        log_path = BASE_DIR / "logs" / "optimize.log"
        if not log_path.exists():
            return []
        text = log_path.read_text(encoding="utf-8")
    results = []
    # 「テスト:」行を1行に整形して解析
    for m in re.finditer(
        r"テスト: \[(\w+)\] 賭:(\d+)回 的中:(\d+)回\(([0-9.]+)%\) 回収率:([0-9.]+)% ROI:([+\-][0-9.]+)%\s*([✅❌])",
        text,
    ):
        results.append({
            "戦略":   m.group(1),
            "賭回数": int(m.group(2)),
            "的中数": int(m.group(3)),
            "的中率": float(m.group(4)),
            "回収率": float(m.group(5)),
            "ROI":    float(m.group(6)),
            "実運用": m.group(7) == "✅",
        })
    return results


def parse_ml_log() -> list[dict]:
    log_path = BASE_DIR / "logs" / "ml.log"
    if not log_path.exists():
        return []
    text = log_path.read_text(encoding="utf-8")
    results = []
    for m in re.finditer(
        r"最良: \[ML-(\w+)\] 閾値=([0-9.]+)/EV>([0-9.]+) "
        r"賭:(\d+)回 的中:(\d+)回\(([0-9.]+)%\) 回収率:([0-9.]+)% ROI:([+\-][0-9.]+)% AUC:([0-9.]+)",
        text,
    ):
        results.append({
            "モデル":    f"ML-{m.group(1)}",
            "閾値":      float(m.group(2)),
            "EV閾値":    float(m.group(3)),
            "賭回数":    int(m.group(4)),
            "的中数":    int(m.group(5)),
            "的中率":    float(m.group(6)),
            "回収率":    float(m.group(7)),
            "ROI":       float(m.group(8)),
            "AUC":       float(m.group(9)),
        })
    return results


# ──────────────────────────────────────────────
# ページ 1: Today's Picks
# ──────────────────────────────────────────────

def parse_picks_report(text: str) -> list[dict]:
    """picks.log のテキストをレースごとの dict リストに変換"""
    races = []
    current = None
    for line in text.splitlines():
        # レースヘッダー 【会場 Rn ...】
        m = re.match(r"【(.+?) R(\d+)", line)
        if m:
            if current:
                races.append(current)
            current = {
                "venue": m.group(1).split("　")[0].strip(),
                "race":  int(m.group(2)),
                "date":  "", "bank": "", "start_time": "",
                "racers": [], "picks": [],
            }
            continue
        if current is None:
            continue
        # 日付・バンク・発走時刻
        dm = re.search(r"日付: (\S+)", line)
        if dm:
            current["date"] = dm.group(1)
        bm = re.search(r"バンク: (\S+)", line)
        if bm:
            current["bank"] = bm.group(1)
        tm = re.search(r"発走: (\d{1,2}:\d{2})", line)
        if tm:
            current["start_time"] = tm.group(1)
        # 出走者行（先頭スペース + 数字車）
        rm = re.match(r"\s+(\d+)車 (.+?) \[([^\]]*)\] ([^\|]+)\| (.+)$", line)
        if rm:
            current["racers"].append({
                "car":   rm.group(1),
                "name":  rm.group(2).strip(),
                "cls":   rm.group(3).strip(),
                "style": rm.group(4).strip(),
                "line":  rm.group(5).strip(),
            })
        # 推奨車券行（◎ or △ or 🔶 or ★）
        pm = re.match(r"\s+([◎△🔶★]) (\w+) (\d+)車 (.+?) \[([^\]]*)\] \((.+?)\) ← (\w+)", line)
        if pm:
            current["picks"].append({
                "ev_mark":  pm.group(1),
                "bet_type": pm.group(2),
                "car":      pm.group(3),
                "name":     pm.group(4).strip(),
                "odds_str": pm.group(6),
                "strategy": pm.group(7),
            })
        # EV説明行（EV=... / 暫定EV≈...）
        em = re.match(r"\s{7}((?:EV=|暫定EV≈|オッズ未確定).+)", line)
        if em and current["picks"]:
            current["picks"][-1]["ev_desc"] = em.group(1)
        # 合計行（買い方）
        tm = re.search(r"合計: (\d+)円（1点100円）", line)
        if tm and current["picks"]:
            current["picks"][-1]["total_yen"] = int(tm.group(1))
        # 相手行
        sm = re.search(r"相手: (.+?)車（全(\d+)点）", line)
        if sm and current["picks"]:
            current["picks"][-1]["aite"] = sm.group(1)
            current["picks"][-1]["n_combos"] = int(sm.group(2))
    if current:
        races.append(current)
    return [r for r in races if r["picks"]]


if page == "🏆 今日の買い目":
    st.title("🏆 今日の買い目")
    st.caption(f"生成日: {date.today()}")

    strat_path = BASE_DIR / "reports" / "live_strategies.json"
    picks_log  = BASE_DIR / "logs" / "picks.log"

    col1, col2, col3 = st.columns([3, 1, 1])
    with col1:
        if st.button("🔄 最新予想を取得", type="primary"):
            log_area = st.empty()
            logs = []
            def _log(msg):
                logs.append(msg)
                log_area.code("\n".join(logs))

            _log("▶ 開始...")
            try:
                import sys as _sys
                _sys.path.insert(0, str(BASE_DIR))
                _log("▶ main をインポート中...")
                from main import cmd_picks
                _log("▶ keirin.jp から出走表取得中（数分かかります）...")
                cmd_picks()
                _log("✅ 完了！")
                st.success("✅ ピックス更新完了！")
            except Exception as e:
                import traceback
                _log(f"❌ エラー: {e}")
                _log(traceback.format_exc())
                st.error(f"エラー: {e}")
            st.rerun()
    with col2:
        if strat_path.exists():
            strategies = json.loads(strat_path.read_text(encoding="utf-8"))
            st.metric("使用中の戦略", f"{len(strategies)} 戦略")
    with col3:
        if picks_log.exists():
            mtime = picks_log.stat().st_mtime
            from datetime import datetime
            st.caption(f"最終更新: {datetime.fromtimestamp(mtime).strftime('%H:%M')}")

    st.divider()

    # picks.log（ローカル）または picks_cache テーブル（クラウド）から取得
    picks_text = None
    if picks_log.exists():
        picks_text = picks_log.read_text(encoding="utf-8")
    if not picks_text:
        picks_text = load_picks_from_db()

    if not picks_text:
        st.info("「最新予想を取得」ボタンを押すと、今日の推奨を表示します。")
    else:
        # 出走表未公開メッセージの場合
        if "出走表取得失敗" in picks_text or ("開催なし" in picks_text and "【" not in picks_text):
            st.warning("🕐 出走表はまだ公開されていません。\n\nkeirin.jp は毎朝 **8時頃** に出走表を公開します。更新後に「ピックス更新」を再実行してください。")
            st.stop()

        races = parse_picks_report(picks_text)

        if not races:
            if "開催なし" in picks_text or "取得失敗" in picks_text:
                st.info("🚫 本日は開催なし、または出走表が未公開です。")
            else:
                st.warning("推奨車券が見つかりません。オッズ確定後（10〜11時頃）に再取得してください。")
        else:
            is_provisional = "オッズ未確定" in picks_text
            if is_provisional:
                st.warning("⚠️ オッズはまだ確定していません — 10〜11時以降に再取得するとオッズが表示されます")

            st.subheader(f"📋 今日の推奨（{len(races)}件）")

            # 発走時刻 → 時系列ソート（時刻がある場合は時刻順、ない場合はrace_no順）
            def _sort_key(r):
                st_raw = r.get("start_time", "")
                if st_raw and re.match(r"\d{1,2}:\d{2}", st_raw):
                    h, mn = map(int, st_raw.split(":"))
                    return (h * 60 + mn, r["race"])
                return (9999, r["race"])

            races_sorted = sorted(races, key=_sort_key)

            # JST基準で「今日」「これから」のレースのみ表示
            from datetime import datetime as _dt, timezone, timedelta as _td
            _JST = timezone(_td(hours=9))
            _now_jst = _dt.now(_JST)
            today_str   = _now_jst.strftime("%Y-%m-%d")
            now_minutes = _now_jst.hour * 60 + _now_jst.minute

            def _is_today(r):
                """今日の日付のレースのみ通す。日付不明は今日扱い。"""
                d = r.get("date", "")
                if not d or len(d) < 8:
                    return True   # 日付不明 → 時刻フィルターに委ねる
                # YYYY-MM-DD形式以外（"|" など解析ミス）は除外
                if not re.match(r"\d{4}-\d{2}-\d{2}", d):
                    return False
                return d == today_str

            def _is_upcoming(r):
                """発走時刻が5分以上前でないものを通す。時刻不明は通す。"""
                st_raw = r.get("start_time", "")
                if st_raw and re.match(r"\d{1,2}:\d{2}", st_raw):
                    h, mn = map(int, st_raw.split(":"))
                    return (h * 60 + mn) >= now_minutes - 5
                return True

            races_sorted = [r for r in races_sorted if _is_today(r) and _is_upcoming(r)]

            # 今日分のレースが0件 → 古いキャッシュの可能性
            if not races_sorted and races:
                st.warning(
                    "⚠️ キャッシュされたデータに今日のレースが見つかりません。\n\n"
                    "「🔄 ピックス更新」ボタンで最新データを取得してください。"
                )

            BET_COLOR = {"NISHAFUKU": "#1f77b4", "WIDE": "#2ca02c"}

            for r in races_sorted:
                venue  = r["venue"]
                race_n = r["race"]
                picks  = r["picks"]
                pick   = picks[0]
                btype  = pick["bet_type"]
                color  = BET_COLOR.get(btype, "#888")

                bet_name  = "2車複" if btype == "NISHAFUKU" else "ワイド"
                total_yen = pick.get("total_yen", "?")
                ev_mark   = pick.get("ev_mark", "△")
                ev_desc   = pick.get("ev_desc", "オッズ未確定")
                is_go        = ev_mark == "◎"
                is_prov      = ev_mark == "🔶"
                bg_color  = "#e8f5e9" if is_go else ("#fff3e0" if is_prov else "#fff8e1")
                bd_color  = "#2ecc71" if is_go else ("#e67e22" if is_prov else "#f39c12")
                go_label  = "◎ 買い推奨" if is_go else ("🔶 暫定推奨" if is_prov else "△ 様子見")

                start_time = r.get("start_time", "")
                time_badge = f"🕐{start_time} " if start_time else ""
                label = f"{time_badge}{ev_mark} **{venue} R{race_n}**　{bet_name}　軸{pick['car']}車 {pick['name']}　{total_yen}円　← {pick['strategy']}"
                with st.expander(label):
                    # EV バッジ
                    st.markdown(
                        f'<span style="background:{bd_color};color:white;padding:4px 12px;'
                        f'border-radius:12px;font-weight:bold">{go_label}</span>'
                        f'&nbsp;&nbsp;<span style="color:#555;font-size:0.9em">{ev_desc}</span>',
                        unsafe_allow_html=True,
                    )
                    st.markdown("")

                    # 買い方ボックス
                    aite     = pick.get("aite", "")
                    n_combos = pick.get("n_combos", "?")
                    st.markdown(
                        f"""
<div style="background:{bg_color};border-left:4px solid {bd_color};padding:10px 16px;border-radius:4px;margin-bottom:12px">
<b>🎯 買い方ガイド（ウィンチケット）</b><br>
① 「{bet_name}」を選択<br>
② 軸：<b>{pick['car']}車</b>（{pick['name']}）を選択<br>
③ 相手：<b>{aite}車</b>をすべて選択<br>
④ 全 <b>{n_combos}点</b> × 100円 ＝ <b>合計 {total_yen}円</b>
</div>
""",
                        unsafe_allow_html=True,
                    )

                    c1, c2 = st.columns([1, 2])
                    with c1:
                        st.markdown(f"**賭種:** `{bet_name}`")
                        st.markdown(f"**推奨軸:** {pick['car']}車 **{pick['name']}**")
                        st.markdown(f"**オッズ:** {pick['odds_str']}")
                        st.markdown(f"**戦略:** {pick['strategy']}")
                    with c2:
                        if r["racers"]:
                            df_r = pd.DataFrame(r["racers"])
                            df_r.columns = ["車番", "選手名", "クラス", "脚質", "ライン"]
                            def highlight_pick(row):
                                c_str = f"background-color: {color}33; font-weight:bold" if row["車番"] == pick["car"] else ""
                                return [c_str] * len(row)
                            st.dataframe(
                                df_r.style.apply(highlight_pick, axis=1),
                                hide_index=True, use_container_width=True,
                            )

    # 実運用戦略の詳細
    if strat_path.exists():
        st.divider()
        st.subheader("使用中の戦略")
        strategies = json.loads(strat_path.read_text(encoding="utf-8"))
        for s in strategies:
            with st.expander(f"**{s['name']}** ({s['bet_type'].upper()})"):
                hr = s.get("hit_rate")
                ap = s.get("avg_payout")
                bet = "2車複" if s.get("bet_type","").lower() == "nishafuku" else "ワイド"
                hr_str = f"{hr*100:.1f}%" if hr else "-"
                ap_str = f"平均{ap:.0f}円" if ap else "-"
                st.markdown(f"**賭種:** {bet}　**的中率:** {hr_str}　**平均払戻:** {ap_str}")


# ──────────────────────────────────────────────
# ページ 2: 収支・実績
# ──────────────────────────────────────────────

elif page == "📈 成績・収支":
    st.title("📈 成績・収支")

    tab_signal, tab_bet = st.tabs(["🤖 AI予想の成績", "💰 自分の賭け記録"])

    # ────────────────────────────────
    # タブ1: シグナル実績
    # ────────────────────────────────
    with tab_signal:
        st.subheader("🤖 AI予想の成績")
        st.caption("AIが予想した車券を自動記録し、レース後に的中/外れを自動判定します")

        # 🔄 更新・照合ボタン
        col_ref, col_fetch = st.columns([1, 2])
        with col_ref:
            if st.button("🔄 表示更新", key="refresh_signals"):
                st.cache_data.clear()
                st.rerun()
        with col_fetch:
            if st.button("📥 今日の結果を取得", key="fetch_and_grade",
                         help="今日のレース結果を取得して的中/外れを確認します（2〜3分かかります）"):
                _log_area = st.empty()
                _logs = []
                def _lg(msg):
                    _logs.append(msg)
                    _log_area.code("\n".join(_logs))
                try:
                    import sys as _sys
                    _sys.path.insert(0, str(BASE_DIR))
                    _lg("▶ レース結果を取得中...")
                    from main import cmd_fetch, cmd_grade_signals
                    cmd_fetch(years=0, specific_date=None)
                    _lg("✅ 取得完了。的中/外れを確認中...")
                    cmd_grade_signals()
                    _lg("✅ 確認完了！")
                    st.cache_data.clear()
                    st.success("今日のレース結果を取得しました！")
                except Exception as e:
                    import traceback as _tb
                    _lg(f"❌ エラー: {e}\n{_tb.format_exc()}")
                    st.error(f"エラー: {e}")
                st.rerun()
        st.caption("レースが終わったら「今日の結果を取得」を押すと的中/外れが反映されます（毎日21時頃に自動更新）")

        # 🔁 過去の答え合わせ（列の外に置く：列内のexpanderはStreamlitの制約でボタンが動かない）
        with st.expander("🔁 過去の答え合わせ"):
            from datetime import date as _d, timedelta as _td_r
            rc1, rc2 = st.columns(2)
            with rc1:
                _r_start = st.date_input("開始日", value=_d.today() - _td_r(days=7), key="retro_start")
            with rc2:
                _r_end = st.date_input("終了日", value=_d.today() - _td_r(days=1), key="retro_end")
            if st.button("▶ 答え合わせを実行", key="run_retro", type="primary"):
                _log_area2 = st.empty()
                _logs2 = []
                def _lg2(msg):
                    _logs2.append(msg)
                    _log_area2.code("\n".join(_logs2))
                try:
                    import sys as _sys2
                    _sys2.path.insert(0, str(BASE_DIR))
                    _lg2(f"▶ {_r_start} 〜 {_r_end} の答え合わせを開始...")
                    from main import cmd_retro
                    cmd_retro(str(_r_start), str(_r_end))
                    _lg2("✅ 完了！")
                    st.cache_data.clear()
                    st.success("答え合わせが完了しました！")
                except Exception as e:
                    import traceback as _tb2
                    _lg2(f"❌ エラー: {e}\n{_tb2.format_exc()}")
                    st.error(f"エラー: {e}")
                st.rerun()

        df_sig = query_db("""
            SELECT date, venue, race_no, strategy, bet_type,
                   axis_car, racer_name, odds_at_pick, ev_mark,
                   is_hit, actual_payout
            FROM signals
            ORDER BY date DESC, race_no
        """)

        if df_sig.empty:
            st.info("まだAI予想の記録がありません。「最新予想を取得」を押すと記録が始まります。")
        else:
            # 照合済みデータのみで集計
            df_graded = df_sig[df_sig["is_hit"].notna()].copy()
            df_graded["is_hit"] = df_graded["is_hit"].astype(int)
            df_graded["actual_payout"] = df_graded["actual_payout"].fillna(0).astype(int)

            if not df_graded.empty:
                total   = len(df_graded)
                hits    = df_graded["is_hit"].sum()
                hit_pct = hits / total * 100 if total else 0
                total_paid = df_graded["actual_payout"].sum()
                total_bet  = total * 100
                roi = (total_paid - total_bet) / total_bet * 100 if total_bet else 0

                c1, c2, c3, c4, c5 = st.columns(5)
                c1.metric("結果確認済み", f"{total}件")
                c2.metric("的中数",   f"{int(hits)}件")
                c3.metric("的中率",   f"{hit_pct:.1f}%")
                c4.metric("払戻合計", f"{total_paid:,}円")
                c5.metric("ROI",      f"{roi:+.1f}%",
                          delta_color="normal" if roi >= 0 else "inverse")

                st.divider()

                # 戦略別集計
                st.subheader("戦略別成績")
                grp = df_graded.groupby("strategy").agg(
                    賭回数=("is_hit", "count"),
                    的中数=("is_hit", "sum"),
                    払戻合計=("actual_payout", "sum"),
                ).reset_index()
                grp["的中率(%)"] = (grp["的中数"] / grp["賭回数"] * 100).round(1)
                grp["投資額"]    = grp["賭回数"] * 100
                grp["ROI(%)"]    = ((grp["払戻合計"] - grp["投資額"]) / grp["投資額"] * 100).round(1)
                st.dataframe(grp[["strategy","賭回数","的中数","的中率(%)","払戻合計","ROI(%)"]],
                             hide_index=True, use_container_width=True)

                # 累積損益チャート
                st.subheader("損益の推移")
                df_sorted = df_graded.sort_values("date").reset_index(drop=True)
                df_sorted["損益"] = df_sorted["actual_payout"] - 100
                df_sorted["累積損益"] = df_sorted["損益"].cumsum()
                fig_cum = px.line(df_sorted, y="累積損益",
                                  labels={"index": "シグナル番号", "累積損益": "累積損益（円）"},
                                  title="累積損益推移（1点100円換算）",
                                  color_discrete_sequence=["#2ecc71"])
                fig_cum.add_hline(y=0, line_dash="dash", line_color="gray")
                fig_cum.update_layout(
                    plot_bgcolor="#0e1117", paper_bgcolor="#0e1117",
                    font_color="white", height=300,
                )
                st.plotly_chart(fig_cum, use_container_width=True)

            # ── シグナル詳細（買い方指示カード形式）
            st.divider()
            st.subheader("📋 予想の詳細")

            # 日付ごとにグループ化して表示
            sig_dates = sorted(df_sig["date"].unique(), reverse=True)
            for sig_date in sig_dates:
                df_day = df_sig[df_sig["date"] == sig_date].copy()
                # 的中/外れ/未確定の件数を集計
                n_hit   = int((df_day["is_hit"] == 1).sum())
                n_miss  = int((df_day["is_hit"] == 0).sum())
                n_tbd   = int(df_day["is_hit"].isna().sum())
                day_pay = int(df_day["actual_payout"].fillna(0).sum())
                day_lbl = f"📅 {sig_date}　✅{n_hit}的中 ❌{n_miss}外れ ⏳{n_tbd}未確定"
                if n_hit > 0:
                    day_lbl += f"　💰払戻合計 {day_pay:,}円"

                with st.expander(day_lbl, expanded=(sig_date == sig_dates[0])):
                    for _, row in df_day.iterrows():
                        is_hit_val  = row.get("is_hit")
                        payout_val  = int(row.get("actual_payout") or 0)
                        bet_raw     = str(row.get("bet_type", "")).upper()
                        bet_name    = "2車複" if "NISHAFUKU" in bet_raw else "ワイド"
                        ev_mark     = row.get("ev_mark", "")
                        ev_go       = ev_mark == "◎"
                        ev_prov     = ev_mark in ("🔶", "retro")
                        odds_val    = row.get("odds_at_pick") or 0

                        # 結果バッジ
                        if is_hit_val == 1:
                            res_icon   = "◎ 的中"
                            res_detail = f"💰 {payout_val:,}円払戻 (投資100円 → +{payout_val - 100:,}円)"
                            bd_color   = "#2ecc71"
                            bg_color   = "#e8f5e9"
                        elif is_hit_val == 0:
                            res_icon   = "✗ 外れ"
                            res_detail = "（-100円）"
                            bd_color   = "#e74c3c"
                            bg_color   = "#ffebee"
                        else:
                            res_icon   = "⏳ 結果待ち"
                            res_detail = "（今日の結果取得後に自動確認）"
                            bd_color   = "#f39c12"
                            bg_color   = "#fff8e1"

                        ev_label_str = (
                            "◎ 買い推奨" if ev_go
                            else ("🔶 暫定推奨" if ev_prov else "△ 様子見")
                        )
                        odds_str     = f"{int(odds_val):,}円" if odds_val > 0 else "オッズ未確定"
                        card_title   = (
                            f"**{res_icon}** &nbsp;|&nbsp; "
                            f"{row['venue']} R{row['race_no']} &nbsp;{bet_name}&nbsp; "
                            f"軸**{row['axis_car']}**車 {row['racer_name']} "
                            f"← {row['strategy']}"
                        )
                        st.markdown(card_title, unsafe_allow_html=True)

                        # 買い方ボックス
                        st.markdown(
                            f"""
<div style="background:{bg_color};border-left:4px solid {bd_color};
     padding:10px 16px;border-radius:4px;margin:4px 0 10px 0;font-size:0.95em">
<b>🎯 買い方ガイド（ウィンチケット）</b><br>
① 「{bet_name}」を選択<br>
② 軸：<b>{row['axis_car']}車</b>（{row['racer_name']}）を選択<br>
③ 相手：残り全車を選択<br>
④ 100円 × 組み合わせ数 を購入<br>
<span style="color:#888;font-size:0.9em">収益性: {ev_label_str} ／ オッズ目安: {odds_str}</span><br>
<b style="color:{bd_color}">{res_icon} {res_detail}</b>
</div>
""",
                            unsafe_allow_html=True,
                        )
                    # 区切り
                    st.markdown("---")

    # ────────────────────────────────
    # タブ2: 実際の賭け記録
    # ────────────────────────────────
    with tab_bet:
        st.subheader("💰 賭け記録入力")

        with st.form("bet_form", clear_on_submit=True):
            fc1, fc2, fc3 = st.columns(3)
            with fc1:
                bet_date    = st.date_input("日付", value=date.today())
                bet_venue   = st.text_input("会場", placeholder="例: 松阪")
            with fc2:
                bet_race_no = st.number_input("レース番号", min_value=1, max_value=12, value=1, step=1)
                bet_type    = st.selectbox("賭種", ["2車複(NISHAFUKU)", "ワイド(WIDE)"])
            with fc3:
                bet_axis    = st.number_input("軸車番", min_value=1, max_value=9, value=1, step=1)
                bet_amount  = st.number_input("賭け金合計(円)", min_value=100, value=700, step=100)

            fc4, fc5 = st.columns(2)
            with fc4:
                bet_result  = st.selectbox("結果", ["未確定", "的中", "外れ"])
            with fc5:
                bet_payout  = st.number_input("払戻額(円)", min_value=0, value=0, step=100)

            bet_strategy = st.text_input("戦略名（任意）", placeholder="例: FormPeak")
            bet_notes    = st.text_input("メモ（任意）")
            submitted    = st.form_submit_button("💾 記録する", type="primary")

        if submitted:
            try:
                conn = _db.get_connection()
                c    = _db.get_cursor(conn)
                is_hit = {"的中": 1, "外れ": 0, "未確定": None}[bet_result]
                profit = bet_payout - bet_amount if is_hit == 1 else (-bet_amount if is_hit == 0 else None)
                bet_t  = "nishafuku" if "NISHAFUKU" in bet_type else "wide"
                from datetime import datetime as _dtt
                c.execute(_db.sql("""
                    INSERT INTO bets
                    (date, race_id, venue, race_no, strategy, bet_type,
                     axis_car, amount, is_hit, payout, profit, notes, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """), (
                    bet_date.isoformat(),
                    f"{bet_date.strftime('%Y%m%d')}??{bet_race_no:02d}",
                    bet_venue, bet_race_no,
                    bet_strategy, bet_t,
                    bet_axis, bet_amount,
                    is_hit, bet_payout if is_hit == 1 else 0,
                    profit, bet_notes,
                    _dtt.now().isoformat(),
                ))
                conn.commit()
                conn.close()
                st.success("✅ 記録しました！")
                st.cache_data.clear()
            except Exception as e:
                st.error(f"保存エラー: {e}")

        # 賭け記録一覧
        st.divider()
        df_bets = query_db("""
            SELECT date, venue, race_no, strategy, bet_type,
                   axis_car, amount, is_hit, payout, profit, notes
            FROM bets ORDER BY date DESC, race_no DESC
        """)

        if df_bets.empty:
            st.info("まだ賭け記録がありません。上のフォームから入力してください。")
        else:
            # サマリ
            df_b = df_bets.copy()
            df_decided = df_b[df_b["is_hit"].notna()].copy()
            if not df_decided.empty:
                df_decided["is_hit"]  = df_decided["is_hit"].astype(int)
                df_decided["profit"]  = df_decided["profit"].fillna(0).astype(int)
                df_decided["payout"]  = df_decided["payout"].fillna(0).astype(int)
                df_decided["amount"]  = df_decided["amount"].astype(int)

                total_b   = len(df_decided)
                hits_b    = int(df_decided["is_hit"].sum())
                total_amt = int(df_decided["amount"].sum())
                total_pay = int(df_decided["payout"].sum())
                net       = int(df_decided["profit"].sum())
                roi_b     = net / total_amt * 100 if total_amt else 0

                b1, b2, b3, b4, b5 = st.columns(5)
                b1.metric("賭回数",     f"{total_b}回")
                b2.metric("的中数",     f"{hits_b}回")
                b3.metric("合計投資額", f"{total_amt:,}円")
                b4.metric("合計払戻",   f"{total_pay:,}円")
                b5.metric("収支",       f"{net:+,}円",
                          delta_color="normal" if net >= 0 else "inverse")

                # 累積収支チャート
                df_sorted_b = df_decided.sort_values("date").reset_index(drop=True)
                df_sorted_b["累積収支"] = df_sorted_b["profit"].cumsum()
                fig_b = px.line(df_sorted_b, y="累積収支",
                                title="累積収支推移",
                                color_discrete_sequence=["#f39c12"])
                fig_b.add_hline(y=0, line_dash="dash", line_color="gray")
                fig_b.update_layout(
                    plot_bgcolor="#0e1117", paper_bgcolor="#0e1117",
                    font_color="white", height=280,
                )
                st.plotly_chart(fig_b, use_container_width=True)

            # 一覧テーブル
            st.subheader("賭け記録一覧")
            df_b["is_hit"] = df_b["is_hit"].map(
                lambda v: "◎ 的中" if v == 1 else ("✗ 外れ" if v == 0 else "⏳ 未確定")
            )
            df_b["profit"] = df_b["profit"].apply(
                lambda v: f"{int(v):+,}円" if v is not None and str(v) != "None" else "-"
            )
            df_b.columns = ["日付","会場","R","戦略","賭種","軸車","金額","結果","払戻","収支","メモ"]
            st.dataframe(df_b, hide_index=True, use_container_width=True)


# ──────────────────────────────────────────────
# ページ 3: 戦略パフォーマンス
# ──────────────────────────────────────────────

elif page == "📊 戦略の成績":
    st.title("📊 戦略の成績")

    # ── ルールベース最適化結果 ──
    st.subheader("AI戦略の成績")
    opt_results = parse_optimize_log()
    if opt_results:
        df_opt = pd.DataFrame(opt_results)

        # 回収率バーチャート
        colors = ["#2ecc71" if r else "#e74c3c" for r in df_opt["実運用"]]
        fig = go.Figure(go.Bar(
            x=df_opt["戦略"], y=df_opt["回収率"],
            marker_color=colors,
            text=[f"{v:.1f}%" for v in df_opt["回収率"]],
            textposition="outside",
        ))
        fig.add_hline(y=100, line_dash="dash", line_color="white", annotation_text="100% ライン")
        fig.update_layout(
            title="テスト期間 回収率（緑=実運用OK）",
            yaxis_title="回収率 (%)", xaxis_title="",
            plot_bgcolor="#0e1117", paper_bgcolor="#0e1117",
            font_color="white", height=380,
        )
        st.plotly_chart(fig, use_container_width=True)

        # テーブル
        df_disp = df_opt[["戦略", "賭回数", "的中率", "回収率", "ROI", "実運用"]].copy()
        df_disp["的中率"]  = df_disp["的中率"].map("{:.1f}%".format)
        df_disp["回収率"]  = df_disp["回収率"].map("{:.1f}%".format)
        df_disp["ROI"]     = df_disp["ROI"].map("{:+.1f}%".format)
        df_disp["実運用"]  = df_disp["実運用"].map({True: "✅", False: "❌"})
        st.dataframe(df_disp, use_container_width=True, hide_index=True)
    else:
        st.info("--optimize を実行すると結果が表示されます。")

    st.divider()

    # ── ML結果 ──
    st.subheader("XGBoostモデル（--ml）")
    ml_results = parse_ml_log()
    if ml_results:
        df_ml = pd.DataFrame(ml_results)

        col1, col2 = st.columns(2)
        with col1:
            fig2 = go.Figure(go.Bar(
                x=df_ml["モデル"], y=df_ml["回収率"],
                marker_color="#3498db",
                text=[f"{v:.1f}%" for v in df_ml["回収率"]],
                textposition="outside",
            ))
            fig2.add_hline(y=100, line_dash="dash", line_color="white")
            fig2.update_layout(
                title="回収率", yaxis_title="(%)",
                plot_bgcolor="#0e1117", paper_bgcolor="#0e1117",
                font_color="white", height=300,
            )
            st.plotly_chart(fig2, use_container_width=True)

        with col2:
            fig3 = go.Figure(go.Bar(
                x=df_ml["モデル"], y=df_ml["AUC"],
                marker_color="#9b59b6",
                text=[f"{v:.4f}" for v in df_ml["AUC"]],
                textposition="outside",
            ))
            fig3.update_layout(
                title="AUC（予測精度）", yaxis_title="AUC",
                yaxis_range=[0.5, 1.0],
                plot_bgcolor="#0e1117", paper_bgcolor="#0e1117",
                font_color="white", height=300,
            )
            st.plotly_chart(fig3, use_container_width=True)

        df_ml_disp = df_ml[["モデル", "閾値", "EV閾値", "賭回数", "的中率", "回収率", "AUC"]].copy()
        df_ml_disp["的中率"] = df_ml_disp["的中率"].map("{:.1f}%".format)
        df_ml_disp["回収率"] = df_ml_disp["回収率"].map("{:.1f}%".format)
        st.dataframe(df_ml_disp, use_container_width=True, hide_index=True)
    else:
        st.info("--ml を実行すると結果が表示されます。")


# ──────────────────────────────────────────────
# ページ 3: DB概要
# ──────────────────────────────────────────────

elif page == "🗃️ DB概要":
    st.title("🗃️ DB概要")

    # サマリーメトリクス
    counts = query_db("""
        SELECT
            (SELECT COUNT(*) FROM races)   AS races,
            (SELECT COUNT(*) FROM results) AS results,
            (SELECT COUNT(*) FROM lines)   AS lines,
            (SELECT COUNT(*) FROM payouts) AS payouts
    """).iloc[0]

    date_range = query_db("SELECT MIN(date) AS min_d, MAX(date) AS max_d FROM races").iloc[0]

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("レース数",   f"{int(counts['races']):,}")
    col2.metric("選手結果数", f"{int(counts['results']):,}")
    col3.metric("ライン数",   f"{int(counts['lines']):,}")
    col4.metric("払戻数",     f"{int(counts['payouts']):,}")
    col5.metric("期間", f"{date_range['min_d']} 〜 {date_range['max_d']}")

    st.divider()

    col_a, col_b = st.columns(2)

    with col_a:
        # 会場別レース数
        df_venue = query_db("""
            SELECT venue, COUNT(*) AS cnt
            FROM races GROUP BY venue ORDER BY cnt DESC
        """)
        fig_v = px.bar(
            df_venue, x="venue", y="cnt",
            title="会場別レース数", labels={"venue": "会場", "cnt": "レース数"},
            color="cnt", color_continuous_scale="Blues",
        )
        fig_v.update_layout(
            plot_bgcolor="#0e1117", paper_bgcolor="#0e1117",
            font_color="white", showlegend=False,
            coloraxis_showscale=False,
        )
        st.plotly_chart(fig_v, use_container_width=True)

    with col_b:
        # クラス分布
        df_cls = query_db("""
            SELECT class_rank, COUNT(*) AS cnt
            FROM results WHERE class_rank != ''
            GROUP BY class_rank ORDER BY cnt DESC
        """)
        fig_c = px.pie(
            df_cls, names="class_rank", values="cnt",
            title="クラス分布",
            color_discrete_sequence=px.colors.sequential.Blues_r,
        )
        fig_c.update_layout(
            plot_bgcolor="#0e1117", paper_bgcolor="#0e1117",
            font_color="white",
        )
        st.plotly_chart(fig_c, use_container_width=True)

    # 月別レース数（時系列）
    df_monthly = query_db("""
        SELECT SUBSTR(date, 1, 7) AS month, COUNT(*) AS cnt
        FROM races GROUP BY month ORDER BY month
    """)
    fig_m = px.bar(
        df_monthly, x="month", y="cnt",
        title="月別レース数", labels={"month": "年月", "cnt": "レース数"},
        color_discrete_sequence=["#3498db"],
    )
    fig_m.update_layout(
        plot_bgcolor="#0e1117", paper_bgcolor="#0e1117",
        font_color="white",
    )
    st.plotly_chart(fig_m, use_container_width=True)

    col_c, col_d = st.columns(2)

    with col_c:
        # ライン規模分布
        df_line = query_db("""
            SELECT line_size, COUNT(*) AS cnt FROM (
                SELECT race_id, line_no, COUNT(*) AS line_size
                FROM lines GROUP BY race_id, line_no
            ) GROUP BY line_size ORDER BY line_size
        """)
        fig_l = px.bar(
            df_line, x="line_size", y="cnt",
            title="ライン規模分布", labels={"line_size": "ライン人数", "cnt": "件数"},
            color_discrete_sequence=["#2ecc71"],
        )
        fig_l.update_layout(
            plot_bgcolor="#0e1117", paper_bgcolor="#0e1117",
            font_color="white",
        )
        st.plotly_chart(fig_l, use_container_width=True)

    with col_d:
        # 賭種別払戻分布
        df_payout = query_db("""
            SELECT bet_type, AVG(payout) AS avg_p, COUNT(*) AS cnt
            FROM payouts GROUP BY bet_type ORDER BY avg_p DESC
        """)
        fig_p = px.bar(
            df_payout, x="bet_type", y="avg_p",
            title="賭種別 平均払戻（円）",
            labels={"bet_type": "賭種", "avg_p": "平均払戻（円）"},
            color="avg_p", color_continuous_scale="Oranges",
            text=df_payout["avg_p"].map("{:,.0f}円".format),
        )
        fig_p.update_layout(
            plot_bgcolor="#0e1117", paper_bgcolor="#0e1117",
            font_color="white", coloraxis_showscale=False,
        )
        st.plotly_chart(fig_p, use_container_width=True)


# ──────────────────────────────────────────────
# ページ 4: レース検索
# ──────────────────────────────────────────────

elif page == "🔍 レース検索":
    st.title("🔍 レース検索")

    # フィルタ
    venues = query_db("SELECT DISTINCT venue FROM races ORDER BY venue")["venue"].tolist()
    dates  = query_db("SELECT DISTINCT date FROM races ORDER BY date DESC")["date"].tolist()

    col1, col2, col3 = st.columns(3)
    with col1:
        sel_venue = st.selectbox("会場", ["（全て）"] + venues)
    with col2:
        sel_date = st.selectbox("日付", ["（最新）"] + dates[:60])
    with col3:
        sel_grade = st.selectbox("グレード", ["（全て）", "GP", "G1", "G2", "G3", "F1", "F2"])

    # クエリ組み立て
    where = ["1=1"]
    params: list = []
    if sel_venue != "（全て）":
        where.append("r.venue = ?")
        params.append(sel_venue)
    if sel_date != "（最新）":
        where.append("r.date = ?")
        params.append(sel_date)
    elif dates:
        where.append("r.date = ?")
        params.append(dates[0])
    if sel_grade != "（全て）":
        where.append("r.grade = ?")
        params.append(sel_grade)

    df_races = query_db(f"""
        SELECT r.race_id, r.date, r.venue, r.race_no, r.race_name,
               r.grade, r.num_racers, r.bank_length,
               COALESCE(r.start_time, '') AS start_time
        FROM races r
        WHERE {' AND '.join(where)}
        ORDER BY r.date DESC, COALESCE(r.start_time, '99:99'), r.venue, r.race_no
        LIMIT 50
    """, tuple(params))

    if df_races.empty:
        st.warning("該当レースがありません。")
    else:
        st.write(f"**{len(df_races)} レース**")
        sel_idx = st.dataframe(
            df_races.drop(columns=["race_id"]),
            use_container_width=True,
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
        )

        selected_rows = sel_idx.selection.rows
        if selected_rows:
            race_id = df_races.iloc[selected_rows[0]]["race_id"]
            race_info = df_races.iloc[selected_rows[0]]

            st.divider()
            st.subheader(
                f"📋 {race_info['venue']} R{race_info['race_no']} "
                f"{race_info['race_name']} ({race_info['grade']}) — {race_info['date']}"
            )

            col_r, col_l = st.columns([3, 2])

            with col_r:
                # 選手結果
                df_res = query_db("""
                    SELECT finish_pos AS 着順, car_no AS 車番, racer_name AS 選手名,
                           class_rank AS 級班, racing_style AS 脚質, prefecture AS 府県
                    FROM results WHERE race_id = ?
                    ORDER BY finish_pos
                """, (race_id,))
                st.write("**選手結果**")
                st.dataframe(df_res, use_container_width=True, hide_index=True)

            with col_l:
                # ライン情報
                df_lines = query_db("""
                    SELECT line_no AS ライン, position AS 位置, car_no AS 車番
                    FROM lines WHERE race_id = ?
                    ORDER BY line_no, position
                """, (race_id,))
                if not df_lines.empty:
                    df_lines["位置"] = df_lines["位置"].map(
                        {0: "先頭", 1: "番手", 2: "三番手"}
                    ).fillna(df_lines["位置"].astype(str))
                    st.write("**ライン（ナラビ）**")
                    st.dataframe(df_lines, use_container_width=True, hide_index=True)

            # 払戻
            df_pay = query_db("""
                SELECT bet_type AS 賭種, car_no1, car_no2, car_no3, payout AS 払戻
                FROM payouts WHERE race_id = ?
                ORDER BY CASE bet_type
                    WHEN 'nishafuku' THEN 1 WHEN 'nishan' THEN 2
                    WHEN 'wide' THEN 3 WHEN 'sanrenfuku' THEN 4
                    WHEN 'sanrentan' THEN 5 ELSE 6 END
            """, (race_id,))
            if not df_pay.empty:
                st.write("**払戻**")
                # 組み合わせを文字列化
                def fmt_combo(row):
                    cars = [str(int(c)) for c in [row["car_no1"], row["car_no2"], row["car_no3"]]
                            if c is not None and str(c) != "None"]
                    return "=".join(cars)
                df_pay["組み合わせ"] = df_pay.apply(fmt_combo, axis=1)
                df_pay["払戻"] = df_pay["払戻"].map("{:,}円".format)
                st.dataframe(
                    df_pay[["賭種", "組み合わせ", "払戻"]],
                    use_container_width=True, hide_index=True,
                )
