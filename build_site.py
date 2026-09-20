"""
把 screener.py 產生的資料做成靜態網頁 site/index.html（樣板在 template.html）。

網頁內容：搜尋欄（查全部上市櫃股票有無買進訊號）、我的觀察清單（可加入／編輯移除，
存在瀏覽器裡）、每日與每週候選股、大盤強弱。
網站不含買進價、股數、損益（避免公開個人持股）。

用法：先執行 python screener.py，再執行 python build_site.py
"""

import html
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from stock_checker import load_config

BASE_DIR = Path(__file__).parent
SITE_DIR = BASE_DIR / "site"
TEMPLATE = BASE_DIR / "template.html"


def links(code, symbol):
    """第三方網站快速連結：TradingView（圖表）、Goodinfo（財務/籌碼）、Yahoo 股市（行情/新聞）。"""
    market = "TPEX" if str(symbol).endswith(".TWO") else "TWSE"   # TradingView 用交易所前綴
    items = [
        ("圖表", f"https://www.tradingview.com/chart/?symbol={market}%3A{code}"),
        ("財務", f"https://goodinfo.tw/tw/StockDetail.asp?STOCK_ID={code}"),
        ("行情", f"https://tw.stock.yahoo.com/quote/{code}"),
    ]
    return "".join(f'<a href="{u}" target="_blank" rel="noopener">{n}</a>' for n, u in items)


def picks_table(csv_name):
    """讀 candidates_*.csv，回傳表格 HTML。沒有檔案或沒有候選時顯示說明。"""
    path = BASE_DIR / csv_name
    if not path.exists():
        return '<div class="tablewrap"><div class="empty">尚無資料</div></div>'
    d = pd.read_csv(path, dtype={"代號": str})
    if d.empty:
        return ('<div class="tablewrap"><div class="empty">目前沒有符合條件的股票'
                '（沒有好標的時空手也是一種選擇）。</div></div>')
    rows = "".join(
        f"<tr><td class='l'>{r['代號']}</td><td class='l'>{html.escape(str(r['名稱']))}</td>"
        f"<td class='l'>{html.escape(str(r['型態']))}"
        + (' <span class="tag t-hot">左側</span>' if r.get("側別") == "左側" else "") + "</td>"
        f"<td>{r['買進價']:,.2f}</td>"
        f"<td class='stopc'>{r['停損價']:,.2f}<br><small>-{r['風險%']}%</small></td>"
        f"<td class='tpc'>{r['停利價']:,.2f}<br><small>+{r['報酬%']}%</small></td>"
        f"<td>{r.get('賺賠比', '')}</td>"
        f"<td class='reason'>{html.escape(str(r['理由']))}</td>"
        f"<td class='l links'>{links(r['代號'], r.get('Yahoo代號', ''))}"
        f"<br><button class='btn sm' data-add='{r['代號']}'>＋觀察</button></td></tr>"
        for _, r in d.iterrows()
    )
    return ('<div class="tablewrap"><table class="picks"><thead><tr><th class="l">代號</th><th class="l">名稱</th>'
            '<th class="l">訊號</th><th>買進價</th><th>停損價</th><th>停利價</th><th>賺賠比</th>'
            '<th class="l">理由</th><th class="l">連結</th></tr></thead>'
            f"<tbody>{rows}</tbody></table></div>")


def main():
    SITE_DIR.mkdir(exist_ok=True)
    try:
        stocks = json.loads((BASE_DIR / "stocks.json").read_text(encoding="utf-8"))
    except Exception:
        stocks = {"date": "-", "stocks": []}    # 選股失敗時仍產生網頁，搜尋會顯示提示

    watchlist, _, _, _ = load_config()          # 預設的觀察清單（使用者在瀏覽器裡改的不會動到這裡）
    data = {"stocks": stocks["stocks"], "default_watchlist": watchlist}

    market_html = ""
    try:
        m = json.loads((BASE_DIR / "market.json").read_text(encoding="utf-8"))
        market_html = (f'<div class="market"><b>大盤</b>（{m["date"]}　加權 {m["close"]:,.0f}，'
                       f'月線 {m["ma20"]:,.0f}／季線 {m["ma60"]:,.0f}）：{html.escape(m["text"])}</div>')
    except Exception:
        pass                                    # 沒有大盤資料就不顯示這一塊

    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    # JSON 放進 <script> 前，把 "</" 換掉避免提早結束標籤
    data_json = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    page = (TEMPLATE.read_text(encoding="utf-8")
            .replace("__DATA_DATE__", stocks["date"]).replace("__NOW__", now)
            .replace("__MARKET__", market_html)
            .replace("__DAILY__", picks_table("candidates_daily.csv"))
            .replace("__WEEKLY__", picks_table("candidates_weekly.csv"))
            .replace("__DATA__", data_json))
    (SITE_DIR / "index.html").write_text(page, encoding="utf-8")
    print(f"已產生 {SITE_DIR.name}/index.html（{len(stocks['stocks'])} 檔可搜尋）")


if __name__ == "__main__":
    main()
