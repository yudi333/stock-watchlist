"""
更新完成後的 Telegram 通知。只做通知，不會下單。

用法（在 GitHub Actions 裡自動執行）：
  python notify.py make                      # 依今天的分析結果產生 message.txt
  python notify.py send <build結果> <deploy結果>  # 傳送到 Telegram（成功傳摘要、失敗傳警告）

需要兩個環境變數（在 GitHub repo 的 Settings > Secrets 設定，不要寫進程式）：
  TG_BOT_TOKEN   BotFather 給的機器人 token
  TG_CHAT_ID     你的聊天室 ID
沒設定的話會直接略過，不會讓更新失敗。
"""

import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent
MESSAGE_FILE = BASE_DIR / "message.txt"
SITE_URL = "https://yudi333.github.io/stock-watchlist/"


def read_csv(name):
    path = BASE_DIR / name
    if not path.exists():
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def make_message():
    """組出摘要文字（純文字，Telegram 上好讀）。"""
    lines = []
    stocks = None
    try:
        stocks = json.loads((BASE_DIR / "stocks.json").read_text(encoding="utf-8"))
    except Exception:
        pass

    if stocks:
        date = stocks["date"]
        today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
        lines.append(f"台股觀察清單已更新（資料日期 {date}）")
        if date != today:
            lines.append("※ 資料日期不是今天，可能休市或 Yahoo 還沒更新")
    else:
        lines.append("台股觀察清單已更新，但今天的選股資料產生失敗（網頁的搜尋功能可能無資料）")

    try:
        lines.append("大盤：" + json.loads((BASE_DIR / "market.json").read_text(encoding="utf-8"))["text"])
    except Exception:
        pass

    daily = read_csv("candidates_daily.csv")
    lines.append("")
    lines.append(f"【每日候選】{len(daily)} 檔")
    for r in daily:
        left = "（左側）" if r.get("側別") == "左側" else ""
        lines.append(f"・{r['代號']} {r['名稱']} {r['型態']}{left}\n"
                     f"  買 {r['買進下限']}～{r['買進上限']}（{r['現價位置']}）\n"
                     f"  損 {r['停損價']}（-{r['風險%']}%）　利 {r['停利價']}（+{r['報酬%']}%）")
    if not daily:
        lines.append("今天沒有符合條件的股票")

    weekly = read_csv("candidates_weekly.csv")
    lines.append("")
    lines.append(f"【每週候選】{len(weekly)} 檔：" + "、".join(f"{r['代號']}{r['名稱']}" for r in weekly))

    if stocks:
        multi = [s for s in stocks["stocks"]
                 if len({g["name"] for g in s["sigs"] if g["side"] == "右側"}) >= 2
                 and "過熱，不追" not in s["tags"]]
        lines.append("")
        lines.append(f"【多重訊號】{len(multi)} 檔同時符合 2 個以上買進訊號（網頁展開查看）")

    lines += ["", f"網頁：{SITE_URL}", "僅為資料分析，不是投資建議；停損以收盤價跌破為準。"]
    return "\n".join(lines)


def send(text):
    token, chat = os.environ.get("TG_BOT_TOKEN"), os.environ.get("TG_CHAT_ID")
    if not token or not chat:
        print("沒有設定 TG_BOT_TOKEN / TG_CHAT_ID，略過 Telegram 通知。")
        return 0
    import requests
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": text[:4000], "disable_web_page_preview": True},
                          timeout=30)
        r.raise_for_status()
        print("已傳送 Telegram 通知。")
    except Exception as e:
        # 不印出例外內容：requests 的錯誤訊息會帶到含 token 的網址
        print(f"Telegram 傳送失敗（{type(e).__name__}），請檢查 token 與 chat id。")
    return 0                                # 通知失敗不該讓整個更新流程失敗


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "make":
        MESSAGE_FILE.write_text(make_message(), encoding="utf-8")
        print(MESSAGE_FILE.read_text(encoding="utf-8"))
        return 0
    if cmd == "send":
        build, deploy = (sys.argv[2:4] + ["", ""])[:2]
        run_url = os.environ.get("RUN_URL", "")
        if build == "success" and deploy == "success" and MESSAGE_FILE.exists():
            return send(MESSAGE_FILE.read_text(encoding="utf-8"))
        return send(f"台股觀察清單更新失敗（分析：{build}，發布：{deploy}）\n"
                    f"網頁還是上一次的內容。請查看：{run_url}")
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
