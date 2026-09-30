"""
更新完成後的 Telegram 通知。只做通知，不會下單。

一天不是發好幾則，是**一天只留一則、內容更新成最新一次的結果**：daily-update 一天跑
3 次，同一天內第二、三次執行不會再發新訊息，而是編輯（edit）當天第一次發的那一則，換成
這次的最新內容——避免手機上塞滿好幾則同一天的重複通知，也讓看到的永遠是最後一次（資料
最完整）的結果，跟網站、Google 試算表現在的邏輯一致。是不是要編輯舊訊息，由呼叫端
（daily.yml）透過 TG_EDIT_ID 這個環境變數告訴這支程式；這支程式本身不記狀態。

用法（在 GitHub Actions 裡自動執行）：
  python notify.py make                      # 依今天的分析結果產生 message.txt
  python notify.py send <build結果> <deploy結果>  # 傳送或編輯 Telegram 訊息（成功傳摘要、失敗傳警告）

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
MESSAGE_ID_FILE = BASE_DIR / ".tg-message-id" / "id"


def read_csv(name):
    path = BASE_DIR / name
    if not path.exists():
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def badge(pts, pct):
    """跟網頁上一致的漲跌樣式：▲/▼ + 點數 + %。"""
    arrow = "▲" if pct > 0 else ("▼" if pct < 0 else "－")
    pts_txt = f"{abs(pts):.2f}　" if pts is not None else ""
    return f"{arrow}{pts_txt}{abs(pct):.2f}%"


def make_message():
    """組出摘要文字（純文字，Telegram 上好讀）。只列代號＋名稱＋現價漲跌，詳細的買進區間/
    停損/停利要點連結進網頁看；每週候選變化慢，不放進通知裡。"""
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
    by_code = {s["code"]: s for s in stocks["stocks"]} if stocks else {}

    def stock_line(code, name):
        """代號＋名稱＋收盤＋（跟前一天比）漲跌點數與漲跌幅；stocks.json 只存了漲跌幅（chg），
        沒有存漲跌點數，用「收盤 ÷ (1+漲跌幅) = 前一天收盤」反推算出點數，四捨五入前的
        誤差極小，這裡只是通知摘要，不影響網頁上實際的價格與訊號判斷。"""
        s = by_code.get(code)
        if not s:
            return f"・{code} {name}"
        close, pct = s["close"], s["chg"]
        prev = close / (1 + pct / 100) if pct != -100 else close
        return f"・{code} {name}　{close:.2f}　{badge(close - prev, pct)}"

    date = (stocks or {}).get("date") or (market or {}).get("date") or "-"
    lines = [f"日期：{date}"]
    if market:
        m_badge = f"　{badge(market.get('chg_pts'), market['chg_pct'])}" if market.get("chg_pct") is not None else ""
        lines.append(f"大盤：{market['close']:.2f}{m_badge}")
        if "otc" in market:
            o = market["otc"]
            lines.append(f"櫃買：{o['close']:.2f}　{badge(o.get('chg_pts'), o['chg_pct'])}")
    else:
        lines.append("大盤：（資料產生失敗）")

    lines.append("")
    if stocks:
        multi = [s for s in stocks["stocks"]
                 if len({g["name"] for g in s["sigs"] if g["side"] == "右側"}) >= 2
                 and "過熱，不追" not in s["tags"]]
        lines.append(f"多重訊號：{len(multi)} 檔")
        lines += [stock_line(s["code"], s["name"]) for s in multi] or ["（無）"]
    else:
        lines.append("多重訊號：（資料產生失敗）")

    lines.append("")
    daily = read_csv("candidates_daily.csv")
    lines.append(f"每日候選：{len(daily)} 檔")
    lines += [stock_line(r["代號"], r["名稱"]) for r in daily] or ["（無）"]

    lines += ["", "僅為資料分析，不是投資建議；自負盈虧"]
    return "\n".join(lines)


def send(text, edit_id=None):
    """傳送或編輯今天的 Telegram 通知。edit_id 有值就先試著編輯那則舊訊息（同一天內把內容
    換成最新結果，不多發一則）；沒有 edit_id，或編輯失敗（例如舊訊息已經被手動刪除），就
    改發一則新的。回傳這次用到的 message_id（給下次同一天執行時繼續編輯用），完全沒送出
    （沒設定 token 或送出失敗）回傳 None。"""
    token, chat = os.environ.get("TG_BOT_TOKEN"), os.environ.get("TG_CHAT_ID")
    if not token or not chat:
        print("沒有設定 TG_BOT_TOKEN / TG_CHAT_ID，略過 Telegram 通知。")
        return None
    import requests
    base = f"https://api.telegram.org/bot{token}"
    if edit_id:
        try:
            r = requests.post(f"{base}/editMessageText",
                              json={"chat_id": chat, "message_id": edit_id, "text": text[:4000],
                                    "disable_web_page_preview": True},
                              timeout=30)
            r.raise_for_status()
            print("已把今天稍早那則 Telegram 通知更新成最新結果。")
            return edit_id
        except Exception as e:
            print(f"編輯今天稍早那則訊息失敗（{type(e).__name__}），改發一則新的。")
    try:
        r = requests.post(f"{base}/sendMessage",
                          json={"chat_id": chat, "text": text[:4000], "disable_web_page_preview": True},
                          timeout=30)
        r.raise_for_status()
        print("已傳送 Telegram 通知。")
        return r.json()["result"]["message_id"]
    except Exception as e:
        # 不印出例外內容：requests 的錯誤訊息會帶到含 token 的網址
        print(f"Telegram 傳送失敗（{type(e).__name__}），請檢查 token 與 chat id。")
        return None


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "make":
        MESSAGE_FILE.write_text(make_message(), encoding="utf-8")
        print(MESSAGE_FILE.read_text(encoding="utf-8"))
        return 0
    if cmd == "send":
        build, deploy = (sys.argv[2:4] + ["", ""])[:2]
        run_url = os.environ.get("RUN_URL", "")
        edit_id = os.environ.get("TG_EDIT_ID") or None
        if build == "success" and deploy == "success" and MESSAGE_FILE.exists():
            text = MESSAGE_FILE.read_text(encoding="utf-8")
        else:
            text = (f"DiStocks 更新失敗（分析：{build}，發布：{deploy}）\n"
                    f"網頁還是上一次的內容。請查看：{run_url}")
        mid = send(text, edit_id)
        if mid:   # 存下來給同一天後面的執行編輯用；沒送出成功就不寫，維持原本（如果有的話）的 id
            MESSAGE_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
            MESSAGE_ID_FILE.write_text(str(mid), encoding="utf-8")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
