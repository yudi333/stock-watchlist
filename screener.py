"""
選股器：每天（日線）與每週（週線）從「成交金額前 N 名」的股票中，
用「20 種買進訊號」課程的邏輯（見 signals.py）挑出候選股，
並算出 買進區間 / 停損價 / 停利價 / 賺賠比 與白話理由。

只做資料分析，不會下單。結果僅供參考，不是投資建議。

用法：python screener.py
輸出：candidates_daily.csv、candidates_weekly.csv、market.json（網頁會讀取）
"""

import json
import re
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
import yfinance as yf

import institutional
from signals import DETECTORS, RISK_REWARD, Ctx, build_trade, calc_kd
from stock_checker import BASE_DIR, calc_rsi, drop_unfinished_today, judge, load_config

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
    """回傳所有 4 碼個股（上市 + 上櫃）：[{code, name, sym, mkt, value, price}]。"""
    items = []
    sources = [
        ("上市", "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
         "Code", "Name", "TradeValue", "ClosingPrice", ".TW", "TWSE"),
        ("上櫃", "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes",
         "SecuritiesCompanyCode", "CompanyName", "TransactionAmount", "Close", ".TWO", "TPEX"),
    ]
    for label, url, ck, nk, vk, pk, suffix, mkt in sources:
        try:
            for c, n, _, v, pr in fetch_market(url, ck, nk, vk, pk, suffix):
                # 只留 4 碼、且不是 0 開頭的（0 開頭是 ETF）
                if re.fullmatch(r"[1-9]\d{3}", c):
                    items.append(dict(code=c, name=n, sym=c + suffix, mkt=mkt, value=v, price=pr))
        except Exception as e:  # 其中一邊失敗，不影響另一邊
            print(f"[注意] 抓不到{label}行情：{e}")
    if not items:
        print("[注意] 官方行情都抓不到，改掃內建的大型權值股清單。")
        items = [dict(code=c, name=n, sym=c + ".TW", mkt="TWSE", value=1e12 - i, price=999)
                 for i, (c, n) in enumerate(FALLBACK.items())]
    return items


# ---------------------------------------------------------------
# 2. 判斷單一檔：套用課程的訊號，符合就回傳結果 dict，不符合回傳 None
# ---------------------------------------------------------------
POS_TEXT = {"in": "現價在買進區間內", "above": "現價高於區間，勿追，等回測", "below": "現價低於區間，尚未到買點"}


def position(close, lo, hi):
    """現價相對於買進區間：in 區間內 / above 高於（別追）/ below 低於（還沒到）。"""
    return "in" if lo <= close <= hi else "above" if close > hi else "below"


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
    for t in matches:
        t["pos"] = position(x.c[-1], t["zone_lo"], t["zone_hi"])
    return {
        "型態": main["signal"], "側別": main["side"], "收盤": round(x.c[-1], 2),
        "買進下限": round(main["zone_lo"], 2), "買進上限": round(main["zone_hi"], 2),
        "現價位置": POS_TEXT[main["pos"]], "停損價": round(main["stop"], 2),
        "停利價": round(main["target"], 2), "風險%": round(main["risk_pct"], 1),
        "報酬%": round(main["reward_pct"], 1), "賺賠比": round(main["rr"], 1),
        "RSI": round(rsi), "量比": round(x.vol_ratio, 2),
        "距52週高%": round((x.c[-1] / high52 - 1) * 100, 1),
        "理由": "；".join([main["reason"]] + notes),
        # 右側先；現價在區間內的先於「高於區間」；優先度高先；加分多先
        "_sort": is_left * 1000 + (main["pos"] != "in") * 200 + main["prio"] * 10 - bonus,
        "_matches": matches,
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
    """大盤（加權指數）強弱：課程提醒「大盤在漲時，個股漲的機會較大」。寫入 market.json 給網頁用。
    回傳實際的交易日期（給 fetch_institutional_notes 用，比系統時鐘準——排程萬一延遲跨到隔天，
    系統日期就會跟股票資料所屬的交易日對不上）；抓不到就回傳 None。"""
    try:
        c = yf.Ticker("^TWII").history(period="1y")["Close"].dropna()
        c = c[c.index.dayofweek < 5]        # 剔除週末假資料
        c = drop_unfinished_today(c.to_frame()).iloc[:, 0]   # 盤中不採用今天還沒收完的價格
        ma20, ma60 = c.rolling(20).mean().iloc[-1], c.rolling(60).mean().iloc[-1]
        if c.iloc[-1] > ma20 and c.iloc[-1] > ma60:
            text = "偏強：加權指數站上月線與季線，順勢操作機會較大"
        elif c.iloc[-1] > ma60:
            text = "中性：加權指數在季線之上但跌破月線，進場宜保守"
        else:
            text = "偏弱：加權指數跌破季線，個股上漲機會較小，買進請更保守"
        date = c.index[-1].strftime("%Y-%m-%d")
        info = {"date": date, "close": round(float(c.iloc[-1]), 2),
                "ma20": round(float(ma20), 2), "ma60": round(float(ma60), 2), "text": text}
        (BASE_DIR / "market.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
        print(f"大盤：{text}")
        return date
    except Exception as e:
        print(f"[注意] 抓不到大盤資料：{e}")
        return None


def fetch_institutional_notes(trade_date):
    """三大法人買賣超（加分項，抓不到就回傳 {}，不影響其他任何功能）。
    讀線上滾動紀錄 → 併入今天 → 存回 BASE_DIR（build_site.py 會複製到 site/ 供下次讀取）→
    算出連續買賣超天數的提示文字。trade_date：實際交易日（來自 market_status()），
    比系統時鐘準——不然排程萬一延遲跨到隔天，系統日期就會跟股票資料對不上。"""
    today = trade_date or datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    try:
        today_data = institutional.fetch_today(today)
        rolling = institutional.update_rolling(institutional.load_rolling(), today, today_data)
        (BASE_DIR / "inst_history.json").write_text(
            json.dumps(rolling, separators=(",", ":")), encoding="utf-8")
        notes = institutional.compute_notes(rolling, today)
        print(f"三大法人買賣超：今天抓到 {len(today_data)} 檔，{len(notes)} 檔連續買／賣超達提示門檻")
        return notes
    except Exception as e:
        print(f"[注意] 三大法人資料處理失敗（不影響其他功能）：{e}")
        return {}


def r2(v):
    return round(float(v), 2)


def signal_rows(tf, res):
    """把 evaluate 的所有符合訊號轉成網頁用的簡單 dict。"""
    if not res:
        return []
    return [dict(tf=tf, name=t["signal"], side=t["side"], lo=r2(t["zone_lo"]), hi=r2(t["zone_hi"]), pos=t["pos"],
                 stop=r2(t["stop"]),
                 target=r2(t["target"]), rr=round(t["rr"], 1), risk=round(t["risk_pct"], 1),
                 gain=round(t["reward_pct"], 1),
                 reason=t["reason"] + ("；" + t["extra"] if t["extra"] and not t["extra"].startswith("盤整") else ""))
            for t in res["_matches"]]


def advise(tags, sigs):
    """綜合判斷：回傳 (等級, 白話建議)。等級：buy / left / watch / avoid。"""
    right = [x for x in sigs if x["side"] == "右側"]
    left = [x for x in sigs if x["side"] == "左側"]
    if right:
        txt = f"符合 {len(right)} 個買進訊號，可留意買進"
        if "轉弱，暫不買" in tags:
            txt += "（股價仍在季線之下，屬底部型態，風險較高）"
        return "buy", txt
    if left:
        return "left", "只有左側（提前布局）訊號，風險高，僅適合小量或先觀察"
    if "過熱，不追" in tags:
        return "avoid", "過熱，不追"
    if "轉弱，暫不買" in tags:
        return "avoid", "轉弱（跌破季線），暫不買"
    if "多方" in tags:
        return "watch", "趨勢偏多，但目前沒有進場訊號，等回測均線或突破再看"
    return "watch", "站上季線但季線未向上，觀望"


HIST_DAYS = 6            # 網頁上每檔股票額外保存前幾個交易日的狀態（預設收合，點開可回測）


def snapshot(df):
    """算 df「最後一根」當天的狀態。回看歷史時，df 會被截掉最後幾天再呼叫，等於回到當天判斷。"""
    close = df["Close"]
    ma60 = close.rolling(60).mean()
    last, prev = close.iloc[-1], close.iloc[-2]
    rsi = calc_rsi(close).iloc[-1]
    avg_vol = df["Volume"].iloc[-21:-1].mean()
    row = {"close": last, "ma60": ma60.iloc[-1], "ma60_up": bool(ma60.iloc[-1] > ma60.iloc[-6]),
           "day_change_pct": (last / prev - 1) * 100, "rsi": rsi}
    tags = judge(row).split("；")
    rd, rw = evaluate(df, DAILY), evaluate(to_weekly(df), WEEKLY)
    sigs = signal_rows("日線", rd) + signal_rows("週線", rw)
    level, advice = advise(tags, sigs)
    core = dict(date=df.index[-1].strftime("%Y-%m-%d"), close=r2(last),
                chg=round(row["day_change_pct"], 2), vs60=round((last / ma60.iloc[-1] - 1) * 100, 1),
                rsi=round(rsi), vr=round(df["Volume"].iloc[-1] / avg_vol, 2) if avg_vol > 0 else 0,
                fh=round((last / df["High"].iloc[-252:].max() - 1) * 100, 1),
                tags=tags, level=level, advice=advice, sigs=sigs)
    return core, rd, rw


def analyze_stock(item, df, inst_notes=None):
    """分析一檔：回傳 (網頁用紀錄, 日線結果, 週線結果, 資料日期)；資料不足回傳 None。
    inst_notes：{代號: "三大法人連買超 N 天"} 這種加分提示，只套用在「今天」，不影響歷史回看。"""
    df = df.dropna(subset=["Close"])
    df = df[df.index.dayofweek < 5]        # 剔除週末假資料
    df = drop_unfinished_today(df)         # 盤中不採用今天還沒收完的 K 棒
    if len(df) < 130:
        return None                        # 上市不到半年，資料不足
    core, rd, rw = snapshot(df)
    date = core.pop("date")
    note = (inst_notes or {}).get(item["code"])
    if note:
        core["tags"] = core["tags"] + [note]           # 一般標籤（觀察清單／持股都看得到）
        for r in (rd, rw):
            if r:
                r["理由"] += "；" + note                 # 候選股與搜尋結果的理由欄
    rec = dict(code=item["code"], name=item["name"], mkt=item["mkt"], **core)

    # 前 HIST_DAYS 個交易日：回到當天重新判斷一次（用精簡欄位，減少網頁大小）
    hist = []
    for k in range(1, HIST_DAYS + 1):
        sub = df.iloc[:-k]
        if len(sub) < 130:
            break
        h, _, _ = snapshot(sub)
        hist.append(dict(d=h["date"], c=h["close"], g=h["chg"], v=h["vs60"], r=h["rsi"], l=h["level"],
                         t=h["tags"], s=[[g["name"], g["side"], g["tf"], g["lo"], g["hi"], g["stop"], g["target"]]
                                         for g in h["sigs"]]))
    rec["hist"] = hist
    return rec, rd, rw, date


def main():
    try:
        watchlist, _, _, _ = load_config()
        stocks = get_universe()
    except Exception as e:
        print(f"取得股票清單失敗：{e}")
        return 1
    # 候選股只從「成交金額前 N 名」挑（流動性夠），並排除你已在觀察的
    ranked = sorted((s for s in stocks if s["price"] >= MIN_PRICE), key=lambda s: s["value"], reverse=True)
    liquid = {s["code"] for s in ranked[:UNIVERSE_SIZE]} - set(watchlist)
    trade_date = market_status()
    inst_notes = fetch_institutional_notes(trade_date)
    print(f"分析全部 {len(stocks)} 檔（候選股從成交金額前 {UNIVERSE_SIZE} 名挑，{len(liquid)} 檔）...")

    results = {"daily": [], "weekly": []}
    records, data_date, failed = [], "", 0
    CHUNK = 100                             # 分批下載，避免一次要太多被擋
    for i in range(0, len(stocks), CHUNK):
        part = stocks[i:i + CHUNK]
        try:
            data = yf.download([s["sym"] for s in part], period="2y", auto_adjust=False,
                               group_by="ticker", progress=False, threads=True)
        except Exception as e:
            print(f"[注意] 第 {i // CHUNK + 1} 批下載失敗：{e}")
            failed += len(part)
            continue
        for item in part:
            try:
                out = analyze_stock(item, data[item["sym"]], inst_notes)
                if not out:
                    continue
                rec, rd, rw, date = out
                records.append(rec)
                data_date = max(data_date, date)
                if item["code"] in liquid:
                    for key, r in (("daily", rd), ("weekly", rw)):
                        if r:
                            results[key].append({"代號": item["code"], "名稱": item["name"],
                                                 "Yahoo代號": item["sym"], "資料日期": date, **r})
            except Exception:
                failed += 1                 # 單檔失敗不影響其他檔
        print(f"  已處理 {min(i + CHUNK, len(stocks))}/{len(stocks)}", flush=True)

    (BASE_DIR / "stocks.json").write_text(
        json.dumps({"date": data_date, "stocks": records}, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8")
    print(f"\n已存 stocks.json：{len(records)} 檔可搜尋")

    for key, title in (("daily", "每日"), ("weekly", "每週")):
        picks, per = [], {}
        for r in sorted(results[key], key=lambda r: r["_sort"]):
            if per.get(r["型態"], 0) < MAX_PER_SIGNAL and len(picks) < MAX_PICKS:
                picks.append(r)
                per[r["型態"]] = per.get(r["型態"], 0) + 1
        out = pd.DataFrame(picks).drop(columns=["_sort", "_matches"], errors="ignore")
        if out.empty:
            out = pd.DataFrame(columns=["代號", "名稱", "Yahoo代號", "資料日期", "型態", "側別", "收盤",
                                        "買進下限", "買進上限", "現價位置", "停損價", "停利價", "風險%", "報酬%", "賺賠比", "RSI", "量比",
                                        "距52週高%", "理由"])
        out.to_csv(BASE_DIR / f"candidates_{key}.csv", index=False, encoding="utf-8-sig")
        print(f"\n【{title}候選】{len(out)} 檔")
        for _, r in out.iterrows():
            print(f"  {r['代號']} {r['名稱']}［{r['型態']}］買 {r['買進下限']}～{r['買進上限']}（{r['現價位置']}，收 {r['收盤']}）  "
                  f"停損 {r['停損價']}（-{r['風險%']}%）  停利 {r['停利價']}（+{r['報酬%']}%）  賺賠比 {r['賺賠比']}")
    if failed:
        print(f"\n[注意] {failed} 檔資料有問題，已略過。")
    print("\n提醒：以上僅為資料篩選，不是投資建議，本程式不會下單。")
    return 0 if records else 1


if __name__ == "__main__":
    sys.exit(main())
