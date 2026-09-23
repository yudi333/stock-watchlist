"""
台指期夜盤：每天早上單獨更新一次「昨晚完整跑完」的夜盤收盤資料，只更新 market.json 的
night 欄位，其他欄位（大盤、櫃買，由 screener.py 算好）維持原樣不動。

為什麼要獨立一支：台指期夜盤（盤後交易）15:00 到隔天 05:00 才收盤，但 daily.yml 完整分析
排在收盤後那幾次（14:03~16:33）跑，當晚的夜盤那時候才剛開盤或還在跑，抓到的是還沒結束的
盤中價格，不是最終結果。改成每天早上 8 點（留了緩衝時間）單獨抓一次，這時候昨晚的夜盤已經
收盤，才是「跑完整晚」的最終結果。

資料來源：官方 TAIFEX OpenAPI（https://openapi.taifex.com.tw/），近月台指期（TX）合約的
盤後交易。這個 API 沒有「查詢特定日期」的功能，只給「目前最新一筆」——多數時候就是昨晚，
但官方系統偶爾會延遲一兩個交易時段，抓到的可能不是最新的；網頁會照實顯示那筆資料的實際
交易日期，不會假裝是今天。

用法：python night_futures.py [market.json 路徑，預設 market.json]
"""

import json
import sys
from pathlib import Path

import requests

HEADERS = {"User-Agent": "Mozilla/5.0"}


def fetch_night_futures():
    """近月台指期（TX）的盤後（夜盤）交易：抓不到、或近月合約還沒有盤後成交就回傳 None。"""
    try:
        r = requests.get("https://openapi.taifex.com.tw/v1/DailyMarketReportFut", headers=HEADERS, timeout=20)
        r.raise_for_status()
        rows = [d for d in r.json() if d.get("Contract") == "TX" and d.get("TradingSession") == "盤後"
                and d.get("Last") not in (None, "-", "NULL")]
        if not rows:
            return None
        near = min(rows, key=lambda d: d["ContractMonth(Week)"])
        d = near["Date"]
        return {"date": f"{d[:4]}-{d[4:6]}-{d[6:]}", "month": near["ContractMonth(Week)"][4:] + "月",
                "close": float(near["Last"]), "chg_pct": float(near["%"].rstrip("%"))}
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
    print(f"已更新夜盤：{night['month']}台指期 {night['close']:,.0f}（{night['chg_pct']:+.2f}%），資料日期 {night['date']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
