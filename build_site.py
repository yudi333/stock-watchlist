"""
把 screener.py 產生的資料做成靜態網頁 site/index.html（樣板在 template.html）。

網頁內容：搜尋欄（查全部上市櫃股票有無買進訊號）、我的觀察清單（可加入／編輯移除，
存在瀏覽器裡）、每日與每週候選股、大盤強弱。
網站不含持股的買進價、股數、損益（避免公開個人持股）；候選股的「買進區間」是程式算的建議價。

用法：先執行 python screener.py，再執行 python build_site.py
"""

import html
import json
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

import history

BASE_DIR = Path(__file__).parent
SITE_DIR = BASE_DIR / "site"
TEMPLATE = BASE_DIR / "template.html"
SITE_URL = "https://yudi333.github.io/stock-watchlist"


def fetch_live_file(path):
    """讀「目前已發布網頁」的某個檔案原始內容，讀不到就回傳 None。用來保留只有另外的
    workflow（intraday-quotes.yml）才會更新、這裡不會重新產生的檔案——不然 daily.yml
    每次重新發布整個 site/ 目錄時，會把這些檔案整個蓋掉／刪掉，直到下次 intraday 執行
    才會補回來，中間這段時間網頁會抓不到（例如「我的持股」的盤中報價）。"""
    try:
        req = urllib.request.Request(f"{SITE_URL}/{path}?t={int(datetime.now().timestamp())}",
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read().decode("utf-8")
    except Exception:
        return None


def fetch_live_night():
    """讀「目前已發布網頁」的 market.json，只要 night（夜盤）那一欄——這樣重新產生網頁時
    才不會蓋掉 night_futures.py 每天早上另外更新的夜盤資料（這裡的 screener.py 不算夜盤，
    見 screener.py market_status() 的說明）。讀不到（還沒發布過、網路問題）就回傳 None，
    不影響網頁其他任何部分。"""
    try:
        req = urllib.request.Request(f"{SITE_URL}/market.json?t={int(datetime.now().timestamp())}",
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8")).get("night")
    except Exception:
        return None


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
    """同時符合 2 個（含）以上「不同種類」右側買進訊號的股票。跟每日/每週候選一樣，
    用固定顯示的標題＋說明＋表格，不用可收合的 <details>。"""
    found = []
    for st in stocks:
        right = [g for g in st["sigs"] if g["side"] == "右側"]
        kinds = {g["name"] for g in right}
        if len(kinds) >= 2 and "過熱，不追" not in st["tags"]:   # 過熱的不列入「可買進」
            found.append((len(kinds), st, right))
    found.sort(key=lambda x: (-x[0], x[1]["code"]))
    if not found:
        table = '<div class="tablewrap"><div class="empty">目前沒有同時符合 2 個以上買進訊號的股票。</div></div>'
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
        table = ('<div class="tablewrap"><table class="multi"><thead><tr><th class="l">代號</th><th class="l">名稱</th>'
                 '<th>收盤</th><th class="l">符合的訊號與價位</th><th class="l">連結</th></tr></thead>'
                 f"<tbody>{rows}</tbody></table></div>")
    return (f'<h2>多重訊號：同時符合 2 個（含）以上買進訊號（{len(found)} 檔）</h2>'
            '<div class="sub">點代號可看每個訊號的詳細理由。</div>'
            f'{table}')


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

    data = {"date": stocks["date"], "stocks": stocks["stocks"], "recorded": sorted(hist_file["days"])}

    # 給盤中報價用的輕量清單（只有代號和市場別），讓 quotes.py 不用下載整份 stocks.json
    universe = [{"code": s["code"], "mkt": s["mkt"]} for s in stocks["stocks"]]
    (SITE_DIR / "universe.json").write_text(json.dumps(universe, separators=(",", ":")), encoding="utf-8")

    # 三大法人買賣超的滾動紀錄：screener.py 已經算好存在 BASE_DIR，這裡複製進 site/
    # 讓下次執行能讀回（跟 quotes.json 一樣，缺少也不影響網頁其他部分）
    inst_path = BASE_DIR / "inst_history.json"
    if inst_path.exists():
        (SITE_DIR / "inst_history.json").write_text(inst_path.read_text(encoding="utf-8"), encoding="utf-8")

    # 服務條款／隱私權政策是獨立的靜態頁面（不是樣板產生的），原封不動複製進 site/
    for name in ("terms.html", "privacy.html"):
        src = BASE_DIR / name
        if src.exists():
            (SITE_DIR / name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    # 盤中報價（quotes.json）只有 intraday-quotes.yml 會更新，這裡（daily.yml）不會重算；
    # 沒有這一段的話，daily.yml 每次發布都會把它整個蓋掉，要等到下一次 intraday 執行
    # （最快 30 分鐘後，若已過盤中時段就要等到隔天開盤）才會補回來，這段時間「我的持股」
    # 會抓不到盤中報價。讀不到（還沒發布過、網路問題）就跳過，不影響網頁其他部分。
    live_quotes = fetch_live_file("quotes.json")
    if live_quotes:
        (SITE_DIR / "quotes.json").write_text(live_quotes, encoding="utf-8")

    market_html = ""
    try:
        m = json.loads((BASE_DIR / "market.json").read_text(encoding="utf-8"))
        live_night = fetch_live_night()          # 延續 night_futures.py 每天早上更新的夜盤，不被這次蓋掉
        if isinstance(live_night, dict) and "label" in live_night:
            m["night"] = live_night
        elif "night" in m and "label" not in m["night"]:
            m.pop("night", None)   # 改版前的舊格式（TAIFEX 資料，用 month 欄位），先拿掉避免顯示出錯，
                                    # 等 night_futures.py 下次跑完就會補上新格式
        (SITE_DIR / "market.json").write_text(
            json.dumps(m, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

        def pct_badge(pct):   # 統一的漲跌 %：紅色▲／綠色▼，跟觀察清單、持股的顏色邏輯一致
            arrow, cls = ("▲", "up") if pct > 0 else (("▼", "down") if pct < 0 else ("－", ""))
            return f'<span class="{cls}">{arrow} {abs(pct)}%</span>'

        rows = [f'<div><b>日期</b>：{m["date"]}</div>',
                f'<div><b>大盤</b>：加權 {m["close"]:,.0f}'
                + (f'　{pct_badge(m["chg_pct"])}' if m.get("chg_pct") is not None else '')
                + f'（月線 {m["ma20"]:,.0f}／季線 {m["ma60"]:,.0f}）{html.escape(m["text"])}</div>']
        # 櫃買、夜盤都是「現價快照」，Yahoo 沒提供完整歷史資料，只能顯示現價漲跌，不像大盤（加權指數）
        # 能算月線/季線判斷多空；個別欄位格式不對就跳過那一行，不影響日期/大盤這兩行一定會顯示
        try:
            if "otc" in m:
                d = m["otc"]
                rows.append(f'<div><b>櫃買</b>：{d["close"]:,.2f}　{pct_badge(d["chg_pct"])}</div>')
        except Exception:
            pass
        try:
            if "night" in m:
                d = m["night"]
                rows.append(f'<div><b>夜盤</b>：{d["label"]} {d["close"]:,.0f}　{pct_badge(d["chg_pct"])}　'
                            f'<small style="color:var(--muted)">（{d["date"]} 收盤）</small></div>')
        except Exception:
            pass
        # id="market"：讓網頁載入後，client 端 JS 再讀一次 market.json 覆蓋這裡的內容，
        # 這樣才會抓到 night_futures.py 在網頁「這次產生之後」才更新的夜盤（見 template.html 的 loadMarket()）
        market_html = f'<div class="market" id="market">{"".join(rows)}</div>'
    except Exception:
        pass                                    # 沒有大盤資料就不顯示這一塊

    # 個股資料若比整體最新日期舊（Yahoo／證交所那天資料剛好延遲），screener.py 已經在
    # 個股標籤加註「資料延遲」；這裡另外抓比例夠高時在最上方加一則明顯提示，避免被埋沒
    stale = sum(1 for s in stocks["stocks"] if s.get("date") and s["date"] != stocks["date"])
    if stocks["stocks"] and stale / len(stocks["stocks"]) >= 0.1:
        market_html += (f'<div class="market" style="border-color:var(--warn)">'
                        f'<b>注意</b>：{stale} / {len(stocks["stocks"])} 檔股票的資料還停在前一個交易日'
                        f'（Yahoo 或證交所資料延遲，不是這個工具的問題），個股標籤會標「資料延遲」，'
                        f'這些股票的數字請晚點再看。</div>')

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
