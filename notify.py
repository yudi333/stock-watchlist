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
    """組出摘要文字（純文字，Telegram 上好讀）。只列代號＋名稱，詳細的買進區間/停損/停利
    要點連結進網頁看；每週候選變化慢，不放進通知裡。"""
    stocks = None
    try:
        stocks = json.loads((BASE_DIR / "stocks.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    market = None
    try:
        market = json.loads((BASE_DIR / "market.json").read_text(encoding="utf-8"))
    except Exception:
        pass

    date = (stocks or {}).get("date") or (market or {}).get("date") or "-"
    lines = [f"日期：{date}"]
    if market:
        lines.append(f"大盤：{market['text']}")
        if "otc" in market:
            o = market["otc"]
            arrow = "▲" if o["chg_pct"] > 0 else ("▼" if o["chg_pct"] < 0 else "－")
            lines.append(f"櫃買：{o['close']:.2f}　{arrow}{abs(o['chg_pct']):.2f}%")
    else:
        lines.append("大盤：（資料產生失敗）")

    lines.append("")
    if stocks:
        multi = [s for s in stocks["stocks"]
                 if len({g["name"] for g in s["sigs"] if g["side"] == "右側"}) >= 2
                 and "過熱，不追" not in s["tags"]]
        lines.append(f"多重訊號：{len(multi)} 檔")
        lines += [f"・{s['code']} {s['name']}" for s in multi] or ["（無）"]
    else:
        lines.append("多重訊號：（資料產生失敗）")

    lines.append("")
    daily = read_csv("candidates_daily.csv")
    lines.append(f"每日候選：{len(daily)} 檔")
    lines += [f"・{r['代號']} {r['名稱']}" for r in daily] or ["（無）"]

    lines += ["", f"網頁：{SITE_URL}", "僅為資料分析，不是投資建議；自負盈虧"]
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
