"""
台指期夜盤：每天早上單獨更新一次「昨晚完整跑完」的夜盤收盤資料，只更新 market.json 的
night 欄位，其他欄位（大盤、櫃買，由 screener.py 算好）維持原樣不動。

為什麼要獨立一支：台指期夜盤（盤後交易）15:00 到隔天 05:00 才收盤，但 daily.yml 完整分析
排在收盤後那幾次（14:03~16:33）跑，當晚的夜盤那時候才剛開盤或還在跑，抓到的是還沒結束的
盤中價格，不是最終結果。改成每天早上 8 點（留了緩衝時間）單獨抓一次，這時候昨晚的夜盤已經
收盤，才是「跑完整晚」的最終結果。

資料來源：Yahoo奇摩股市「台指期近一（WTX&）」頁面——這是日盤＋夜盤合一的連續合約（不是
分開的兩個商品），頁面內嵌一段可以直接解析的 JSON，用簡單的 HTTP GET 就能拿到，不需要像
wantgoo 那樣處理 WebSocket 即時推播（試過，那個沒辦法用簡單的排程腳本抓）。

用的是 regularMarketPreviousClose（上一個完整交易日的收盤），不是即時跳動的 price——
price 只有在 05:00~09:00 這個沒有新成交的空檔抓才準，一旦排程延遲、晚一點才跑（GitHub
Actions 排程本來就可能延遲），price 就已經是新的一天、還在跳動的價格了；
regularMarketPreviousClose 不管什麼時間抓都是同一個穩定數字，比較可靠。
日期就是這個收盤所屬的交易日（抓取時的前一個交易日，遇到週末會自動往前跳過）。

漲跌（點數／%）：Yahoo 這個頁面沒有直接給「這個收盤價自己的漲跌」，所以用滾動比較的方式
自己算——上次執行時存的收盤價，就是「再前一個交易日」的收盤，兩者一減就是這次收盤的漲跌。
第一次套用這個版本、或中間漏跑了一天，會抓不到基準，那天不顯示漲跌，隔天開始就正常。

跟其他看盤網站（例如 wantgoo）的數字可能會有小差異，是不同資料商本來就會有的正常現象。

用法：python night_futures.py [market.json 路徑，預設 market.json]
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

HEADERS = {"User-Agent": "Mozilla/5.0"}
YAHOO_URL = "https://tw.stock.yahoo.com/future/WTX%26"


def _extract_json_object(text, key):
    """從 HTML 裡的一大段內嵌 JS 物件中，挖出 "key": {...} 這個子物件（用括號配對找結尾，
    比正規表示式可靠——嵌套很深，正規表示式抓不出正確的結尾位置）。找不到就回傳 None。"""
    idx = text.find(f'"{key}":')
    if idx < 0:
        return None
    start = text.index("{", idx)
    depth = in_str = esc = 0
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                frag = text[start:i + 1]
                return json.loads(frag.replace(":undefined", ":null"))
    return None


def _prev_trading_day(now):
    """回傳「上一個交易日」的日期字串（跳過週六日）。"""
    d = now - timedelta(days=1)
    while d.weekday() >= 5:      # 5=週六, 6=週日
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def fetch_night_futures():
    """抓不到、或欄位對不起來就回傳 None。"""
    try:
        r = requests.get(YAHOO_URL, headers=HEADERS, timeout=20)
        r.raise_for_status()
        fundamental = _extract_json_object(r.text, "QuoteFundamental")
        if not fundamental:
            return None
        q = fundamental["quote"]["data"]
        close = float(q["regularMarketPreviousClose"]["raw"])
        date = _prev_trading_day(datetime.now(timezone(timedelta(hours=8))))
        return {"date": date, "label": "台指期盤後一", "close": round(close, 2)}
    except Exception as e:
        print(f"[注意] 抓不到台指期夜盤：{e}")
        return None


def main():
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "market.json"
    try:
        market = json.loads(out_path.read_text(encoding="utf-8"))
    except Exception:
        market = {}   # 還沒有 market.json 也沒關係，這次先只存夜盤這一欄

    night = fetch_night_futures()
    if not night:
        print("抓不到夜盤資料，market.json 維持原樣不動")
        return 0

    prev = market.get("night") or {}
    if prev.get("close") is not None and prev.get("date") != night["date"]:
        # 用「上次記錄的收盤」當基準，算出這次收盤自己的漲跌（等於跟前一個交易日比）
        night["chg_pts"] = round(night["close"] - prev["close"], 2)
        night["chg_pct"] = round((night["close"] / prev["close"] - 1) * 100, 2)
    elif prev.get("date") == night["date"] and "chg_pts" in prev:
        # 同一天重複執行（例如手動重跑測試），沿用已經算好的漲跌，避免自己跟自己比變成 0
        night["chg_pts"] = prev["chg_pts"]
        night["chg_pct"] = prev["chg_pct"]

    if market.get("night") == night:
        print(f"夜盤資料跟現有的一樣（{night['date']}），不用更新")
        return 0

    market["night"] = night
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(market, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    chg_txt = f"（{night['chg_pts']:+.2f}／{night['chg_pct']:+.2f}%）" if "chg_pct" in night else "（尚無比較基準）"
    print(f"已更新夜盤：{night['label']} {night['close']:,.2f}{chg_txt}，資料日期 {night['date']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
