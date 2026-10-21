"""
更新完成後的 Telegram 通知。只做通知，不會下單。

每次執行都發一則新訊息，不編輯舊的——同一天跑幾次（排程、或用 Telegram「重新跑」手動
補跑）就會收到幾則，不會悄悄把前一則的內容換掉。（舊版本原本是「一天只留一則、編輯成
最新結果」，但編輯訊息不會觸發手機通知、也容易讓人找不到到底有沒有更新，改成每次都發
新的，代價是同一天重跑幾次就會收到幾則通知）。

用法（在 GitHub Actions 裡自動執行）：
  python notify.py make                      # 依今天的分析結果產生 message.txt
  python notify.py send <build結果> <deploy結果>  # 傳送 Telegram 訊息（成功傳摘要、失敗傳警告）
  python notify.py resend                     # 不重新分析，把 message.txt 現有內容原封不動發一次
                                                # （Telegram 指令「訊息重發」用，見 tg_commands.py）

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

    def stock_line(code, name, conf=None):
        """代號＋名稱＋收盤＋（跟前一天比）漲跌點數與漲跌幅＋信心程度；stocks.json 只存了
        漲跌幅（chg），沒有存漲跌點數，用「收盤 ÷ (1+漲跌幅) = 前一天收盤」反推算出點數，
        四捨五入前的誤差極小，這裡只是通知摘要，不影響網頁上實際的價格與訊號判斷。
        conf：信心百分比（見 screener.py 的 evaluate()），沒有值（例如查無資料）就不顯示。"""
        s = by_code.get(code)
        if not s:
            return f"・{code} {name}"
        close, pct = s["close"], s["chg"]
        prev = close / (1 + pct / 100) if pct != -100 else close
        conf_txt = ""
        if conf not in (None, ""):
            try:
                conf_txt = f"　信心{int(round(float(conf)))}%"
            except (ValueError, TypeError):
                pass
        return f"・{code} {name}　{close:.2f}　{badge(close - prev, pct)}{conf_txt}"

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
        # 一檔股票可能同時符合好幾個右側訊號、各自信心不同，通知摘要只列一行，取其中最高的
        multi_conf = {s["code"]: max((g["conf"] for g in s["sigs"] if g["side"] == "右側" and g.get("conf") is not None),
                                     default=None) for s in multi}
        lines += [stock_line(s["code"], s["name"], multi_conf.get(s["code"])) for s in multi] or ["（無）"]
    else:
        lines.append("多重訊號：（資料產生失敗）")

    lines.append("")
    daily = read_csv("candidates_daily.csv")
    lines.append(f"每日候選：{len(daily)} 檔")
    lines += [stock_line(r["代號"], r["名稱"], r.get("信心")) for r in daily] or ["（無）"]

    lines += ["", "僅為資料分析，不是投資建議；自負盈虧"]
    return "\n".join(lines)


def send(text):
    """傳送一則新的 Telegram 通知，不編輯舊訊息。回傳這次的 message_id，完全沒送出
    （沒設定 token 或送出失敗）回傳 None。"""
    token, chat = os.environ.get("TG_BOT_TOKEN"), os.environ.get("TG_CHAT_ID")
    if not token or not chat:
        print("沒有設定 TG_BOT_TOKEN / TG_CHAT_ID，略過 Telegram 通知。")
        return None
    import requests
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
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
        if build == "success" and deploy == "success" and MESSAGE_FILE.exists():
            text = MESSAGE_FILE.read_text(encoding="utf-8")
        else:
            text = (f"DiStocks 更新失敗（分析：{build}，發布：{deploy}）\n"
                    f"網頁還是上一次的內容。請查看：{run_url}")
        send(text)
        return 0
    if cmd == "resend":
        if MESSAGE_FILE.exists():
            send(MESSAGE_FILE.read_text(encoding="utf-8"))
        else:
            send("目前還沒有可以重發的分析摘要，請先用「重新跑」整個分析一次。")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
