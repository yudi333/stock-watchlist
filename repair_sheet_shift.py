"""
一次性修正腳本：修正「每日候選」「每週候選」分頁的欄位位移問題。

背景：10/13 新增「信心」欄時，sheets_log.py 的 sync_rows_top() 只有在分頁「第一次建立」
才會寫入標題列，既有分頁（這兩個分頁早就建立過了）的標題列沒有跟著更新——導致 10/13
之後寫入的資料（已經是新格式，信心值插在「量比」後面）跟還停在舊版本的標題列對不上，
「距52週高%」「理由」「Yahoo代號」這三欄的資料全部位移、顯示在錯的標題底下。
sheets_log.py 的 ensure_header() 已經修好「標題列」本身，但這支腳本要處理的是：
標題列修好之後，**10/13 之前**寫入的舊資料（整列只有舊格式的欄位數，沒有信心這個值）
反而會變成「比新標題少一欄」，需要在「信心」的位置補一個空字串，把後面的資料推回
正確位置，才會重新對齊。10/13 之後寫入的資料本來就已經是新格式，不用動。

判斷一列是不是舊格式，不是用「欄位數量」（gspread 回傳的列，尾端空字串有沒有被補齊
不保證一致，用長度判斷不可靠；而且這幾個分頁的標題列後面可能還接著 backtest.py 自己
管理的「結果/結果日期/實際報酬%/持有天數」4 欄，長度會因列而異，用長度猜更不準），
改用「新格式裡 Yahoo代號 該在的固定位置，這一列那個位置現在是不是像股票代號」來判斷——
像代號格式，代表已經對齊，不用動；不像，才進一步確認是不是舊格式（少一欄）的典型樣子，
是的話才修正，兩種都不像的列不猜，直接列出來讓人自己檢查。

用法：
  python repair_sheet_shift.py          # 預覽模式，只印出會修改哪些列，不會真的寫入
  python repair_sheet_shift.py --apply  # 真的套用修正

需要環境變數（跟 sheets_log.py 共用）：GOOGLE_SHEETS_KEY / GOOGLE_SHEETS_ID
"""

import re
import sys

from sheets_log import SHEET_HEADERS, connect, ensure_header

TITLES = ["每日候選", "每週候選"]   # 多重訊號的「信心」加在最後面，沒有位移問題，不用修
YAHOO_RE = re.compile(r"^\d{4,6}[A-Z]?\.(TW|TWO)$")


def strip_trailing_empty(row):
    """去掉尾端的空字串——gspread 回傳的列，尾端空白有沒有被補齊到整張表的寬度不保證
    一致，先去掉尾端空白，拿到這一列「真正」寫了多少東西，判斷起來才準。"""
    r = list(row)
    while r and r[-1] == "":
        r.pop()
    return r


def plan_fixes(canonical_header, values):
    """算出哪些列需要修正，回傳 [(列號（1-based，對應試算表實際列數）, 修正後的內容或 None, 原始內容)]。
    不碰 Google 試算表，純粹算而已，方便先用假資料測試這段邏輯對不對。

    canonical_header 一定要傳 SHEET_HEADERS[title]（sheets_log.py 自己管的那 20 欄），
    不能傳從試算表讀回來的完整標題列——這幾個分頁的標題列後面可能還接著 backtest.py
    自己管理、附加在後面的「結果/結果日期/實際報酬%/持有天數」，不在這支腳本要處理的
    範圍內，用完整標題列的長度去定位 Yahoo代號 的位置會抓到結果欄那邊去，判斷整批都會
    錯（第一版就是犯了這個錯，拿接近真實的假資料測才抓到）。

    判斷依據：直接檢查「新格式裡 Yahoo代號 該在的位置」（conf_idx+3，信心 已經在正確
    位置時 Yahoo代號 緊接在距52週高%/理由後面）這一列那個位置有沒有放著看起來像代號的
    內容——有，代表已經是新格式、對齊了，不用動（不管後面接不接 backtest 的結果欄，
    這個位置都不受影響）。不是的話，改檢查「舊格式裡 Yahoo代號 該在的位置」（conf_idx+2，
    少了信心那一欄）——是，代表確認是舊格式，要在 conf_idx 插入空字串修正；兩個位置
    都不像代號格式，代表這列本身可能有別的問題，列出來讓人自己檢查，不要用猜的。"""
    conf_idx = canonical_header.index("信心")
    yahoo_pos_new = conf_idx + 3   # 新格式（含信心）：...,信心,距52週高%,理由,Yahoo代號
    yahoo_pos_old = conf_idx + 2   # 舊格式（不含信心）：...,距52週高%,理由,Yahoo代號
    fixes = []
    for i, row in enumerate(values[1:], start=2):   # 從第 2 列開始（跳過標題），i = 試算表實際列號
        real = strip_trailing_empty(row)
        if not real:
            continue   # 整列都是空的，跳過
        at_new = real[yahoo_pos_new].strip() if yahoo_pos_new < len(real) else ""
        if YAHOO_RE.match(at_new):
            continue   # 新格式該有 Yahoo代號 的位置，現在就有有效的代號格式，已經對齊，不用動
        at_old = real[yahoo_pos_old].strip() if yahoo_pos_old < len(real) else ""
        if not YAHOO_RE.match(at_old):
            # 兩個位置都抓不到像代號的內容，不是這支腳本處理得了的典型位移問題，列出來讓人自己看
            fixes.append((i, None, real))
            continue
        fixed = real[:conf_idx] + [""] + real[conf_idx:]
        fixes.append((i, fixed, real))
    return fixes


def main():
    apply = "--apply" in sys.argv
    sh = connect()
    if sh is None:
        return 1
    import gspread
    for title in TITLES:
        try:
            ws = sh.worksheet(title)
        except gspread.WorksheetNotFound:
            print(f"{title}：分頁不存在，跳過。")
            continue

        want = SHEET_HEADERS[title]
        header = ws.row_values(1)
        if header[:len(want)] != want:
            print(f"{title}：標題列還不是最新版本，先更新標題列。")
            ensure_header(ws, title)

        values = ws.get_all_values()
        fixes = plan_fixes(want, values)   # 一定傳 SHEET_HEADERS 的版本，不是讀回來的完整標題列（可能還接著結果欄）
        need_manual = [f for f in fixes if f[1] is None]
        need_fix = [f for f in fixes if f[1] is not None]

        print(f"\n=== {title}：共 {len(values) - 1} 列資料 ===")
        print(f"需要修正（補上信心欄位）：{len(need_fix)} 列")
        print(f"欄位數量異常、需要自己手動檢查：{len(need_manual)} 列")
        for i, _, real in need_manual[:10]:
            print(f"  第 {i} 列（欄位數 {len(real)}）：{real}")

        if need_fix:
            i0, fixed0, real0 = need_fix[0]
            print(f"範例（第 {i0} 列）：")
            print(f"  修正前：{real0}")
            print(f"  修正後：{fixed0}")

        if need_fix and apply:
            # 一列一列分開呼叫 API 會撞到 Google 試算表「每分鐘最多幾十次寫入」的速率限制
            # （實測撞到過：128+133 列，一列一次 update() 跑到一半就被 429 擋下來）。
            # 改成全部包進同一次 batch_update()，不管有幾百列都只算一次 API 呼叫。
            data = [{"range": f"A{i}:{gspread.utils.rowcol_to_a1(i, len(fixed))}", "values": [fixed]}
                    for i, fixed, _ in need_fix]
            ws.batch_update(data, value_input_option="USER_ENTERED")
            print(f"{title}：已修正 {len(need_fix)} 列。")
        elif need_fix:
            print(f"{title}：預覽模式，沒有真的寫入。加 --apply 參數才會套用。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
