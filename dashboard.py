"""
dashboard.py - PISTA 競輪AI ダッシュボード
"""

import os
import re
import json
from pathlib import Path
from datetime import date, timedelta, datetime, timezone

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import db as _db

BASE_DIR = Path(__file__).parent

# Streamlit Secrets → 環境変数に反映
if "DATABASE_URL" in st.secrets and not os.environ.get("DATABASE_URL"):
    os.environ["DATABASE_URL"] = st.secrets["DATABASE_URL"]
    _db.DATABASE_URL = st.secrets["DATABASE_URL"]

st.set_page_config(page_title="PISTA 競輪AI", page_icon="🚴", layout="wide")

# ──────────────────────────────────────────────
# ユーティリティ
# ──────────────────────────────────────────────

JST = timezone(timedelta(hours=9))

@st.cache_data(ttl=60)
def query_db(sql: str, params: tuple = ()) -> pd.DataFrame:
    conn = _db.get_connection()
    try:
        if _db.is_pg():
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
    text = load_optimize_report()
    if not text:
        log_path = BASE_DIR / "logs" / "optimize.log"
        if not log_path.exists():
            return []
        text = log_path.read_text(encoding="utf-8")
    results = []
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
            "収益率": float(m.group(6)),
            "採用":   m.group(7) == "✅",
        })
    return results


def parse_picks_report(text: str) -> list[dict]:
    races = []
    current = None
    for line in text.splitlines():
        m = re.match(r"【(.+?) R(\d+).*?(?:\(([^)]+)\))?】", line)
        if m:
            if current:
                races.append(current)
            current = {
                "venue": m.group(1).split("　")[0].strip(),
                "race":  int(m.group(2)),
                "grade": m.group(3) or "",
                "date": "", "bank": "", "start_time": "",
                "racers": [], "picks": [],
            }
            continue
        if current is None:
            continue
        dm = re.search(r"日付: (\S+)", line)
        if dm:
            current["date"] = dm.group(1)
        bm = re.search(r"バンク: (\S+)", line)
        if bm:
            current["bank"] = bm.group(1)
        tm = re.search(r"発走: (\d{1,2}:\d{2})", line)
        if tm:
            current["start_time"] = tm.group(1)
        rm = re.match(r"\s+(\d+)車 (.+?) \[([^\]]*)\] ([^\|]+)\| (.+)$", line)
        if rm:
            current["racers"].append({
                "car":   rm.group(1),
                "name":  rm.group(2).strip(),
                "cls":   rm.group(3).strip(),
                "style": rm.group(4).strip(),
                "line":  rm.group(5).strip(),
            })
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
        em = re.match(r"\s{7}((?:EV=|暫定EV≈|オッズ未確定).+)", line)
        if em and current["picks"]:
            current["picks"][-1]["ev_desc"] = em.group(1)
        tm2 = re.search(r"合計: (\d+)円（1点100円）", line)
        if tm2 and current["picks"]:
            current["picks"][-1]["total_yen"] = int(tm2.group(1))
        sm = re.search(r"相手: (.+?)車（全(\d+)点）", line)
        if sm and current["picks"]:
            current["picks"][-1]["aite"]    = sm.group(1)
            current["picks"][-1]["n_combos"] = int(sm.group(2))
    if current:
        races.append(current)
    return [r for r in races if r["picks"]]


# ──────────────────────────────────────────────
# 戦略の説明（パラメーターを日本語で解説）
# ──────────────────────────────────────────────

STRATEGY_INFO = {
    "FormPeak": {
        "label":    "好調選手・中穴狙い",
        "bet":      "2車複",
        "emoji":    "🔥",
        "summary":  "直近で好成績・短い間隔で出走している「今乗っている選手」を、人気3〜8番手の中穴ゾーンで捕まえる戦略。",
        "detail": [
            "✅ 直近着順が **3着以内**（好調な選手のみ）",
            "✅ 前走から **7日以内**（間隔が短くコンディションが良い）",
            "✅ 勝率 **12%以上** の実力者に絞る",
            "✅ 人気 **3〜8番手**（人気通りに評価されていない中穴ゾーン）",
        ],
        "point": "調子が良いのに見過ごされている選手を 2車複（相手は残り全員流し）で狙う。",
    },
    "BankSpec": {
        "label":    "会場巧者・手堅く回収",
        "bet":      "ワイド",
        "emoji":    "🏟️",
        "summary":  "特定の競輪場での勝率が高い「その会場に強い選手」を、上位人気の中から選んでワイドで手堅く回収する戦略。",
        "detail": [
            "✅ 当該会場での勝率 **20%以上**（会場への適性が高い）",
            "✅ 人気 **3番手以内**（大崩れしにくい実力者）",
            "✅ 級班スコアが一定以上（クラスの裏付けあり）",
        ],
        "point": "的中率が高く（約61%）コツコツ積み上げるタイプ。1点あたりの払戻は小さめだが安定している。",
    },
    "ValueHunt": {
        "label":    "穴狙い・ライン前方の実力者",
        "bet":      "ワイド",
        "emoji":    "💎",
        "summary":  "人気は低いが実力があり、ライン（連携する選手グループ）の前方に位置している選手を狙う穴狙い型戦略。",
        "detail": [
            "✅ 人気 **6〜8番手**（大穴ゾーン・オッズが高い）",
            "✅ ライン内ポジションが **先頭 or 番手**（前に出やすい位置）",
            "✅ 2車以上のラインに属している（連携の恩恵を受けやすい）",
            "✅ 勝率 **8%以上**、直近着順 **6着以内** で実力は確か",
        ],
        "point": "的中率は低め（約31%）だが当たれば高配当。期待収益はFormPeakと並ぶ高水準。",
    },
}


# ──────────────────────────────────────────────
# サイドバー
# ──────────────────────────────────────────────

st.sidebar.title("🚴 PISTA 競輪AI")
page = st.sidebar.radio(
    "",
    ["🏠 今日の買い目", "📊 成績を見る", "📋 戦略一覧", "⚙️ ツール"],
    label_visibility="collapsed",
)

picks_log  = BASE_DIR / "logs" / "picks.log"
strat_path = BASE_DIR / "reports" / "live_strategies.json"

if picks_log.exists():
    mtime = picks_log.stat().st_mtime
    st.sidebar.caption(f"予想 最終更新: {datetime.fromtimestamp(mtime).strftime('%m/%d %H:%M')}")

if strat_path.exists():
    _strats = json.loads(strat_path.read_text(encoding="utf-8"))
    st.sidebar.caption(f"使用中の戦略: {len(_strats)} 種類")

st.sidebar.divider()
st.sidebar.caption("毎朝10時に自動取得\n毎晩21時に結果確認")


# ──────────────────────────────────────────────
# ページ 1: 今日の買い目
# ──────────────────────────────────────────────

if page == "🏠 今日の買い目":
    now_jst     = datetime.now(JST)
    today_str   = now_jst.strftime("%Y-%m-%d")
    now_minutes = now_jst.hour * 60 + now_jst.minute

    col_title, col_btn = st.columns([3, 1])
    with col_title:
        st.title("🏠 今日の買い目")
        st.caption(now_jst.strftime("%Y年%m月%d日"))
    with col_btn:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("🔄 最新予想を取得", type="primary", use_container_width=True):
            with st.spinner("keirin.jp から出走表を取得中...（1〜2分）"):
                try:
                    import sys as _sys
                    _sys.path.insert(0, str(BASE_DIR))
                    from main import cmd_picks
                    cmd_picks()
                    st.cache_data.clear()
                    st.success("✅ 取得完了！")
                except Exception as e:
                    import traceback
                    st.error(f"取得エラー: {e}")
                    st.code(traceback.format_exc())
            st.rerun()

    # picks テキスト読み込み
    picks_text = None
    if picks_log.exists():
        picks_text = picks_log.read_text(encoding="utf-8")
    if not picks_text:
        picks_text = load_picks_from_db()

    if not picks_text:
        st.info("「最新予想を取得」を押すと今日の買い目が表示されます。\n\n毎朝10時頃に自動で取得されます。")
        st.stop()

    if "出走表取得失敗" in picks_text or ("開催なし" in picks_text and "【" not in picks_text):
        st.warning("本日は開催がないか、出走表がまだ公開されていません。\n\nkeirin.jp は毎朝8時頃に出走表を公開します。")
        st.stop()

    is_provisional = "オッズ未確定" in picks_text

    all_races = parse_picks_report(picks_text)

    def _is_today(r):
        d = r.get("date", "")
        if not d or len(d) < 8:
            return True
        if not re.match(r"\d{4}-\d{2}-\d{2}", d):
            return False
        return d == today_str

    def _sort_key(r):
        st_raw = r.get("start_time", "")
        if st_raw and re.match(r"\d{1,2}:\d{2}", st_raw):
            h, mn = map(int, st_raw.split(":"))
            return (h * 60 + mn, r["race"])
        return (9999, r["race"])

    def _is_upcoming(r):
        st_raw = r.get("start_time", "")
        if st_raw and re.match(r"\d{1,2}:\d{2}", st_raw):
            h, mn = map(int, st_raw.split(":"))
            return (h * 60 + mn) >= now_minutes - 5
        return True

    races_today    = sorted([r for r in all_races if _is_today(r)], key=_sort_key)
    races_upcoming = [r for r in races_today if _is_upcoming(r)]

    if is_provisional:
        st.info("⚠️ オッズはまだ確定していません。10〜11時以降に再取得するとオッズが表示されます。")

    if not races_upcoming:
        if races_today:
            st.success("🏁 本日の推奨レースはすべて終了しました。「📊 成績を見る」で結果を確認できます。")
        else:
            st.warning("本日の推奨レースが見つかりません。「最新予想を取得」を押してください。")
        st.stop()

    st.markdown(f"### 本日の推奨　{len(races_upcoming)} 件")

    for r in races_upcoming:
        pick     = r["picks"][0]
        btype    = pick["bet_type"]
        bet_name = "2車複" if btype == "NISHAFUKU" else "ワイド"
        ev_mark  = pick.get("ev_mark", "△")
        ev_desc  = pick.get("ev_desc", "")
        aite     = pick.get("aite", "")
        n_combos = pick.get("n_combos", "?")
        total_yen = pick.get("total_yen", "?")

        if ev_mark == "◎":
            bd, bg, label = "#2ecc71", "#e8f5e9", "◎ 買い推奨"
        elif ev_mark == "🔶":
            bd, bg, label = "#e67e22", "#fff3e0", "🔶 暫定推奨"
        else:
            bd, bg, label = "#9e9e9e", "#f5f5f5", "△ 様子見"

        odds_str = pick.get("odds_str", "未確定")

        # 発走・締切時刻の計算
        st_raw = r.get("start_time", "")
        time_info_html = ""
        if st_raw and re.match(r"\d{1,2}:\d{2}", st_raw):
            h, mn = map(int, st_raw.split(":"))
            close_total = h * 60 + mn - 2
            close_h, close_m = divmod(close_total, 60)
            close_str = f"{close_h}:{close_m:02d}"
            remaining = close_total - now_minutes
            if remaining > 0:
                remain_html = f'<span style="color:#e74c3c;font-weight:bold">あと{remaining}分</span>'
            else:
                remain_html = '<span style="color:#999">締切済み</span>'
            time_info_html = (
                f'<div style="font-size:0.9em;margin-bottom:10px;color:#555">'
                f'🕐 発走 <b>{st_raw}</b>　'
                f'🔒 締切 <b>{close_str}</b>　{remain_html}'
                f'</div>'
            )

        # 会場情報バッジ
        grade = r.get("grade", "")
        bank  = r.get("bank", "")
        meta_parts = [r["venue"], f"R{r['race']}"]
        if grade:
            meta_parts.append(f'<span style="background:#555;color:#fff;padding:1px 7px;border-radius:4px;font-size:0.8em">{grade}</span>')
        if bank:
            meta_parts.append(f'<span style="color:#777;font-size:0.85em">バンク{bank}m</span>')

        st.markdown(
            f"""
<div style="border:2px solid {bd};border-radius:10px;padding:16px 20px;margin-bottom:16px">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
    <div style="font-size:1.2em;font-weight:bold">
      {"　".join(meta_parts[:2])}&nbsp;{"　".join(meta_parts[2:])}
      &nbsp;<span style="font-size:0.8em;background:#444;color:#fff;padding:2px 10px;border-radius:4px">{bet_name}</span>
    </div>
    <span style="background:{bd};color:white;padding:5px 16px;border-radius:20px;font-weight:bold">{label}</span>
  </div>
  {time_info_html}
  <div style="background:{bg};border-left:5px solid {bd};padding:14px 18px;border-radius:0 8px 8px 0">
    <div style="font-size:1.05em;margin-bottom:8px">
      <b>① 「{bet_name}」を選択</b><br>
      <b>② 軸：{pick['car']}車　{pick['name']}</b> を選択<br>
      <b>③ 相手：{aite}車</b>　をすべて選択<br>
      <b>④ 全 {n_combos}点 × 100円 ＝ 合計 {total_yen}円</b>
    </div>
    <div style="color:#666;font-size:0.85em">
      オッズ: {odds_str}{"　　" + ev_desc if ev_desc else ""}
    </div>
  </div>
</div>
""",
            unsafe_allow_html=True,
        )

        with st.expander(f"出走表　{r['venue']} R{r['race']}", expanded=False):
            if r["racers"]:
                df_r = pd.DataFrame(r["racers"])
                df_r.columns = ["車番", "選手名", "クラス", "脚質", "ライン"]
                color_hex = "#1565c0" if btype == "NISHAFUKU" else "#2e7d32"
                def _hl(row, car=pick["car"], c=color_hex):
                    s = f"background-color:{c}22;font-weight:bold" if row["車番"] == car else ""
                    return [s] * len(row)
                st.dataframe(df_r.style.apply(_hl, axis=1), hide_index=True, use_container_width=True)


# ──────────────────────────────────────────────
# ページ 2: 戦略一覧
# ──────────────────────────────────────────────

elif page == "📋 戦略一覧":
    st.title("📋 戦略一覧")
    st.caption("PISTAが使用しているAI戦略の説明と実績です。")

    # 戦略データ読み込み
    strategies_data = []
    if strat_path.exists():
        strategies_data = json.loads(strat_path.read_text(encoding="utf-8"))

    # 実績データ（signals テーブルから集計）
    df_perf = query_db("""
        SELECT strategy,
               COUNT(*) AS total,
               SUM(CASE WHEN is_hit = 1 THEN 1 ELSE 0 END) AS hits,
               SUM(CASE WHEN is_hit = 1 THEN actual_payout ELSE 0 END) AS paid
        FROM signals
        WHERE is_hit IS NOT NULL
        GROUP BY strategy
    """)
    perf_map = {}
    if not df_perf.empty:
        for _, r in df_perf.iterrows():
            total = int(r["total"])
            hits  = int(r["hits"])
            paid  = int(r["paid"])
            invest = total * 100
            perf_map[r["strategy"]] = {
                "total":    total,
                "hits":     hits,
                "hit_pct":  hits / total * 100 if total else 0,
                "paid":     paid,
                "roi":      (paid - invest) / invest * 100 if invest else 0,
                "avg_win":  paid / hits if hits else 0,
            }

    for s in strategies_data:
        name = s["name"]
        info = STRATEGY_INFO.get(name, {})
        bet  = "2車複" if s.get("bet_type", "").lower() == "nishafuku" else "ワイド"
        hr   = s.get("hit_rate")
        ap   = s.get("avg_payout")
        ev   = round(hr * ap) if (hr and ap) else None
        perf = perf_map.get(name, {})

        emoji   = info.get("emoji", "📌")
        label   = info.get("label", name)
        summary = info.get("summary", "")
        detail  = info.get("detail", [])
        point   = info.get("point", "")

        # カード本体
        st.markdown(f"### {emoji} {name}　―　{label}")
        st.markdown(f"**賭種：{bet}**　　{summary}")

        col_left, col_right = st.columns([3, 2])

        with col_left:
            st.markdown("**📌 選出条件**")
            for d in detail:
                st.markdown(f"- {d}")
            if point:
                st.info(f"💡 {point}")

        with col_right:
            st.markdown("**📊 バックテスト実績**")
            m1, m2, m3 = st.columns(3)
            m1.metric("的中率",   f"{hr*100:.1f}%" if hr else "-")
            m2.metric("平均払戻", f"{ap:.0f}円"    if ap else "-")
            m3.metric("期待収益\n（100円あたり）", f"{ev:.0f}円" if ev else "-")

            if perf:
                st.markdown("**📅 記録済みシグナル実績**")
                p1, p2, p3 = st.columns(3)
                p1.metric("予想数 / 的中", f"{perf['total']} / {perf['hits']} 件")
                p2.metric("的中率",        f"{perf['hit_pct']:.1f}%")
                p3.metric("収益率",        f"{perf['roi']:+.1f}%",
                          delta_color="normal" if perf["roi"] >= 0 else "inverse")

        st.divider()


# ──────────────────────────────────────────────
# ページ 3: 成績を見る
# ──────────────────────────────────────────────

elif page == "📊 成績を見る":
    st.title("📊 成績を見る")

    tab_ai, tab_manual = st.tabs(["🤖 AI予想の成績", "💰 自分の賭け記録"])

    # ────────────────────────────────
    # AI予想の成績
    # ────────────────────────────────
    with tab_ai:
        # ボタン行
        if st.button("📥 今日の結果を取得", key="fetch_and_grade",
                     help="今日のレース結果を取得して的中/外れを確認します（2〜3分かかります）"):
            with st.spinner("レース結果を取得中..."):
                try:
                    import sys as _sys
                    _sys.path.insert(0, str(BASE_DIR))
                    from main import cmd_fetch, cmd_grade_signals
                    cmd_fetch(years=0, specific_date=None)
                    cmd_grade_signals()
                    st.cache_data.clear()
                    st.success("✅ 確認完了！")
                except Exception as e:
                    import traceback as _tb
                    st.error(f"エラー: {e}")
                    st.code(_tb.format_exc())
            st.rerun()
        st.caption("毎朝10時に自動取得・記録。毎晩21時に的中/外れを自動確認します。")

        st.divider()

        # 期間プリセット
        _period_labels = {"直近7日": 7, "直近30日": 30, "全期間": None}
        _sel = st.segmented_control(
            "期間", list(_period_labels.keys()), default="直近7日", key="period_preset"
        )
        _days_back = _period_labels.get(_sel, 7)
        if _days_back is not None:
            _start_str = str(date.today() - timedelta(days=_days_back))
        else:
            _start_str = "2000-01-01"
        _end_str = str(date.today())

        df_sig = query_db("""
            SELECT s.date, s.venue, s.race_no, s.strategy, s.bet_type,
                   s.axis_car, s.racer_name, s.odds_at_pick, s.ev_mark,
                   s.is_hit, s.actual_payout,
                   r.start_time, r.grade, r.bank_length
            FROM signals s
            LEFT JOIN races r ON s.race_id = r.race_id
            WHERE s.date >= ? AND s.date <= ?
            ORDER BY s.date DESC, s.race_no
        """, params=(_start_str, _end_str))

        if df_sig.empty:
            st.info(f"📭 この期間に記録されたAI予想はありません。")
        else:
            # 集計（結果確認済みのみ）
            df_graded = df_sig[df_sig["is_hit"].notna()].copy()
            df_graded["is_hit"]        = df_graded["is_hit"].astype(int)
            df_graded["actual_payout"] = df_graded["actual_payout"].fillna(0).astype(int)

            if not df_graded.empty:
                total      = len(df_graded)
                hits       = int(df_graded["is_hit"].sum())
                hit_pct    = hits / total * 100 if total else 0
                total_paid = int(df_graded["actual_payout"].sum())
                total_bet  = total * 100
                net        = total_paid - total_bet
                roi        = net / total_bet * 100 if total_bet else 0

                st.caption(f"集計期間: {_start_str} 〜 {_end_str}　（結果確認済み {total}件）")
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("的中数 / 予想数", f"{hits} / {total} 件")
                c2.metric("的中率",          f"{hit_pct:.1f}%")
                c3.metric("収支",            f"{net:+,}円",
                          delta_color="normal" if net >= 0 else "inverse")
                c4.metric("収益率",          f"{roi:+.1f}%",
                          delta_color="normal" if roi >= 0 else "inverse")

                st.divider()

                # 戦略別
                grp = df_graded.groupby("strategy").agg(
                    予想数=("is_hit", "count"),
                    的中数=("is_hit", "sum"),
                    払戻合計=("actual_payout", "sum"),
                ).reset_index()
                grp["的中率"] = (grp["的中数"] / grp["予想数"] * 100).round(1).astype(str) + "%"
                grp["投資額"] = grp["予想数"] * 100
                grp["収益率"] = ((grp["払戻合計"] - grp["投資額"]) / grp["投資額"] * 100)\
                                .round(1).astype(str) + "%"
                grp = grp.rename(columns={"strategy": "戦略"})
                st.dataframe(
                    grp[["戦略", "予想数", "的中数", "的中率", "払戻合計", "収益率"]],
                    hide_index=True, use_container_width=True,
                )

                # 損益チャート
                df_s = df_graded.sort_values("date").reset_index(drop=True)
                df_s["損益"]     = df_s["actual_payout"] - 100
                df_s["累積損益"] = df_s["損益"].cumsum()
                fig_cum = px.line(
                    df_s, y="累積損益",
                    labels={"index": "予想番号", "累積損益": "累積損益（円）"},
                    title=f"損益の推移　{_start_str} 〜 {_end_str}",
                    color_discrete_sequence=["#2ecc71"],
                )
                fig_cum.add_hline(y=0, line_dash="dash", line_color="gray")
                fig_cum.update_layout(
                    plot_bgcolor="#0e1117", paper_bgcolor="#0e1117",
                    font_color="white", height=260,
                )
                st.plotly_chart(fig_cum, use_container_width=True)

            # 日別詳細
            st.divider()
            st.subheader("日別の記録")

            today_iso = date.today().isoformat()
            sig_dates = sorted(df_sig["date"].unique(), reverse=True)

            for sig_date in sig_dates:
                df_day  = df_sig[df_sig["date"] == sig_date].copy()
                n_hit   = int((df_day["is_hit"] == 1).sum())
                n_miss  = int((df_day["is_hit"] == 0).sum())
                n_tbd   = int(df_day["is_hit"].isna().sum())
                day_pay = int(df_day["actual_payout"].fillna(0).sum())

                parts = []
                if n_hit:  parts.append(f"✅ {n_hit}的中")
                if n_miss: parts.append(f"❌ {n_miss}外れ")
                if n_tbd:  parts.append(f"⏳ {n_tbd}未確定")
                summary = "　".join(parts)
                if n_hit:  summary += f"　💰 {day_pay:,}円払戻"

                is_today = sig_date == today_iso
                icon     = "🔴" if is_today else "📅"
                lbl      = f"{icon} {sig_date}　{summary}"

                with st.expander(lbl, expanded=is_today):
                    for _, row in df_day.iterrows():
                        is_hit_val = row.get("is_hit")
                        payout_val = int(row.get("actual_payout") or 0)
                        bet_raw    = str(row.get("bet_type", "")).upper()
                        bet_name   = "2車複" if "NISHAFUKU" in bet_raw else "ワイド"
                        ev_mark    = row.get("ev_mark", "")
                        odds_val   = row.get("odds_at_pick") or 0

                        if is_hit_val == 1:
                            ico = "✅"; txt = f"的中　💰 {payout_val:,}円払戻 (+{payout_val - 100:,}円)"; lc = "#2ecc71"
                        elif is_hit_val == 0:
                            ico = "❌"; txt = "外れ　（-100円）"; lc = "#e74c3c"
                        else:
                            ico = "⏳"; txt = "結果待ち"; lc = "#f39c12"

                        odds_str  = f"{int(odds_val):,}円" if odds_val > 0 else "未確定"
                        ev_prov   = ev_mark in ("🔶", "retro")
                        ev_str    = ("◎ 買い推奨" if ev_mark == "◎"
                                     else ("🔶 暫定推奨" if ev_prov else "△ 様子見"))
                        st_raw    = str(row.get("start_time") or "")
                        grade_str = str(row.get("grade") or "")
                        bank_str  = str(row.get("bank_length") or "")

                        # 発走時刻・グレード・バンク
                        meta_parts = []
                        if st_raw:
                            meta_parts.append(f"🕐 {st_raw}")
                        if grade_str:
                            meta_parts.append(grade_str)
                        if bank_str:
                            meta_parts.append(f"バンク{bank_str}m")
                        meta_html = (
                            f'<span style="color:#aaa;font-size:0.82em">{"　".join(meta_parts)}</span><br>'
                            if meta_parts else ""
                        )

                        st.markdown(
                            f"""
<div style="border-left:4px solid {lc};padding:8px 14px;margin:5px 0;border-radius:0 6px 6px 0;background:#1a1a2e">
  {meta_html}
  {ico} &nbsp;<b>{row['venue']} R{row['race_no']}</b>　{bet_name}　軸 <b>{row['axis_car']}車 {row['racer_name']}</b>
  &nbsp;<span style="color:#aaa;font-size:0.85em">← {row['strategy']}</span><br>
  <span style="color:{lc}">{txt}</span>
  &nbsp;&nbsp;<span style="color:#888;font-size:0.82em">オッズ: {odds_str}　{ev_str}</span>
</div>
""",
                            unsafe_allow_html=True,
                        )

    # ────────────────────────────────
    # 自分の賭け記録
    # ────────────────────────────────
    with tab_manual:
        st.subheader("💰 賭け記録")

        with st.form("bet_form", clear_on_submit=True):
            fc1, fc2, fc3 = st.columns(3)
            with fc1:
                bet_date  = st.date_input("日付",   value=date.today())
                bet_venue = st.text_input("会場",   placeholder="例: 松阪")
            with fc2:
                bet_race_no = st.number_input("レース番号", min_value=1, max_value=12, value=1, step=1)
                bet_type    = st.selectbox("賭種", ["2車複", "ワイド"])
            with fc3:
                bet_axis   = st.number_input("軸車番",        min_value=1, max_value=9, value=1, step=1)
                bet_amount = st.number_input("賭け金合計(円)", min_value=100, value=700, step=100)

            fc4, fc5 = st.columns(2)
            with fc4:
                bet_result = st.selectbox("結果", ["未確定", "的中", "外れ"])
            with fc5:
                bet_payout = st.number_input("払戻額(円)", min_value=0, value=0, step=100)

            bet_notes = st.text_input("メモ（任意）")
            submitted = st.form_submit_button("💾 記録する", type="primary")

        if submitted:
            try:
                conn   = _db.get_connection()
                c      = _db.get_cursor(conn)
                is_hit = {"的中": 1, "外れ": 0, "未確定": None}[bet_result]
                profit = (bet_payout - bet_amount if is_hit == 1
                          else (-bet_amount if is_hit == 0 else None))
                bet_t  = "nishafuku" if bet_type == "2車複" else "wide"
                c.execute(_db.sql("""
                    INSERT INTO bets
                    (date, race_id, venue, race_no, strategy, bet_type,
                     axis_car, amount, is_hit, payout, profit, notes, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """), (
                    bet_date.isoformat(),
                    f"{bet_date.strftime('%Y%m%d')}??{bet_race_no:02d}",
                    bet_venue, bet_race_no, "", bet_t,
                    bet_axis, bet_amount,
                    is_hit, bet_payout if is_hit == 1 else 0,
                    profit, bet_notes, datetime.now().isoformat(),
                ))
                conn.commit()
                conn.close()
                st.success("✅ 記録しました！")
                st.cache_data.clear()
            except Exception as e:
                st.error(f"保存エラー: {e}")

        df_bets = query_db("""
            SELECT date, venue, race_no, bet_type, axis_car,
                   amount, is_hit, payout, profit, notes
            FROM bets ORDER BY date DESC, race_no DESC
        """)

        if df_bets.empty:
            st.info("まだ記録がありません。上のフォームから入力してください。")
        else:
            df_b      = df_bets.copy()
            df_dec    = df_b[df_b["is_hit"].notna()].copy()
            if not df_dec.empty:
                df_dec["is_hit"]  = df_dec["is_hit"].astype(int)
                df_dec["profit"]  = df_dec["profit"].fillna(0).astype(int)
                df_dec["payout"]  = df_dec["payout"].fillna(0).astype(int)
                df_dec["amount"]  = df_dec["amount"].astype(int)

                total_b   = len(df_dec)
                hits_b    = int(df_dec["is_hit"].sum())
                total_amt = int(df_dec["amount"].sum())
                net_b     = int(df_dec["profit"].sum())
                roi_b     = net_b / total_amt * 100 if total_amt else 0

                b1, b2, b3, b4 = st.columns(4)
                b1.metric("賭回数 / 的中",f"{total_b} / {hits_b} 回")
                b2.metric("的中率",       f"{hits_b/total_b*100:.1f}%" if total_b else "-")
                b3.metric("収支",         f"{net_b:+,}円",
                          delta_color="normal" if net_b >= 0 else "inverse")
                b4.metric("収益率",       f"{roi_b:+.1f}%",
                          delta_color="normal" if roi_b >= 0 else "inverse")

                df_s2 = df_dec.sort_values("date").reset_index(drop=True)
                df_s2["累積収支"] = df_s2["profit"].cumsum()
                fig_b = px.line(df_s2, y="累積収支", title="累積収支",
                                color_discrete_sequence=["#f39c12"])
                fig_b.add_hline(y=0, line_dash="dash", line_color="gray")
                fig_b.update_layout(
                    plot_bgcolor="#0e1117", paper_bgcolor="#0e1117",
                    font_color="white", height=240,
                )
                st.plotly_chart(fig_b, use_container_width=True)

            st.divider()
            df_b["is_hit"] = df_b["is_hit"].map(
                lambda v: "✅ 的中" if v == 1 else ("❌ 外れ" if v == 0 else "⏳ 未確定")
            )
            df_b["profit"] = df_b["profit"].apply(
                lambda v: f"{int(v):+,}円" if v is not None and str(v) != "None" else "-"
            )
            df_b.columns = ["日付", "会場", "R", "賭種", "軸", "金額", "結果", "払戻", "収支", "メモ"]
            st.dataframe(df_b, hide_index=True, use_container_width=True)


# ──────────────────────────────────────────────
# ページ 3: ツール
# ──────────────────────────────────────────────

elif page == "⚙️ ツール":
    st.title("⚙️ ツール")

    t1, t2, t3 = st.tabs(["📈 戦略バックテスト", "🗃️ データ概要", "🔍 レース検索"])

    # ── 戦略バックテスト
    with t1:
        st.subheader("戦略バックテスト成績")
        opt_results = parse_optimize_log()
        if opt_results:
            df_opt = pd.DataFrame(opt_results)
            colors = ["#2ecc71" if r else "#e74c3c" for r in df_opt["採用"]]
            fig = go.Figure(go.Bar(
                x=df_opt["戦略"], y=df_opt["回収率"],
                marker_color=colors,
                text=[f"{v:.1f}%" for v in df_opt["回収率"]],
                textposition="outside",
            ))
            fig.add_hline(y=100, line_dash="dash", line_color="white",
                          annotation_text="損益分岐ライン（100%）")
            fig.update_layout(
                title="バックテスト 回収率（緑 = 採用済み）",
                yaxis_title="回収率 (%)",
                plot_bgcolor="#0e1117", paper_bgcolor="#0e1117",
                font_color="white", height=380,
            )
            st.plotly_chart(fig, use_container_width=True)

            df_disp = df_opt[["戦略", "賭回数", "的中率", "回収率", "収益率", "採用"]].copy()
            df_disp["的中率"] = df_disp["的中率"].map("{:.1f}%".format)
            df_disp["回収率"] = df_disp["回収率"].map("{:.1f}%".format)
            df_disp["収益率"] = df_disp["収益率"].map("{:+.1f}%".format)
            df_disp["採用"]   = df_disp["採用"].map({True: "✅ 採用", False: "❌ 未採用"})
            st.dataframe(df_disp, use_container_width=True, hide_index=True)
        else:
            st.info("バックテスト結果がありません。")

    # ── データ概要
    with t2:
        st.subheader("データ概要")
        try:
            counts = query_db("""
                SELECT
                    (SELECT COUNT(*) FROM races)   AS races,
                    (SELECT COUNT(*) FROM results) AS results,
                    (SELECT COUNT(*) FROM payouts) AS payouts
            """).iloc[0]
            dr = query_db("SELECT MIN(date) AS mn, MAX(date) AS mx FROM races").iloc[0]

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("レース数",   f"{int(counts['races']):,}")
            c2.metric("選手結果数", f"{int(counts['results']):,}")
            c3.metric("払戻データ", f"{int(counts['payouts']):,}")
            c4.metric("期間",       f"{dr['mn']} 〜 {dr['mx']}")

            col_a, col_b = st.columns(2)
            with col_a:
                df_v = query_db("SELECT venue, COUNT(*) AS cnt FROM races GROUP BY venue ORDER BY cnt DESC")
                fig_v = px.bar(df_v, x="venue", y="cnt",
                               title="会場別レース数",
                               labels={"venue": "会場", "cnt": "レース数"},
                               color="cnt", color_continuous_scale="Blues")
                fig_v.update_layout(plot_bgcolor="#0e1117", paper_bgcolor="#0e1117",
                                    font_color="white", showlegend=False,
                                    coloraxis_showscale=False)
                st.plotly_chart(fig_v, use_container_width=True)
            with col_b:
                df_m = query_db("""
                    SELECT SUBSTR(date,1,7) AS month, COUNT(*) AS cnt
                    FROM races GROUP BY month ORDER BY month
                """)
                fig_m = px.bar(df_m, x="month", y="cnt",
                               title="月別レース数",
                               labels={"month": "年月", "cnt": "レース数"},
                               color_discrete_sequence=["#3498db"])
                fig_m.update_layout(plot_bgcolor="#0e1117", paper_bgcolor="#0e1117",
                                    font_color="white")
                st.plotly_chart(fig_m, use_container_width=True)
        except Exception as e:
            st.error(f"データ取得エラー: {e}")

    # ── レース検索
    with t3:
        st.subheader("レース検索")
        venues = query_db("SELECT DISTINCT venue FROM races ORDER BY venue")["venue"].tolist()
        dates  = query_db("SELECT DISTINCT date FROM races ORDER BY date DESC")["date"].tolist()

        col1, col2, col3 = st.columns(3)
        with col1:
            sel_venue = st.selectbox("会場", ["（全て）"] + venues)
        with col2:
            sel_date  = st.selectbox("日付", ["（最新）"] + dates[:60])
        with col3:
            sel_grade = st.selectbox("グレード", ["（全て）", "GP", "G1", "G2", "G3", "F1", "F2"])

        where   = ["1=1"]
        qparams: list = []
        if sel_venue != "（全て）":
            where.append("r.venue = ?");  qparams.append(sel_venue)
        if sel_date != "（最新）":
            where.append("r.date = ?");   qparams.append(sel_date)
        elif dates:
            where.append("r.date = ?");   qparams.append(dates[0])
        if sel_grade != "（全て）":
            where.append("r.grade = ?");  qparams.append(sel_grade)

        df_races = query_db(f"""
            SELECT r.race_id, r.date, r.venue, r.race_no, r.race_name,
                   r.grade, r.num_racers, r.bank_length,
                   COALESCE(r.start_time,'') AS start_time
            FROM races r
            WHERE {' AND '.join(where)}
            ORDER BY r.date DESC, COALESCE(r.start_time,'99:99'), r.venue, r.race_no
            LIMIT 50
        """, tuple(qparams))

        if df_races.empty:
            st.warning("該当レースがありません。")
        else:
            sel_idx = st.dataframe(
                df_races.drop(columns=["race_id"]),
                use_container_width=True, hide_index=True,
                on_select="rerun", selection_mode="single-row",
            )
            selected_rows = sel_idx.selection.rows
            if selected_rows:
                race_id   = df_races.iloc[selected_rows[0]]["race_id"]
                race_info = df_races.iloc[selected_rows[0]]
                st.divider()
                st.subheader(
                    f"📋 {race_info['venue']} R{race_info['race_no']} "
                    f"{race_info['race_name']} — {race_info['date']}"
                )
                col_r, col_l = st.columns([3, 2])
                with col_r:
                    df_res = query_db("""
                        SELECT finish_pos AS 着順, car_no AS 車番,
                               racer_name AS 選手名, class_rank AS 級班,
                               racing_style AS 脚質
                        FROM results WHERE race_id = ?
                        ORDER BY finish_pos
                    """, (race_id,))
                    st.write("**選手結果**")
                    st.dataframe(df_res, use_container_width=True, hide_index=True)
                with col_l:
                    df_pay = query_db("""
                        SELECT bet_type AS 賭種, car_no1 AS 1着,
                               car_no2 AS 2着, payout AS 払戻
                        FROM payouts WHERE race_id = ?
                        ORDER BY payout DESC
                    """, (race_id,))
                    if not df_pay.empty:
                        st.write("**払戻**")
                        st.dataframe(df_pay, use_container_width=True, hide_index=True)
