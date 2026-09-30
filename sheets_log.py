"""
把每天分析出來的「多重訊號／每日候選／每週候選」記錄到 Google 試算表，逐日累積成歷史紀錄
（不會覆蓋前一天，每天新的一批插在標題列正下方、日期新的在上面）。daily-update 一天會跑
3 次，同一天如果重複執行，會把當天稍早記錄的那一批刪掉、換成這次（更新）的結果，讓試算表
跟網站顯示的內容一致，不是維持當天第一次、可能已經過時的那份。
欄位盡量拆成數字（買進區間、停損、停利、風險%、報酬%、賺賠比都是獨立欄位），方便之後拿來
回測——用公式篩同一代號、比對訊號出現後的實際走勢。只做記錄，不會下單，也不影響網頁本身。

用法（在 GitHub Actions 裡自動執行，daily-update 每次跑都會執行，見 daily.yml 的說明）：
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


def _color_for(name):
    return FILL_GRAY if name == "gray" else FILL_WHITE


def _meta_ws(sh):
    import gspread
    try:
        return sh.worksheet(META_SHEET)
    except gspread.WorksheetNotFound:
        meta = sh.add_worksheet(title=META_SHEET, rows=10, cols=2)
        meta.append_row(["分頁", "上次記錄用的底色（不要刪除這個分頁）"])
        return meta


def current_fill_color(sh, title):
    """讀這個分頁目前記著、正在使用中的顏色，不會去改它——同一天內重複寫入（一天跑 3 次，
    把今天那批刪掉重寫成最新結果，見 sync_rows_top）用的還是同一個顏色，不算真的換了一天。
    找不到紀錄就當白色。"""
    meta = _meta_ws(sh)
    cell = meta.find(title, in_column=1)   # 找不到會回傳 None（不是丟例外）
    last = meta.cell(cell.row, 2).value if cell else None
    return _color_for(last)


def next_fill_color(sh, title):
    """跟這個分頁上次記錄的顏色相反，並把新的存回去——只有真的換了新的一天才呼叫這個。
    三個分頁（多重訊號／每日候選／每週候選）分開各記各的——不能共用同一個顏色狀態，不然
    某天剛好某個分頁沒有候選股（0 列，沒真的插入），沒資料卻也算跳過一輪顏色，會害那個
    分頁下一次真正有資料時顏色接不起來（跟它自己前一批緊鄰卻同色）。"""
    meta = _meta_ws(sh)
    cell = meta.find(title, in_column=1)
    last = meta.cell(cell.row, 2).value if cell else None
    this_name = "white" if last != "white" else "gray"
    if cell:
        meta.update_cell(cell.row, 2, this_name)
    else:
        meta.append_row([title, this_name])
    return _color_for(this_name)


def sync_rows_top(sh, title, rows):
    """插入在標題列（第 1 列）正下方，日期新的一直往上疊，最新一批永遠在最上面。

    daily-update 一天會跑 3 次（15:07/16:10/18:00），如果那天 Yahoo 資料中途有修正，
    3 次分析出來的候選股可能不一樣——網站顯示的永遠是「最後一次」的結果，所以試算表
    也要跟著看齊：如果最上面那一批的日期就是今天，代表今天稍早已經記過一次了，先把那
    一批刪掉，再插入這次（更新、更完整）的結果，而不是维持當天第一次跑到、可能已經過時
    的那份。真的是新的一天才會換交錯的底色；同一天內重寫不算換了一天，顏色不變。"""
    if not rows:
        return 0
    import gspread
    try:
        ws = sh.worksheet(title)
    except gspread.WorksheetNotFound:
        # rows 不能給 1：insert_rows 是「插入在第 2 列」，如果整張表格只有 1 列（剛好等於
        # 標題列），插入位置就已經超出格線範圍，Google 那邊會回 400 錯誤。給一般試算表
        # 常見的預設大小 1000，之後累積再多年的資料也還早才會用完。
        ws = sh.add_worksheet(title=title, rows=1000, cols=len(SHEET_HEADERS[title]))
        ws.append_row(SHEET_HEADERS[title])

    today = rows[0][0]   # 這批資料的日期（每列第一欄），假設同一批都是同一天
    existing = ws.get_all_values()
    same_day_count = 0
    for r in existing[1:]:
        if r and r[0] == today:
            same_day_count += 1
        else:
            break
    if same_day_count:
        fill_color = current_fill_color(sh, title)   # 沿用今天稍早已經決定的顏色，不要再換
        ws.delete_rows(2, 1 + same_day_count)
    else:
        fill_color = next_fill_color(sh, title)      # 真的是新的一天，才换顏色

    # 全部轉成字串：數字欄位混著理由這種長文字，讓試算表用字串顯示最不會出錯（要算的話自己在表上轉）
    ws.insert_rows([[str(v) for v in row] for row in rows], row=2, value_input_option="USER_ENTERED")
    last_row = 1 + len(rows)
    rng = f"{gspread.utils.rowcol_to_a1(2, 1)}:{gspread.utils.rowcol_to_a1(last_row, len(SHEET_HEADERS[title]))}"
    ws.format(rng, {"backgroundColor": fill_color})
    return len(rows)


def main():
    # 回傳值：0 代表「不用記（沒設定）或真的記成功了」，1 代表「有設定但失敗了」——單純讓
    # Actions 執行紀錄裡看得出這個步驟是真的成功還是失敗（daily.yml 有 continue-on-error，
    # 這裡失敗不會讓網頁發布跟著失敗，但步驟本身會標紅方便你發現）。
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
        n1 = sync_rows_top(sh, "多重訊號", multi_signal_rows(stocks))
        n2 = sync_rows_top(sh, "每日候選", candidates_rows("candidates_daily.csv"))
        n3 = sync_rows_top(sh, "每週候選", candidates_rows("candidates_weekly.csv"))
        print(f"已記錄到 Google 試算表：多重訊號 {n1} 列、每日候選 {n2} 列、每週候選 {n3} 列")
    except Exception as e:
        print(f"[注意] 寫入 Google 試算表失敗（{type(e).__name__}：{e}），略過這次記錄。完整錯誤：")
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
