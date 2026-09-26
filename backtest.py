"""
回頭檢查 Google 試算表裡記錄過的候選股／多重訊號，後來的股價有沒有觸發停損或停利，
把結果直接補寫回原本那一列（不是另外開新分頁）：結果、結果日期、實際報酬%、持有天數。
只统计已經記錄過的建議後來的走勢，不會下單，也不影響網頁本身。

判斷方式：從訊號那天開始往後看每天的最高/最低價——
  還沒進場（最低價沒跌到買進上限之下、或最高價沒漲到買進下限之上）：標記「尚未進場」
  進場後最低價 <= 停損價：「已停損」（當天最高價同時也 >= 停利價的話，保守當作先停損）
  進場後最高價 >= 停利價：「已停利」
  超過追蹤期限還沒發生：「過期未觸發」／「過期未進場」
  以上都還沒發生：「追蹤中」，下次繼續看
已經有終局結果（已停利/已停損/過期）的列不會再重查，只處理還在追蹤或還沒進場的列，
所以就算歷史紀錄累積很多筆，每次真正要重新抓資料的也只有一小部分。

用法（在 GitHub Actions 裡自動執行，見 daily.yml）：
  python backtest.py

需要環境變數（跟 sheets_log.py 共用同一組）：
  GOOGLE_SHEETS_KEY / GOOGLE_SHEETS_ID
沒設定的話會直接略過，不會讓更新失敗。
"""

import json
import os
import sys
import traceback
from datetime import datetime

import pandas as pd

from stock_checker import fetch_history

RESULT_COLS = ["結果", "結果日期", "實際報酬%", "持有天數"]
# 追蹤期限（交易日）：超過這麼多天還沒進場/沒觸發停損停利，就不再繼續無限期等下去
EXPIRE_DAYS = {"多重訊號": 60, "每日候選": 60, "每週候選": 120}
OPEN_STATUSES = {"", "追蹤中", "尚未進場"}   # 這些狀態的列才需要重新檢查


def connect():
    key_json = os.environ.get("GOOGLE_SHEETS_KEY")
    sheet_id = os.environ.get("GOOGLE_SHEETS_ID")
    if not key_json or not sheet_id:
        print("沒有設定 GOOGLE_SHEETS_KEY / GOOGLE_SHEETS_ID，略過回測。")
        return None
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_info(
        json.loads(key_json), scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(creds).open_by_key(sheet_id)


def ensure_result_cols(ws, header):
    """確保標題列有「結果／結果日期／實際報酬%／持有天數」這 4 欄，沒有就補在最後面。
    回傳補完後的標題列跟各結果欄的欄位編號（1-based）。"""
    changed = False
    for c in RESULT_COLS:
        if c not in header:
            header = header + [c]
            changed = True
    if changed:
        ws.update([header], "1:1")
    return header, {c: header.index(c) + 1 for c in RESULT_COLS}


def evaluate(df, signal_date, buy_lo, buy_hi, stop, target, expire_days):
    """回傳 (結果, 結果日期, 實際報酬%, 持有天數)；資料不足（例如剛下市查不到）回傳 None，
    這種情況維持原狀，不覆蓋，下次還可以再試。"""
    try:
        sub = df[df.index >= pd.Timestamp(signal_date)]
    except Exception:
        return None
    if sub.empty:
        return None
    mid = (buy_lo + buy_hi) / 2   # 沒有實際成交紀錄，用買進區間中點當估計進場價算報酬%
    entered, entry_idx = False, None
    for i in range(len(sub)):
        row = sub.iloc[i]
        if not entered and row["Low"] <= buy_hi and row["High"] >= buy_lo:
            entered, entry_idx = True, i
        if entered:
            held = i - entry_idx
            if row["Low"] <= stop:
                return "已停損", sub.index[i].strftime("%Y-%m-%d"), round(stop / mid * 100 - 100, 1), held
            if row["High"] >= target:
                return "已停利", sub.index[i].strftime("%Y-%m-%d"), round(target / mid * 100 - 100, 1), held
            if held >= expire_days:
                return ("過期未觸發", sub.index[i].strftime("%Y-%m-%d"),
                        round(row["Close"] / mid * 100 - 100, 1), held)
    if entered:
        held = len(sub) - 1 - entry_idx
        return "追蹤中", "", round(sub.iloc[-1]["Close"] / mid * 100 - 100, 1), held
    days_since = len(sub) - 1
    if days_since >= expire_days:
        return "過期未進場", "", None, days_since
    return "尚未進場", "", None, days_since


def backtest_sheet(sh, title, code_col, date_col):
    """處理一個分頁，回傳更新了幾列。code_col/date_col 是「代號」「日期」欄的名字。"""
    import gspread
    try:
        ws = sh.worksheet(title)
    except gspread.WorksheetNotFound:
        return 0
    values = ws.get_all_values()
    if len(values) < 2:
        return 0
    header, result_col_idx = ensure_result_cols(ws, values[0])
    idx = {name: header.index(name) for name in
           [code_col, date_col, "買進下限", "買進上限", "停損價", "停利價"] if name in header}
    needed = [code_col, date_col, "買進下限", "買進上限", "停損價", "停利價"]
    if any(n not in idx for n in needed):
        print(f"[注意] {title} 缺少必要欄位（{[n for n in needed if n not in idx]}），略過這個分頁。")
        return 0

    expire_days = EXPIRE_DAYS[title]
    cache = {}          # 代號 -> DataFrame，同一檔股票在同一次執行裡只抓一次
    updates = []         # gspread batch_update 用
    checked, changed = 0, 0
    for r in range(1, len(values)):
        row = values[r] + [""] * (len(header) - len(values[r]))   # 舊資料列可能還沒有結果欄，補空字串對齊
        status = row[result_col_idx["結果"] - 1]
        if status not in OPEN_STATUSES:
            continue   # 已經有終局結果，不用再查
        code = row[idx[code_col]]
        try:
            buy_lo, buy_hi = float(row[idx["買進下限"]]), float(row[idx["買進上限"]])
            stop, target = float(row[idx["停損價"]]), float(row[idx["停利價"]])
        except (ValueError, IndexError):
            continue   # 這列資料格式怪怪的（例如空白），跳過
        checked += 1
        if code not in cache:
            try:
                cache[code], _ = fetch_history(code)
            except Exception:
                cache[code] = None
        df = cache[code]
        if df is None:
            continue
        result = evaluate(df, row[idx[date_col]], buy_lo, buy_hi, stop, target, expire_days)
        if result is None:
            continue
        新結果, 結果日期, 報酬, 天數 = result
        # 「追蹤中」「尚未進場」這種還沒定案的狀態，就算結果分類沒變，報酬%/持有天數每天都會
        # 往前走，所以不做「跟上次一樣就跳過」的最佳化，每次查到都直接寫回去。
        changed += 1
        new_row = list(row)
        new_row[result_col_idx["結果"] - 1] = 新結果
        new_row[result_col_idx["結果日期"] - 1] = 結果日期
        new_row[result_col_idx["實際報酬%"] - 1] = "" if 報酬 is None else 報酬
        new_row[result_col_idx["持有天數"] - 1] = 天數
        lo = min(result_col_idx.values())
        updates.append({"range": f"{gspread.utils.rowcol_to_a1(r + 1, lo)}:"
                                  f"{gspread.utils.rowcol_to_a1(r + 1, len(header))}",
                         "values": [new_row[lo - 1:]]})
    if updates:
        ws.batch_update(updates, value_input_option="USER_ENTERED")
    print(f"{title}：檢查 {checked} 列，更新 {changed} 列")
    return changed


def main():
    try:
        sh = connect()
    except Exception as e:
        print(f"[注意] 連線 Google 試算表失敗（{type(e).__name__}：{e}），略過這次回測。完整錯誤：")
        traceback.print_exc()
        return 1
    if sh is None:
        return 0

    try:
        n1 = backtest_sheet(sh, "多重訊號", "代號", "日期")
        n2 = backtest_sheet(sh, "每日候選", "代號", "日期")
        n3 = backtest_sheet(sh, "每週候選", "代號", "日期")
        print(f"回測完成：多重訊號更新 {n1} 列、每日候選更新 {n2} 列、每週候選更新 {n3} 列")
    except Exception as e:
        print(f"[注意] 回測失敗（{type(e).__name__}：{e}），略過這次。完整錯誤：")
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
