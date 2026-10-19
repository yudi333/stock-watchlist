"""
一次性腳本：把「多重訊號／每日候選／每週候選」表格裡已經存在的「已停損」「已停利」列
補上底色——跟 backtest.py 裡「新產生已停損/已停利時順便整列上色」用的是同一套顏色定義
（RESULT_FILL，直接 import，不重複定義），差別只是這支腳本回頭處理已經存在的舊資料，
backtest.py 本身只會處理「這次執行才第一次定案」的列（已經是已停損/已停利的舊列，
status 不在 OPEN_STATUSES 裡，backtest.py 的迴圈一開始就會跳過，不會碰到）。

用法：
  python color_existing_results.py          # 預覽模式，只印出會處理幾列，不會真的寫入
  python color_existing_results.py --apply  # 真的套用

需要環境變數（跟 sheets_log.py / backtest.py 共用）：GOOGLE_SHEETS_KEY / GOOGLE_SHEETS_ID
"""

import sys

from backtest import RESULT_FILL, connect

TITLES = ["多重訊號", "每日候選", "每週候選"]


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

        values = ws.get_all_values()
        if len(values) < 2:
            print(f"{title}：沒有資料，跳過。")
            continue
        header = values[0]
        if "結果" not in header:
            print(f"{title}：標題列沒有「結果」欄，跳過。")
            continue
        result_idx = header.index("結果")

        colors = []
        counts = {"已停損": 0, "已停利": 0}
        for r in range(1, len(values)):
            row = values[r]
            result = row[result_idx] if result_idx < len(row) else ""
            if result in RESULT_FILL:
                counts[result] += 1
                colors.append({"range": f"{gspread.utils.rowcol_to_a1(r + 1, 1)}:"
                                         f"{gspread.utils.rowcol_to_a1(r + 1, len(header))}",
                                "format": {"backgroundColor": RESULT_FILL[result]}})

        print(f"{title}：共 {len(values) - 1} 列，已停損 {counts['已停損']} 列、"
              f"已停利 {counts['已停利']} 列需要補色。")
        if colors and apply:
            ws.batch_format(colors)
            print(f"{title}：已補上 {len(colors)} 列的底色。")
        elif colors:
            print(f"{title}：預覽模式，沒有真的寫入。加 --apply 參數才會套用。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
