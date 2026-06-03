"""
data_fetcher.py  - PISTA 競輪版 v2
keirin.jp + chariloto.com の二重ソース対応スクレイパー

使い方:
  python3 data_fetcher.py --years 2    # 過去2年分を取得
  python3 data_fetcher.py --date 20240101  # 指定日のみ取得

データソース:
  keirin.jp  → レーススケジュール / 選手エントリ (名前・登録番号・脚質・府県)
  chariloto  → 着順・クラス・年齢・上りタイム / 並び / 払戻 (2車連・三連勝・ワイド)

keirin.jp ナビゲーション構造:
  1. /pc/raceschedule?scyy=YYYY&scym=MM
       → var pc0101_json に RaceList (kaisaiDate, naibuKeirinCd, touhyouLivePara)
  2. /pc/racelist?encp={touhyouLivePara}&dkbn=1
       → jsonData['PC0201'] (encParaR per race)
          jsonData['PJ0305'] (sInfo: syaban/senNo/senName/huken/kyaku)

chariloto.com 構造:
  /keirin/results/{venue_code}/{YYYY-MM-DD}
       → 全レース分の結果テーブル (着・車番・選手名・年齢・府県・期別・級班・上り)
          周回予想テーブル (narabi 情報)
          払戻テーブル (2車連複・2車連単・三連複・三連単・ワイド)
"""

import json
import re
import time
import logging
import argparse
from datetime import date, timedelta
from pathlib import Path

import db as _db

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("pip install requests beautifulsoup4 が必要です")
    raise

logger = logging.getLogger(__name__)
_LOG_DIR = Path(__file__).parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)
_handlers = [logging.StreamHandler()]
try:
    _handlers.append(logging.FileHandler(_LOG_DIR / "fetch.log", encoding="utf-8"))
except Exception:
    pass
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=_handlers,
)

# DB_PATH は SQLite ローカル用（_db モジュールが管理）

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://keirin.jp/",
}

BASE_URL       = "https://keirin.jp"
CHARILOTO_URL  = "https://chariloto.com"

# 競輪場コード (naibuKeirinCd) → (名前, バンク長)
# keirin.jp の RaceList.naibuKeirinCd に基づく内部コード
# VENUE_MAP: keirin.jp の naibuKeirinCd → (会場名, バンク長)
# ※ このマップは fetch_kaisai_for_month() で keirin.jp の実データにより随時上書きされる。
#   keirin.jp コード = chariloto コードであることが実測で確認された（2025〜2026年）。
#   古いコードは参考値。
VENUE_MAP: dict[str, tuple[str, int]] = {}

# 会場名 → バンク長（固定物理データ）
_BANK_BY_NAME: dict[str, int] = {
    "函館": 333, "青森": 400, "いわき平": 400, "弥彦": 400,
    "前橋": 333, "取手": 400, "宇都宮": 333, "大宮": 400,
    "西武園": 400, "京王閣": 400, "立川": 400, "松戸": 400,
    "千葉": 400, "川崎": 400, "平塚": 335, "小田原": 500,
    "伊東": 333, "静岡": 333, "名古屋": 400, "岐阜": 400,
    "大垣": 333, "豊橋": 400, "富山": 400, "松阪": 400,
    "四日市": 333, "福井": 400, "奈良": 400, "向日町": 333,
    "和歌山": 400, "岸和田": 400, "玉野": 400, "広島": 400,
    "防府": 400, "高松": 333, "小松島": 400, "高知": 333,
    "松山": 333, "小倉": 400, "久留米": 400, "武雄": 400,
    "佐世保": 400, "別府": 400, "熊本": 400,
}

# 会場名 → chariloto.com の venue コード
# ※ keirin.jp コード = chariloto コードであることを実測で確認済み（2025〜2026年）。
#   このマップは会場名を介することで keirin.jp コードの変更に強くなる。
NAME_TO_CHARILOTO: dict[str, str] = {
    "函館": "11", "青森": "12", "いわき平": "13",
    "弥彦": "21", "前橋": "22", "取手": "23", "宇都宮": "24",
    "大宮": "25", "西武園": "26", "京王閣": "27", "立川": "28",
    "松戸": "31", "千葉": "32", "川崎": "34", "平塚": "35",
    "小田原": "36", "伊東": "37", "静岡": "38",
    "名古屋": "42", "岐阜": "43", "大垣": "44", "豊橋": "45",
    "富山": "46", "松阪": "47", "四日市": "48",
    "福井": "51", "奈良": "53", "向日町": "54", "和歌山": "55",
    "岸和田": "56", "玉野": "61", "広島": "62", "防府": "63",
    "高松": "71", "小松島": "73", "高知": "74", "松山": "75",
    "小倉": "81", "久留米": "83", "武雄": "84", "佐世保": "85",
    "別府": "86", "熊本": "87",
}

# sInfo.sstyle (int) → 脚質文字
STYLE_MAP = {0: "逃", 1: "捲", 2: "差", 3: "追", 4: "自"}

# 車券種別（日本語キー → DB カラム名）
BET_TYPES = {
    "単勝":  "tansho",
    "複勝":  "fukusho",
    "二車複": "nishafuku",
    "二車単": "nishan",
    "ワイド": "wide",
    "三連複": "sanrenfuku",
    "三連単": "sanrentan",
}

GRADE_PATTERN = re.compile(r"\b(GP|G[123]|F[12])\b", re.IGNORECASE)

# race_id → {encParaR, encParaK, narabiFlg, sInfo, narabiInfo}
_encp_cache: dict[str, dict] = {}

# encp → jsondata（会場ページの重複フェッチを防ぐ）
_venue_page_cache: dict[str, dict] = {}

# "{venue_code}/{date_str}" → {race_no: {results, narabi, payouts}}
# chariloto.com からの1日分結果キャッシュ
_chariloto_day_cache: dict[str, dict] = {}


# ============================================================
# DB 初期化
# ============================================================

def init_db():
    spk = _db.serial_pk()
    conn = _db.get_connection()
    c    = _db.get_cursor(conn)

    c.execute(f"""
        CREATE TABLE IF NOT EXISTS races (
            race_id     TEXT PRIMARY KEY,
            date        TEXT,
            venue       TEXT,
            venue_code  TEXT,
            race_no     INTEGER,
            race_name   TEXT,
            grade       TEXT,
            num_racers  INTEGER,
            bank_length INTEGER,
            start_time  TEXT
        )
    """)
    # 既存DBにカラムが無い場合は追加（PostgreSQLはIF NOT EXISTSで安全に実行）
    try:
        c.execute("ALTER TABLE races ADD COLUMN IF NOT EXISTS start_time TEXT")
        conn.commit()
    except Exception:
        conn.rollback()
        pass
    try:
        c.execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS start_time TEXT")
        conn.commit()
    except Exception:
        conn.rollback()
        pass
    try:
        c.execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS n_combos INTEGER")
        conn.commit()
    except Exception:
        conn.rollback()
        pass

    c.execute(f"""
        CREATE TABLE IF NOT EXISTS results (
            id           {spk},
            race_id      TEXT,
            finish_pos   INTEGER,
            car_no       INTEGER,
            racer_no     TEXT,
            racer_name   TEXT,
            class_rank   TEXT,
            prefecture   TEXT,
            age          INTEGER,
            racing_style TEXT,
            gear_ratio   REAL,
            time_sec     REAL,
            odds         REAL,
            popularity   INTEGER,
            FOREIGN KEY (race_id) REFERENCES races(race_id)
        )
    """)

    c.execute(f"""
        CREATE TABLE IF NOT EXISTS lines (
            id           {spk},
            race_id      TEXT,
            line_no      INTEGER,
            position     INTEGER,
            car_no       INTEGER,
            racer_name   TEXT,
            FOREIGN KEY (race_id) REFERENCES races(race_id)
        )
    """)

    c.execute(f"""
        CREATE TABLE IF NOT EXISTS payouts (
            id         {spk},
            race_id    TEXT,
            bet_type   TEXT,
            car_no1    INTEGER,
            car_no2    INTEGER,
            car_no3    INTEGER,
            payout     INTEGER,
            popularity INTEGER,
            FOREIGN KEY (race_id) REFERENCES races(race_id)
        )
    """)

    c.execute(f"""
        CREATE TABLE IF NOT EXISTS signals (
            id            {spk},
            date          TEXT,
            race_id       TEXT,
            venue         TEXT,
            race_no       INTEGER,
            strategy      TEXT,
            bet_type      TEXT,
            axis_car      INTEGER,
            racer_name    TEXT,
            odds_at_pick  REAL,
            ev_mark       TEXT,
            is_hit        INTEGER,
            actual_payout INTEGER,
            created_at    TEXT,
            start_time    TEXT,
            n_combos      INTEGER
        )
    """)

    c.execute(f"""
        CREATE TABLE IF NOT EXISTS bets (
            id          {spk},
            date        TEXT,
            race_id     TEXT,
            venue       TEXT,
            race_no     INTEGER,
            strategy    TEXT,
            bet_type    TEXT,
            axis_car    INTEGER,
            aite_cars   TEXT,
            n_combos    INTEGER,
            amount      INTEGER,
            is_hit      INTEGER,
            payout      INTEGER,
            profit      INTEGER,
            notes       TEXT,
            created_at  TEXT
        )
    """)

    c.execute(f"""
        CREATE TABLE IF NOT EXISTS picks_cache (
            date       TEXT PRIMARY KEY,
            report     TEXT,
            updated_at TEXT
        )
    """)

    c.execute(f"""
        CREATE TABLE IF NOT EXISTS optimize_cache (
            id         TEXT PRIMARY KEY,
            report     TEXT,
            updated_at TEXT
        )
    """)

    # 一意制約インデックス（重複防止）
    try:
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS lines_unique_idx ON lines (race_id, line_no, position)")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS payouts_unique_idx ON payouts (race_id, bet_type, car_no1, car_no2, car_no3)")
        conn.commit()
    except Exception:
        conn.rollback()

    # signals 重複防止インデックス
    # 既存重複を先に削除してからインデックスを作成
    try:
        if _db.is_pg():
            # PostgreSQL: 古い重複行を削除（id最小を残す）
            c.execute("""
                DELETE FROM signals WHERE id NOT IN (
                    SELECT MIN(id) FROM signals
                    GROUP BY race_id, strategy, bet_type, axis_car
                )
            """)
        else:
            c.execute("""
                DELETE FROM signals WHERE rowid NOT IN (
                    SELECT MIN(rowid) FROM signals
                    GROUP BY race_id, strategy, bet_type, axis_car
                )
            """)
        conn.commit()
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS signals_unique_idx ON signals (race_id, strategy, bet_type, axis_car)")
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.warning(f"signals unique index 作成スキップ: {e}")

    conn.commit()
    conn.close()


# ============================================================
# HTTP helper
# ============================================================

def _get(url: str, timeout: int = 20):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "utf-8"
        return resp
    except Exception as e:
        logger.warning(f"GET失敗: {url} → {e}")
        return None


# ============================================================
# keirin.jp HTML 内埋め込み JSON 抽出
# ============================================================

def _extract_json_obj(html: str, start_pos: int) -> dict:
    """html[start_pos] が '{' の位置から完全な JSON オブジェクトを抽出"""
    depth = 0
    in_str = False
    escape = False
    for i, ch in enumerate(html[start_pos:], start_pos):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_str:
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start_pos:i + 1])
                except Exception:
                    return {}
    return {}


def _extract_pc0101(html: str) -> dict:
    """var pc0101_json = {...} を HTML から抽出"""
    m = re.search(r"var\s+pc0101_json\s*=\s*(\{)", html)
    if not m:
        return {}
    return _extract_json_obj(html, m.start(1))


def _extract_jsondata(html: str) -> dict:
    """jsonData['KEY'] = {...}; を全て抽出して {KEY: dict, ...} を返す"""
    result = {}
    for m in re.finditer(r"jsonData\['(\w+)'\]\s*=\s*(\{)", html):
        key  = m.group(1)
        data = _extract_json_obj(html, m.start(2))
        if data:
            result[key] = data
    return result


# ============================================================
# 開催スケジュール取得
# ============================================================

def _make_race_id(venue_code: str, date_str: str, race_no: int) -> str:
    return f"{date_str}{venue_code}{str(race_no).zfill(2)}"


def fetch_kaisai_for_month(year: int, month: int) -> list[dict]:
    """
    指定年月の全開催情報を取得。
    返り値: [{"venue_code", "venue_name", "kaisai_date", "encp"}, ...]

    RaceList の各エントリは 1会場×1日 単位。
    """
    url = f"{BASE_URL}/pc/raceschedule?scyy={year}&scym={month:02d}"
    resp = _get(url)
    if not resp:
        return []

    data      = _extract_pc0101(resp.text)
    race_list = data.get("RaceList", [])

    meetings = []
    for entry in race_list:
        # 内部会場コード
        venue_code = str(entry.get("naibuKeirinCd", "") or "").zfill(2)
        venue_name = str(entry.get("keirinjoName", "") or "")
        kaisai_date = str(entry.get("kaisaiDate", "") or "")
        encp = entry.get("touhyouLivePara") or entry.get("encParaTop") or ""

        if not encp or not kaisai_date:
            continue

        # keirin.jp の実データで VENUE_MAP を常に更新（名前が正しいソース）
        if venue_code and venue_name:
            bank = _BANK_BY_NAME.get(venue_name, 400)
            VENUE_MAP[venue_code] = (venue_name, bank)

        meetings.append({
            "venue_code":  venue_code,
            "venue_name":  venue_name,
            "kaisai_date": kaisai_date,   # YYYYMMDD (1エントリ = 1日)
            "encp":        encp,
        })

    logger.debug(f"{year}-{month:02d}: {len(meetings)}開催エントリ")
    return meetings


# ============================================================
# 会場ページ取得 + encp キャッシュ構築
# ============================================================

def _fetch_venue_page(encp: str) -> dict:
    """
    会場ページ (racelist?encp={touhyouLivePara}) を取得し
    {PC0201: {...}, PJ0305: {...}, ...} を返す。
    同一 encp は再リクエストしない。
    """
    if encp in _venue_page_cache:
        return _venue_page_cache[encp]
    url  = f"{BASE_URL}/pc/racelist?encp={encp}&dkbn=1"
    resp = _get(url)
    if not resp:
        return {}
    result = _extract_jsondata(resp.text)
    _venue_page_cache[encp] = result
    return result


def _populate_cache_from_venue(
    venue_code: str, date_str: str, venue_jd: dict
) -> list[tuple[str, str, int]]:
    """
    会場ページの JSON から各レースの encp・sInfo を _encp_cache に格納。
    返り値: [(venue_code, date_str, race_no), ...]

    実際の構造:
      PC0201.C0201data.C0201race[i] → {encParaR, flgRaceEnd, rcvKekka, ...}  (index順)
      PJ0305.rInfo[i]               → {raceNo, sInfo, narabiFlg, resultFlg, ...}
    両配列は同じ順序なのでインデックスで対応させる。
    """
    pc0201 = venue_jd.get("PC0201", {})
    pj0305 = venue_jd.get("PJ0305", {})

    c0201_races = pc0201.get("C0201data", {}).get("C0201race", [])
    pj_rinfo    = pj0305.get("rInfo", [])

    race_list = []
    for idx, cr in enumerate(c0201_races):
        enc_r = cr.get("encParaR", "")
        if not enc_r:
            continue

        # race_no は PJ0305.rInfo[idx].raceNo が正確
        pj_entry = pj_rinfo[idx] if idx < len(pj_rinfo) else {}
        race_no  = int(pj_entry.get("raceNo", idx + 1) or idx + 1)
        race_id  = _make_race_id(venue_code, date_str, race_no)

        # 発走時刻: C0201race の stTime / hassojikan / 時刻系フィールドを試みる
        start_time = ""
        for tf in ["stTime", "hassojikan", "startTime", "hasso", "raceTime"]:
            v = cr.get(tf) or pj_entry.get(tf)
            if v:
                start_time = str(v).strip()
                break
        # "HHMM" → "HH:MM" に正規化
        if re.match(r"^\d{4}$", start_time):
            start_time = f"{start_time[:2]}:{start_time[2:]}"

        _encp_cache[race_id] = {
            "encParaR":   enc_r,
            "encParaK":   cr.get("encParaK", ""),
            "narabiFlg":  int(pj_entry.get("narabiFlg", 0) or 0),
            "resultFlg":  int(pj_entry.get("resultFlg", 0) or 0),
            "sInfo":      pj_entry.get("sInfo", []),
            "narabiInfo": (
                pj_entry.get("narabiInfo")
                or pj_entry.get("narabi")
                or []
            ),
            "start_time": start_time,
        }
        race_list.append((venue_code, date_str, race_no))

    return race_list


def fetch_race_ids_for_date(target_date: date) -> list[tuple[str, str, int]]:
    """
    指定日の全レース (venue_code, date_str, race_no) を返す。
    副作用として _encp_cache を更新する。

    RaceList は 1エントリ = 1会場×1日 なので kaisai_date と完全一致で照合する。
    """
    year     = target_date.year
    month    = target_date.month
    date_str = target_date.strftime("%Y%m%d")

    meetings = fetch_kaisai_for_month(year, month)
    results  = []

    for m in meetings:
        if m["kaisai_date"] != date_str:
            continue

        venue_code = m["venue_code"]
        encp       = m["encp"]
        venue_jd   = _fetch_venue_page(encp)
        if not venue_jd:
            logger.warning(
                f"会場ページ取得失敗: {VENUE_MAP.get(venue_code, (venue_code,))[0]} "
                f"encp={encp[:20]}..."
            )
            continue

        races = _populate_cache_from_venue(venue_code, date_str, venue_jd)
        results.extend(races)
        venue_name = VENUE_MAP.get(venue_code, (venue_code,))[0]
        logger.info(f"{venue_name} ({date_str}): {len(races)}レース found")
        time.sleep(1.0)

    return results


# ============================================================
# 個別レースページ解析
# ============================================================

def _sinfo_to_entry_base(si: dict, race_id: str) -> dict:
    """
    PJ0305 sInfo エントリ → entry dict（基本フィールドのみ）

    実際のフィールド名（取手 F1 から確認済み）:
      syaban  = 車番
      senNo   = 登録番号
      senName = 選手名
      huken   = 府県
      kyaku   = 脚質（文字列: "逃"/"差"/"追"/"捲"/"自"）
    class_rank・gear_ratio・odds は個別レースページ HTML から取得する。
    """
    # 脚質: 文字列のまま使用（"逃"/"差"/"追"/"捲"/"自"）
    kyaku = str(si.get("kyaku", si.get("sstyle", si.get("kstyle", ""))) or "")
    # STYLE_MAP (int) にも対応
    if kyaku.lstrip("-").isdigit():
        kyaku = STYLE_MAP.get(int(kyaku), "")

    car_no = 0
    try:
        car_no = int(si.get("syaban", si.get("shano", si.get("carno", 0))) or 0)
    except (ValueError, TypeError):
        pass

    return {
        "race_id":      race_id,
        "car_no":       car_no,
        "racer_no":     str(si.get("senNo", si.get("tno", si.get("sno", ""))) or ""),
        "racer_name":   str(si.get("senName", si.get("mnam", si.get("name", ""))) or ""),
        "class_rank":   "",   # 個別レースページから補完
        "prefecture":   str(si.get("huken", si.get("pref", si.get("ken", ""))) or ""),
        "age":          0,    # sInfo にない場合は個別ページから補完
        "racing_style": kyaku,
        "gear_ratio":   0.0,  # 個別レースページから補完
        "odds":         0.0,  # 個別レースページから補完
        "popularity":   0,    # 個別レースページから補完
    }


def _parse_race_page_html(soup: BeautifulSoup, race_id: str) -> dict:
    """
    個別レースページ HTML から着順・払戻・詳細選手情報を解析。
    返り値: {"entries": [...], "results": [...], "payouts": [...]}
    """
    entries  = _parse_entries(soup, race_id)
    results  = _parse_results(soup, race_id)
    payouts  = _parse_payouts(soup, race_id)
    return {"entries": entries, "results": results, "payouts": payouts}


def _parse_entries(soup: BeautifulSoup, race_id: str) -> list[dict]:
    """出走表テーブルから選手情報を解析（HTML フォールバック）"""
    entries = []

    table = None
    for sel in [
        "table.table-raceinfo",
        "table.syutsusouji",
        "table.raceinfo",
        "table.race-entry",
        "table.entry",
    ]:
        table = soup.select_one(sel)
        if table:
            break

    if not table:
        for t in soup.select("table"):
            txt = t.get_text()
            if "車番" in txt or "選手名" in txt or "登録番号" in txt:
                table = t
                break

    if not table:
        return entries

    for tr in table.select("tr")[1:]:
        tds   = tr.select("td")
        if len(tds) < 4:
            continue
        texts = [td.get_text(strip=True) for td in tds]

        car_no = None
        for t in texts[:3]:
            if t.isdigit() and 1 <= int(t) <= 9:
                car_no = int(t)
                break
        if car_no is None:
            continue

        entry = {"race_id": race_id, "car_no": car_no}

        # 登録番号（4〜5桁）
        entry["racer_no"] = ""
        for t in texts:
            if re.match(r"^\d{4,5}$", t):
                entry["racer_no"] = t
                break

        # 選手名（漢字 2〜5文字）
        entry["racer_name"] = ""
        for t in texts:
            if re.match(r"^[一-鿿぀-ゟ゠-ヿ]{2,6}$", t):
                entry["racer_name"] = t
                break

        # クラス (S1/S2/A1/A2/A3)
        entry["class_rank"] = ""
        for t in texts:
            if re.match(r"^[SA]\d$", t):
                entry["class_rank"] = t
                break

        # 府県
        entry["prefecture"] = ""
        for t in texts:
            if (re.match(r"^[一-鿿]{1,4}$", t)
                    and t != entry.get("racer_name", "")):
                entry["prefecture"] = t
                break

        # 年齢
        entry["age"] = 0
        for t in texts:
            if re.match(r"^\d{2}$", t):
                age = int(t)
                if 18 <= age <= 60:
                    entry["age"] = age
                    break

        # 脚質
        entry["racing_style"] = ""
        for t in texts:
            if t in ("逃", "差", "追", "捲", "自"):
                entry["racing_style"] = t
                break

        # ギア比
        entry["gear_ratio"] = 0.0
        for t in texts:
            if re.match(r"^\d\.\d{2,3}$", t):
                entry["gear_ratio"] = float(t)
                break

        # オッズ
        entry["odds"] = 0.0
        entry["popularity"] = 0
        for i, t in enumerate(texts):
            if re.match(r"^\d+\.\d$", t):
                entry["odds"] = float(t)
                # 次の数字を人気とみなす
                if i + 1 < len(texts) and texts[i + 1].isdigit():
                    entry["popularity"] = int(texts[i + 1])
                break

        entries.append(entry)

    return entries


def _parse_results(soup: BeautifulSoup, race_id: str) -> list[dict]:
    """結果テーブルから着順・タイムを解析"""
    results = []

    table = None
    for t in soup.select("table"):
        txt = t.get_text()
        if "着順" in txt or "1着" in txt or "2着" in txt:
            table = t
            break

    if not table:
        return results

    for tr in table.select("tr")[1:]:
        tds = tr.select("td")
        if len(tds) < 2:
            continue
        texts = [td.get_text(strip=True) for td in tds]

        pos = None
        for t in texts[:2]:
            if t.isdigit() and 1 <= int(t) <= 9:
                pos = int(t)
                break
        if pos is None:
            continue

        car_no = None
        for t in texts[1:4]:
            if t.isdigit() and 1 <= int(t) <= 9 and int(t) != pos:
                car_no = int(t)
                break
        if car_no is None:
            # 着順 = 車番が同じ列に入っているケース
            car_no = pos

        time_sec = None
        for t in texts:
            m = re.match(r"(\d+)[:\.](\d+)$", t)
            if m:
                try:
                    time_sec = int(m.group(1)) * 60 + float(m.group(2)) / (
                        10 ** len(m.group(2))
                    ) * 60
                except Exception:
                    pass
                break

        results.append({
            "race_id":    race_id,
            "finish_pos": pos,
            "car_no":     car_no,
            "time_sec":   time_sec,
        })

    return results


def _parse_payouts(soup: BeautifulSoup, race_id: str) -> list[dict]:
    """払戻テーブルを解析"""
    payouts = []

    for table in soup.select("table"):
        txt = table.get_text()
        if not any(k in txt for k in BET_TYPES.keys()):
            continue

        current_type = None
        for tr in table.select("tr"):
            th = tr.select_one("th")
            if th:
                label = th.get_text(strip=True)
                current_type = None
                for jname, ename in BET_TYPES.items():
                    if jname in label:
                        current_type = ename
                        break

            if not current_type:
                continue

            tds = tr.select("td")
            if len(tds) < 2:
                continue

            def td_strings(td):
                return [
                    s.strip().replace(",", "").replace("円", "").replace("人気", "")
                    for s in td.strings
                    if s.strip()
                ]

            combos   = td_strings(tds[0])
            amounts  = td_strings(tds[1]) if len(tds) > 1 else []
            pops     = td_strings(tds[2]) if len(tds) > 2 else []

            for i, combo_str in enumerate(combos):
                amt_str = amounts[i] if i < len(amounts) else ""
                pop_str = pops[i]    if i < len(pops)    else ""
                if not re.match(r"^\d+$", amt_str):
                    continue

                car_nos = [
                    int(x) for x in re.findall(r"\d+", combo_str) if 1 <= int(x) <= 9
                ]
                if not car_nos:
                    continue

                payouts.append({
                    "race_id":    race_id,
                    "bet_type":   current_type,
                    "car_no1":    car_nos[0] if len(car_nos) > 0 else None,
                    "car_no2":    car_nos[1] if len(car_nos) > 1 else None,
                    "car_no3":    car_nos[2] if len(car_nos) > 2 else None,
                    "payout":     int(amt_str),
                    "popularity": int(pop_str) if re.match(r"^\d+$", pop_str) else 0,
                })

    return payouts


def _parse_lines_from_narabi(narabi_info: list, entries: list[dict], race_id: str) -> list[dict]:
    """narabiInfo JSON からライン情報を構築"""
    lines = []
    for line_no, group in enumerate(narabi_info, 1):
        car_nos = []
        if isinstance(group, dict):
            # {"cars": [1,2,3]} or {"carNos": [...]} or直接 list
            car_nos = (
                group.get("cars") or group.get("carNos") or group.get("shanoList") or []
            )
        elif isinstance(group, list):
            car_nos = group

        for pos, cno in enumerate(car_nos):
            try:
                cno_int = int(cno)
            except (TypeError, ValueError):
                continue
            name = next((e["racer_name"] for e in entries if e["car_no"] == cno_int), "")
            lines.append({
                "race_id":    race_id,
                "line_no":    line_no,
                "position":   pos,
                "car_no":     cno_int,
                "racer_name": name,
            })
    return lines


def _parse_lines_from_html(soup: BeautifulSoup, race_id: str, entries: list[dict]) -> list[dict]:
    """HTML からライン情報を解析（テキストパターン / テーブル）"""
    lines = []
    text  = soup.get_text(" ")

    # パターン1: "1-2-3 / 4-5 / ..." 形式のテキスト
    m = re.search(r"ライン[^\n]{0,20}?([\d][- \d/　・]+)", text)
    if m:
        raw    = m.group(1).strip()
        groups = re.split(r"[/　 ・]+", raw)
        for line_no, group in enumerate(groups, 1):
            car_nos = [int(x) for x in re.findall(r"\d+", group) if 1 <= int(x) <= 9]
            for pos, cno in enumerate(car_nos):
                name = next((e["racer_name"] for e in entries if e["car_no"] == cno), "")
                lines.append({
                    "race_id": race_id, "line_no": line_no,
                    "position": pos, "car_no": cno, "racer_name": name,
                })
        if lines:
            return lines

    # パターン2: ライン関連テーブル
    for table in soup.select("table"):
        txt_t = table.get_text()
        if "ライン" in txt_t or ("先頭" in txt_t and "番手" in txt_t):
            line_no = 0
            for tr in table.select("tr"):
                tds     = tr.select("td")
                car_nos = []
                for td in tds:
                    for x in re.findall(r"\b([1-9])\b", td.get_text()):
                        car_nos.append(int(x))
                car_nos = list(dict.fromkeys(car_nos))
                if car_nos:
                    line_no += 1
                    for pos, cno in enumerate(car_nos):
                        name = next(
                            (e["racer_name"] for e in entries if e["car_no"] == cno), ""
                        )
                        lines.append({
                            "race_id": race_id, "line_no": line_no,
                            "position": pos, "car_no": cno, "racer_name": name,
                        })
            if lines:
                return lines

    return lines


def _infer_lines_from_style(entries: list[dict], race_id: str) -> list[dict]:
    """脚質からライン構造を推定（ライン情報が得られない場合のフォールバック）"""
    lines     = []
    assigned  = set()
    line_no   = 0

    for e in entries:
        if e["car_no"] in assigned or e.get("racing_style") != "逃":
            continue
        line_no += 1
        lines.append({
            "race_id": race_id, "line_no": line_no,
            "position": 0, "car_no": e["car_no"], "racer_name": e.get("racer_name", ""),
        })
        assigned.add(e["car_no"])

    for e in entries:
        if e["car_no"] not in assigned:
            line_no += 1
            lines.append({
                "race_id": race_id, "line_no": line_no,
                "position": 0, "car_no": e["car_no"], "racer_name": e.get("racer_name", ""),
            })
            assigned.add(e["car_no"])

    return lines


# ============================================================
# 公開 API: 出走表・結果取得
# ============================================================

def fetch_race_entry(venue_code: str, date_str: str, race_no: int) -> dict | None:
    """
    出走表を取得。
    返り値: {"race_info": {...}, "entries": [...], "lines": [...]}

    _encp_cache に race_id が存在する場合は sInfo を優先使用し、
    個別レースページ HTML で補完する。
    """
    race_id  = _make_race_id(venue_code, date_str, race_no)
    cached   = _encp_cache.get(race_id, {})
    enc_r    = cached.get("encParaR", "")

    venue_name, bank = VENUE_MAP.get(venue_code, (venue_code, 400))

    # --- sInfo ベースのエントリ（会場ページから取得済み）---
    sinfo_list = cached.get("sInfo", [])
    entries_base = [_sinfo_to_entry_base(si, race_id) for si in sinfo_list
                    if si.get("syaban") or si.get("shano") or si.get("carno")]

    # --- 個別レースページ HTML で補完 ---
    html_entries: list[dict] = []
    html_results: list[dict] = []
    race_name = ""
    grade     = ""
    narabi_json: list = cached.get("narabiInfo", [])

    if enc_r:
        resp = _get(f"{BASE_URL}/pc/racelist?encp={enc_r}&dkbn=1")
        if resp:
            jd   = _extract_jsondata(resp.text)
            soup = BeautifulSoup(resp.text, "html.parser")

            # JSON から追加情報を試みる
            for key, jval in jd.items():
                pj_rinfo = jval.get("rInfo", []) if isinstance(jval, dict) else []
                for ri in pj_rinfo:
                    rno_match = (int(ri.get("rno", 0) or 0) == race_no)
                    if not rno_match:
                        continue
                    extra_si  = ri.get("sInfo", [])
                    if extra_si and not sinfo_list:
                        entries_base = [_sinfo_to_entry_base(si, race_id)
                                        for si in extra_si
                                        if si.get("syaban") or si.get("shano") or si.get("carno")]
                    if not narabi_json:
                        narabi_json = (
                            ri.get("narabiInfo") or ri.get("narabi")
                            or ri.get("liInfo") or []
                        )

            # HTML パース（クラス・詳細補完）
            parsed    = _parse_race_page_html(soup, race_id)
            html_entries = parsed["entries"]
            html_results = parsed["results"]

            # レース名・グレード
            for sel in ["h1", "h2", ".race-name", "title"]:
                el = soup.select_one(sel)
                if el:
                    race_name = el.get_text(strip=True)[:60]
                    break
            gm = GRADE_PATTERN.search(soup.get_text(" ")[:2000])
            if gm:
                grade = gm.group(1).upper()
    else:
        logger.debug(f"encParaR なし: {race_id} (encp キャッシュ未取得)")

    # sInfo ベースと HTML 解析を統合（class_rank は HTML 優先）
    html_by_car = {e["car_no"]: e for e in html_entries}
    if entries_base:
        entries = []
        for e in entries_base:
            he = html_by_car.get(e["car_no"], {})
            if not e["class_rank"] and he.get("class_rank"):
                e["class_rank"] = he["class_rank"]
            if not e["racing_style"] and he.get("racing_style"):
                e["racing_style"] = he["racing_style"]
            if not e["gear_ratio"] and he.get("gear_ratio"):
                e["gear_ratio"] = he["gear_ratio"]
            if not e["odds"] and he.get("odds"):
                e["odds"] = he["odds"]
            if not e["popularity"] and he.get("popularity"):
                e["popularity"] = he["popularity"]
            entries.append(e)
    else:
        entries = html_entries

    if not entries:
        logger.debug(f"選手情報取得失敗: {race_id}")
        return None

    # chariloto から class_rank / age を補完
    chariloto_data = _fetch_chariloto_day(venue_code, date_str).get(race_no, {})
    class_by_car   = chariloto_data.get("class_by_car", {})
    age_by_car     = chariloto_data.get("age_by_car", {})
    for e in entries:
        if not e["class_rank"] and class_by_car.get(e["car_no"]):
            e["class_rank"] = class_by_car[e["car_no"]]
        if not e["age"] and age_by_car.get(e["car_no"]):
            e["age"] = age_by_car[e["car_no"]]

    # ライン情報: keirin.jp narabi → chariloto narabi → HTML → 脚質推定
    if narabi_json and int(cached.get("narabiFlg", 0) or 0) == 1:
        lines = _parse_lines_from_narabi(narabi_json, entries, race_id)
    else:
        lines = chariloto_data.get("narabi", [])
        # narabi の racer_name を entries から補完
        for ln in lines:
            if not ln.get("racer_name"):
                ln["racer_name"] = next(
                    (e["racer_name"] for e in entries if e["car_no"] == ln["car_no"]),
                    "",
                )

    if not lines:
        lines = _infer_lines_from_style(entries, race_id)

    # HTMLから発走時刻を補完（キャッシュに無い場合）
    start_time = cached.get("start_time", "")
    if not start_time and enc_r:
        # ページHTMLから "発走" の近くの時刻を探す
        resp2 = _get(f"{BASE_URL}/pc/racelist?encp={enc_r}&dkbn=1") if not soup else None
        search_text = resp2.text if resp2 else (soup.get_text() if soup else "")
        tm = re.search(r'発走[^\d]*(\d{1,2}:\d{2})', search_text)
        if not tm:
            tm = re.search(r'(\d{1,2}:\d{2})', search_text[:3000])
        if tm:
            start_time = tm.group(1)

    race_info = {
        "race_id":    race_id,
        "date":       f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}",
        "venue":      venue_name,
        "venue_code": venue_code,
        "race_no":    race_no,
        "race_name":  race_name,
        "grade":      grade,
        "num_racers": len(entries),
        "bank_length": bank,
        "start_time": start_time,
    }

    return {"race_info": race_info, "entries": entries, "lines": lines}


def fetch_race_result(venue_code: str, date_str: str, race_no: int) -> dict | None:
    """
    chariloto.com から着順・払戻を取得。
    返り値: {"results": [...], "payouts": [...], "class_by_car": {...}, "age_by_car": {...}, "narabi": [...]}
    """
    day_data = _fetch_chariloto_day(venue_code, date_str)
    race_data = day_data.get(race_no)
    if not race_data:
        logger.debug(f"chariloto 結果なし: {venue_code}/{date_str} R{race_no}")
        return None
    return race_data


# ============================================================
# DB 保存
# ============================================================

def already_fetched(race_id: str) -> bool:
    conn = _db.get_connection()
    c    = _db.get_cursor(conn)
    c.execute(_db.sql("SELECT 1 FROM races WHERE race_id=?"), (race_id,))
    exists = c.fetchone() is not None
    conn.close()
    return exists


def save_to_db(
    race_info: dict,
    entries:   list[dict],
    lines:     list[dict],
    results:   list[dict],
    payouts:   list[dict],
):
    conn = _db.get_connection()
    c    = _db.get_cursor(conn)

    c.execute(_db.sql("""
        INSERT OR IGNORE INTO races
        (race_id, date, venue, venue_code, race_no, race_name, grade, num_racers, bank_length, start_time)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """), (
        race_info["race_id"], race_info["date"], race_info["venue"],
        race_info["venue_code"], race_info["race_no"], race_info["race_name"],
        race_info["grade"], race_info["num_racers"], race_info["bank_length"],
        race_info.get("start_time", ""),
    ))

    results_by_car = {r["car_no"]: r for r in results}

    for e in entries:
        res = results_by_car.get(e["car_no"], {})
        c.execute(_db.sql("""
            INSERT OR IGNORE INTO results
            (race_id, finish_pos, car_no, racer_no, racer_name,
             class_rank, prefecture, age, racing_style, gear_ratio,
             time_sec, odds, popularity)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """), (
            e["race_id"],
            res.get("finish_pos"),
            e["car_no"],
            e.get("racer_no", ""),
            e.get("racer_name", ""),
            e.get("class_rank", ""),
            e.get("prefecture", ""),
            e.get("age", 0),
            e.get("racing_style", ""),
            e.get("gear_ratio", 0.0),
            res.get("time_sec"),
            e.get("odds", 0.0),
            e.get("popularity", 0),
        ))

    for ln in lines:
        c.execute(_db.sql("""
            INSERT OR IGNORE INTO lines (race_id, line_no, position, car_no, racer_name)
            VALUES (?,?,?,?,?)
        """), (ln["race_id"], ln["line_no"], ln["position"], ln["car_no"], ln["racer_name"]))

    for p in payouts:
        c.execute(_db.sql("""
            INSERT OR IGNORE INTO payouts (race_id, bet_type, car_no1, car_no2, car_no3, payout, popularity)
            VALUES (?,?,?,?,?,?,?)
        """), (
            p["race_id"], p["bet_type"],
            p.get("car_no1"), p.get("car_no2"), p.get("car_no3"),
            p["payout"], p.get("popularity", 0),
        ))

    conn.commit()
    conn.close()


def _fetch_and_save(venue_code: str, date_str: str, race_no: int) -> bool:
    race_id = _make_race_id(venue_code, date_str, race_no)
    if already_fetched(race_id):
        return False

    venue_name = VENUE_MAP.get(venue_code, (venue_code,))[0]

    entry_data = fetch_race_entry(venue_code, date_str, race_no)
    if not entry_data:
        logger.warning(f"出走表取得失敗: {venue_name} R{race_no} ({date_str})")
        return False

    result_data = fetch_race_result(venue_code, date_str, race_no)
    results = result_data["results"] if result_data else []
    payouts = result_data["payouts"] if result_data else []

    save_to_db(
        entry_data["race_info"],
        entry_data["entries"],
        entry_data["lines"],
        results,
        payouts,
    )

    logger.info(
        f"保存: {race_id} {venue_name}R{race_no} "
        f"({len(entry_data['entries'])}車, 着順{len(results)}件, 払戻{len(payouts)}件)"
    )
    return True


# ============================================================
# chariloto.com スクレイピング（結果・narabi・払戻）
# ============================================================

def _fetch_chariloto_day(venue_code: str, date_str: str) -> dict:
    """
    chariloto.com から指定会場・日付の全レース結果を取得。
    venue_code は keirin.jp の naibuKeirinCd。chariloto URL には KEIRIN_TO_CHARILOTO で変換したコードを使う。
    キャッシュ済みの場合はキャッシュを返す。
    返り値: {race_no: {"results": [...], "narabi": [...], "payouts": [...], "class_by_car": {...}, "age_by_car": {...}}}
    """
    cache_key = f"{venue_code}/{date_str}"
    if cache_key in _chariloto_day_cache:
        return _chariloto_day_cache[cache_key]

    # 会場名から chariloto コードを引く（名前ベースのほうが keirin.jp コード変更に強い）
    venue_name     = VENUE_MAP.get(venue_code, (None,))[0]
    chariloto_code = NAME_TO_CHARILOTO.get(venue_name, venue_code) if venue_name else venue_code
    date_fmt = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    url = f"{CHARILOTO_URL}/keirin/results/{chariloto_code}/{date_fmt}"
    resp = _get(url)
    if not resp:
        logger.debug(f"chariloto 取得失敗: {url}")
        _chariloto_day_cache[cache_key] = {}
        return {}

    soup   = BeautifulSoup(resp.text, "html.parser")
    tables = soup.select("table")
    result = _parse_chariloto_tables(tables, venue_code, date_str)
    _chariloto_day_cache[cache_key] = result
    logger.debug(f"chariloto: {venue_code}({venue_name}→{chariloto_code})/{date_str} → {len(result)}レース分")
    return result


def _parse_chariloto_tables(tables: list, venue_code: str, date_str: str) -> dict:
    """
    chariloto の全テーブルを解析して race_no → data の dict を返す。

    テーブル構造 (7テーブル × レース数 + ナビ1):
      [nav] [result] [narabi] [narabi_dup] [2枠連] [2車連] [3連勝] [ワイド] [result] ...
    """
    RESULT_HEADER = {"着", "車番", "選手名", "級班"}
    NARABI_MARKER = "周回予想"

    groups: list[dict] = []   # 各レースのデータ
    i = 0
    while i < len(tables):
        trs = tables[i].select("tr")
        if not trs:
            i += 1
            continue

        header_texts = set(th.get_text(strip=True) for th in trs[0].select("th"))
        if RESULT_HEADER.issubset(header_texts):
            # 結果テーブル発見: このテーブルから始まるグループを処理
            grp = {
                "results_rows":  trs[1:],   # ヘッダ除く
                "narabi_cells":  [],         # 内側テーブルの td テキスト
                "payout_tables": [],
            }
            j = i + 1
            # 次の結果テーブルまたは終端まで続くテーブルを収集
            while j < len(tables) and j < i + 8:
                next_trs = tables[j].select("tr")
                if not next_trs:
                    j += 1
                    continue
                next_header = set(th.get_text(strip=True) for th in next_trs[0].select("th"))
                if RESULT_HEADER.issubset(next_header):
                    break  # 次の結果テーブル
                first_cell = next_trs[0].select_one("td,th")
                cell_text  = first_cell.get_text(strip=True) if first_cell else ""
                if NARABI_MARKER in cell_text:
                    # 周回予想は <th>周回予想</th><td><table>...</table></td> 構造
                    # 内側テーブルの td テキストを取得
                    outer_td = tables[j].find("td")
                    if outer_td:
                        inner_table = outer_td.find("table")
                        if inner_table:
                            grp["narabi_cells"] = [
                                td.get_text(strip=True)
                                for td in inner_table.select("td")
                            ]
                elif any(k in cell_text for k in ("2車連", "3連勝", "ワイド")):
                    grp["payout_tables"].append(tables[j])
                j += 1

            groups.append(grp)
            i = j
        else:
            i += 1

    output = {}
    for idx, grp in enumerate(groups):
        race_no = idx + 1
        race_id = _make_race_id(venue_code, date_str, race_no)

        # 着順・選手名・クラス・年齢・タイム
        # カラム順: 着(0), 車番(1), 選手名(2), 年齢(3), 府県(4), 期別(5), 級班(6), 着差(7), 上り(8)
        results        = []
        class_by_car   = {}
        age_by_car     = {}
        name_by_car    = {}
        pref_by_car    = {}
        racer_no_by_car = {}  # 期別 → racer_no として使用
        for tr in grp["results_rows"]:
            tds   = tr.select("td")
            if len(tds) < 7:
                continue
            texts = [td.get_text(strip=True) for td in tds]
            try:
                finish_pos = int(texts[0]) if texts[0].isdigit() else None
                car_no     = int(texts[1]) if texts[1].isdigit() else None
            except (ValueError, IndexError):
                continue
            if finish_pos is None or car_no is None:
                continue

            racer_name = texts[2] if len(texts) > 2 else ""
            prefecture = texts[4] if len(texts) > 4 else ""
            racer_no   = texts[5] if len(texts) > 5 else ""  # 期別を登録番号代わりに

            # 年齢 (index 3), 級班 (index 6), 上り (index 8)
            try:
                age = int(texts[3]) if len(texts) > 3 and texts[3].isdigit() else 0
            except (ValueError, IndexError):
                age = 0

            kyu_raw = texts[6].strip() if len(texts) > 6 else ""
            # 全角→半角に変換 (Ａ１→A1, Ｓ２→S2, Ｌ１→L1)
            kyu_str = kyu_raw.translate(
                str.maketrans("ＡＢＳＬａｂｓｌ０１２３４５", "ABSLabsl012345")
            ).strip()
            if not re.match(r"^[SAL]\d$", kyu_str):
                kyu_str = ""

            time_raw = texts[8] if len(texts) > 8 else ""
            time_sec = None
            try:
                time_sec = float(time_raw) if time_raw else None
            except (ValueError, TypeError):
                pass

            results.append({
                "race_id":    race_id,
                "finish_pos": finish_pos,
                "car_no":     car_no,
                "racer_name": racer_name,
                "time_sec":   time_sec,
            })
            class_by_car[car_no]    = kyu_str
            age_by_car[car_no]      = age
            name_by_car[car_no]     = racer_name
            pref_by_car[car_no]     = prefecture
            racer_no_by_car[car_no] = racer_no

        # narabi（内側テーブルの td テキストから構築）
        narabi = _parse_narabi_cells(grp["narabi_cells"], race_id) if grp["narabi_cells"] else []

        # payouts
        payouts = []
        for pt in grp["payout_tables"]:
            pt_rows = pt.select("tr")
            if not pt_rows:
                continue
            header_cell = pt_rows[0].select_one("td,th")
            if not header_cell:
                continue
            table_type = header_cell.get_text(strip=True)
            for pr in pt_rows[1:]:
                tds = pr.select("td")
                if not tds:
                    continue
                # ワイドは 1列のみ（複/単の区別なし）
                # 2車連・3連勝は 2列（列1=複/単, 列2=車番+金額）
                if len(tds) == 1:
                    row_type   = ""
                    payout_str = tds[0].get_text(strip=True)
                else:
                    row_type   = tds[0].get_text(strip=True)
                    payout_str = tds[1].get_text(strip=True)
                parsed = _parse_chariloto_payout_row(
                    table_type, row_type, payout_str, race_id
                )
                payouts.extend(parsed)

        output[race_no] = {
            "results":         results,
            "narabi":          narabi,
            "payouts":         payouts,
            "class_by_car":    class_by_car,
            "age_by_car":      age_by_car,
            "name_by_car":     name_by_car,
            "pref_by_car":     pref_by_car,
            "racer_no_by_car": racer_no_by_car,
        }

    return output


def _parse_narabi_cells(cells: list[str], race_id: str) -> list[dict]:
    """
    ['6', '1', '7', '', '2', '5', '', '3', '', '4', ...] を
    ライン情報 list[dict] に変換する。空文字がグループ区切り。
    """
    lines    = []
    line_no  = 1
    position = 0
    for cell in cells:
        cell = cell.strip()
        if not cell:
            # ライン区切り（次の車番があれば新しいライン）
            if position > 0:
                line_no  += 1
                position  = 0
        elif cell.isdigit() and 1 <= int(cell) <= 9:
            lines.append({
                "race_id":    race_id,
                "line_no":    line_no,
                "position":   position,
                "car_no":     int(cell),
                "racer_name": "",
            })
            position += 1
    return lines


def _parse_chariloto_payout_row(
    table_type: str, row_type: str, payout_str: str, race_id: str
) -> list[dict]:
    """
    chariloto の払戻行 1行 → payouts list に変換。

    table_type: '2車連' / '3連勝' / 'ワイド'
    row_type:   '複' / '単'
    payout_str: '1=42,050円' / '1-42,220円' / '1=2=42,280円' / '1=4430円1=2380円2=4800円'
    """
    if "発売無し" in payout_str or not payout_str:
        return []

    results = []

    if table_type == "2車連":
        bet_type = "nishafuku" if row_type == "複" else "nishan"
        # 車番は1桁。'1=42,050円' → car_a=1, car_b=4, payout=2050
        # (\d)[=\-](\d) で車番2つを確定し、その後方から金額を取得する
        for m in re.finditer(r"(\d)[=\-](\d)", payout_str):
            car_a = int(m.group(1))
            car_b = int(m.group(2))
            amt   = _extract_payout_amount(payout_str, m.end())
            if amt and 1 <= car_a <= 9 and 1 <= car_b <= 9:
                results.append({
                    "race_id":    race_id,
                    "bet_type":   bet_type,
                    "car_no1":    car_a,
                    "car_no2":    car_b,
                    "car_no3":    None,
                    "payout":     amt,
                    "popularity": 0,
                })

    elif table_type == "3連勝":
        bet_type = "sanrenfuku" if row_type == "複" else "sanrentan"
        for m in re.finditer(r"(\d)[=\-](\d)[=\-](\d)", payout_str):
            car_a = int(m.group(1))
            car_b = int(m.group(2))
            car_c = int(m.group(3))
            amt   = _extract_payout_amount(payout_str, m.end())
            if amt and all(1 <= c <= 9 for c in [car_a, car_b, car_c]):
                results.append({
                    "race_id":    race_id,
                    "bet_type":   bet_type,
                    "car_no1":    car_a,
                    "car_no2":    car_b,
                    "car_no3":    car_c,
                    "payout":     amt,
                    "popularity": 0,
                })

    elif table_type == "ワイド":
        # '1=4430円1=2380円2=4800円' → car_a=1,car_b=4,430円 / car_a=1,car_b=2,380円 / ...
        for m in re.finditer(r"(\d)[=\-](\d)", payout_str):
            car_a = int(m.group(1))
            car_b = int(m.group(2))
            amt   = _extract_payout_amount(payout_str, m.end())
            if amt and 1 <= car_a <= 9 and 1 <= car_b <= 9:
                results.append({
                    "race_id":    race_id,
                    "bet_type":   "wide",
                    "car_no1":    car_a,
                    "car_no2":    car_b,
                    "car_no3":    None,
                    "payout":     amt,
                    "popularity": 0,
                })

    return results


def _extract_payout_amount(text: str, hint_pos: int) -> int | None:
    """文字列から '2,050円' のような金額を抽出（hint_pos 以降で最初に見つかるもの）"""
    m = re.search(r"([\d,]+)円", text[hint_pos:])
    if m:
        try:
            return int(m.group(1).replace(",", ""))
        except ValueError:
            pass
    return None


# ============================================================
# DB 読み込み（バックテスト用）
# ============================================================

def load_from_db(days: int = 730) -> tuple[list[dict], dict[str, list[dict]]]:
    """
    DB からレース + 選手結果 + ライン情報を読み込む。
    返り値: (rows, lines_by_race)
    """
    conn   = _db.get_connection()
    c      = _db.get_cursor(conn)
    cutoff = (date.today() - timedelta(days=days)).isoformat()

    c.execute(_db.sql("""
        SELECT
            r.race_id, r.date, r.venue, r.venue_code, r.race_no, r.race_name,
            r.grade, r.num_racers, r.bank_length,
            res.finish_pos, res.car_no, res.racer_no, res.racer_name,
            res.class_rank, res.prefecture, res.age, res.racing_style,
            res.gear_ratio, res.time_sec, res.odds, res.popularity,
            pt.payout AS tansho_payout,
            pf.payout AS fukusho_payout
        FROM races r
        JOIN results res ON r.race_id = res.race_id
        LEFT JOIN payouts pt
            ON pt.race_id = res.race_id
            AND pt.car_no1 = res.car_no
            AND pt.car_no2 IS NULL
            AND pt.bet_type = 'tansho'
        LEFT JOIN payouts pf
            ON pf.race_id = res.race_id
            AND pf.car_no1 = res.car_no
            AND pf.car_no2 IS NULL
            AND pf.bet_type = 'fukusho'
        WHERE r.date >= ?
        ORDER BY r.date, r.race_id, res.finish_pos
    """), (cutoff,))
    rows = [dict(row) for row in c.fetchall()]

    c.execute(_db.sql("""
        SELECT l.* FROM lines l
        JOIN races r ON l.race_id = r.race_id
        WHERE r.date >= ?
        ORDER BY l.race_id, l.line_no, l.position
    """), (cutoff,))
    lines_rows = [dict(row) for row in c.fetchall()]

    conn.close()

    lines_by_race: dict[str, list[dict]] = {}
    for ln in lines_rows:
        rid = ln["race_id"]
        lines_by_race.setdefault(rid, []).append(ln)

    return rows, lines_by_race


def load_payouts_from_db(days: int = 730) -> dict[str, list[dict]]:
    """レースIDをキーに払戻データを返す"""
    conn   = _db.get_connection()
    c      = _db.get_cursor(conn)
    cutoff = (date.today() - timedelta(days=days)).isoformat()

    c.execute(_db.sql("""
        SELECT p.race_id, p.bet_type, p.car_no1, p.car_no2, p.car_no3, p.payout
        FROM payouts p
        JOIN races r ON p.race_id = r.race_id
        WHERE r.date >= ?
    """), (cutoff,))
    rows = [dict(row) for row in c.fetchall()]
    conn.close()

    by_race: dict[str, list[dict]] = {}
    for r in rows:
        by_race.setdefault(r["race_id"], []).append(r)
    return by_race


# ============================================================
# chariloto 過去データ収集
# ============================================================

def _fetch_chariloto_venue_dates(venue_code: str) -> list[str]:
    """
    chariloto の会場ページから過去の開催日一覧を取得。
    venue_code は keirin.jp の naibuKeirinCd（内部は KEIRIN_TO_CHARILOTO で変換）。
    返り値: YYYYMMDD 形式のリスト（新しい順）
    """
    venue_name     = VENUE_MAP.get(venue_code, (None,))[0]
    chariloto_code = NAME_TO_CHARILOTO.get(venue_name, venue_code) if venue_name else venue_code
    url  = f"{CHARILOTO_URL}/keirin/results/{chariloto_code}"
    resp = _get(url)
    if not resp:
        return []
    soup  = BeautifulSoup(resp.text, "html.parser")
    dates = []
    for a in soup.find_all("a", href=True):
        m = re.match(r"/keirin/results/\d+/(\d{4})-(\d{2})-(\d{2})$", a["href"])
        if m:
            dates.append(f"{m.group(1)}{m.group(2)}{m.group(3)}")
    return sorted(set(dates), reverse=True)


def _save_chariloto_date(venue_code: str, date_str: str) -> int:
    """
    chariloto から取得した1日分のレースデータを DB に保存する。
    返り値: 保存したレース数
    """
    day_data = _fetch_chariloto_day(venue_code, date_str)
    if not day_data:
        return 0

    venue_name, bank = VENUE_MAP.get(venue_code, (venue_code, 400))
    saved = 0

    for race_no, race_data in day_data.items():
        race_id = _make_race_id(venue_code, date_str, race_no)
        # payouts が既に入っていればスキップ（races に存在するかは問わない）
        conn_chk = _db.get_connection()
        c_chk    = _db.get_cursor(conn_chk)
        c_chk.execute(_db.sql("SELECT 1 FROM payouts WHERE race_id=? LIMIT 1"), (race_id,))
        has_payouts = c_chk.fetchone() is not None
        conn_chk.close()
        if has_payouts:
            continue

        results       = race_data.get("results", [])
        narabi        = race_data.get("narabi", [])
        payouts       = race_data.get("payouts", [])
        class_by_car  = race_data.get("class_by_car", {})
        age_by_car    = race_data.get("age_by_car", {})
        name_by_car   = race_data.get("name_by_car", {})
        pref_by_car   = race_data.get("pref_by_car", {})
        racer_no_by_car = race_data.get("racer_no_by_car", {})

        if not results:
            continue

        # entries を chariloto の着順データから構築
        entries = []
        leader_cars = {ln["car_no"] for ln in narabi if ln.get("position") == 0}
        for r in results:
            car_no = r["car_no"]
            style  = "逃" if car_no in leader_cars else ""
            entries.append({
                "race_id":      race_id,
                "car_no":       car_no,
                "racer_no":     racer_no_by_car.get(car_no, ""),
                "racer_name":   name_by_car.get(car_no, r.get("racer_name", "")),
                "class_rank":   class_by_car.get(car_no, ""),
                "prefecture":   pref_by_car.get(car_no, ""),
                "age":          age_by_car.get(car_no, 0),
                "racing_style": style,
                "gear_ratio":   0.0,
                "odds":         0.0,
                "popularity":   0,
            })

        # narabi に racer_name を補完
        for ln in narabi:
            if not ln.get("racer_name"):
                ln["racer_name"] = name_by_car.get(ln["car_no"], "")

        if not narabi and entries:
            narabi = _infer_lines_from_style(entries, race_id)

        race_info = {
            "race_id":    race_id,
            "date":       f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}",
            "venue":      venue_name,
            "venue_code": venue_code,
            "race_no":    race_no,
            "race_name":  "",
            "grade":      "",
            "num_racers": len(entries),
            "bank_length": bank,
        }

        save_to_db(race_info, entries, narabi, results, payouts)
        logger.info(
            f"保存(chariloto): {race_id} {venue_name}R{race_no} "
            f"({len(entries)}車, narabi{len(narabi)}件, 払戻{len(payouts)}件)"
        )
        saved += 1

    return saved


# ============================================================
# メイン取得フロー
# ============================================================

def run_fetch(years: int = 2, specific_date: str | None = None, days: int | None = None):
    """
    データ取得のメインフロー。
    - 過去データ: chariloto.com の各会場ページから開催日リストを取得し結果を保存
    - 今日以降:   keirin.jp から出走表を取得して保存（picks 用の詳細情報）

    days: 指定するとその日数分だけ取得（例: days=7 で直近7日）。years より優先。
    years=0 は自動的に days=7 として扱う。
    """
    init_db()
    # days 指定 または years=0 → 直近N日分として扱う
    if days is not None:
        cutoff_str = (date.today() - timedelta(days=days)).strftime("%Y%m%d")
    elif years == 0:
        cutoff_str = (date.today() - timedelta(days=7)).strftime("%Y%m%d")
    else:
        cutoff_str = (date.today() - timedelta(days=365 * years)).strftime("%Y%m%d")
    total_saved = 0

    if specific_date:
        sd = date(int(specific_date[:4]), int(specific_date[4:6]), int(specific_date[6:]))
        if sd >= date.today():
            # 今日以降 → keirin.jp
            races = fetch_race_ids_for_date(sd)
            for vc, ds, rno in races:
                if _fetch_and_save(vc, ds, rno):
                    total_saved += 1
                time.sleep(2.0)
        else:
            # 過去 → chariloto（全会場試行）
            # keirin コード = chariloto コードなので NAME_TO_CHARILOTO の値でループ
            for venue_code in sorted(set(NAME_TO_CHARILOTO.values())):
                n = _save_chariloto_date(venue_code, specific_date)
                total_saved += n
                if n:
                    time.sleep(1.5)
        logger.info(f"完了: {total_saved}レース保存")
        return

    # 全期間: 各会場の過去開催日をcharilotoから取得
    # keirin コード = chariloto コードなので NAME_TO_CHARILOTO の値（chariloto コード）でループ
    logger.info(f"=== 全会場 過去データ取得（{years}年分、cutoff={cutoff_str}）===")
    for venue_code in sorted(set(NAME_TO_CHARILOTO.values())):
        venue_name = dict({v: k for k, v in NAME_TO_CHARILOTO.items()}).get(venue_code, venue_code)
        dates = _fetch_chariloto_venue_dates(venue_code)
        dates = [d for d in dates if d >= cutoff_str]
        logger.info(f"{venue_name} ({venue_code}): {len(dates)}開催日")

        for date_str in dates:
            n = _save_chariloto_date(venue_code, date_str)
            total_saved += n
            time.sleep(1.5)

    logger.info(f"過去データ取得完了: 計{total_saved}レース保存")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", type=int, default=2, help="取得年数")
    parser.add_argument("--date",  type=str, default=None, help="指定日 YYYYMMDD")
    args = parser.parse_args()
    run_fetch(years=args.years, specific_date=args.date)
