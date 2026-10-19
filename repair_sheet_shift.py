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
不保證一致，用長度判斷不可靠），改用「Yahoo代號」那一欄現在實際顯示的內容是不是像
股票代號（例如 2330.TW）來判斷——像代號格式，代表這列已經對齊，不用動；不像，代表
這列目前錯位（代號欄現在顯示的其實是理由的文字，或根本是空的），需要修正。

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


def plan_fixes(header, values):
    """算出哪些列需要修正，回傳 [(列號（1-based，對應試算表實際列數）, 修正後的完整列內容)]。
    不碰 Google 試算表，純粹算而已，方便先用假資料測試這段邏輯對不對。

    判斷依據不是看這一列「自己的最後一個值」長怎樣——舊格式（少一欄）跟新格式（對齊）的
    最後一個值本來就都是 Yahoo代號，兩種格式單看最後一個值完全沒辦法分辨（這是第一版
    腳本寫錯、本機測試時才抓到的地方）。真正該看的是「header 現在認定 Yahoo代號應該在
    哪個位置（最後一欄，index = len(header)-1）」，這一列那個位置實際有沒有值：
    新格式（長度剛好等於 header）在那個位置本來就有東西；舊格式（長度少一欄）那個位置
    根本不存在（超出這一列的範圍），一定抓不到有效的代號格式。"""
    conf_idx = header.index("信心")
    yahoo_pos = len(header) - 1
    fixes = []
    for i, row in enumerate(values[1:], start=2):   # 從第 2 列開始（跳過標題），i = 試算表實際列號
        real = strip_trailing_empty(row)
        if not real:
            continue   # 整列都是空的，跳過
        val_at_yahoo_slot = real[yahoo_pos].strip() if yahoo_pos < len(real) else ""
        if YAHOO_RE.match(val_at_yahoo_slot):
            continue   # header 認定的 Yahoo代號位置，現在就有效的代號格式，已經對齊，不用動
        if len(real) != conf_idx + 3:
            # 正常的舊格式少一欄，應該剛好是 conf_idx + 3（信心之後還有 距52週高%/理由/Yahoo代號
            # 這 3 欄）；長度對不上代表這列可能本來就有問題（例如理由裡剛好帶了類似代號的文字
            # 但其實沒對齊，或資料本身就不完整），不要用猜的，列出來讓人自己看
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

        header = ws.row_values(1)
        want = SHEET_HEADERS[title]
        if header != want:
            print(f"{title}：標題列還不是最新版本，先更新標題列。")
            ensure_header(ws, title)
            header = want

        values = ws.get_all_values()
        fixes = plan_fixes(header, values)
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
            for i, fixed, _ in need_fix:
                rng = f"A{i}:{gspread.utils.rowcol_to_a1(i, len(fixed))}"
                ws.update([fixed], rng, value_input_option="USER_ENTERED")
            print(f"{title}：已修正 {len(need_fix)} 列。")
        elif need_fix:
            print(f"{title}：預覽模式，沒有真的寫入。加 --apply 參數才會套用。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
