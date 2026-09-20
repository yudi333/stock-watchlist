"""
課程「20 種買進訊號」的偵測程式（日線、週線共用）。

每個 sigXX 函式只做一件事：檢查最新一根 K 棒有沒有出現該訊號。
有 -> 回傳 dict（訊號名稱、買進價、防守價、目標價、理由），沒有 -> 回傳 None。
之後由 build_trade() 依課程紀律算出停損與停利。

只做資料分析，不會下單。型態辨識是用規則近似，最後請自己看圖確認。
"""

import numpy as np
import pandas as pd

RISK_REWARD = 2.0       # 賺賠比至少 2:1（課程：2:1 或 3:1）
HARD_MAX_RISK = 0.10    # 課程：停損最多 10%
MAX_TARGET_GAIN = 0.40  # 滿足點公式有時算出翻倍以上，停利價保守限制在 +40%


# ---------------------------------------------------------------
# 共用工具
# ---------------------------------------------------------------
class Ctx:
    """把 K 線資料轉成陣列，並算好常用數字（均線、量比、52 週高低），偵測函式都吃這個。"""

    def __init__(self, df, p):
        self.p = p
        self.o = df["Open"].to_numpy(float)
        self.h = df["High"].to_numpy(float)
        self.l = df["Low"].to_numpy(float)
        self.c = df["Close"].to_numpy(float)
        self.v = df["Volume"].to_numpy(float)
        self.n = len(self.c)
        avg_vol = self.v[-1 - p["vol_n"]:-1].mean()
        self.vol_ratio = self.v[-1] / avg_vol if avg_vol > 0 else 0.0
        self.vol_ok = self.vol_ratio >= p["break_vol"]      # 突破時要有量
        s = pd.Series(self.c)
        keys = {5, 10, 20, 60, p["ma_slow"], *p["pull_mas"]}
        self.ma = {m: s.rolling(m).mean().to_numpy() for m in keys}
        yb = p["y_bars"]
        self.high52 = self.h[-yb:].max()
        self.low52 = self.l[-yb:].min()

    def in_low_zone(self, price, frac=0.4):
        """price 是否在 52 週區間的下方 frac 以內（課程說的「低價圈」）。"""
        return price <= self.low52 + frac * (self.high52 - self.low52)


def pivots(arr, k, kind):
    """找波峰('high') / 波谷('low')：該點在左右各 k 根之內是最高/最低。回傳 [(位置, 值)]。"""
    out = []
    for i in range(k, len(arr) - k):
        w = arr[i - k:i + k + 1]
        hit = arr[i] == (w.max() if kind == "high" else w.min())
        if hit and not (out and out[-1][0] == i - 1 and out[-1][1] == arr[i]):
            out.append((i, float(arr[i])))
    return out


def calc_kd(df, n=9):
    """KD 指標（K9、D9）。回傳 (K, D) 兩個 Series。"""
    low_n = df["Low"].rolling(n).min()
    high_n = df["High"].rolling(n).max()
    rsv = ((df["Close"] - low_n) / (high_n - low_n) * 100).fillna(50)
    k = rsv.ewm(alpha=1 / 3, adjust=False).mean()
    d = k.ewm(alpha=1 / 3, adjust=False).mean()
    return k, d


def _sig(name, side, entry, defence, target, reason, extra=None):
    return dict(signal=name, side=side, entry=float(entry), defence=float(defence),
                target=None if target is None else float(target), reason=reason,
                extra=extra or "")


# ---------------------------------------------------------------
# 訊號 15：杯柄，突破轉折點（杯深 + 突破點 = 滿足點）
# ---------------------------------------------------------------
def sig15_cup_handle(x):
    p = x.p
    W = p["cup_n"]
    if x.n < W + 5:
        return None
    h, l, c = x.h[-W:], x.l[-W:], x.c[-W:]
    hmin, hmax = p["handle_len"]
    li = int(np.argmax(h[:W - 20]))                       # 左杯口（前高）
    LH = h[li]
    bi = li + 1 + int(np.argmin(l[li + 1:W - 8]))         # 杯底
    B = l[bi]
    depth = LH - B
    if not (0.12 <= depth / LH <= 0.40) or bi - li < p["cup_min_side"]:
        return None
    ri = bi + 1 + int(np.argmax(h[bi + 1:W - 1]))         # 右杯口
    RH = h[ri]
    if not (LH * 0.95 <= RH <= LH * 1.05) or ri - bi < p["cup_min_side"]:
        return None
    if not (0.25 <= (bi - li) / (ri - li) <= 0.75):       # 杯底大致在中間
        return None
    if (l[li:ri + 1] <= B + 0.33 * depth).sum() < 0.15 * (ri - li):
        return None                                        # 底部要夠圓（不是尖 V）
    handle_len = W - 2 - ri                                # 杯柄：右杯口之後的整理
    if not (hmin <= handle_len <= hmax):
        return None
    HL = l[ri + 1:W - 1].min()
    if not (0.03 <= (RH - HL) / RH <= 0.15) or HL < B + 0.5 * (RH - B):
        return None
    if not (c[-1] > RH and c[-2] <= RH * 1.02 and x.vol_ok and c[-1] <= RH * 1.06):
        return None
    return _sig("訊號15 杯柄突破", "右側", c[-1], HL, c[-1] + depth,
                f"杯柄型態：杯深 {depth / LH * 100:.0f}%，杯柄整理後突破右杯口 {RH:.2f}，"
                f"量是均量 {x.vol_ratio:.1f} 倍；滿足點 = 突破價 + 杯深",
                "杯柄常在誘空，確認突破當天收盤站上再進")


# ---------------------------------------------------------------
# 訊號 8 / 17：W 底（8 = 突破頸線買；17 = 右底提前買，左側）
# ---------------------------------------------------------------
def sig8_w_bottom(x):
    p = x.p
    N, k = p["w_n"], p["pivot_k"]
    if x.n < N + 5:
        return None
    off = x.n - N
    pl = [(i + off, v) for i, v in pivots(x.l[-N:], k, "low")]
    if len(pl) < 2:
        return None
    (i1, L1), (i2, L2) = pl[-2], pl[-1]
    if i2 - i1 < p["w_gap"] or abs(L2 / L1 - 1) > 0.05:    # 兩個底要相近
        return None
    base = min(L1, L2)
    H = x.h[i1:i2 + 1].max()                               # 頸線（中間反彈高點）
    if (H - base) / base < 0.10 or not x.in_low_zone(base):
        return None
    if x.l[i2:].min() < L2 * 0.985:                        # 右底之後不能再破底
        return None
    c = x.c[-1]
    target = H + (H - base)                                # 突破點 + 震幅
    if c > H and x.c[-2] <= H * 1.03 and x.vol_ok and c <= H * 1.06:
        return _sig("訊號8 W底突破", "右側", c, L2, target,
                    f"低價圈 W 底：兩底 {L1:.2f} / {L2:.2f}，突破頸線 {H:.2f}，"
                    f"量是均量 {x.vol_ratio:.1f} 倍；滿足點 = 突破點 + 震幅")
    if (i2 <= x.n - 1 - k and x.n - 1 - i2 <= 12 and L2 <= c <= L2 * 1.05
            and c > x.c[-2] and c < H):
        return _sig("訊號17 W底右底", "左側", c, min(L1, L2), target,
                    f"低價圈 W 底的右底 {L2:.2f} 附近止穩（頸線 {H:.2f}），提前布局，"
                    f"目標 = 頸線 + 震幅",
                    "左側交易：還沒突破頸線，風險較高，跌破左底就要出場")
    return None


# ---------------------------------------------------------------
# 訊號 7：低價圈頭肩底，突破頸線
# ---------------------------------------------------------------
def sig7_hs_bottom(x):
    p = x.p
    N, k = p["hs_n"], p["pivot_k"]
    if x.n < N + 5:
        return None
    off = x.n - N
    pl = [(i + off, v) for i, v in pivots(x.l[-N:], k, "low")]
    if len(pl) < 3:
        return None
    (a, la), (b, lh), (r, lr) = pl[-3:]                    # 左肩、頭、右肩
    if not (lh < la * 0.97 and lh < lr * 0.97 and abs(lr / la - 1) <= 0.08):
        return None
    if r < x.n - 1 - p["hs_recent"] or b - a < 3 or r - b < 3 or not x.in_low_zone(lh):
        return None
    i1 = a + int(np.argmax(x.h[a:b + 1]))                  # 頸線的兩個高點
    i2 = b + int(np.argmax(x.h[b:r + 1]))
    p1, p2 = x.h[i1], x.h[i2]
    if i2 <= i1 or abs(p2 / p1 - 1) > 0.06:
        return None
    slope = (p2 - p1) / (i2 - i1)
    neck = lambda t: p2 + slope * (t - i2)                 # 頸線在第 t 根的價位
    nk, c = neck(x.n - 1), x.c[-1]
    if not (c > nk and x.c[-2] <= neck(x.n - 2) * 1.02 and x.vol_ok and c <= nk * 1.06):
        return None
    return _sig("訊號7 頭肩底突破", "右側", c, lr, nk + (nk - lh),
                f"低價圈頭肩底：頭 {lh:.2f}、左右肩 {la:.2f} / {lr:.2f}，突破頸線 {nk:.2f}，"
                f"量是均量 {x.vol_ratio:.1f} 倍；滿足點 = 頸線 + (頸線 − 頭)",
                "課程提醒：頭部量大於左肩、右肩量縮後帶量突破更可靠")


# ---------------------------------------------------------------
# 訊號 4：三角收斂整理後向上突破
# ---------------------------------------------------------------
def sig4_triangle(x):
    p = x.p
    N, k, gap = p["tri_n"], p["pivot_k"], p["pivot_gap"]
    if x.n < N + 5:
        return None
    off = x.n - N
    ph = [(i + off, v) for i, v in pivots(x.h[-N:], k, "high")]
    pl = [(i + off, v) for i, v in pivots(x.l[-N:], k, "low")]
    if len(ph) < 2 or len(pl) < 2:
        return None
    (i1, h1), (i2, h2) = ph[0], ph[-1]
    (j1, l1), (j2, l2) = pl[0], pl[-1]
    if i2 - i1 < gap or j2 - j1 < gap:
        return None
    sh, sl = (h2 - h1) / (i2 - i1), (l2 - l1) / (j2 - j1)
    up = lambda t: h2 + sh * (t - i2)                      # 上緣壓力線
    lo = lambda t: l2 + sl * (t - j2)                      # 下緣支撐線
    start = min(i1, j1)
    w0, w1 = up(start) - lo(start), up(x.n - 1) - lo(x.n - 1)
    if w0 <= 0 or w1 <= 0 or w1 > 0.75 * w0:               # 要明顯收斂
        return None
    if not (sh <= 0.0003 * h1 and sl >= -0.0003 * l1):     # 上緣走平或下彎、下緣走平或上揚
        return None
    U, c = up(x.n - 1), x.c[-1]
    if not (c > U and x.c[-2] <= up(x.n - 2) * 1.02 and x.vol_ok and c <= U * 1.06):
        return None
    top, bottom = x.h[-N:].max(), x.l[-N:].min()
    short_t, long_t = c + (top - l2), c + (top - bottom)   # 課程：最高 − 最低 + 突破價
    if short_t <= c:
        return None
    return _sig("訊號4 三角收斂突破", "右側", c, l2, short_t,
                f"三角收斂整理後突破上緣 {U:.2f}，量是均量 {x.vol_ratio:.1f} 倍；"
                f"短線目標 {short_t:.2f}、長線目標 {long_t:.2f}",
                "課程建議新手買在突破，不要提前買在盤整低點")


# ---------------------------------------------------------------
# 訊號 10：旗型整理後突破（振幅 + 整理低點 = 滿足點）
# ---------------------------------------------------------------
def sig10_flag(x):
    p = x.p
    fmin, fmax = p["flag_len"]
    pn = p["pole_n"]
    for t in range(x.n - 2 - fmin, x.n - 2 - fmax - 1, -1):    # t = 旗桿頂
        if t - pn < 0:
            break
        top = x.h[t]
        if top < x.h[t - pn:x.n - 1].max():
            continue
        start = x.l[t - pn:t].min()
        if (top - start) / start < p["pole_gain"]:
            continue
        fl, fh = x.l[t + 1:x.n - 1], x.h[t + 1:x.n - 1]
        f_low, f_high = fl.min(), fh.max()
        if f_high >= top or (top - f_low) / (top - start) > 0.5:
            continue
        if np.polyfit(range(len(fl)), x.c[t + 1:x.n - 1], 1)[0] > 0.001 * top:
            continue                                        # 旗面要是平或下傾
        rb = min(5, x.n - 2 - t)
        brk = x.h[x.n - 1 - rb:x.n - 1].max()
        c = x.c[-1]
        if not (c > brk and c > x.c[-2] and x.vol_ok):
            continue
        target = (top - start) + f_low                      # 振幅 + 整理低點
        if target <= c:
            continue
        return _sig("訊號10 旗型突破", "右側", c, f_low, target,
                    f"旗型：先大漲 {(top - start) / start * 100:.0f}%（旗桿），"
                    f"再緩降整理 {x.n - 2 - t} 根後突破，量是均量 {x.vol_ratio:.1f} 倍；"
                    f"滿足點 = 旗桿振幅 + 整理低點")
    return None


# ---------------------------------------------------------------
# 訊號 3：窄幅盤整（≤15%）向上突破；盤整越久越可靠
# ---------------------------------------------------------------
def sig3_narrow_box(x):
    p = x.p
    m = p["box_min"]
    if x.n < m * 3 + 5:
        return None
    best = None
    for L in range(m, m * 3 + 1):                           # 找「還維持窄幅」的最長盤整
        hi, lw = x.h[-1 - L:-1].max(), x.l[-1 - L:-1].min()
        if (hi - lw) / lw <= p["box_range"]:
            best = (L, hi, lw)
        else:
            break
    if not best:
        return None
    L, hi, lw = best
    c = x.c[-1]
    if not (c > hi and x.c[-2] <= hi * 1.02 and x.vol_ok and c <= hi * 1.06):
        return None
    return _sig("訊號3 窄幅盤整突破", "右側", c, lw, hi + (hi - lw),
                f"窄幅盤整 {L}{p['unit']}（區間 {(hi - lw) / lw * 100:.0f}% 以內）後向上突破 {hi:.2f}，"
                f"量是均量 {x.vol_ratio:.1f} 倍；盤整越久突破越可靠",
                extra=f"盤整{L}")


# ---------------------------------------------------------------
# 訊號 16：鏡射理論（突破前高買；拉回不破防守也可買）
# ---------------------------------------------------------------
def sig16_mirror(x):
    p = x.p
    N, M = p["mirror_n"], p["mirror_pull_n"]
    if x.n < N + M + 5:
        return None
    c = x.c[-1]
    prior_high = x.h[-1 - N:-1].max()
    start = x.l[-1 - N:-1].min()                            # 起漲點
    if c > prior_high and x.c[-2] <= prior_high * 1.02 and x.vol_ok and c <= prior_high * 1.06:
        amp = (c - start) / start                           # 震幅
        return _sig("訊號16 鏡射突破前高", "右側", c, prior_high, c * (1 + amp),
                    f"突破近 {N}{p['unit']}高點 {prior_high:.2f}（創新高），量是均量 "
                    f"{x.vol_ratio:.1f} 倍；目標 = 突破點 × (1 + 震幅 {amp * 100:.0f}%)")
    # 突破後拉回：現價約在高點 ×0.85~0.93，且沒跌破原本的前高（防守）
    win = x.h[-M:]
    pk = int(np.argmax(win))
    peak = win[pk]
    old_high = x.h[-1 - N - M:-M].max()
    start2 = x.l[-1 - N - M:-M].min()
    if peak > old_high and pk < M - 3 and 0.83 <= c / peak <= 0.93 \
            and c >= old_high * 0.98 and c > x.c[-2]:
        pull_low = x.l[-M + pk:].min()
        amp = (peak - start2) / start2
        return _sig("訊號16 鏡射拉回", "右側", c, min(pull_low, old_high), pull_low * (1 + amp),
                    f"突破前高 {old_high:.2f} 後拉回到高點 {peak:.2f} 的 {c / peak * 100:.0f}%，"
                    f"沒跌破防守且止穩；目標 = 拉回低點 × (1 + 震幅 {amp * 100:.0f}%)")
    return None


# ---------------------------------------------------------------
# 訊號 9：跳空暴漲後，再突破盤整
# ---------------------------------------------------------------
def sig9_gap_breakout(x):
    p = x.p
    c = x.c[-1]
    for g in range(x.n - 1 - p["gap_look"], x.n - 4):       # g = 跳空那根
        if g < 1 or not (x.o[g] > x.h[g - 1] and x.o[g] / x.c[g - 1] - 1 >= p["gap_pct"]):
            continue
        consol_high = x.h[g:x.n - 1].max()
        low_after = x.l[g + 1:x.n - 1].min()
        if low_after < x.h[g - 1] * 0.99:                   # 缺口被回補就不算
            continue
        if c > consol_high and c > x.c[-2] and x.vol_ok and c <= consol_high * 1.06:
            return _sig("訊號9 跳空後突破", "右側", c, low_after, None,
                        f"{x.n - 1 - g} 根前跳空大漲 {(x.o[g] / x.c[g - 1] - 1) * 100:.0f}%，"
                        f"缺口未補、整理後再突破 {consol_high:.2f}，主力積極的強勢股")
    return None


# ---------------------------------------------------------------
# 訊號 1：上漲趨勢中，下跌到均線買進
# ---------------------------------------------------------------
def sig1_ma_pullback(x):
    p = x.p
    lb, slow = p["up_lookback"], p["ma_slow"]
    ms = x.ma[slow]
    c = x.c[-1]
    if x.n < slow + lb + 2 or not (c > ms[-1] > ms[-1 - lb]):
        return None
    if x.h[-p["recent_n"]:].max() < c * 1.03:               # 要先有回檔才算「下跌到」均線
        return None
    for m in p["pull_mas"]:                                 # 短均線優先（適合短線）
        ma = x.ma[m]
        if np.isnan(ma[-1]) or ma[-1] <= ma[-1 - lb]:
            continue
        d = (c / ma[-1] - 1) * 100
        if 0 <= d <= p["pullback_pct"]:
            return _sig("訊號1 上漲回測均線", "右側", c, min(ma[-1], x.l[-3:].min()), None,
                        f"上漲趨勢中（站上 {slow}{p['unit']}線且向上）回測 {m}{p['unit']}均線，"
                        f"僅高於均線 {d:.1f}%",
                        "出場：短均線與長均線死亡交叉，或收黑跌破均線")
    return None


# ---------------------------------------------------------------
# 訊號 12：持續下跌中，大量＋長下影線（止跌）
# ---------------------------------------------------------------
def sig12_capitulation(x):
    p = x.p
    c, o, h, l = x.c[-1], x.o[-1], x.h[-1], x.l[-1]
    rng = h - l
    if x.n < 65 or rng <= 0:
        return None
    ma20, ma60 = x.ma[20][-1], x.ma[60][-1]
    if not (c < ma20 < ma60) or c > x.h[-60:].max() * 0.85:     # 空頭排列且已跌一段
        return None
    lower = min(o, c) - l
    if lower / rng < 0.5 or rng / c < p["hammer_range"] or x.vol_ratio < 2.0:
        return None
    return _sig("訊號12 止跌長下影", "右側", c, l, None,
                f"下跌一段後出現大量（均量 {x.vol_ratio:.1f} 倍）長下影線，下方買盤湧現、賣壓有衰竭跡象",
                "逆勢抄底，風險較高：隔天若跌破這根最低點就要出場")


# ---------------------------------------------------------------
# 訊號 19：左側．假跌破（破底突破失敗）下一根 K 棒買進
# ---------------------------------------------------------------
def sig19_false_breakdown(x):
    p = x.p
    N = p["fb_n"]
    if x.n < N + 6:
        return None
    rng_h, rng_l = x.h[-1 - N - 3:-3], x.l[-1 - N - 3:-3]
    S, H = rng_l.min(), rng_h.max()                         # 支撐與壓力
    if (H - S) / S < 0.10:
        return None
    c = x.c[-1]
    broke = (x.c[-3:-1] < S).any()                          # 前面收盤曾跌破支撐
    if not (broke and c > S * 1.005 and c > x.o[-1] and c > x.c[-2]):
        return None
    fake_low = x.l[-3:].min()
    return _sig("訊號19 假跌破買進", "左側", c, fake_low, H + (H - S),
                f"跌破支撐 {S:.2f} 後隔天收回（破底失敗），目標 = 壓力 {H:.2f} + 震幅",
                "左側交易：跌破這個假跌破低點要立刻停損")


# 偵測順序 = 優先順序（越前面越優先）：突破型態 > 一般進場 > 左側
DETECTORS = [sig15_cup_handle, sig8_w_bottom, sig7_hs_bottom, sig4_triangle,
             sig10_flag, sig3_narrow_box, sig16_mirror, sig9_gap_breakout,
             sig1_ma_pullback, sig12_capitulation, sig19_false_breakdown]


# ---------------------------------------------------------------
# 停損 / 停利：依課程紀律
# ---------------------------------------------------------------
def build_trade(m, p):
    """
    m 有目標價（有公式的訊號）：
        停損 = 防守點下方 1%，但停損幅度 ≤ 獲利目標的 1/2 且 ≤ 10%，賺賠比至少 2:1。
    m 沒有目標價（課程沒給公式）：停損 = 防守點下方 1%，停利 = 2 倍風險。
    不合格回傳 None。
    """
    entry, defence, target = m["entry"], m["defence"], m["target"]
    if not entry > defence > 0:
        return None
    lo, hi = p["risk_range"]
    if target is None:
        stop = defence * 0.99
        risk = (entry - stop) / entry * 100
        if risk > hi:
            return None
        if risk < lo:
            stop = entry * (1 - lo / 100)
        target = entry + (entry - stop) * RISK_REWARD
    else:
        capped_from = target
        target = min(target, entry * (1 + MAX_TARGET_GAIN))
        gain = target - entry
        if gain <= 0:
            return None
        cap = min(HARD_MAX_RISK * entry, gain / RISK_REWARD)
        stop = max(defence * 0.99, entry - cap)
        if (entry - stop) / entry * 100 < lo:
            stop = entry * (1 - lo / 100)
        if gain < (entry - stop) * RISK_REWARD:
            return None
    risk_pct = (entry - stop) / entry * 100
    if m["target"] is not None and capped_from > target * 1.001:
        m = {**m, "reason": m["reason"] + f"；課程公式算出 {capped_from:.2f}"
                            f"（+{(capped_from / entry - 1) * 100:.0f}%），停利價保守限制在 +{MAX_TARGET_GAIN * 100:.0f}%"}
    return {**m, "stop": stop, "target": target, "risk_pct": risk_pct,
            "reward_pct": (target / entry - 1) * 100,
            "rr": (target - entry) / (entry - stop)}
