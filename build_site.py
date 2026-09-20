"""
把 stock_checker.py 產生的 report.csv 與 output/*.png 做成靜態網頁 site/index.html。
網站不含買進價、股數、損益（避免公開個人持股），只顯示訊號。

用法：先執行 python stock_checker.py，再執行 python build_site.py
"""

import html
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).parent
REPORT_FILE = BASE_DIR / "report.csv"
CHART_DIR = BASE_DIR / "output"
SITE_DIR = BASE_DIR / "site"

CSS = """
:root{--bg:#f7f7f5;--card:#fff;--text:#1f2328;--muted:#6b7280;--line:#e5e7eb;
--good:#b42318;--bad:#067647;--warn:#b54708;--stop:#fff;--stopbg:#b42318}
@media (prefers-color-scheme:dark){:root{--bg:#111315;--card:#1a1d21;--text:#e6e8eb;
--muted:#9aa1ab;--line:#2c3138;--good:#ff8a80;--bad:#6ee7a8;--warn:#fbbf24}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.5 -apple-system,"PingFang TC","Microsoft JhengHei",sans-serif}
main{max-width:980px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:22px;margin:0 0 4px}
.sub{color:var(--muted);font-size:13px;margin-bottom:20px}
.tablewrap{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;min-width:560px}
th,td{padding:10px 12px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
th{font-size:12px;color:var(--muted);font-weight:600}
th:nth-child(-n+2),td:nth-child(-n+2),th:last-child,td:last-child{text-align:left}
table.picks{min-width:900px}
tr:last-child td{border-bottom:0}
.tag{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;margin-right:4px;
border:1px solid currentColor}
.t-bull{color:var(--good)}.t-weak{color:var(--bad)}.t-hot{color:var(--warn)}
.t-stop{background:var(--stopbg);color:var(--stop);border-color:var(--stopbg)}
.up{color:var(--good)}.down{color:var(--bad)}
h2{font-size:16px;margin:28px 0 10px}
.reason{white-space:normal;text-align:left;min-width:260px;color:var(--muted);font-size:13px}
.stopc{color:var(--bad)}.tpc{color:var(--good)}
.empty{color:var(--muted);padding:14px 12px}
.links a{font-size:12px;margin-right:8px;color:var(--muted);text-decoration:none;border-bottom:1px dotted}
.links a:hover{color:var(--text)}
.charts{display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:12px}
@media (max-width:480px){.charts{grid-template-columns:1fr}}
.charts figure{margin:0;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px}
.charts img{width:100%;height:auto;display:block;border-radius:6px}
.note{color:var(--muted);font-size:12px;margin-top:28px}
"""


def tags(signal):
    """把「多方；過熱，不追」拆成一顆顆標籤，並依內容上色。"""
    out = []
    for s in str(signal).split("；"):
        cls = "t-stop" if "停損" in s else "t-hot" if "過熱" in s else \
              "t-weak" if "轉弱" in s else "t-bull" if "多方" in s else ""
        out.append(f'<span class="tag {cls}">{html.escape(s)}</span>')
    return "".join(out)


def links(code, symbol):
    """第三方網站快速連結：TradingView（圖表）、Goodinfo（財務/籌碼）、Yahoo 股市（行情/新聞）。"""
    market = "TPEX" if str(symbol).endswith(".TWO") else "TWSE"   # TradingView 用交易所前綴
    items = [
        ("圖表", f"https://www.tradingview.com/chart/?symbol={market}%3A{code}"),
        ("財務", f"https://goodinfo.tw/tw/StockDetail.asp?STOCK_ID={code}"),
        ("行情", f"https://tw.stock.yahoo.com/quote/{code}"),
    ]
    return "".join(f'<a href="{u}" target="_blank" rel="noopener">{n}</a>' for n, u in items)


def pct(v):
    cls = "up" if v > 0 else "down" if v < 0 else ""
    return f'<span class="{cls}">{v:+.1f}%</span>'


def picks_table(csv_name):
    """讀 candidates_*.csv，回傳 (HTML, 資料日期)。沒有檔案或沒有候選時顯示說明。"""
    path = BASE_DIR / csv_name
    if not path.exists():
        return '<div class="empty">尚無資料</div>', ""
    d = pd.read_csv(path, dtype={"代號": str})
    if d.empty:
        return '<div class="empty">目前沒有符合條件的股票（沒有好標的時空手也是一種選擇）。</div>', ""
    rows = "".join(
        f"<tr><td>{r['代號']}</td><td>{html.escape(str(r['名稱']))}</td><td>{r['型態']}</td>"
        f"<td>{r['買進價']:,.2f}</td>"
        f"<td class='stopc'>{r['停損價']:,.2f}<br><small>-{r['風險%']}%</small></td>"
        f"<td class='tpc'>{r['停利價']:,.2f}<br><small>+{r['報酬%']}%</small></td>"
        f"<td class='reason'>{html.escape(str(r['理由']))}</td>"
        f"<td class='links'>{links(r['代號'], r.get('Yahoo代號', ''))}</td></tr>"
        for _, r in d.iterrows()
    )
    table = ('<div class="tablewrap"><table class="picks"><thead><tr><th>代號</th><th>名稱</th>'
             '<th>型態</th><th>買進價</th><th>停損價</th><th>停利價</th><th>理由</th><th>連結</th></tr></thead>'
             f"<tbody>{rows}</tbody></table></div>")
    return table, d["資料日期"].max()


def main():
    df = pd.read_csv(REPORT_FILE, dtype={"代號": str})
    SITE_DIR.mkdir(exist_ok=True)
    (SITE_DIR / "charts").mkdir(exist_ok=True)

    rows, figures = [], []
    for _, r in df.iterrows():
        code = r["代號"]
        rows.append(
            f"<tr><td>{code}</td><td>{html.escape(str(r['名稱']))}</td>"
            f"<td>{r['收盤']:,.2f}</td><td>{pct(r['離季線%'])}</td>"
            f"<td>{r['RSI14']:.0f}</td><td>{tags(r['訊號'])}</td>"
            f"<td class='links'>{links(code, r['Yahoo代號'])}</td></tr>"
        )
        png = CHART_DIR / f"{code}.png"
        if png.exists():
            shutil.copy(png, SITE_DIR / "charts" / png.name)
            figures.append(f'<figure><img src="charts/{code}.png" '
                           f'alt="{code} K 線圖" loading="lazy"></figure>')

    data_date = df["資料日期"].max()
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")

    daily_html, daily_date = picks_table("candidates_daily.csv")
    weekly_html, _ = picks_table("candidates_weekly.csv")

    page = f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>台股觀察清單</title><style>{CSS}</style></head>
<body><main>
<h1>台股觀察清單</h1>
<div class="sub">資料日期 {data_date}　·　網頁更新 {now}（台灣時間）</div>
<h2>每日候選：可留意的買進標的</h2>
<div class="sub">從成交金額前 150 名挑出「趨勢向上＋回測或突破」的股票。停利 = 風險的 2 倍報酬。</div>
{daily_html}
<h2>每週候選（週線，中期趨勢）</h2>
<div class="sub">用週線判斷，較適合抱久一點；每週六依整週資料更新。</div>
{weekly_html}
<h2>我的觀察清單</h2>
<div class="tablewrap"><table>
<thead><tr><th>代號</th><th>名稱</th><th>收盤</th><th>離季線</th><th>RSI</th><th>訊號</th><th>連結</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table></div>
<h2>K 線圖（5MA / 20MA / 60MA 與成交量）</h2>
<div class="charts">{"".join(figures)}</div>
<p class="note">僅為資料分析，不是投資建議，本站不提供下單。資料來自 Yahoo Finance，可能延遲。</p>
</main></body></html>"""
    (SITE_DIR / "index.html").write_text(page, encoding="utf-8")
    print(f"已產生 {SITE_DIR.name}/index.html")


if __name__ == "__main__":
    main()
