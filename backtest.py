"""
回頭檢查 Google 試算表裡記錄過的候選股／多重訊號，後來的股價有沒有觸發停損或停利，
把結果直接補寫回原本那一列（不是另外開新分頁）：結果、結果日期、實際報酬%、持有天數。
只统计已經記錄過的建議後來的走勢，不會下單，也不影響網頁本身。

判斷方式：從訊號隔天開始往後看每天的最高/最低價（訊號是當天收盤才能確認的，隔天才可能真的
進場，不能用訊號當天盤中、收盤前就已經走完的高低點）——
  還沒進場（最低價沒跌到買進上限之下、或最高價沒漲到買進下限之上）：標記「尚未進場」
  進場後最低價 <= 停損價：「已停損」（當天最高價同時也 >= 停利價的話，保守當作先停損）
  進場後最高價 >= 停利價：「已停利」
  超過追蹤期限還沒發生：「過期未觸發」／「過期未進場」
  以上都還沒發生：「追蹤中」，下次繼續看
已經有終局結果（已停利/已停損/過期）的列不會再重查，只處理還在追蹤或還沒進場的列，
所以就算歷史紀錄累積很多筆，每次真正要重新抓資料的也只有一小部分。

這次執行新判定出「已停損」或「已停利」的列（本來是追蹤中/尚未進場，這次才變成終局結果），
會另外發一則 Telegram 通知到專門的群組（跟每天的候選股摘要分開，見下面的環境變數），
方便只想追蹤「結果」的人訂閱這個群組就好，不用每天收候選股清單。

用法（在 GitHub Actions 裡自動執行，見 daily.yml）：
  python backtest.py

需要環境變數（跟 sheets_log.py 共用同一組）：
  GOOGLE_SHEETS_KEY / GOOGLE_SHEETS_ID
停損/停利通知需要（跟 notify.py 共用同一個 bot，但另開一個群組/聊天室）：
  TG_BOT_TOKEN / TG_CHAT_ID_STOP
沒設定的話會直接略過，不會讓更新失敗。
"""

import json
import os
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone

import pandas as pd

from stock_checker import BASE_DIR, fetch_history

RESULT_COLS = ["結果", "結果日期", "實際報酬%", "持有天數"]
# 追蹤期限（交易日）：超過這麼多天還沒進場/沒觸發停損停利，就不再繼續無限期等下去
EXPIRE_DAYS = {"多重訊號": 60, "每日候選": 60, "每週候選": 120}
OPEN_STATUSES = {"", "追蹤中", "尚未進場"}   # 這些狀態的列才需要重新檢查
MIN_SAMPLE = 10   # 少於這個筆數的型態，樣本太少，勝率不穩定、不顯示，避免「3 戰 2 勝=67%」這種誤導

# 結果欄一旦定案（已停損/已停利），整列填滿底色，一眼就看得出哪些已經結束、不用再追——
# 跟台股「紅漲綠跌」一致：已停利（賺錢）用紅、已停損（賠錢）用綠，是 Google 試算表內建色盤
# 裡「淺紅色 3」「淺綠色 3」的 RGB 值（0~1 之間，不是 0~255，格式比照 sheets_log.py 的
# FILL_WHITE/FILL_GRAY）。
RESULT_FILL = {
    "已停利": {"red": 244 / 255, "green": 204 / 255, "blue": 204 / 255},   # 淺紅色 3
    "已停損": {"red": 217 / 255, "green": 234 / 255, "blue": 211 / 255},   # 淺綠色 3
}


def connect():
    key_json = os.environ.get("GOOGLE_SHEETS_KEY")
    sheet_id = os.environ.get("GOOGLE_SHEETS_ID")
    if not key_json or not sheet_id:
        print("沒有設定 GOOGLE_SHEETS_KEY / GOOGLE_SHEETS_ID，略過回測。")
        return None
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_info(
        json.loads(key_json), scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(creds).open_by_key(sheet_id)


def ensure_result_cols(ws, header):
    """確保標題列有「結果／結果日期／實際報酬%／持有天數」這 4 欄，沒有就補在最後面。
    回傳補完後的標題列跟各結果欄的欄位編號（1-based）。"""
    changed = False
    for c in RESULT_COLS:
        if c not in header:
            header = header + [c]
            changed = True
    if changed:
        ws.update([header], "1:1")
    return header, {c: header.index(c) + 1 for c in RESULT_COLS}


def evaluate(df, signal_date, buy_lo, buy_hi, stop, target, expire_days):
    """回傳 (結果, 結果日期, 實際報酬%, 持有天數, 買入日期)；資料不足（例如剛下市查不到）回傳
    None，這種情況維持原狀，不覆蓋，下次還可以再試。買入日期是真正進場那天（不是訊號日），
    還沒進場就是空字串。"""
    try:
        # fetch_history() 回傳的 df 索引是時區感知的（Asia/Taipei），跟沒給時區的 Timestamp
        # 比較會直接丟 TypeError——之前只用沒時區的假資料測過，沒踩到這個問題，實際資料
        # 一定要比照 df 自己的時區建立 Timestamp 才不會整批都比較失敗。
        #
        # 用「>」不是「>=」：訊號是用訊號當天的收盤價才能判斷出來的（例如突破要收盤價突破才算），
        # 代表要等訊號當天收盤之後才會知道有這個訊號，最早也只能等「隔天」才可能進場。如果把
        # 訊號當天本身也算進去，會把「收盤前盤中已經走完、你根本還不知道有訊號」的那段高低點
        # 誤判成「進場後的走勢」——實測真的發生過：某檔訊號當天盤中最低點就已經低於停損價，
        # 但收盤時突破拉回來，變成「當天進場、當天就停損」，但現實中你收盤才知道有訊號，
        # 不可能用那個已經過去的盤中低點被停損。
        sub = df[df.index > pd.Timestamp(signal_date, tz=df.index.tz)]
    except Exception:
        return None
    if sub.empty:
        return None
    # 沒有實際成交紀錄，用買進區間「上緣」當估計進場價算報酬%——不是用中點：signals.py 的
    # build_trade() 本來就是以買進上緣（最壞的成交價）去反推停損/停利，兩邊要用同一個進場價
    # 假設，算出來的報酬%才會跟訊號當初設計的風險（risk_pct）對得起來；用中點會比較樂觀，
    # 同一筆 3% 設計風險的停損，算出來會變成只有約 1.5%，看起來比實際上安全。
    entry = buy_hi
    entered, entry_idx = False, None
    for i in range(len(sub)):
        row = sub.iloc[i]
        if not entered and row["Low"] <= buy_hi and row["High"] >= buy_lo:
            entered, entry_idx = True, i
        if entered:
            held = i - entry_idx
            buy_date = sub.index[entry_idx].strftime("%Y-%m-%d")
            if row["Low"] <= stop:
                return "已停損", sub.index[i].strftime("%Y-%m-%d"), round(stop / entry * 100 - 100, 1), held, buy_date
            if row["High"] >= target:
                return "已停利", sub.index[i].strftime("%Y-%m-%d"), round(target / entry * 100 - 100, 1), held, buy_date
            if held >= expire_days:
                return ("過期未觸發", sub.index[i].strftime("%Y-%m-%d"),
                        round(row["Close"] / entry * 100 - 100, 1), held, buy_date)
    if entered:
        held = len(sub) - 1 - entry_idx
        buy_date = sub.index[entry_idx].strftime("%Y-%m-%d")
        return "追蹤中", "", round(sub.iloc[-1]["Close"] / entry * 100 - 100, 1), held, buy_date
    days_since = len(sub) - 1
    if days_since >= expire_days:
        return "過期未進場", "", None, days_since, ""
    return "尚未進場", "", None, days_since, ""


def backtest_sheet(sh, title, code_col, date_col):
    """處理一個分頁，回傳 (更新了幾列, 這次新產生的已停損/已停利事件列表)。
    code_col/date_col 是「代號」「日期」欄的名字。"""
    import gspread
    try:
        ws = sh.worksheet(title)
    except gspread.WorksheetNotFound:
        return 0, []
    values = ws.get_all_values()
    if len(values) < 2:
        return 0, []
    header, result_col_idx = ensure_result_cols(ws, values[0])
    idx = {name: header.index(name) for name in
           [code_col, date_col, "名稱", "買進下限", "買進上限", "停損價", "停利價"] if name in header}
    needed = [code_col, date_col, "買進下限", "買進上限", "停損價", "停利價"]
    if any(n not in idx for n in needed):
        print(f"[注意] {title} 缺少必要欄位（{[n for n in needed if n not in idx]}），略過這個分頁。")
        return 0, []

    expire_days = EXPIRE_DAYS[title]
    cache = {}          # 代號 -> DataFrame，同一檔股票在同一次執行裡只抓一次
    updates = []         # gspread batch_update 用
    colors = []          # gspread batch_format 用：已停損/已停利整列填色
    events = []          # 這次新產生的已停損/已停利，給 Telegram 另外通知用
    checked, changed = 0, 0
    for r in range(1, len(values)):
        row = values[r] + [""] * (len(header) - len(values[r]))   # 舊資料列可能還沒有結果欄，補空字串對齊
        status = row[result_col_idx["結果"] - 1]
        if status not in OPEN_STATUSES:
            continue   # 已經有終局結果，不用再查
        code = row[idx[code_col]]
        try:
            buy_lo, buy_hi = float(row[idx["買進下限"]]), float(row[idx["買進上限"]])
            stop, target = float(row[idx["停損價"]]), float(row[idx["停利價"]])
        except (ValueError, IndexError):
            continue   # 這列資料格式怪怪的（例如空白），跳過
        checked += 1
        if code not in cache:
            try:
                cache[code], _ = fetch_history(code)
            except Exception:
                cache[code] = None
        df = cache[code]
        if df is None:
            continue
        result = evaluate(df, row[idx[date_col]], buy_lo, buy_hi, stop, target, expire_days)
        if result is None:
            continue
        新結果, 結果日期, 報酬, 天數, 買入日期 = result
        # 「追蹤中」「尚未進場」這種還沒定案的狀態，就算結果分類沒變，報酬%/持有天數每天都會
        # 往前走，所以不做「跟上次一樣就跳過」的最佳化，每次查到都直接寫回去。
        changed += 1
        new_row = list(row)
        new_row[result_col_idx["結果"] - 1] = 新結果
        new_row[result_col_idx["結果日期"] - 1] = 結果日期
        new_row[result_col_idx["實際報酬%"] - 1] = "" if 報酬 is None else 報酬
        new_row[result_col_idx["持有天數"] - 1] = 天數
        lo = min(result_col_idx.values())
        updates.append({"range": f"{gspread.utils.rowcol_to_a1(r + 1, lo)}:"
                                  f"{gspread.utils.rowcol_to_a1(r + 1, len(header))}",
                         "values": [new_row[lo - 1:]]})
        if 新結果 in ("已停損", "已停利"):
            # 進來這個 for 迴圈的列，status 一定還在 OPEN_STATUSES（追蹤中/尚未進場/空白），
            # 所以只要這次評出「已停損」或「已停利」，就一定是這次執行才第一次定案——
            # 不用另外比對「跟上次一樣就不重複通知」，天生就不會重複發。
            events.append({
                "title": title, "code": code,
                "name": row[idx["名稱"]] if "名稱" in idx else "",
                "result": 新結果, "pct": 報酬, "days": 天數, "buy_date": 買入日期,
            })
            colors.append({"range": f"{gspread.utils.rowcol_to_a1(r + 1, 1)}:"
                                     f"{gspread.utils.rowcol_to_a1(r + 1, len(header))}",
                            "format": {"backgroundColor": RESULT_FILL[新結果]}})
    if updates:
        ws.batch_update(updates, value_input_option="USER_ENTERED")
    if colors:
        ws.batch_format(colors)
    print(f"{title}：檢查 {checked} 列，更新 {changed} 列")
    return changed, events


def compute_signal_stats(sh):
    """統計每一種訊號型態（型態欄位的完整文字，例如「訊號16 鏡射突破前高」）歷史上真正的
    表現，回傳 {型態: {"n": 已定案筆數, "win": 停利筆數, "loss": 停損筆數,
    "win_rate": 勝率%（樣本不足回傳 None）, "avg_return": 平均報酬%}}。

    三個分頁（多重訊號／每日候選／每週候選）同名訊號合併一起算——同一種型態的判斷邏輯
    一樣，只是出現在不同清單，分開算樣本數會太少。只算已經有終局結果的列（已停損／已停利／
    過期未觸發），追蹤中、尚未進場、過期未進場（根本沒進場，無從判斷型態好不好）都跳過。

    勝率的分母只有「已停損＋已停利」，過期未觸發不算進去（它不是贏也不是輸，算進去會
    稀釋/扭曲勝率）；但平均報酬%三種都算，比較能反映「這個型態抱到底平均賺賠多少」。
    樣本數低於 MIN_SAMPLE 時，win_rate 回傳 None（畫面上顯示「樣本不足」，不要硬湊一個
    小樣本的百分比誤導人）。"""
    import gspread
    agg = {}   # 型態 -> {"win":0,"loss":0,"expired":0,"returns":[float]}
    for title in ("多重訊號", "每日候選", "每週候選"):
        try:
            ws = sh.worksheet(title)
        except gspread.WorksheetNotFound:
            continue
        values = ws.get_all_values()
        if len(values) < 2:
            continue
        header = values[0]
        # 訊號型態的欄名三個分頁不一樣：多重訊號叫「訊號」，每日／每週候選叫「型態」
        # （見 sheets_log.py 的 SHEET_HEADERS），兩個都要認得，不然多重訊號那份會整個被跳過
        kind_col = "型態" if "型態" in header else "訊號" if "訊號" in header else None
        if kind_col is None or "結果" not in header or "實際報酬%" not in header:
            continue
        idx = {"kind": header.index(kind_col), "結果": header.index("結果"),
               "實際報酬%": header.index("實際報酬%")}
        for row in values[1:]:
            row = row + [""] * (len(header) - len(row))
            result, kind = row[idx["結果"]], row[idx["kind"]]
            if not kind or result not in ("已停損", "已停利", "過期未觸發"):
                continue
            a = agg.setdefault(kind, {"win": 0, "loss": 0, "expired": 0, "returns": []})
            if result == "已停利":
                a["win"] += 1
            elif result == "已停損":
                a["loss"] += 1
            else:
                a["expired"] += 1
            try:
                a["returns"].append(float(row[idx["實際報酬%"]]))
            except (ValueError, IndexError):
                pass   # 這欄萬一是空字串或格式怪怪的，平均報酬%就少算這一筆，不讓整批失敗
    stats = {}
    for kind, a in agg.items():
        resolved = a["win"] + a["loss"]
        n = resolved + a["expired"]
        stats[kind] = {
            "n": n, "win": a["win"], "loss": a["loss"],
            "win_rate": round(a["win"] / resolved * 100) if resolved >= MIN_SAMPLE else None,
            "avg_return": round(sum(a["returns"]) / len(a["returns"]), 1) if a["returns"] else None,
        }
    return stats


TITLE_ORDER = ["多重訊號", "每日候選", "每週候選"]   # 通知裡分組顯示的順序


def chunk_lines(lines, limit=3500):
    """把一堆文字行組成好幾批，每批串起來不超過 limit 字元，不會從某一行中間硬切斷。
    Telegram 單則訊息上限是 4096 字元，事件一多（實測發生過一次 308 筆）全部塞進一則
    會遠遠超過，原本用 text[:4000] 硬切，超過的內容直接消失、收到的訊息會斷在不上不下
    的地方，而且完全看不出來被截斷過。改成真的分批，每批各自發一則，一筆都不會少。"""
    chunks, cur, cur_len = [], [], 0
    for line in lines:
        add_len = len(line) + 1   # +1 是換行符號
        if cur and cur_len + add_len > limit:
            chunks.append(cur)
            cur, cur_len = [], 0
        cur.append(line)
        cur_len += add_len
    if cur:
        chunks.append(cur)
    return chunks


def notify_stop_events(events):
    """已停損/已停利的列另外發一則 Telegram 通知到專門的群組（TG_CHAT_ID_STOP），
    跟 notify.py 每天發的候選股摘要（TG_CHAT_ID）完全分開、各自獨立的訂閱對象。
    依分頁（多重訊號／每日候選／每週候選）分組，每組前面先列已停損／已停利各幾檔。

    顏色符號刻意跟「紅色＝警示、綠色＝安全」的直覺相反：已停利（賺錢）用🔴、已停損（賠錢）
    用🟢，是跟台股看盤「紅漲綠跌」的習慣對齊，也跟這個網站其他地方的配色一致
    （見 template.html 的 --good 是紅色、--bad 是綠色）。"""
    if not events:
        print("這次沒有新的已停損/已停利，不用發通知。")
        return
    token, chat = os.environ.get("TG_BOT_TOKEN"), os.environ.get("TG_CHAT_ID_STOP")
    if not token or not chat:
        print("沒有設定 TG_CHAT_ID_STOP，略過停損/停利通知。")
        return
    import requests
    today = datetime.now(timezone(timedelta(hours=8)))
    by_title = {}
    for e in events:
        by_title.setdefault(e["title"], []).append(e)
    titles = TITLE_ORDER + sorted(t for t in by_title if t not in TITLE_ORDER)   # 未知分頁名當保險墊在後面

    lines = [f"日期：{today.year}年{today.month}月{today.day}日", f"🔔 {len(events)} 檔觸發停損/停利"]
    for title in titles:
        group = by_title.get(title)
        if not group:
            continue
        stops = sum(1 for e in group if e["result"] == "已停損")
        gains = sum(1 for e in group if e["result"] == "已停利")
        lines += ["", f"［{title}］已停損：{stops} 檔　已停利：{gains} 檔"]
        for e in group:
            emoji = "🔴" if e["result"] == "已停利" else "🟢"
            pct = f"{e['pct']:+.1f}%" if e["pct"] is not None else "-"
            buy = f"買入日期：{e['buy_date']}，" if e["buy_date"] else ""
            lines.append(f"{emoji} {e['code']} {e['name']}　{e['result']}　{pct}"
                        f"（{buy}持有 {e['days']} 天）")
    lines += ["", "僅為資料分析，不是投資建議；自負盈虧"]
    chunks = chunk_lines(lines)
    ok = 0
    for i, chunk in enumerate(chunks, 1):
        text = "\n".join(chunk)
        if len(chunks) > 1:
            text = f"（{i}/{len(chunks)}）\n{text}"   # 分成好幾則時，每則開頭標一下是第幾則，避免看起來像斷掉的訊息
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              json={"chat_id": chat, "text": text}, timeout=30)
            r.raise_for_status()
            ok += 1
        except Exception as e:
            # 不印出例外內容：requests 的錯誤訊息會帶到含 token 的網址
            print(f"停損/停利通知第 {i}/{len(chunks)} 則發送失敗（{type(e).__name__}），請檢查 token 與 chat id。")
        if i < len(chunks):
            time.sleep(0.5)   # 同一個聊天室連續發好幾則，稍微停一下，避免被 Telegram 自己的速率限制擋下來
    if ok == len(chunks):
        print(f"已發送 {len(events)} 筆停損/停利通知（共 {len(chunks)} 則訊息）。")
    else:
        print(f"[注意] 停損/停利通知：{len(chunks)} 則訊息只有 {ok} 則成功送出，有些筆數可能沒送達。")


def main():
    try:
        sh = connect()
    except Exception as e:
        print(f"[注意] 連線 Google 試算表失敗（{type(e).__name__}：{e}），略過這次回測。完整錯誤：")
        traceback.print_exc()
        return 1
    if sh is None:
        return 0

    try:
        n1, e1 = backtest_sheet(sh, "多重訊號", "代號", "日期")
        n2, e2 = backtest_sheet(sh, "每日候選", "代號", "日期")
        n3, e3 = backtest_sheet(sh, "每週候選", "代號", "日期")
        print(f"回測完成：多重訊號更新 {n1} 列、每日候選更新 {n2} 列、每週候選更新 {n3} 列")
        try:
            notify_stop_events(e1 + e2 + e3)
        except Exception as e:   # 通知失敗不該讓回測本身被當成失敗（結果已經寫進試算表了）
            print(f"[注意] 停損/停利通知發送時發生例外（{type(e).__name__}），略過。")
        try:
            # 統計每種訊號型態的歷史勝率，給網頁顯示「信心程度」用（見 README「訊號信心程度」）。
            # 這次算完存在本機，今天 build_site.py 已經跑過、網站已經部署了，來不及影響今天，
            # 跟 inst_history.json／universe_cache.json 同一套模式：明天 screener.py 開始跑時
            # 再讀回去用，差一天不影響——勝率本來就是慢慢累積、不會一天內有感變化的東西。
            stats = compute_signal_stats(sh)
            (BASE_DIR / "signal_stats.json").write_text(
                json.dumps(stats, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            print(f"訊號統計完成：{len(stats)} 種型態。")
        except Exception as e:
            print(f"[注意] 統計訊號勝率時發生例外（{type(e).__name__}），略過，不影響回測本身。")
    except Exception as e:
        print(f"[注意] 回測失敗（{type(e).__name__}：{e}），略過這次。完整錯誤：")
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
