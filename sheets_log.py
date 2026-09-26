"""
把每天分析出來的「多重訊號／每日候選／每週候選」記錄到 Google 試算表，逐日累積成歷史紀錄
（不會覆蓋前一天，每天執行都是新增一批列）。只做記錄，不會下單，也不影響網頁本身。

用法（在 GitHub Actions 裡自動執行，一天只跑一次，見 daily.yml 的說明）：
  python sheets_log.py

需要兩個環境變數（在 GitHub repo 的 Settings > Secrets 設定，不要寫進程式）：
  GOOGLE_SHEETS_KEY   Google 服務帳號的 JSON 金鑰內容（整包貼進去）
  GOOGLE_SHEETS_ID    目標試算表網址中間那段 ID
沒設定的話會直接略過，不會讓更新失敗。設定步驟見 README「記錄到 Google 試算表」。
"""

import json
import os
import sys
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).parent

# 每日／每週候選的分頁欄位：直接對應 candidates_*.csv 的欄位，只是把「資料日期」搬到最前面
# 當作分頁共通的「日期」欄，Yahoo代號留著方便自己點開查股票
CANDIDATE_COLS = ["代號", "名稱", "型態", "側別", "收盤", "買進下限", "買進上限", "現價位置",
                  "停損價", "停利價", "風險%", "報酬%", "賺賠比", "RSI", "量比", "距52週高%",
                  "理由", "Yahoo代號"]

SHEET_HEADERS = {
    "多重訊號": ["日期", "代號", "名稱", "收盤", "漲跌%", "符合訊號數", "訊號內容"],
    "每日候選": ["日期"] + CANDIDATE_COLS,
    "每週候選": ["日期"] + CANDIDATE_COLS,
}


def multi_signal_rows(stocks):
    """算法跟 build_site.py 的 multi_signal_html()／notify.py 一致：同時符合 2 個（含）以上
    不同種類的右側買進訊號，且沒有「過熱，不追」（那種不列入「可買進」）。"""
    rows = []
    for s in stocks:
        right = [g for g in s["sigs"] if g["side"] == "右側"]
        kinds = {g["name"] for g in right}
        if len(kinds) < 2 or "過熱，不追" in s["tags"]:
            continue
        detail = "；".join(
            f"{g['name']}（{g['tf']}）買 {g['lo']:.2f}～{g['hi']:.2f} 停損 {g['stop']:.2f} 停利 {g['target']:.2f}"
            for g in right)
        rows.append([s["date"], s["code"], s["name"], s["close"], s["chg"], len(kinds), detail])
    return rows


def candidates_rows(csv_name):
    path = BASE_DIR / csv_name
    if not path.exists():
        return []
    df = pd.read_csv(path, dtype={"代號": str})
    if df.empty:
        return []
    for c in CANDIDATE_COLS:
        if c not in df.columns:
            df[c] = ""
    return [[row.get("資料日期", "")] + [row.get(c, "") for c in CANDIDATE_COLS]
            for _, row in df.iterrows()]


def connect():
    key_json = os.environ.get("GOOGLE_SHEETS_KEY")
    sheet_id = os.environ.get("GOOGLE_SHEETS_ID")
    if not key_json or not sheet_id:
        print("沒有設定 GOOGLE_SHEETS_KEY / GOOGLE_SHEETS_ID，略過記錄到 Google 試算表。")
        return None
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_info(
        json.loads(key_json), scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(creds).open_by_key(sheet_id)


def append_rows(sh, title, rows):
    if not rows:
        return 0
    import gspread
    try:
        ws = sh.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=1, cols=len(SHEET_HEADERS[title]))
        ws.append_row(SHEET_HEADERS[title])
    # 全部轉成字串：數字欄位混著理由這種長文字，讓試算表用字串顯示最不會出錯（要算的話自己在表上轉）
    ws.append_rows([[str(v) for v in row] for row in rows], value_input_option="USER_ENTERED")
    return len(rows)


def main():
    try:
        sh = connect()
    except Exception as e:
        print(f"[注意] 連線 Google 試算表失敗（{type(e).__name__}：{e}），略過這次記錄。")
        return 0   # 不該讓網頁發布跟著失敗
    if sh is None:
        return 0

    try:
        stocks = json.loads((BASE_DIR / "stocks.json").read_text(encoding="utf-8"))["stocks"]
    except Exception:
        stocks = []

    try:
        n1 = append_rows(sh, "多重訊號", multi_signal_rows(stocks))
        n2 = append_rows(sh, "每日候選", candidates_rows("candidates_daily.csv"))
        n3 = append_rows(sh, "每週候選", candidates_rows("candidates_weekly.csv"))
        print(f"已記錄到 Google 試算表：多重訊號 {n1} 列、每日候選 {n2} 列、每週候選 {n3} 列")
    except Exception as e:
        print(f"[注意] 寫入 Google 試算表失敗（{type(e).__name__}：{e}），略過這次記錄。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
