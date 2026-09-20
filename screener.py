"""
選股器：每天（日線）與每週（週線）從「成交金額前 N 名」的股票中，
用「20 種買進訊號」課程的邏輯（見 signals.py）挑出候選股，
並算出 買進價 / 停損價 / 停利價 / 賺賠比 與白話理由。

只做資料分析，不會下單。結果僅供參考，不是投資建議。

用法：python screener.py
輸出：candidates_daily.csv、candidates_weekly.csv、market.json（網頁會讀取）
"""

import json
import re
import sys

import pandas as pd
import requests
import yfinance as yf

from signals import DETECTORS, RISK_REWARD, Ctx, build_trade, calc_kd
from stock_checker import BASE_DIR, calc_rsi, load_config

# ---- 想調整就改這裡 ----
UNIVERSE_SIZE = 150      # 掃描成交金額前幾名（上市 + 上櫃合計）
MIN_PRICE = 20           # 股價低於這個數字的不看（太便宜的雞蛋水餃股）
MAX_PICKS = 12           # 每個清單最多列幾檔
MAX_PER_SIGNAL = 4       # 同一種訊號最多列幾檔（避免被單一訊號洗版）

# 兩組參數：日線看短波段，週線看中期趨勢（週線的「根」= 週）
DAILY = dict(
    label="日線", unit="日", min_bars=130, ma_slow=60, up_lookback=5, pull_mas=(10, 20, 60),
    pullback_pct=4, recent_n=10, rsi_max=75, max_jump=7, vol_n=20, break_vol=1.3,
    y_bars=252, pivot_k=3, pivot_gap=8, box_min=15, box_range=0.15, tri_n=45, w_n=90,
    w_gap=8, hs_n=120, hs_recent=40, flag_len=(5, 25), pole_n=15, pole_gain=0.20,
    cup_n=120, handle_len=(3, 25), cup_min_side=10, mirror_n=60, mirror_pull_n=30,
    gap_look=10, gap_pct=0.03, fb_n=40, hammer_range=0.03, risk_range=(3, 8),
)
WEEKLY = dict(
    label="週線", unit="週", min_bars=70, ma_slow=13, up_lookback=4, pull_mas=(5, 10),
    pullback_pct=6, recent_n=5, rsi_max=78, max_jump=15, vol_n=10, break_vol=1.2,
    y_bars=52, pivot_k=2, pivot_gap=4, box_min=8, box_range=0.15, tri_n=24, w_n=45,
    w_gap=4, hs_n=60, hs_recent=20, flag_len=(3, 10), pole_n=8, pole_gain=0.25,
    cup_n=60, handle_len=(2, 8), cup_min_side=6, mirror_n=26, mirror_pull_n=12,
    gap_look=4, gap_pct=0.05, fb_n=20, hammer_range=0.06, risk_range=(4, 10),
)

HEADERS = {"User-Agent": "Mozilla/5.0"}


# ---------------------------------------------------------------
# 1. 取得股票清單：證交所（上市）+ 櫃買中心（上櫃）的公開資料
# ---------------------------------------------------------------
def to_float(x):
    try:
        return float(str(x).replace(",", ""))
    except ValueError:
        return 0.0


# 備用清單：官方資料抓不到時（例如雲端主機被擋）改掃這些大型權值股
FALLBACK = {
    "2330": "台積電", "2317": "鴻海", "2454": "聯發科", "2308": "台達電", "2382": "廣達",
    "2881": "富邦金", "2882": "國泰金", "2891": "中信金", "2412": "中華電", "2303": "聯電",
    "3711": "日月光投控", "2886": "兆豐金", "2884": "玉山金", "2885": "元大金", "2892": "第一金",
    "1301": "台塑", "1303": "南亞", "2002": "中鋼", "2603": "長榮", "2609": "陽明",
    "3034": "聯詠", "3231": "緯創", "2376": "技嘉", "2357": "華碩", "2379": "瑞昱",
    "3008": "大立光", "2395": "研華", "2327": "國巨", "2301": "光寶科", "3037": "欣興",
    "2345": "智邦", "2368": "金像電", "3443": "創意", "3035": "智原", "6669": "緯穎",
}


def fetch_market(url, code_key, name_key, value_key, price_key, suffix):
    """抓一個交易所的當日行情，回傳 [(代號, 名稱, 後綴, 成交金額, 收盤價)]。"""
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return [(d[code_key], d[name_key], suffix, to_float(d[value_key]), to_float(d[price_key]))
            for d in r.json()]


def get_universe():
    """回傳 [(代號, 名稱, Yahoo代號)]，依成交金額由大到小取前 UNIVERSE_SIZE 檔。"""
    items = []
    sources = [
        ("上市", "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
         "Code", "Name", "TradeValue", "ClosingPrice", ".TW"),
        ("上櫃", "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes",
         "SecuritiesCompanyCode", "CompanyName", "TransactionAmount", "Close", ".TWO"),
    ]
    for label, *args in sources:
        try:
            items += fetch_market(*args)
        except Exception as e:  # 其中一邊失敗，不影響另一邊
            print(f"[注意] 抓不到{label}行情：{e}")

    # 只留 4 碼、且不是 0 開頭的（0 開頭是 ETF），並排除低價股
    items = [i for i in items
             if re.fullmatch(r"[1-9]\d{3}", i[0]) and i[4] >= MIN_PRICE]
    if not items:
        print("[注意] 官方行情都抓不到，改掃內建的大型權值股清單。")
        return [(c, n, c + ".TW") for c, n in FALLBACK.items()]
    items.sort(key=lambda i: i[3], reverse=True)
    return [(c, n, c + s) for c, n, s, _, _ in items[:UNIVERSE_SIZE]]


# ---------------------------------------------------------------
# 2. 判斷單一檔：套用課程的訊號，符合就回傳結果 dict，不符合回傳 None
# ---------------------------------------------------------------
def evaluate(df, p):
    """df：日線或週線資料；p：DAILY 或 WEEKLY 參數。"""
    if len(df) < p["min_bars"]:
        return None
    x = Ctx(df, p)
    rsi = calc_rsi(df["Close"]).iloc[-1]
    jump = (x.c[-1] / x.c[-2] - 1) * 100

    # 全域過濾一：過熱不追（RSI 太高或單根暴漲）
    if rsi > p["rsi_max"] or jump > p["max_jump"]:
        return None
    # 全域過濾二：長上影線爆量收黑 = 出貨訊號（課程：尤其在高檔）
    rng = x.h[-1] - x.l[-1]
    if rng > 0 and (x.h[-1] - max(x.o[-1], x.c[-1])) / rng >= 0.5 \
            and x.c[-1] < x.o[-1] and x.vol_ratio >= 1.5:
        return None

    # 逐一檢查每個訊號；每個訊號的停損停利再依課程紀律算一次
    matches = []
    for det in DETECTORS:
        try:
            m = det(x)
        except Exception:
            m = None                       # 單一訊號算錯不影響其他訊號
        t = build_trade(m, p) if m else None
        if t:
            t["prio"] = DETECTORS.index(det)
            matches.append(t)
    if not matches:
        return None
    main = min(matches, key=lambda t: t["prio"])   # 優先順序：突破型態 > 一般進場 > 左側

    # 加分項：均線多頭排列、KD 黃金交叉、量能
    ma = {n: x.ma[n] for n in (5, 10, 20, 60)}
    aligned = all(not pd.isna(ma[n][-1]) for n in ma) and \
        ma[5][-1] > ma[10][-1] > ma[20][-1] > ma[60][-1] and \
        all(ma[n][-1] > ma[n][-1 - p["up_lookback"]] for n in ma)
    k, d = calc_kd(df)
    kd_cross = k.iloc[-2] <= d.iloc[-2] and k.iloc[-1] > d.iloc[-1]
    kd_up = k.iloc[-1] > k.iloc[-2] and d.iloc[-1] > d.iloc[-2]
    notes = []
    if aligned:
        notes.append("均線多頭排列（5>10>20>60 且全部向上）")
    if kd_cross:
        notes.append(f"KD 黃金交叉（K {k.iloc[-1]:.0f} / D {d.iloc[-1]:.0f}）")
    elif kd_up:
        notes.append(f"KD 同步向上（K {k.iloc[-1]:.0f} / D {d.iloc[-1]:.0f}）")
    if k.iloc[-1] > 80:
        notes.append("KD 在高檔（>80），追高風險增加")
    others = [t["signal"] for t in matches if t is not main]
    if others:
        notes.append("同時符合：" + "、".join(others))
    high52 = x.high52
    notes.append(f"RSI {rsi:.0f}，距 52 週高點 {abs(x.c[-1] / high52 - 1) * 100:.1f}%")
    if main["extra"] and not main["extra"].startswith("盤整"):
        notes.append(main["extra"])

    bonus = 3 * aligned + 1 * kd_cross + 1 * (x.vol_ratio >= 1.5)
    is_left = main["side"] == "左側"
    return {
        "型態": main["signal"], "側別": main["side"], "收盤": round(x.c[-1], 2),
        "買進價": round(main["entry"], 2), "停損價": round(main["stop"], 2),
        "停利價": round(main["target"], 2), "風險%": round(main["risk_pct"], 1),
        "報酬%": round(main["reward_pct"], 1), "賺賠比": round(main["rr"], 1),
        "RSI": round(rsi), "量比": round(x.vol_ratio, 2),
        "距52週高%": round((x.c[-1] / high52 - 1) * 100, 1),
        "理由": "；".join([main["reason"]] + notes),
        "_sort": is_left * 1000 + main["prio"] * 10 - bonus,   # 右側先、優先度高先、加分多先
    }


def to_weekly(df):
    """日線轉週線（週五結算）。"""
    return df.resample("W-FRI").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    ).dropna()


# ---------------------------------------------------------------
# 3. 主程式
# ---------------------------------------------------------------
def market_status():
    """大盤（加權指數）強弱：課程提醒「大盤在漲時，個股漲的機會較大」。寫入 market.json 給網頁用。"""
    try:
        c = yf.Ticker("^TWII").history(period="1y")["Close"].dropna()
        c = c[c.index.dayofweek < 5]        # 剔除週末假資料
        ma20, ma60 = c.rolling(20).mean().iloc[-1], c.rolling(60).mean().iloc[-1]
        if c.iloc[-1] > ma20 and c.iloc[-1] > ma60:
            text = "偏強：加權指數站上月線與季線，順勢操作機會較大"
        elif c.iloc[-1] > ma60:
            text = "中性：加權指數在季線之上但跌破月線，進場宜保守"
        else:
            text = "偏弱：加權指數跌破季線，個股上漲機會較小，買進請更保守"
        info = {"date": c.index[-1].strftime("%Y-%m-%d"), "close": round(float(c.iloc[-1]), 2),
                "ma20": round(float(ma20), 2), "ma60": round(float(ma60), 2), "text": text}
        (BASE_DIR / "market.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
        print(f"大盤：{text}")
    except Exception as e:
        print(f"[注意] 抓不到大盤資料：{e}")


def main():
    try:
        watchlist, _, _, _ = load_config()
        universe = [u for u in get_universe() if u[0] not in watchlist]  # 排除你已在觀察的
    except Exception as e:
        print(f"取得股票清單失敗：{e}")
        return 1
    market_status()
    print(f"掃描 {len(universe)} 檔（成交金額前 {UNIVERSE_SIZE} 名，已排除觀察清單）...")

    symbols = [u[2] for u in universe]
    try:
        data = yf.download(symbols, period="2y", auto_adjust=False, group_by="ticker",
                           progress=False, threads=True)
    except Exception as e:
        print(f"下載資料失敗：{e}")
        return 1

    results = {"daily": [], "weekly": []}
    failed = 0
    for code, name, sym in universe:
        try:
            df = data[sym].dropna(subset=["Close"])
            df = df[df.index.dayofweek < 5]     # 剔除週末假資料
            if len(df) < 130:
                continue                        # 上市不到半年，資料不足
            for key, rule, frame in (("daily", DAILY, df), ("weekly", WEEKLY, to_weekly(df))):
                r = evaluate(frame, rule)
                if r:
                    results[key].append({"代號": code, "名稱": name, "Yahoo代號": sym,
                                         "資料日期": df.index[-1].strftime("%Y-%m-%d"), **r})
        except Exception:
            failed += 1                          # 單檔失敗不影響其他檔

    for key, title in (("daily", "每日"), ("weekly", "每週")):
        picks, per = [], {}
        for r in sorted(results[key], key=lambda r: r["_sort"]):
            if per.get(r["型態"], 0) < MAX_PER_SIGNAL and len(picks) < MAX_PICKS:
                picks.append(r)
                per[r["型態"]] = per.get(r["型態"], 0) + 1
        out = pd.DataFrame(picks).drop(columns=["_sort"], errors="ignore")
        if out.empty:
            out = pd.DataFrame(columns=["代號", "名稱", "Yahoo代號", "資料日期", "型態", "側別", "收盤",
                                        "買進價", "停損價", "停利價", "風險%", "報酬%", "賺賠比", "RSI", "量比",
                                        "距52週高%", "理由"])
        out.to_csv(BASE_DIR / f"candidates_{key}.csv", index=False, encoding="utf-8-sig")
        print(f"\n【{title}候選】{len(out)} 檔")
        for _, r in out.iterrows():
            print(f"  {r['代號']} {r['名稱']}［{r['型態']}］買 {r['買進價']}  "
                  f"停損 {r['停損價']}（-{r['風險%']}%）  停利 {r['停利價']}（+{r['報酬%']}%）  賺賠比 {r['賺賠比']}")
    if failed:
        print(f"\n[注意] {failed} 檔資料有問題，已略過。")
    print("\n提醒：以上僅為資料篩選，不是投資建議，本程式不會下單。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
