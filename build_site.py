"""
把 screener.py 產生的資料做成靜態網頁 site/index.html（樣板在 template.html）。

網頁內容：搜尋欄（查全部上市櫃股票有無買進訊號）、我的觀察清單（可加入／編輯移除，
存在瀏覽器裡）、每日與每週候選股、大盤強弱。
網站不含持股的買進價、股數、損益（避免公開個人持股）；候選股的「買進區間」是程式算的建議價。

用法：先執行 python screener.py，再執行 python build_site.py
"""

import html
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

import history
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


POS_CLASS = {"現價在買進區間內": "in", "現價高於區間，勿追，等回測": "above", "現價低於區間，尚未到買點": "below"}
POS_SHORT = {"in": "區間內", "above": "高於區間", "below": "低於區間"}


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
        f"<tr><td class='l'>{r['代號']}</td><td class='l'>{html.escape(str(r['名稱']))}"
        f"<div class='links' style='margin-top:4px'>{links(r['代號'], r.get('Yahoo代號', ''))}</div>"
        f"<button class='btn sm' style='margin-top:4px' data-add='{r['代號']}'>＋觀察</button></td>"
        f"<td class='l'>{html.escape(str(r['型態']))}"
        + (' <span class="tag t-hot">左側</span>' if r.get("側別") == "左側" else "") + "</td>"
        f"<td>{r['買進下限']:,.2f}～{r['買進上限']:,.2f}<br>"
        f"<small class='pos-{POS_CLASS.get(r['現價位置'], '')}'>收 {r['收盤']:,.2f}．{r['現價位置']}</small></td>"
        f"<td class='stopc'>{r['停損價']:,.2f}<br><small>-{r['風險%']}%</small></td>"
        f"<td class='tpc'>{r['停利價']:,.2f}<br><small>+{r['報酬%']}%</small></td>"
        f"<td>{r.get('賺賠比', '')}</td>"
        f"<td class='reason'>{html.escape(str(r['理由']))}</td></tr>"
        for _, r in d.iterrows()
    )
    return ('<div class="tablewrap"><table class="picks"><thead><tr><th class="l">代號</th><th class="l">名稱</th>'
            '<th class="l">訊號</th><th>買進區間</th><th>停損價</th><th>停利價</th><th>賺賠比</th>'
            '<th class="l">理由</th></tr></thead>'
            f"<tbody>{rows}</tbody></table></div>")


def multi_signal_html(stocks):
    """同時符合 2 個（含）以上「不同種類」右側買進訊號的股票，包成預設收合的 <details>。"""
    found = []
    for st in stocks:
        right = [g for g in st["sigs"] if g["side"] == "右側"]
        kinds = {g["name"] for g in right}
        if len(kinds) >= 2 and "過熱，不追" not in st["tags"]:   # 過熱的不列入「可買進」
            found.append((len(kinds), st, right))
    found.sort(key=lambda x: (-x[0], x[1]["code"]))
    if not found:
        body = '<div class="empty">目前沒有同時符合 2 個以上買進訊號的股票。</div>'
    else:
        rows = ""
        for n, st, right in found:
            lines = "".join(
                f"<div class='sigline'><b>{html.escape(g['name'])}</b>"
                f"<span class='tag t-mute'>{g['tf']}</span><br>"
                f"<small>買進 {g['lo']:,.2f}～{g['hi']:,.2f}（{POS_SHORT[g['pos']]}）　"
                f"停損 <span class='stopc'>{g['stop']:,.2f}</span>（-{g['risk']}%）　"
                f"停利 <span class='tpc'>{g['target']:,.2f}</span>（+{g['gain']}%）　"
                f"賺賠比 {g['rr']}</small></div>" for g in right)
            mkt = st["mkt"]
            rows += (f"<tr><td class='l'><a href='#' data-detail='{st['code']}'>{st['code']}</a></td>"
                     f"<td class='l'>{html.escape(st['name'])}<br><span class='tag t-bull'>{n} 個訊號</span></td>"
                     f"<td>{st['close']:,.2f}</td><td class='l reason' style='color:inherit'>{lines}</td>"
                     f"<td class='l links'>{links(st['code'], '.TWO' if mkt == 'TPEX' else '.TW')}"
                     f"<br><button class='btn sm' data-add='{st['code']}'>＋觀察</button></td></tr>")
        body = ('<div class="tablewrap"><table class="multi"><thead><tr><th class="l">代號</th><th class="l">名稱</th>'
                '<th>收盤</th><th class="l">符合的訊號與價位</th><th class="l">連結</th></tr></thead>'
                f"<tbody>{rows}</tbody></table></div>"
                '<div class="sub" style="margin-top:8px">點代號可看每個訊號的詳細理由。'
                '提醒：部分組合本質上是同一個動作（例如「鏡射突破前高」與「窄幅盤整突破」常一起出現，'
                '「回測均線」與「鏡射拉回」也是），並非完全獨立的確認，仍請自己看 K 線圖。</div>')
    return (f'<details class="fold"><summary>多重訊號：同時符合 2 個（含）以上買進訊號'
            f'<span class="tag t-bull">{len(found)} 檔</span></summary>{body}</details>')


def main():
    SITE_DIR.mkdir(exist_ok=True)
    try:
        stocks = json.loads((BASE_DIR / "stocks.json").read_text(encoding="utf-8"))
    except Exception:
        stocks = {"date": "-", "stocks": []}    # 選股失敗時仍產生網頁，搜尋會顯示提示

    # 滾動紀錄：讀線上上一版的紀錄，加入今天，只留最近 7 個交易日；讀取失敗就中止，避免紀錄被蓋掉
    try:
        hist_file = history.update(history.load_previous(), stocks)
    except RuntimeError as e:
        print(f"[錯誤] {e}")
        return 1
    history.apply(stocks, hist_file)
    (SITE_DIR / "history.json").write_text(
        json.dumps(hist_file, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    watchlist, _, _, _ = load_config()          # 預設的觀察清單（使用者在瀏覽器裡改的不會動到這裡）
    data = {"date": stocks["date"], "stocks": stocks["stocks"], "default_watchlist": watchlist,
            "recorded": sorted(hist_file["days"])}

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
            .replace("__MULTI__", multi_signal_html(stocks["stocks"]))
            .replace("__DAILY__", picks_table("candidates_daily.csv"))
            .replace("__WEEKLY__", picks_table("candidates_weekly.csv"))
            .replace("__DATA__", data_json))
    (SITE_DIR / "index.html").write_text(page, encoding="utf-8")
    print(f"已產生 {SITE_DIR.name}/index.html（{len(stocks['stocks'])} 檔可搜尋；已記錄 {len(hist_file['days'])} 天）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
