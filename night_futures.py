"""
台指期夜盤：每天早上單獨更新一次「昨晚完整跑完」的夜盤收盤資料，只更新 market.json 的
night 欄位，其他欄位（大盤、櫃買，由 screener.py 算好）維持原樣不動。

為什麼要獨立一支：台指期夜盤（盤後交易）15:00 到隔天 05:00 才收盤，但 daily.yml 完整分析
排在收盤後那幾次（14:03~16:33）跑，當晚的夜盤那時候才剛開盤或還在跑，抓到的是還沒結束的
盤中價格，不是最終結果。改成每天早上 8 點（留了緩衝時間）單獨抓一次，這時候昨晚的夜盤已經
收盤，才是「跑完整晚」的最終結果。

資料來源：Yahoo奇摩股市「台指期近一（WTX&）」頁面——這是日盤＋夜盤合一的連續合約（不是
分開的兩個商品），頁面內嵌一段可以直接解析的 JSON，用簡單的 HTTP GET 就能拿到，不需要像
wantgoo 那樣處理 WebSocket 即時推播（試過，那個沒辦法用簡單的排程腳本抓）。05:00 收盤到
隔天 09:00 開盤這段沒有新成交，數字會停在夜盤最後一筆，這也是為什麼要在這段時間抓。

跟其他看盤網站（例如 wantgoo）的數字可能會有幾十到幾百點的小差異，是不同資料商本來就會有的
正常現象，不是抓錯。

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


def fetch_night_futures():
    """抓不到、或欄位對不起來就回傳 None。"""
    try:
        r = requests.get(YAHOO_URL, headers=HEADERS, timeout=20)
        r.raise_for_status()
        fundamental = _extract_json_object(r.text, "QuoteFundamental")
        if not fundamental:
            return None
        q = fundamental["quote"]["data"]
        close = float(q["price"]["raw"])
        chg_pct = float(q["changePercent"].rstrip("%"))
        ts = datetime.fromisoformat(q["regularMarketTime"].replace("Z", "+00:00"))
        ts = ts.astimezone(timezone(timedelta(hours=8)))
        return {"date": ts.strftime("%Y-%m-%d"), "label": "台指期盤後一", "close": close, "chg_pct": chg_pct}
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
    if market.get("night") == night:
        print(f"夜盤資料跟現有的一樣（{night['date']}），不用更新")
        return 0

    market["night"] = night
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(market, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"已更新夜盤：{night['label']} {night['close']:,.0f}（{night['chg_pct']:+.2f}%），資料日期 {night['date']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
