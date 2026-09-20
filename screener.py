"""
選股器：每天（日線）與每週（週線）從「成交金額前 N 名」的股票中，
挑出符合條件的候選股，並算出 買進價 / 停損價 / 停利價 與白話理由。

只做資料分析，不會下單。結果僅供參考，不是投資建議。

用法：python screener.py
輸出：candidates_daily.csv、candidates_weekly.csv（網頁會讀取這兩個檔）
"""

import re
import sys

import pandas as pd
import requests
import yfinance as yf

from stock_checker import BASE_DIR, calc_rsi, load_config

# ---- 想調整就改這裡 ----
UNIVERSE_SIZE = 150      # 掃描成交金額前幾名（上市 + 上櫃合計）
MIN_PRICE = 20           # 股價低於這個數字的不看（太便宜的雞蛋水餃股）
MAX_PICKS = 10           # 每個清單最多列幾檔
RISK_REWARD = 2.0        # 停利 = 買進價 + 風險 × 這個倍數（2 = 賠 1 元要賺 2 元才值得）

# 兩組規則：日線看短波段，週線看中期趨勢。數字意義見 evaluate() 內註解
DAILY = dict(
    label="日線", unit="日", ma_fast=20, ma_slow=60, up_lookback=5,
    rsi_range=(45, 70), max_from_high=15, pullback_pct=4, break_n=20,
    vol_n=20, break_vol=1.5, stop_n=10, risk_range=(3, 8), max_jump=7,
)
WEEKLY = dict(
    label="週線", unit="週", ma_fast=10, ma_slow=13, up_lookback=4,
    rsi_range=(50, 72), max_from_high=12, pullback_pct=6, break_n=8,
    vol_n=10, break_vol=1.2, stop_n=4, risk_range=(4, 12), max_jump=15,
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
# 2. 判斷單一檔：符合就回傳結果 dict，不符合回傳 None
# ---------------------------------------------------------------
def evaluate(df, p):
    """df：日線或週線資料；p：DAILY 或 WEEKLY 規則。"""
    if len(df) < p["ma_slow"] + p["up_lookback"] + 2:
        return None
    close, unit = df["Close"], p["unit"]
    ma_fast = close.rolling(p["ma_fast"]).mean()
    ma_slow = close.rolling(p["ma_slow"]).mean()   # 日線=季線(60MA)；週線=13 週線(也是一季)
    rsi = calc_rsi(close).iloc[-1]
    last = close.iloc[-1]
    prev = close.iloc[-2]
    high52 = df["High"].max()
    from_high = (last / high52 - 1) * 100                 # 負數 = 比高點低多少 %
    jump = (last / prev - 1) * 100
    vol_ratio = df["Volume"].iloc[-1] / df["Volume"].iloc[-1 - p["vol_n"]:-1].mean()

    # --- 條件 A：趨勢向上（站上長均線、長均線向上、短均線在長均線之上）---
    if not (last > ma_slow.iloc[-1]
            and ma_slow.iloc[-1] > ma_slow.iloc[-1 - p["up_lookback"]]
            and ma_fast.iloc[-1] > ma_slow.iloc[-1]):
        return None
    # --- 條件 B：不過熱（RSI 在區間內、單根漲幅沒有太誇張）---
    if not (p["rsi_range"][0] <= rsi <= p["rsi_range"][1]) or jump > p["max_jump"]:
        return None
    # --- 條件 C：離 52 週高點不遠（強勢股）---
    if from_high < -p["max_from_high"]:
        return None

    # --- 條件 D：要有明確的進場型態：回測 或 突破 ---
    dist_fast = (last / ma_fast.iloc[-1] - 1) * 100       # 離短均線 %
    prior_high = df["High"].iloc[-1 - p["break_n"]:-1].max()
    if 0 <= dist_fast <= p["pullback_pct"]:
        setup = "回測"
        setup_reason = (f"回測 {p['ma_fast']}{unit}均線附近（僅高於均線 {dist_fast:.1f}%），"
                        f"拉回沒跌破，風險相對小")
    elif last > prior_high and vol_ratio >= p["break_vol"]:
        setup = "突破"
        setup_reason = (f"突破近 {p['break_n']}{unit}高點，成交量是均量的 {vol_ratio:.1f} 倍，"
                        f"有資金進場")
    else:
        return None

    # --- 買進 / 停損 / 停利 ---
    entry = last
    # 停損：放在近幾根的最低點下方 1%（跌破代表這波走勢失敗）
    stop = df["Low"].iloc[-p["stop_n"]:].min() * 0.99
    risk_pct = (entry - stop) / entry * 100
    lo, hi = p["risk_range"]
    if risk_pct > hi:
        return None                        # 停損離太遠，單筆虧損太大，跳過
    if risk_pct < lo:                      # 停損太近容易被洗出場，至少留 lo%
        stop = entry * (1 - lo / 100)
        risk_pct = lo
    target = entry + (entry - stop) * RISK_REWARD
    reward_pct = (target / entry - 1) * 100

    reasons = [
        f"股價在 {p['ma_slow']}{unit}均線之上且均線向上，{p['ma_fast']}{unit}均線也在其上（多頭排列）",
        setup_reason,
        f"RSI {rsi:.0f}，還沒過熱",
        f"距 52 週高點只差 {abs(from_high):.1f}%，屬強勢股",
    ]
    return {
        "型態": setup, "收盤": round(last, 2),
        "買進價": round(entry, 2), "停損價": round(stop, 2), "停利價": round(target, 2),
        "風險%": round(risk_pct, 1), "報酬%": round(reward_pct, 1),
        "RSI": round(rsi), "量比": round(vol_ratio, 2), "距52週高%": round(from_high, 1),
        "理由": "；".join(reasons),
        "_sort": abs(from_high) + (0 if setup == "突破" else 1),  # 越接近高點排越前
    }


def to_weekly(df):
    """日線轉週線（週五結算）。"""
    return df.resample("W-FRI").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    ).dropna()


# ---------------------------------------------------------------
# 3. 主程式
# ---------------------------------------------------------------
def main():
    try:
        watchlist, _, _, _ = load_config()
        universe = [u for u in get_universe() if u[0] not in watchlist]  # 排除你已在觀察的
    except Exception as e:
        print(f"取得股票清單失敗：{e}")
        return 1
    print(f"掃描 {len(universe)} 檔（成交金額前 {UNIVERSE_SIZE} 名，已排除觀察清單）...")

    symbols = [u[2] for u in universe]
    try:
        data = yf.download(symbols, period="1y", auto_adjust=False, group_by="ticker",
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
            for key, rule, frame in (("daily", DAILY, df), ("weekly", WEEKLY, to_weekly(df))):
                r = evaluate(frame, rule)
                if r:
                    results[key].append({"代號": code, "名稱": name, "Yahoo代號": sym,
                                         "資料日期": df.index[-1].strftime("%Y-%m-%d"), **r})
        except Exception:
            failed += 1                          # 單檔失敗不影響其他檔

    for key, title in (("daily", "每日"), ("weekly", "每週")):
        picks = sorted(results[key], key=lambda r: r["_sort"])[:MAX_PICKS]
        out = pd.DataFrame(picks).drop(columns=["_sort"], errors="ignore")
        if out.empty:
            out = pd.DataFrame(columns=["代號", "名稱", "Yahoo代號", "資料日期", "型態", "收盤", "買進價",
                                        "停損價", "停利價", "風險%", "報酬%", "RSI", "量比",
                                        "距52週高%", "理由"])
        out.to_csv(BASE_DIR / f"candidates_{key}.csv", index=False, encoding="utf-8-sig")
        print(f"\n【{title}候選】{len(out)} 檔")
        for _, r in out.iterrows():
            print(f"  {r['代號']} {r['名稱']}［{r['型態']}］買 {r['買進價']}  "
                  f"停損 {r['停損價']}（-{r['風險%']}%）  停利 {r['停利價']}（+{r['報酬%']}%）")
    if failed:
        print(f"\n[注意] {failed} 檔資料有問題，已略過。")
    print("\n提醒：以上僅為資料篩選，不是投資建議，本程式不會下單。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
