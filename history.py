"""
「當時公告過的結果」的滾動紀錄。

網站是靜態的，本身沒有記憶。做法：每次更新網頁時，先從「線上上一版網頁」讀取 history.json，
把今天的分析結果加進去，只保留最近 KEEP_DAYS 個交易日，再跟網頁一起發布。
這樣不需要寫入 repo，也不需要額外權限。

- 有記錄的日子：網頁顯示「當時記錄」（當天真的算出來的結果，之後改規則也不會變）
- 還沒有記錄的日子（剛開始的前幾天）：用重算補上，網頁顯示「重算」

安全：如果讀取線上紀錄「失敗」（不是單純還沒有），就中止更新，寧可保留舊網頁，
也不要用「只有今天」的版本蓋掉已累積的紀錄。
"""

import json
import os
import time
import urllib.error
import urllib.request

KEEP_DAYS = 7    # 保留最近幾個交易日（含今天）；超過的最舊一天會被丟掉 = 滾動式更新
DEFAULT_URL = "https://yudi333.github.io/stock-watchlist/history.json"


def load_previous():
    """讀線上上一版的紀錄。404 = 第一次還沒有紀錄；其他錯誤重試後丟出 RuntimeError。"""
    if os.environ.get("HISTORY_RESET") == "1":        # 手動重置用：明確要求從零開始
        print("[注意] HISTORY_RESET=1：忽略舊紀錄，從零開始記錄。")
        return {"days": {}}
    url = os.environ.get("HISTORY_URL", DEFAULT_URL)
    last = None
    for wait in (0, 3, 8):
        if wait:
            time.sleep(wait)
        try:
            # 加時間參數避免讀到 CDN 的舊快取
            req = urllib.request.Request(f"{url}?t={int(time.time())}", headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                print("線上還沒有 history.json，從今天開始記錄。")
                return {"days": {}}
            last = e
        except Exception as e:
            last = e
    raise RuntimeError(f"讀取線上紀錄失敗（{type(last).__name__}），為避免紀錄被覆蓋，中止這次更新。")


def compact(rec):
    """一檔股票「當天」要記錄的精簡內容（和網頁展開列用的格式相同）。"""
    return dict(c=rec["close"], g=rec["chg"], v=rec["vs60"], r=rec["rsi"], l=rec["level"], t=rec["tags"],
                s=[[g["name"], g["side"], g["tf"], g["lo"], g["hi"], g["stop"], g["target"]] for g in rec["sigs"]])


def update(prev, stocks):
    """把今天（資料日期）的結果加進去，只留最近 KEEP_DAYS 個交易日。"""
    days = dict(prev.get("days", {}))
    if stocks["stocks"] and stocks["date"] not in ("", "-"):
        days[stocks["date"]] = {s["code"]: compact(s) for s in stocks["stocks"]}   # 同一天重跑 = 覆蓋
    keep = sorted(days)[-KEEP_DAYS:]
    return {"days": {d: days[d] for d in keep}}


def apply(stocks, hist_file):
    """把「當時記錄」套到每檔股票的前幾天；沒有記錄的日子保留重算結果。標記來源 src。"""
    days, today = hist_file["days"], stocks["date"]
    for s in stocks["stocks"]:
        for h in s.get("hist", []):
            rec = days.get(h["d"], {}).get(s["code"])
            if rec and h["d"] != today:
                h.update(rec)
                h["src"] = "rec"
            else:
                h["src"] = "calc"
