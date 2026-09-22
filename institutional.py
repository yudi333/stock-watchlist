"""
三大法人（外資、投信、自營商）買賣超：只當候選股與持股的「加分項」輔助資訊，
不影響任何進出場邏輯、買進區間、停損停利。

資料來源：證交所（上市）、櫃買中心（上櫃）官方查詢介面——不是正式公開文件的 API，是網站
本身在用的查詢端點，跟 screener.py 用的 openapi.twse.com.tw 同屬官方資料，風險一樣低，
但格式仍可能未來改版。抓不到就安全地不顯示這項資訊，不影響其他任何功能。

用法：
    today = fetch_today("2026-09-22")                       # 抓當天三大法人買賣超
    rolling = update_rolling(load_rolling(), "2026-09-22", today)   # 併入滾動紀錄
    notes = compute_notes(rolling, "2026-09-22")             # {代號: "三大法人連買超 5 天"}
"""

import time

import requests

BASE_URL = "https://yudi333.github.io/stock-watchlist"
KEEP_DAYS = 10     # 保留最近幾個交易日，用來算連續買賣超天數（比網頁的 7 天歷史回看多一點緩衝）
STREAK_MIN = 3     # 連續幾天以上才提示，避免天天都顯示、資訊過多反而沒用
HEADERS = {"User-Agent": "Mozilla/5.0"}


def _to_int(s):
    try:
        return int(str(s).replace(",", ""))
    except ValueError:
        return None


def fetch_today(date):
    """date：yyyy-mm-dd。回傳 {代號: 三大法人買賣超股數}（正 = 買超，負 = 賣超）。抓不到就回傳 {}。"""
    y, m, d = date.split("-")
    out = {}
    try:  # 上市（證交所）
        r = requests.get("https://www.twse.com.tw/rwd/zh/fund/T86",
                         params={"date": f"{y}{m}{d}", "selectType": "ALL", "response": "json"},
                         headers=HEADERS, timeout=20)
        j = r.json()
        if j.get("stat") == "OK":
            for row in j["data"]:
                code, v = row[0].strip(), _to_int(row[-1])
                if code.isdigit() and len(code) == 4 and v is not None:
                    out[code] = v
    except Exception as e:
        print(f"[注意] 上市三大法人資料抓取失敗：{e}")
    try:  # 上櫃（櫃買中心）
        r = requests.get("https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade",
                         params={"date": f"{y}/{m}/{d}", "type": "Daily", "response": "json"},
                         headers=HEADERS, timeout=20)
        j = r.json()
        for row in j["tables"][0]["data"]:
            code, v = row[0].strip(), _to_int(row[-1])
            if code.isdigit() and len(code) == 4 and v is not None:
                out[code] = v
    except Exception as e:
        print(f"[注意] 上櫃三大法人資料抓取失敗：{e}")
    return out


def load_rolling():
    """讀線上目前的滾動紀錄；抓不到就當作「還沒有」，不會讓整個更新失敗（這只是加分項）。"""
    try:
        r = requests.get(f"{BASE_URL}/inst_history.json?t={int(time.time())}", headers=HEADERS, timeout=20)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {"days": {}}


def update_rolling(rolling, date, today):
    """把今天併入，只保留最近 KEEP_DAYS 個交易日（滾動式，同 history.py 的做法）。"""
    days = dict(rolling.get("days", {}))
    if today:
        days[date] = today
    keep = sorted(days)[-KEEP_DAYS:]
    return {"days": {d: days[d] for d in keep}}


def compute_notes(rolling, today_date):
    """回傳 {代號: 提示文字}：只列出連續買超或賣超達 STREAK_MIN 天（含今天）以上的股票。"""
    days = sorted(d for d in rolling.get("days", {}) if d <= today_date)
    codes = {c for d in days for c in rolling["days"][d]}
    notes = {}
    for code in codes:
        streak, direction = 0, None
        for d in reversed(days):
            v = rolling["days"][d].get(code)
            if not v:                          # 沒資料或剛好買賣平衡，streak 中斷
                break
            sign = "buy" if v > 0 else "sell"
            if direction is None:
                direction = sign
            elif sign != direction:
                break
            streak += 1
        if streak >= STREAK_MIN:
            notes[code] = f"三大法人連{'買' if direction == 'buy' else '賣'}超 {streak} 天"
    return notes
