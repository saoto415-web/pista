"""
dashboard.py - PISTA 競輪AI ダッシュボード
起動: streamlit run dashboard.py
"""

import re
import json
import subprocess
import sys
from pathlib import Path
from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import db as _db

BASE_DIR = Path(__file__).parent

st.set_page_config(page_title="PISTA 競輪AI", page_icon="🚴", layout="wide")

st.sidebar.title("🚴 PISTA 競輪AI")
page = st.sidebar.radio(
    "ページ選択",
    ["🏆 Today's Picks", "📊 戦略パフォーマンス", "🗃️ DB概要", "🔍 レース検索"],
)

# ──────────────────────────────────────────────
# ユーティリティ
# ──────────────────────────────────────────────

@st.cache_data(ttl=300)
def query_db(sql: str, params: tuple = ()) -> pd.DataFrame:
    conn = _db.get_connection()
    df   = pd.read_sql_query(sql, conn, params=params)
    conn.close()
    return df


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


def parse_optimize_log() -> list[dict]:
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
        # 推奨車券行（◎ or △ or ★）
        pm = re.match(r"\s+([◎△★]) (\w+) (\d+)車 (.+?) \[([^\]]*)\] \((.+?)\) ← (\w+)", line)
        if pm:
            current["picks"].append({
                "ev_mark":  pm.group(1),
                "bet_type": pm.group(2),
                "car":      pm.group(3),
                "name":     pm.group(4).strip(),
                "odds_str": pm.group(6),
                "strategy": pm.group(7),
            })
        # EV説明行
        em = re.match(r"\s{7}(EV=.+)", line)
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


if page == "🏆 Today's Picks":
    st.title("🏆 今日の注目選手")
    st.caption(f"生成日: {date.today()}")

    strat_path = BASE_DIR / "reports" / "live_strategies.json"
    picks_log  = BASE_DIR / "logs" / "picks.log"

    col1, col2, col3 = st.columns([3, 1, 1])
    with col1:
        if st.button("🔄 ピックス更新（keirin.jp取得）", type="primary"):
            st.info("⏳ 特徴量計算中…7〜9分かかります。このままお待ちください。")
            with st.spinner("出走表取得・推奨車券生成中（7〜9分）…"):
                subprocess.run(
                    [sys.executable, "main.py", "--picks"],
                    cwd=BASE_DIR,
                )
            st.rerun()
    with col2:
        if strat_path.exists():
            strategies = json.loads(strat_path.read_text(encoding="utf-8"))
            st.metric("実際の戦略戦略数", f"{len(strategies)} 戦略")
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
        st.info("「ピックス更新」ボタンを押すと、今日の推奨車券を取得します。")
    else:
        races = parse_picks_report(picks_text)

        if not races:
            if "開催なし" in picks_text or "取得失敗" in picks_text:
                st.info("🚫 本日は開催なし、または出走表が未公開です。")
            else:
                st.warning("推奨車券が見つかりません。オッズ確定後（10〜11時頃）に再取得してください。")
        else:
            is_provisional = "オッズ未確定" in picks_text
            if is_provisional:
                st.warning("⚠️ オッズ未確定（暫定ピックス）— 10〜11時以降に再取得するとオッズ付きで表示されます")

            st.subheader(f"📋 推奨レース一覧（{len(races)}件）")

            # 発走時刻 → 時系列ソート（時刻がある場合は時刻順、ない場合はrace_no順）
            def _sort_key(r):
                st_raw = r.get("start_time", "")
                if st_raw and re.match(r"\d{1,2}:\d{2}", st_raw):
                    h, mn = map(int, st_raw.split(":"))
                    return (h * 60 + mn, r["race"])
                return (9999, r["race"])

            races_sorted = sorted(races, key=_sort_key)

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
                is_go     = ev_mark == "◎"
                bg_color  = "#e8f5e9" if is_go else "#fff8e1"
                bd_color  = "#2ecc71" if is_go else "#f39c12"
                go_label  = "◎ 賭け推奨" if is_go else "△ 見送り推奨"

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
<b>🎯 買い方（ウィンチケット操作）</b><br>
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
        st.subheader("実運用戦略一覧")
        strategies = json.loads(strat_path.read_text(encoding="utf-8"))
        for s in strategies:
            with st.expander(f"**{s['name']}** ({s['bet_type'].upper()})"):
                st.json(s["params"])


# ──────────────────────────────────────────────
# ページ 2: 戦略パフォーマンス
# ──────────────────────────────────────────────

elif page == "📊 戦略パフォーマンス":
    st.title("📊 戦略パフォーマンス")

    # ── ルールベース最適化結果 ──
    st.subheader("ルールベース戦略（--optimize）")
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
