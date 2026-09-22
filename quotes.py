"""
盤中報價：只抓「目前價格」，不做任何技術分析，跟每天兩次的完整分析（screener.py）分開跑。

為什麼要分開：股市 13:30 才收盤，日線資料的「今天」在盤中是空的（Yahoo 還沒結算），
會被 stock_checker.py 的 drop_unfinished_today() 濾掉。但改抓「分鐘線」的最後一筆，
盤中就有真正的成交價，可以讓「我的持股」在收盤前也能檢查停損。

只在瀏覽器的「我的持股」用得到這個資料，觀察清單和候選股不受影響（那些維持每天兩次更新）。
只做資料抓取，不會下單，可能有數分鐘延遲，不是官方即時報價。

用法：python quotes.py [輸出檔案路徑，預設 quotes.json]
"""

import json
import sys
import time
from pathlib import Path

import requests
import yfinance as yf

BASE_DIR = Path(__file__).parent
SITE_URL = "https://yudi333.github.io/stock-watchlist"
CHUNK = 100
HEADERS = {"User-Agent": "Mozilla/5.0"}


def fetch_universe():
    """讀目前已發布的 universe.json，取得要查報價的股票清單（跟每日分析同一批）。"""
    r = requests.get(f"{SITE_URL}/universe.json?t={int(time.time())}", headers=HEADERS, timeout=30)
    r.raise_for_status()
    items = r.json()
    return [(it["code"], it["code"] + (".TWO" if it["mkt"] == "TPEX" else ".TW")) for it in items]


def fetch_quotes(pairs):
    """用 1 分鐘線抓每檔最後一筆成交價；單一批失敗就跳過，不影響其他批。"""
    prices = {}
    for i in range(0, len(pairs), CHUNK):
        chunk = pairs[i:i + CHUNK]
        syms = [s for _, s in chunk]
        try:
            data = yf.download(syms, period="1d", interval="1m", group_by="ticker",
                               progress=False, threads=True)
        except Exception as e:
            print(f"[注意] 第 {i // CHUNK + 1} 批查詢失敗：{e}")
            continue
        for code, sym in chunk:
            try:
                frame = data[sym] if len(chunk) > 1 else data
                c = frame["Close"].dropna()
                if len(c):
                    prices[code] = round(float(c.iloc[-1]), 2)
            except Exception:
                pass                      # 單檔沒資料（例如剛好停牌）就跳過
    return prices


def main():
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else BASE_DIR / "quotes.json"
    try:
        pairs = fetch_universe()
    except Exception as e:
        print(f"讀取股票清單失敗：{e}")
        return 1
    print(f"查詢 {len(pairs)} 檔的盤中價格...")
    prices = fetch_quotes(pairs)
    payload = {"time": time.strftime("%H:%M"), "prices": prices}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    print(f"已存 {out_path.name}：{len(prices)}/{len(pairs)} 檔（{payload['time']}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
