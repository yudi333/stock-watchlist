"""
盤中報價：只抓「目前價格」，不做任何技術分析，跟每天兩次的完整分析（screener.py）分開跑。

為什麼要分開：股市 13:30 才收盤，日線資料的「今天」在盤中是空的（Yahoo 還沒結算），
會被 stock_checker.py 的 drop_unfinished_today() 濾掉。這裡改成：

1. 優先用鉅亨網（cnyes）的報價介面：可以一次查 250 檔、上市上櫃格式相同，全市場約 5 秒。
2. 失敗（改版、被擋、網路問題）就自動退回用 Yahoo 的「分鐘線」：日線在盤中收盤價是空的，
   但分鐘線有真正的成交價，全市場約 40 秒。

兩者都是沒有正式文件的介面（不是官方公開 API），只做資料抓取、不會下單，可能有數分鐘延遲，
不是官方即時報價；哪天真的失效，這支程式就是安全地失敗（回傳空結果），不影響其他部分。

只在瀏覽器的「我的持股」用得到這個資料，觀察清單和候選股不受影響（那些維持每天兩次更新）。

用法：python quotes.py [輸出檔案路徑，預設 quotes.json]
"""

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yfinance as yf

BASE_DIR = Path(__file__).parent
SITE_URL = "https://yudi333.github.io/stock-watchlist"
HEADERS = {"User-Agent": "Mozilla/5.0"}


def fetch_universe():
    """讀目前已發布的 universe.json，取得要查報價的股票清單（跟每日分析同一批）。"""
    r = requests.get(f"{SITE_URL}/universe.json?t={int(time.time())}", headers=HEADERS, timeout=30)
    r.raise_for_status()
    items = r.json()
    return [(it["code"], it["code"] + (".TWO" if it["mkt"] == "TPEX" else ".TW")) for it in items]


def fetch_via_cnyes(codes):
    """鉅亨網報價：一次查 250 檔，上市上櫃都用 TWS 前綴，欄位 21 是最新成交價。"""
    prices, chunk = {}, 250
    for i in range(0, len(codes), chunk):
        batch = codes[i:i + chunk]
        syms = ",".join(f"TWS:{c}:STOCK" for c in batch)
        try:
            r = requests.get(f"https://ws.api.cnyes.com/ws/api/v1/quote/quotes/{syms}",
                             headers=HEADERS, timeout=20)
            r.raise_for_status()
            for d in r.json().get("data", []):
                code, price = d.get("200010"), d.get("21")
                if code and price is not None:
                    prices[code] = round(float(price), 2)
        except Exception as e:
            print(f"[注意] 鉅亨網第 {i // chunk + 1} 批查詢失敗：{e}")
        time.sleep(0.2)          # 對他們的伺服器客氣一點，不要連續轟炸
    return prices


def fetch_via_yahoo(pairs):
    """備援：Yahoo 的 1 分鐘線，取每檔最後一筆成交價。單一批失敗就跳過，不影響其他批。"""
    prices = {}
    CHUNK = 100
    for i in range(0, len(pairs), CHUNK):
        chunk = pairs[i:i + CHUNK]
        syms = [s for _, s in chunk]
        try:
            data = yf.download(syms, period="1d", interval="1m", group_by="ticker",
                               progress=False, threads=True)
        except Exception as e:
            print(f"[注意] Yahoo 第 {i // CHUNK + 1} 批查詢失敗：{e}")
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

    print(f"查詢 {len(pairs)} 檔的盤中價格（優先用鉅亨網，失敗才用 Yahoo 分鐘線）...")
    prices = fetch_via_cnyes([code for code, _ in pairs])
    missing = [(c, s) for c, s in pairs if c not in prices]
    if missing:
        print(f"鉅亨網缺 {len(missing)} 檔，改用 Yahoo 分鐘線補齊...")
        prices.update(fetch_via_yahoo(missing))

    # GitHub Actions 的伺服器用 UTC 時間，time.strftime 會抓到 UTC，跟台灣時間差 8 小時，
    # 網頁上顯示「盤中 HH:MM」才不會誤導成台灣時間
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%H:%M")
    payload = {"time": now, "prices": prices}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    print(f"已存 {out_path.name}：{len(prices)}/{len(pairs)} 檔（{payload['time']}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
