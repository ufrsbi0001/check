"""
ML Research Pipeline for Raw OHLCV Feather Data
- Feature engineering from OHLCV
- Triple-barrier target (TP/SL)
- Purged Walk-Forward CV + SHAP
- Full PnL simulation with fees & slippage
"""
import os
import warnings
import numpy as np
import pandas as pd
import xgboost as xgb
import shap
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from sklearn.metrics import roc_auc_score, precision_score

warnings.filterwarnings("ignore", category=FutureWarning)

# ══════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════
DATA_DIR  = r"D:\New folder\backtest-data\1hr"
TIMEFRAME = "1h"

SYMBOLS   = ["BTC_USDT_USDT"]      # add "ETH_USDT_USDT", "SOL_USDT_USDT" later

# Triple-barrier
TP_PCT    = 0.02       # +2% take-profit
SL_PCT    = 0.01       # -1% stop-loss
MAX_HOLD  = 24         # max hold = 24 bars (hours)

# CV
N_SPLITS  = 5
GAP_SIZE  = MAX_HOLD

# Costs (per side, in %)
FEE_PCT       = 0.05   # Binance futures taker
SLIPPAGE_PCT  = 0.05   # market order slippage

RANDOM_STATE = 42
np.random.seed(RANDOM_STATE)


# ══════════════════════════════════════════════════════════════
# 1. LOAD
# ══════════════════════════════════════════════════════════════
def load_candles(symbol, timeframe=TIMEFRAME):
    candidates = [
        os.path.join(DATA_DIR, f"{symbol}-{timeframe}-futures.feather"),
        os.path.join(DATA_DIR, f"{symbol}-{timeframe}r-futures.feather"),
        os.path.join(DATA_DIR, f"{symbol}-{timeframe}hr-futures.feather"),
    ]
    path = next((p for p in candidates if os.path.exists(p)), None)
    if path is None:
        raise FileNotFoundError(
            f"❌ Not found for {symbol}. Tried:\n  " + "\n  ".join(candidates)
        )
    print(f"   ↳ {os.path.basename(path)}")
    df = pd.read_feather(path)
    df.columns = [c.lower() for c in df.columns]
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


# ══════════════════════════════════════════════════════════════
# 2. INDICATORS
# ══════════════════════════════════════════════════════════════
def rsi(s, p=14):
    d = s.diff()
    g = d.clip(lower=0).rolling(p).mean()
    l = (-d.clip(upper=0)).rolling(p).mean()
    return 100 - 100 / (1 + g / (l + 1e-9))


def atr(h, l, c, p=14):
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()],
                   axis=1).max(axis=1)
    return tr.rolling(p).mean()


def adx(h, l, c, p=14):
    up, down = h.diff(), -l.diff()
    pdm = np.where((up > down) & (up > 0), up, 0.0)
    mdm = np.where((down > up) & (down > 0), down, 0.0)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()],
                   axis=1).max(axis=1)
    atr_ = tr.rolling(p).mean()
    pdi = 100 * pd.Series(pdm).rolling(p).mean() / (atr_ + 1e-9)
    mdi = 100 * pd.Series(mdm).rolling(p).mean() / (atr_ + 1e-9)
    dx  = 100 * (pdi - mdi).abs() / (pdi + mdi + 1e-9)
    return dx.rolling(p).mean()


def add_features(df):
    df = df.copy()
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]

    df["ret_1"]  = c.pct_change(1)
    df["ret_3"]  = c.pct_change(3)
    df["ret_6"]  = c.pct_change(6)
    df["ret_24"] = c.pct_change(24)

    df["atr_14"]    = atr(h, l, c, 14)
    df["atr_pct"]   = df["atr_14"] / c
    df["atr_ratio"] = df["atr_14"] / df["atr_14"].rolling(50).mean()

    df["rsi_14"] = rsi(c, 14)
    df["rsi_7"]  = rsi(c, 7)
    df["adx_14"] = adx(h, l, c, 14)

    df["ema_20"]     = c.ewm(span=20).mean()
    df["ema_50"]     = c.ewm(span=50).mean()
    df["ema_200"]    = c.ewm(span=200).mean()
    df["ema_20_50"]  = (df["ema_20"] - df["ema_50"]) / c
    df["ema_50_200"] = (df["ema_50"] - df["ema_200"]) / c
    df["dist_ema20"] = (c - df["ema_20"]) / c

    ema12 = c.ewm(span=12).mean()
    ema26 = c.ewm(span=26).mean()
    df["macd"]      = ema12 - ema26
    df["macd_sig"]  = df["macd"].ewm(span=9).mean()
    df["macd_hist"] = df["macd"] - df["macd_sig"]

    ma20, sd20 = c.rolling(20).mean(), c.rolling(20).std()
    df["bb_upper"] = ma20 + 2 * sd20
    df["bb_lower"] = ma20 - 2 * sd20
    df["bb_pos"]   = (c - df["bb_lower"]) / (df["bb_upper"] - df["bb_lower"] + 1e-9)
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / ma20

    df["vol_ratio"] = v / v.rolling(50).mean()
    signed_vol = np.sign(df["ret_1"].fillna(0)) * v
    df["cvd_slope"] = signed_vol.rolling(20).sum() / v.rolling(20).sum().replace(0, np.nan)

    df["high_24"]   = h.rolling(24).max()
    df["low_24"]    = l.rolling(24).min()
    df["range_pos"] = (c - df["low_24"]) / (df["high_24"] - df["low_24"] + 1e-9)

    return df


# ══════════════════════════════════════════════════════════════
# 3. TRIPLE-BARRIER TARGET
# ══════════════════════════════════════════════════════════════
def triple_barrier(df, tp=TP_PCT, sl=SL_PCT, max_hold=MAX_HOLD):
    close = df["close"].values
    high  = df["high"].values
    low   = df["low"].values
    n     = len(df)
    y     = np.full(n, np.nan)

    for i in range(n - max_hold):
        entry = close[i]
        tp_px = entry * (1 + tp)
        sl_px = entry * (1 - sl)
        for j in range(i + 1, i + max_hold + 1):
            if low[j] <= sl_px:
                y[i] = 0; break
            if high[j] >= tp_px:
                y[i] = 1; break
    return y


# ══════════════════════════════════════════════════════════════
# 4. DATASET
# ══════════════════════════════════════════════════════════════
FEATURE_COLS = [
    "ret_1", "ret_3", "ret_6", "ret_24",
    "atr_pct", "atr_ratio",
    "rsi_14", "rsi_7", "adx_14",
    "ema_20_50", "ema_50_200", "dist_ema20",
    "macd_hist",
    "bb_pos", "bb_width",
    "vol_ratio", "cvd_slope",
    "range_pos",
]


def build_dataset(symbols):
    frames = []
    for sym in symbols:
        print(f"📂 {sym}")
        d = load_candles(sym)
        d = add_features(d)
        d["target"] = triple_barrier(d)
        d["symbol"] = sym
        frames.append(d)

    df = pd.concat(frames, ignore_index=True)
    df = df.dropna(subset=FEATURE_COLS + ["target"]).reset_index(drop=True)
    df = df.sort_values("timestamp").reset_index(drop=True)

    X  = df[FEATURE_COLS].values.astype(np.float32)
    y  = df["target"].values.astype(int)
    ts = df["timestamp"].values

    print(f"\n✅ Dataset: {X.shape[0]:,} rows, {X.shape[1]} features, "
          f"class balance = {y.mean():.3f} positive")
    print(f"📅 Range  : {df['timestamp'].min()} → {df['timestamp'].max()}")
    print(f"   Span   : {(df['timestamp'].max()-df['timestamp'].min()).days} days")
    return X, y, ts, FEATURE_COLS, df


# ══════════════════════════════════════════════════════════════
# 5. PURGED WALK-FORWARD CV
# ══════════════════════════════════════════════════════════════
class PurgedWalkForwardSplit:
    def __init__(self, n_splits=5, gap_size=24):
        self.n_splits = n_splits
        self.gap_size = gap_size

    def get_n_splits(self, X=None, y=None, groups=None):
        return self.n_splits

    def split(self, X, y=None, groups=None):
        n = len(X)
        fold_size = n // (self.n_splits + 1)
        for i in range(1, self.n_splits + 1):
            train_end  = i * fold_size
            test_start = train_end + self.gap_size
            test_end   = min(test_start + fold_size, n)
            if test_start >= n or test_end <= test_start:
                break
            yield np.arange(0, train_end), np.arange(test_start, test_end)


def precision_at_top_k(y_true, y_prob, k_frac=0.10):
    k = max(1, int(len(y_prob) * k_frac))
    order = np.argsort(y_prob)[::-1][:k]
    return precision_score(y_true[order],
                           (y_prob[order] > 0.5).astype(int),
                           zero_division=0)


def train_and_evaluate(X, y, feature_names):
    print("\n🚀 Purged Walk-Forward CV...")
    splitter = PurgedWalkForwardSplit(N_SPLITS, GAP_SIZE)

    def new_model():
        return xgb.XGBClassifier(
            n_estimators=400, max_depth=4, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.8,
            eval_metric="logloss", random_state=RANDOM_STATE,
            n_jobs=-1, tree_method="hist",
        )

    oof = np.full(len(y), np.nan)
    for fold, (tr, te) in enumerate(splitter.split(X), 1):
        m = new_model()
        m.fit(X[tr], y[tr], verbose=False)
        p = m.predict_proba(X[te])[:, 1]
        oof[te] = p
        print(f"   Fold {fold} | n_train={len(tr):>6,} | n_test={len(te):>6,} "
              f"| AUC = {roc_auc_score(y[te], p):.4f}")

    mask = ~np.isnan(oof)
    auc = roc_auc_score(y[mask], oof[mask])
    p10 = precision_at_top_k(y[mask], oof[mask], 0.10)
    p20 = precision_at_top_k(y[mask], oof[mask], 0.20)
    print(f"\n🏆 OOF AUC          : {auc:.4f}")
    print(f"🎯 Precision@Top10% : {p10:.4f}   (baseline = {y.mean():.4f})")
    print(f"🎯 Precision@Top20% : {p20:.4f}")

    final = new_model()
    final.fit(X, y, verbose=False)
    return final, auc, oof


# ══════════════════════════════════════════════════════════════
# 6. SHAP
# ══════════════════════════════════════════════════════════════
def explain_with_shap(model, X, feature_names):
    print("\n🔍 SHAP analysis...")
    explainer = shap.TreeExplainer(model)
    sv = explainer.shap_values(X)
    if isinstance(sv, list):
        sv = sv[1]
    sv = np.asarray(sv)
    if sv.ndim == 3:
        sv = sv[:, :, 1]

    plt.figure()
    shap.summary_plot(sv, X, feature_names=feature_names, show=False)
    plt.title("SHAP Summary: Feature Importance & Direction")
    plt.tight_layout()
    plt.savefig("shap_summary.png", dpi=120, bbox_inches="tight")
    plt.close()

    plt.figure()
    shap.summary_plot(sv, X, feature_names=feature_names,
                      plot_type="bar", show=False)
    plt.title("Top Indicators by Mean |SHAP|")
    plt.tight_layout()
    plt.savefig("shap_bar.png", dpi=120, bbox_inches="tight")
    plt.close()

    imp = (pd.DataFrame({"Feature": feature_names,
                         "Importance": np.abs(sv).mean(axis=0)})
             .sort_values("Importance", ascending=False)
             .reset_index(drop=True))

    print("\n👑 TOP 5 'KING' INDICATORS:")
    print(imp.head(5).to_string(index=False))
    print("\n🗑️  BOTTOM 5 (noise):")
    print(imp.tail(5).to_string(index=False))
    return imp


# ══════════════════════════════════════════════════════════════
# 7. PnL SIMULATION
# ══════════════════════════════════════════════════════════════
def simulate_pnl(df_full, oof_preds, threshold=0.5,
                 fee_pct=FEE_PCT, slippage_pct=SLIPPAGE_PCT,
                 top_k_frac=None, label=""):
    """
    Simulate trading based on model's OOF predictions.

    - df_full: chronological dataframe (same as build_dataset output)
    - oof_preds: same length as df_full (NaN where no CV prediction)
    - threshold: min prob to take trade
    - top_k_frac: if set (e.g. 0.10), only trade top-K% by prob
    """
    df = df_full.reset_index(drop=True).copy()
    probs = np.asarray(oof_preds, dtype=float)
    assert len(df) == len(probs), f"df/probs mismatch: {len(df)} vs {len(probs)}"

    valid = ~np.isnan(probs)
    # Only trade rows where we had a CV prediction
    df = df[valid].reset_index(drop=True)
    probs = probs[valid]

    # Determine probability cutoff
    if top_k_frac is not None:
        k = max(1, int(len(probs) * top_k_frac))
        cutoff = np.sort(probs)[-k]
    else:
        cutoff = threshold

    sig_positions = np.where(probs >= cutoff)[0]

    close = df["close"].values
    high  = df["high"].values
    low   = df["low"].values
    ts    = df["timestamp"].values
    n     = len(df)
    cost  = (fee_pct + slippage_pct) / 100 * 2     # round-trip

    trades = []
    for i in sig_positions:
        entry = close[i]
        tp_px = entry * (1 + TP_PCT)
        sl_px = entry * (1 - SL_PCT)

        outcome = None
        pnl_pct = 0.0
        for j in range(i + 1, min(i + MAX_HOLD + 1, n)):
            if low[j] <= sl_px:
                outcome, pnl_pct = "SL", -SL_PCT
                break
            if high[j] >= tp_px:
                outcome, pnl_pct = "TP", +TP_PCT
                break

        if outcome is None:
            last = min(i + MAX_HOLD, n - 1)
            exit_px = close[last]
            pnl_pct = (exit_px - entry) / entry
            outcome = "TIME"

        net = pnl_pct - cost

        trades.append({
            "timestamp": ts[i],
            "entry":     entry,
            "outcome":   outcome,
            "gross_pct": pnl_pct,
            "net_pct":   net,
            "R":         net / SL_PCT,
            "prob":      probs[i],
        })

    trades = pd.DataFrame(trades)
    if trades.empty:
        print(f"\n❌ No trades taken for: {label}")
        return trades

    n_t = len(trades)
    wins = int((trades["net_pct"] > 0).sum())
    win_rate = wins / n_t
    total_net = trades["net_pct"].sum()
    avg_net = trades["net_pct"].mean()
    total_R = trades["R"].sum()
    avg_R = trades["R"].mean()

    equity = (1 + trades["net_pct"]).cumprod()
    max_dd = (equity / equity.cummax() - 1).min()

    print(f"\n{'═'*60}")
    print(f"  💰 PnL SIMULATION — {label}")
    print(f"{'═'*60}")
    print(f"  Trades              : {n_t:,}")
    print(f"  Wins / Losses       : {wins:,} / {n_t-wins:,}")
    print(f"  Win rate            : {win_rate:.2%}")
    print(f"  Avg gross / trade   : {trades['gross_pct'].mean()*100:+.3f}%")
    print(f"  Avg net   / trade   : {avg_net*100:+.3f}%   (after {cost*100:.2f}% round-trip cost)")
    print(f"  Total net PnL       : {total_net*100:+.2f}%")
    print(f"  Total R (1R={SL_PCT*100:.2f}%) : {total_R:+.1f}R")
    print(f"  Avg R / trade       : {avg_R:+.3f}R")
    print(f"  Final equity (1x)   : {equity.iloc[-1]:.3f}x")
    print(f"  Max drawdown        : {max_dd*100:.2f}%")
    print(f"  Outcome breakdown   :")
    for o in trades["outcome"].unique():
        cnt = (trades["outcome"] == o).sum()
        print(f"     {o:5s}: {cnt:>6,}  ({cnt/n_t:.1%})")
    print(f"{'═'*60}")

    return trades


def plot_equity_curves(curves, filename="equity_curves.png"):
    """curves = list of (label, trades_df)"""
    fig, ax = plt.subplots(figsize=(12, 5))
    for label, t in curves:
        if t is None or t.empty:
            continue
        eq = (1 + t["net_pct"]).cumprod()
        ax.plot(range(len(eq)), eq.values, label=f"{label}  (n={len(t)})")
    ax.axhline(1.0, color="gray", lw=0.8, ls="--")
    ax.set_title("Equity Curves — After Fees & Slippage")
    ax.set_xlabel("Trade #")
    ax.set_ylabel("Equity multiplier")
    ax.legend(loc="best")
    ax.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(filename, dpi=120, bbox_inches="tight")
    plt.close()
    print(f"\n✅ Saved: {filename}")


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    # 1) Data
    X, y, ts, feat_names, df = build_dataset(SYMBOLS)

    # 2) Train + CV
    model, auc, oof = train_and_evaluate(X, y, feat_names)

    # 3) SHAP (only if model learned something)
    if auc > 0.52:
        imp = explain_with_shap(model, X, feat_names)
        imp.to_csv("feature_importance.csv", index=False)
        print("\n✅ Saved: shap_summary.png, shap_bar.png, feature_importance.csv")

    # 4) PnL Simulations
    print("\n\n" + "█" * 60)
    print("   PnL SIMULATIONS  (fees + slippage applied)")
    print("█" * 60)

    t_all   = simulate_pnl(df, oof, threshold=0.5,             label="ALL signals (prob>0.5)")
    t_top10 = simulate_pnl(df, oof, threshold=0.0, top_k_frac=0.10, label="TOP 10% by prob")
    t_top5  = simulate_pnl(df, oof, threshold=0.0, top_k_frac=0.05, label="TOP 5% by prob")
    t_top2  = simulate_pnl(df, oof, threshold=0.0, top_k_frac=0.02, label="TOP 2% by prob")

    # 5) Equity curves
    plot_equity_curves([
        ("ALL",    t_all),
        ("Top10%", t_top10),
        ("Top5%",  t_top5),
        ("Top2%",  t_top2),
    ])

    # 6) Save trades
    for name, t in [("all", t_all), ("top10", t_top10),
                    ("top5", t_top5), ("top2", t_top2)]:
        if t is not None and not t.empty:
            t.to_csv(f"trades_{name}.csv", index=False)
    print("\n✅ Saved: trades_all.csv, trades_top10.csv, trades_top5.csv, trades_top2.csv")
    print("✅ Saved: equity_curves.png")