"""
把每天分析出來的「多重訊號／每日候選／每週候選」記錄到 Google 試算表，逐日累積成歷史紀錄
（不會覆蓋前一天，每天執行都是插入新的一批列，最新日期永遠插在標題列正下方、日期新的在上面）。
欄位盡量拆成數字（買進區間、停損、停利、風險%、報酬%、賺賠比都是獨立欄位），方便之後拿來
回測——用公式篩同一代號、比對訊號出現後的實際走勢。只做記錄，不會下單，也不影響網頁本身。

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
import traceback
from pathlib import Path

import pandas as pd

from build_site import POS_SHORT   # {"in":"區間內","above":"高於區間","below":"低於區間"}，跟網頁共用同一份對照表

BASE_DIR = Path(__file__).parent

# 每日／每週候選的分頁欄位：直接對應 candidates_*.csv 的欄位，只是把「資料日期」搬到最前面
# 當作分頁共通的「日期」欄，Yahoo代號留著方便自己點開查股票
CANDIDATE_COLS = ["代號", "名稱", "型態", "側別", "收盤", "買進下限", "買進上限", "現價位置",
                  "停損價", "停利價", "風險%", "報酬%", "賺賠比", "RSI", "量比", "距52週高%",
                  "理由", "Yahoo代號"]

# 多重訊號分頁：拆成「一檔股票、一個符合的訊號」一列（不是一檔股票塞一整包文字），
# 買進區間／停損／停利／風險％／報酬％／賺賠比都是獨立的數字欄位，格式盡量對齊每日／每週候選，
# 方便之後回測——例如可以直接用公式篩「同一代號」比較多次出現的訊號、拿收盤價序列回頭比對
# 買進區間有沒有觸及、停損/停利哪個先發生。
SHEET_HEADERS = {
    "多重訊號": ["日期", "代號", "名稱", "收盤", "漲跌%", "符合訊號數", "訊號", "時間框",
               "現價位置", "買進下限", "買進上限", "停損價", "停利價", "風險%", "報酬%", "賺賠比"],
    "每日候選": ["日期"] + CANDIDATE_COLS,
    "每週候選": ["日期"] + CANDIDATE_COLS,
}


def multi_signal_rows(stocks):
    """算法跟 build_site.py 的 multi_signal_html()／notify.py 一致：同時符合 2 個（含）以上
    不同種類的右側買進訊號，且沒有「過熱，不追」（那種不列入「可買進」）；一個訊號一列。"""
    rows = []
    for s in stocks:
        right = [g for g in s["sigs"] if g["side"] == "右側"]
        kinds = {g["name"] for g in right}
        if len(kinds) < 2 or "過熱，不追" in s["tags"]:
            continue
        for g in right:
            rows.append([s["date"], s["code"], s["name"], s["close"], s["chg"], len(kinds),
                         g["name"], g["tf"], POS_SHORT.get(g["pos"], g["pos"]), g["lo"], g["hi"],
                         g["stop"], g["target"], g["risk"], g["gain"], g["rr"]])
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


# 交錯底色，讓連續插入的每一批（＝每一個有記錄的日子）一眼就看得出分界在哪裡：
# 白色／Google 試算表調色盤內建的「淺灰色 1」，兩者輪流。
FILL_WHITE = {"red": 1, "green": 1, "blue": 1}
FILL_GRAY = {"red": 217 / 255, "green": 217 / 255, "blue": 217 / 255}
META_SHEET = "_設定"      # 記著「每個分頁上次用了哪個顏色」的隱藏用分頁，不是資料


def next_fill_color(sh, title):
    """跟這個分頁上次記錄的顏色相反：讀 _設定 分頁記著的上次顏色，這次換另一種，並把新的
    存回去。三個分頁（多重訊號／每日候選／每週候選）分開各記各的——不能共用同一個顏色狀態，
    不然某天剛好某個分頁沒有候選股（0 列，沒真的插入），沒資料卻也算跳過一輪顏色，會害那個
    分頁下一次真正有資料時顏色接不起來（跟它自己前一批緊鄰卻同色）。用分頁名稱找 _設定裡
    對應的那一列，找不到就當作還沒記錄過、從白色開始。"""
    import gspread
    try:
        meta = sh.worksheet(META_SHEET)
    except gspread.WorksheetNotFound:
        meta = sh.add_worksheet(title=META_SHEET, rows=10, cols=2)
        meta.append_row(["分頁", "上次記錄用的底色（不要刪除這個分頁）"])
    cell = meta.find(title, in_column=1)   # 找不到會回傳 None（不是丟例外）
    last = meta.cell(cell.row, 2).value if cell else None
    this_time, next_time = (FILL_GRAY, "gray") if last == "white" else (FILL_WHITE, "white")
    if cell:
        meta.update_cell(cell.row, 2, next_time)
    else:
        meta.append_row([title, next_time])
    return this_time


def insert_rows_top(sh, title, rows):
    """插入在標題列（第 1 列）正下方，日期新的一直往上疊，最新一批永遠在最上面，
    並把這批（今天）的範圍整個上色，跟這個分頁上一批（前一個記錄的日子）的顏色不同。"""
    if not rows:
        return 0
    import gspread
    fill_color = next_fill_color(sh, title)
    try:
        ws = sh.worksheet(title)
    except gspread.WorksheetNotFound:
        # rows 不能給 1：insert_rows 是「插入在第 2 列」，如果整張表格只有 1 列（剛好等於
        # 標題列），插入位置就已經超出格線範圍，Google 那邊會回 400 錯誤。給一般試算表
        # 常見的預設大小 1000，之後累積再多年的資料也還早才會用完。
        ws = sh.add_worksheet(title=title, rows=1000, cols=len(SHEET_HEADERS[title]))
        ws.append_row(SHEET_HEADERS[title])
    # 全部轉成字串：數字欄位混著理由這種長文字，讓試算表用字串顯示最不會出錯（要算的話自己在表上轉）
    ws.insert_rows([[str(v) for v in row] for row in rows], row=2, value_input_option="USER_ENTERED")
    last_row = 1 + len(rows)
    rng = f"{gspread.utils.rowcol_to_a1(2, 1)}:{gspread.utils.rowcol_to_a1(last_row, len(SHEET_HEADERS[title]))}"
    ws.format(rng, {"backgroundColor": fill_color})
    return len(rows)


def main():
    # 回傳值特別分開：0 只代表「不用記（沒設定）或真的記成功了」，1 代表「有設定但失敗了」。
    # daily.yml 是看這個回傳值決定要不要把「今天已記錄」的標記寫進快取——失敗的話回傳 1，
    # 不寫標記，今天稍後的執行才會再重試；不然標記一旦寫下去，今天就再也不會重試了。
    try:
        sh = connect()
    except Exception as e:
        # {e} 有時候是空字串（某些例外不會帶訊息，例如底層函式庫丟出的 PermissionError），
        # 只印類型名稱看不出真正原因，所以連完整 traceback 一起印出來，方便到 Actions
        # 的執行紀錄裡直接看到是哪一行、哪個函式庫丟出來的
        print(f"[注意] 連線 Google 試算表失敗（{type(e).__name__}：{e}），略過這次記錄。完整錯誤：")
        traceback.print_exc()
        return 1
    if sh is None:
        return 0   # 沒設定 GOOGLE_SHEETS_KEY/ID，本來就不用記，不算失敗

    try:
        stocks = json.loads((BASE_DIR / "stocks.json").read_text(encoding="utf-8"))["stocks"]
    except Exception:
        stocks = []

    try:
        n1 = insert_rows_top(sh, "多重訊號", multi_signal_rows(stocks))
        n2 = insert_rows_top(sh, "每日候選", candidates_rows("candidates_daily.csv"))
        n3 = insert_rows_top(sh, "每週候選", candidates_rows("candidates_weekly.csv"))
        print(f"已記錄到 Google 試算表：多重訊號 {n1} 列、每日候選 {n2} 列、每週候選 {n3} 列")
    except Exception as e:
        print(f"[注意] 寫入 Google 試算表失敗（{type(e).__name__}：{e}），略過這次記錄。完整錯誤：")
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
