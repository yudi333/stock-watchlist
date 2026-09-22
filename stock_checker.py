"""
台股觀察清單每日檢查工具
------------------------
只做資料分析與提示，不會下單，也不會連接任何券商帳戶。
訊號僅供參考，不是投資建議。

流程：讀 config.yaml -> 用 yfinance 抓日線 -> 計算指標 -> 判斷訊號
      -> 印出摘要表 -> 存 K 線圖到 output/ -> 存 report.csv
"""

import sys
import unicodedata
from pathlib import Path

import pandas as pd
import yaml
import yfinance as yf

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.yaml"
OUTPUT_DIR = BASE_DIR / "output"
REPORT_FILE = BASE_DIR / "report.csv"

# ---- 訊號門檻（想調整就改這裡）----
HOT_DAILY_GAIN = 0.07   # 單日漲幅超過 7% 算過熱
HOT_RSI = 75            # RSI 超過 75 算過熱
MA60_LOOKBACK = 5       # 季線方向：和幾天前比

# matplotlib 只有畫圖（plot_chart）才用得到，且是比較重的套件，改成用到才載入：
# screener.py／build_site.py 這些自動更新的流程只是共用這裡的計算函式，不畫圖，
# 這樣它們就不用安裝 matplotlib，GitHub Actions 的 pip install 步驟能跑快一點。
def _init_matplotlib():
    import matplotlib
    matplotlib.use("Agg")  # 只存圖、不開視窗（這樣在沒有螢幕的環境也能跑）
    import matplotlib.pyplot as plt
    # 中文字型：依序找電腦上有的第一個（Mac / Windows / Linux 都涵蓋）
    plt.rcParams["font.sans-serif"] = [
        "PingFang TC", "Heiti TC", "Microsoft JhengHei", "Noto Sans CJK TC",
        "Arial Unicode MS", "DejaVu Sans",
    ]
    plt.rcParams["axes.unicode_minus"] = False
    return plt


# ---------------------------------------------------------------
# 1. 讀設定檔
# ---------------------------------------------------------------
def load_config():
    with open(CONFIG_FILE, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    # 代號統一轉成文字（YAML 會把 2337 當成數字）
    watchlist = [str(c) for c in cfg.get("watchlist", [])]
    names = {str(k): str(v) for k, v in (cfg.get("names") or {}).items()}
    holdings = {}
    for h in cfg.get("holdings") or []:
        holdings[str(h["code"])] = {
            "buy_price": float(h["buy_price"]),
            "shares": int(h["shares"]),
        }
    stop_loss = float(cfg.get("stop_loss_pct", 0.08))
    return watchlist, names, holdings, stop_loss


# ---------------------------------------------------------------
# 2. 抓資料：先試上市 .TW，抓不到再試上櫃 .TWO
# ---------------------------------------------------------------
def drop_unfinished_today(df):
    """盤中（台灣時間 14:00 前）Yahoo 會給「今天還沒收完」的 K 棒，量與價都不完整，會誤導判斷，直接不用。"""
    now = pd.Timestamp.now(tz="Asia/Taipei")
    if len(df) and df.index[-1].date() == now.date() and now.hour < 14:
        return df.iloc[:-1]
    return df


def fetch_history(code):
    """回傳 (DataFrame, 使用的代號)；抓不到就丟出 ValueError。"""
    for suffix in (".TW", ".TWO"):
        symbol = code + suffix
        try:
            # auto_adjust=False：用「真實成交價」，才能和你的買進價比較
            df = yf.Ticker(symbol).history(period="1y", auto_adjust=False)
        except Exception:
            continue  # 網路或其他錯誤，換下一個後綴試試
        if df.empty:
            continue  # 這個後綴沒資料，換下一個
        df = df.dropna(subset=["Close"])
        # 台股週六日不開盤，但 Yahoo 偶爾會多給一列假資料，會讓漲跌與量比失真，直接剔除
        df = df[df.index.dayofweek < 5]
        df = drop_unfinished_today(df)
        if not df.empty:
            return df, symbol
    raise ValueError("抓不到資料（.TW 與 .TWO 都失敗，請檢查代號或網路）")


# ---------------------------------------------------------------
# 3. 計算指標
# ---------------------------------------------------------------
def calc_rsi(close, period=14):
    """RSI：0~100，越高代表近期漲得越多。>70 偏熱，<30 偏冷。"""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    # Wilder 平滑法（RSI 的標準算法）
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def add_indicators(df):
    df = df.copy()
    for n in (5, 20, 60):
        df[f"MA{n}"] = df["Close"].rolling(n).mean()  # 移動平均線
    df["VolMA20"] = df["Volume"].rolling(20).mean()   # 20 日均量
    df["RSI"] = calc_rsi(df["Close"])
    return df


# ---------------------------------------------------------------
# 4. 訊號判斷（白話文）
# ---------------------------------------------------------------
def judge(row):
    """row 是包含指標的 dict，回傳訊號文字（可能同時有多個，用「；」隔開）。"""
    signals = []

    # 季線判斷
    if row["close"] < row["ma60"]:
        signals.append("轉弱，暫不買")
    elif row["ma60_up"]:
        signals.append("多方")
    else:
        signals.append("站上季線但季線未向上，觀望")

    # 過熱判斷
    if row["day_change_pct"] > HOT_DAILY_GAIN * 100 or row["rsi"] > HOT_RSI:
        signals.append("過熱，不追")

    # 停損判斷（只有你有持股才會檢查）
    if row.get("stop_hit"):
        signals.append("達停損")

    return "；".join(signals)


def analyze(code, name, holding, stop_loss):
    """分析一檔股票，回傳 (結果 dict, 含指標的 DataFrame)。"""
    raw, symbol = fetch_history(code)
    if len(raw) < 65:
        raise ValueError(f"資料只有 {len(raw)} 天，不足以計算季線（至少需 65 天）")
    df = add_indicators(raw)
    last, prev = df.iloc[-1], df.iloc[-2]

    r = {
        "code": code,
        "name": name,
        "symbol": symbol,
        "date": df.index[-1].strftime("%Y-%m-%d"),
        "close": round(last["Close"], 2),
        "day_change_pct": round((last["Close"] / prev["Close"] - 1) * 100, 2),
        "ma5": round(last["MA5"], 2),
        "ma20": round(last["MA20"], 2),
        "ma60": round(last["MA60"], 2),
        # 季線方向：今天的 60MA 是否高於 5 天前的 60MA
        "ma60_up": bool(last["MA60"] > df["MA60"].iloc[-1 - MA60_LOOKBACK]),
        "vs_ma60_pct": round((last["Close"] / last["MA60"] - 1) * 100, 2),
        "volume_ratio": round(last["Volume"] / last["VolMA20"], 2),
        "rsi": round(last["RSI"], 1),
        # 距 52 週高點的跌幅（負數 = 比高點低多少 %）
        "from_high_pct": round((last["Close"] / df["High"].max() - 1) * 100, 2),
    }

    # 持股資訊（選填）
    r["buy_price"] = r["shares"] = r["pnl_pct"] = None
    r["stop_hit"] = False
    if holding:
        r["buy_price"] = holding["buy_price"]
        r["shares"] = holding["shares"]
        r["pnl_pct"] = round((last["Close"] / holding["buy_price"] - 1) * 100, 2)
        r["stop_hit"] = bool(last["Close"] <= holding["buy_price"] * (1 - stop_loss))

    r["signal"] = judge(r)
    return r, df


# ---------------------------------------------------------------
# 5. 畫 K 線圖（含 5/20/60MA 與成交量）
# ---------------------------------------------------------------
def plot_chart(df, r):
    plt = _init_matplotlib()  # 第一次呼叫才載入 matplotlib
    UP, DOWN = "#d6336c", "#2f9e6b"  # 台股慣例：紅漲綠跌
    MA_COLORS = {"MA5": "#f59f00", "MA20": "#1c7ed6", "MA60": "#7048e8"}

    d = df.tail(150)  # 只畫最近約 7 個月，K 棒才看得清楚
    x = range(len(d))  # 用序號當 x 軸，避免假日產生空隙
    fig, (ax, axv) = plt.subplots(
        2, 1, figsize=(11, 6.5), sharex=True,
        gridspec_kw={"height_ratios": [3, 1], "hspace": 0.05},
    )

    # K 棒：影線 = 最高~最低，實體 = 開盤~收盤
    colors = [UP if c >= o else DOWN for o, c in zip(d["Open"], d["Close"])]
    ax.vlines(x, d["Low"], d["High"], color=colors, linewidth=1)
    ax.bar(x, (d["Close"] - d["Open"]).abs(), bottom=d[["Open", "Close"]].min(axis=1),
           color=colors, width=0.7)
    for name, c in MA_COLORS.items():
        ax.plot(x, d[name], color=c, linewidth=1.6, label=name + ("（季線）" if name == "MA60" else ""))

    # 成交量
    axv.bar(x, d["Volume"] / 1000, color=colors, width=0.7)
    axv.set_ylabel("成交量(張)")

    ax.set_title(f"{r['code']} {r['name']}　收盤 {r['close']}　{r['signal']}", fontsize=13)
    ax.legend(loc="upper left", frameon=False, ncol=3)
    ax.grid(alpha=0.2)
    axv.grid(alpha=0.2)
    for a in (ax, axv):
        a.spines[["top", "right"]].set_visible(False)

    # x 軸只標幾個日期
    step = max(len(d) // 6, 1)
    ticks = list(range(0, len(d), step))
    axv.set_xticks(ticks)
    axv.set_xticklabels([d.index[i].strftime("%Y-%m-%d") for i in ticks])

    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / f"{r['code']}.png"
    fig.savefig(path, dpi=110, bbox_inches="tight")
    plt.close(fig)


# ---------------------------------------------------------------
# 6. 輸出：終端機表格 + CSV
# ---------------------------------------------------------------
def display_width(s):
    """中文字在終端機佔 2 格寬，要另外算才對得齊。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def pad(s, width):
    return s + " " * (width - display_width(s))


def print_table(rows):
    headers = ["代號", "名稱", "收盤", "離季線%", "RSI", "訊號"]
    table = [headers] + [
        [r["code"], r["name"], f"{r['close']:.2f}", f"{r['vs_ma60_pct']:+.1f}%",
         f"{r['rsi']:.0f}", r["signal"]]
        for r in rows
    ]
    widths = [max(display_width(line[i]) for line in table) for i in range(len(headers))]
    for i, line in enumerate(table):
        print("  ".join(pad(cell, widths[j]) for j, cell in enumerate(line)))
        if i == 0:
            print("-" * (sum(widths) + 2 * (len(widths) - 1)))


def save_csv(rows):
    cols = {
        "code": "代號", "name": "名稱", "symbol": "Yahoo代號", "date": "資料日期",
        "close": "收盤", "day_change_pct": "單日漲跌%",
        "ma5": "5MA", "ma20": "20MA", "ma60": "60MA(季線)",
        "ma60_up": "季線向上", "vs_ma60_pct": "離季線%",
        "volume_ratio": "量比(對20日均量)", "rsi": "RSI14",
        "from_high_pct": "距52週高%", "buy_price": "買進價", "shares": "股數",
        "pnl_pct": "持股損益%", "signal": "訊號",
    }
    df = pd.DataFrame(rows)[list(cols)].rename(columns=cols)
    # utf-8-sig：用 Excel 開啟中文才不會亂碼
    df.to_csv(REPORT_FILE, index=False, encoding="utf-8-sig")


# ---------------------------------------------------------------
# 主程式
# ---------------------------------------------------------------
def main():
    try:
        watchlist, names, holdings, stop_loss = load_config()
    except Exception as e:
        print(f"讀取 config.yaml 失敗：{e}")
        return 1

    rows, errors = [], []
    for code in watchlist:
        print(f"分析 {code} ...", flush=True)
        try:
            r, df = analyze(code, names.get(code, code), holdings.get(code), stop_loss)
            rows.append(r)
            plot_chart(df, r)
        except Exception as e:  # 單一檔失敗不影響其他檔
            errors.append((code, str(e)))

    print()
    if rows:
        print_table(rows)
        save_csv(rows)
        print(f"\n已存檔：{REPORT_FILE.name}、{OUTPUT_DIR.name}/（{len(rows)} 張 K 線圖）")
    for code, msg in errors:
        print(f"[錯誤] {code}：{msg}")
    print("\n提醒：以上僅為資料分析，不是投資建議，本程式不會下單。")
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
