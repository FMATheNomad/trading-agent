# FMA ALPHA QUANT LABS 🤖

**Fully Autonomous AI Trading Bot untuk Indodax** — Systematic quant + AI Chief Investment Officer (DeepSeek V4 Flash). Berjalan 24/7 di Railway tanpa intervensi manual. Target: compound equity secara stabil dengan risk management berlapis.

## Status: ✅ PRODUCTION — LIVE TRADING

- **Mode:** LIVE (Alpha Mode ON)
- **Platform:** Railway (Southeast Asia)
- **Region:** Singapore (latency ~30-50ms ke Indodax)
- **Equity:** Rp300-350rb (seed capital Rp200rb + deposit)
- **Uptime:** 24/7, restart otomatis jika crash

## Arsitektur

```
┌──────────────────────────────────────────────────────────────────┐
│                     main.py (Orchestrator)                       │
│  ┌──────────┐ ┌──────────┐ ┌────────────┐ ┌──────────────────┐ │
│  │ Scan 40  │ │ OHLCV    │ │  HMM +    │ │   DeepSeek CIO    │ │
│  │ Pairs    │→│ 1h + 4h  │→│Indicators │→│ (entry only)      │ │
│  └──────────┘ └──────────┘ │+ XGBoost  │ └──────────────────┘ │
│                            │+ Features │         ↓             │
│  ┌──────────┐ ┌──────────┐ └────────────┘ ┌──────────────────┐ │
│  │ ATR SL   │ │ Profit   │← Kelly ← Blacklist               │ │
│  │ ATR TP   │ │ Rotate   │                └──────────────────┘ │
│  │ Trailing │ │ ≥2%      │                                      │
│  └──────────┘ └──────────┘                                      │
│       ↓                                                         │
│  ┌──────────┐ ┌──────────┐ ┌──────────────────┐               │
│  │ Telegram │ │ Volume   │ │ Deadman Switch   │               │
│  │ Event    │ │ /data    │ │ + Order Cleanup  │               │
│  └──────────┘ └──────────┘ └──────────────────┘               │
└──────────────────────────────────────────────────────────────────┘
```

## Fitur Lengkap

### 📊 Market Analysis — 3-Layer Signal Stack

| Layer | Modul | Fungsi |
|---|---|---|
| **Layer 1: Technical** | `indicators.py` | RSI(14), Stoch, ADX, MFI, EMA(9/21/50), MACD, BB, ATR, Hurst exponent, skew/kurtosis, VPIN, entropy, volume ratio, momentum streak |
| **Layer 2: ML** | `ml_signal.py` | XGBoost ensemble — train dari OHLCV historis, infer real-time. Retrain tiap 50 cycle |
| **Layer 3: AI** | `llm_filter.py` | DeepSeek V4 Flash sebagai portfolio manager, analyze semua data + regime + orderbook |

### 🧠 Regime Detection — HMM Probabilistic

Hidden Markov Model 4-state dengan covariance analysis, fallback ke heuristic jika confidence < 60%:

| State | Karakteristik |
|---|---|
| SIDEWAYS | Volatility rendah, mean-reversion |
| BULL | Vol medium, return positif |
| BEAR | Vol medium, return negatif |
| HIGH_VOL | Vol tinggi, outlier detection |

### 📈 Cointegration Engine

Johansen test + ADF test + half-life mean reversion + Hurst exponent untuk pasangan BTC/ETH, SOL/ADA, BNB/XRP. Signal LONG_SPREAD / SHORT_SPREAD saat z > 2 atau z < -2.

### 💰 Entry & Exit Strategy

```
CIO → hanya bisa BUY (entry only)
Exit → ATR SL / ATR TP / Trailing Stop / Profit Rotate ≥2%
Semua posisi diperlakukan sama — tidak ada beda "bot" vs "external"
```

### 📉 Risk Management (Current)

| Parameter | Alpha Mode 🔴 |
|---|---|
| Stop Loss | **ATR × 2** (dinamis, 1.3-3.5%) — NO fixed floor |
| Take Profit | **ATR × 2.5** (via cycle check) |
| Trailing Stop | **4%** dari harga tertinggi |
| Position Sizing | **Kelly Criterion** (optimal f) |
| Max Positions | **Dinamis** (4 untuk equity <5jt, 5 untuk <10jt, 6 untuk ≥10jt) |
| Allocation per asset | max **95%** |
| Portfolio Drawdown | **35%** (warning only) |
| Daily Loss Floor | **Rp60.000** (real stop) |
| Fee Model | **Market order** (~0.35% taker) — zero standing limit orders |
| Coin Blacklist | Otomatis — pair yang kena SL diskip |
| Profit Rotate | CIO hanya bisa jual jika profit ≥2% |

### 🔌 Real-Time & Execution

- **Market WebSocket** — streaming 24h summary semua pair
- **Private WebSocket** — notifikasi order FILL/DONE real-time
- **Balance Poller** — update tiap 30 detik
- **Deadman Switch** — 15 menit. Auto cancel orders jika bot mati
- **Format Quantity** — `fmt_qty()` handle integer-only coins (PIPPIN, TROLLSOL) dan multi-decimal
- **Dedup** — positions otomatis dibersihkan dari duplikat tiap cycle

### 📱 Notifikasi (Event-Based)

Tidak ada spam. Telegram hanya dikirim saat:

- **Trade execution** (BUY/SELL)
- **SL/TP/Trailing triggered**
- **Regime change**
- **Equity move >5%**
- **Signal muncul** (BUY score ≥+3)
- **Summary** tiap 60 cycle (~5 jam)
- **Error** serius

### 🛡️ Error Handling & Recovery

- **Startup Guard** — cycle 1 setelah restart: semua posisi dari balance di-restore, CIO dilarang jual
- **Persist Volume** — `/data/state.json` di Railway volume mount, tahan restart
- **Order Cleanup** — cancel orphan sell orders di startup
- **Deadman Switch** — cancel semua order jika bot mati >15 menit

## Struktur File

```
trading-agent/
├── main.py            # ~1.100 baris — Orchestrator
├── config.py          # 110+ parameter
├── indicators.py      # TA + scoring + advanced features
├── hmm_regime.py      # Hidden Markov Model 4-state
├── cointegration.py   # Johansen/ADF + half-life + Hurst
├── ml_signal.py       # XGBoost ensemble
├── features.py        # Microstructure (entropy, VPIN, order flow)
├── llm_filter.py      # DeepSeek prompt builder + CIO context
├── executor.py        # HMAC-SHA512 TAPI v1
├── risk_manager.py    # SL/TP, trailing, Kelly, ATR
├── data_layer.py      # Indodax public REST API
├── pairs.py           # Legacy z-score
├── db.py              # SQLite (trades, meta)
├── persist.py         # JSON state persistence (Railway volume)
├── deadman.py         # Deadman switch
├── market_ws.py       # Market data WebSocket
├── private_ws.py      # Private WebSocket
├── notifier.py        # Telegram sender
├── backtest.py        # Walk-forward + Monte Carlo
├── Procfile           # Railway worker
├── runtime.txt        # Python 3.12
├── requirements.txt   # hmmlearn, statsmodels, xgboost, scikit-learn
└── .env               # API keys (gitignored)
```

Total: **~3.300 baris Python, 18 modul**

## Cara Kerja (1 Cycle = 5 menit)

```
1. FETCH 40 viable IDR pairs (volume > 100jt/24h)
2. FETCH OHLCV 1h + 4h (concurrent, max 10)
3. COMPUTE signals:
   a. 16 technical indicators
   b. XGBoost ML prediction
   c. Multi-factor scoring → BUY/SELL/HOLD
4. COINTEGRATION scan (3 correlated pairs)
5. HMM regime detection (4-state probabilistic)
6. DETECT balance + restore positions dari Indodax
7. CHECK SL/TP/trailing untuk semua posisi (ATR-based)
8. CALL DeepSeek CIO → decision (BUY only — profit rotate ≥2%)
9. BLACKLIST filter + startup guard + dedup
10. VALIDATE allocation (Kelly + correlation)
11. EXECUTE market orders (zero standing limit orders)
12. EVENT-BASED Telegram notification
13. REFRESH deadman switch
14. DEDUP positions
15. PERSIST state ke volume
16. SLEEP 5 menit → ulang
```

## Setup

```bash
pip install -r requirements.txt
```

```env
INDODAX_API_KEY=...
INDODAX_SECRET_KEY=...
DEEPSEEK_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
PAPER_TRADING=false
ALPHA_MODE=true
PLAY_CAPITAL_IDR=200000
```

## Railway Deployment

```bash
railway volume add -m /data           # persistent state
railway up --service trading-agent
```


