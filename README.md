# FMA ALPHA QUANT LABS 🤖

**AI trading bot untuk Indodax** — Systematic quant + AI Chief Investment Officer (DeepSeek). Target: compound equity secara stabil dengan risk management berlapis.

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
│  │ SL/TP    │ │Validate  │← Kelly ← Cooldown ← Blacklist     │ │
│  │ Trailing │ │Allocation│                └──────────────────┘ │
│  └──────────┘ └──────────┘                                      │
│       ↓                                                         │
│  ┌──────────┐ ┌──────────┐ ┌──────────────────┐               │
│  │ Telegram │ │ SQLite   │ │ Deadman Switch   │               │
│  │ Event    │ │ Logging  │ │ + Order Cleanup  │               │
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

**Sebelum:** `if buys/total >= 0.4 → BULL` (heuristic)
**Sesudah:** Hidden Markov Model 4-state dengan covariance analysis:

| State | Karakteristik |
|---|---|
| SIDEWAYS | Volatility rendah, mean-reversion |
| BULL | Vol medium, return positif |
| BEAR | Vol medium, return negatif |
| HIGH_VOL | Vol tinggi, outlier detection |

Fallback ke heuristic kalo HMM confidence < 60%.

### 📈 Cointegration Engine

**Sebelum:** z-score simple dari ratio harga / rolling window
**Sesudah:** Johansen test + ADF test + half-life mean reversion + Hurst exponent

Pasangan: BTC/ETH, SOL/ADA, BNB/XRP. Signal LONG_SPREAD / SHORT_SPREAD saat z > 2 atau z < -2.

### 💰 Entry & Exit Strategy

```
CIO → hanya bisa BUY   (entry only)
Exit → SL / TP / Trailing Stop   (exit otomatis)
Cooldown → 12 jam setelah posisi closed
Blacklist → skip pair yang pernah SL
```

### 📉 Risk Management

| Parameter | Alpha Mode 🔴 |
|---|---|
| Stop Loss | min **8%** (ATR×1.5 floor) |
| Take Profit | **ATR×3** dinamis (1.5% utk BTC, 25%+ utk memecoin) |
| Trailing Stop | **4%** dari harga tertinggi |
| Position Sizing | **Kelly Criterion** (optimal f) |
| Max Positions | **4** |
| Allocation per asset | max **95%** |
| Portfolio Drawdown | **35%** |
| Daily Loss Floor | **Rp60.000** (real stop, bukan warning) |
| Fee Model | **Market order** (~0.35% taker) |
| Cooldown | **12 jam** — gak re-entry pair yg baru dijual |
| Coin Blacklist | Otomatis — pair yang kena SL diskip |

### 🔌 Real-Time & Execution

- **Market WebSocket** — streaming 24h summary semua pair
- **Private WebSocket** — notifikasi order FILL/DONE real-time
- **Balance Poller** — update tiap 30 detik
- **Deadman Switch** — 15 menit. Auto cancel orders kalo bot mati
- **Order Cleanup** — cancel orphan TP limit orders dari deploy sebelumnya
- **fmt_qty** — format quantity sesuai `trade_min_traded_currency` (integer utk PIPPIN, 8 desimal utk BTC)

### 📱 Notifikasi (Event-Based)

Tidak ada spam tiap 5 menit. Telegram cuma dikirim pas:

- **Trade execution** (BUY/SELL)
- **SL/TP/Trailing triggered**
- **Regime change** (BULL↔BEAR)
- **Equity move >5%**
- **Signal muncul** (BUY score ≥+3)
- **Summary** tiap 60 cycle (~5 jam)
- **Error** serius

## Struktur File

```
trading-agent/
├── main.py            # 1.022 baris — Orchestrator
├── config.py          # 105 parameter
├── indicators.py      # TA + scoring + advanced features (16 indikator)
├── hmm_regime.py      # Hidden Markov Model 4-state
├── cointegration.py   # Johansen/ADF + half-life + Hurst
├── ml_signal.py       # XGBoost ensemble
├── features.py        # Microstructure (entropy, VPIN, order flow)
├── llm_filter.py      # DeepSeek prompt builder + CIO context
├── executor.py        # HMAC-SHA512 TAPI v1
├── risk_manager.py    # SL/TP, trailing, Kelly, ATR
├── data_layer.py      # Indodax public REST API
├── pairs.py           # Legacy z-score (cointegration engine yg dipake)
├── db.py              # SQLite (trades, meta, positions)
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

Total: **~3.300 baris Python**

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Buat API Key
- **Indodax**: https://indodax.com/trade_api → permission **trade-only**
- **DeepSeek**: https://platform.deepseek.com/api_keys
- **Telegram**: chat @BotFather → `/newbot` → token

### 3. Konfigurasi `.env`
```env
INDODAX_API_KEY=...
INDODAX_SECRET_KEY=...
DEEPSEEK_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
PAPER_TRADING=false
ALPHA_MODE=true
PLAY_CAPITAL_IDR=200000
MAX_OPEN_POSITIONS=4
```

### 4. Jalankan
```bash
python main.py
```

## Deployment (Railway)

```bash
railway up --service trading-agent
```

Auto-deploy dari GitHub: push ke `main` → Railway deploy otomatis.

## Cara Kerja (1 Cycle = 5 menit)

```
1. FETCH 40 viable IDR pairs (volume > 100jt/24h)
2. FETCH OHLCV 1h + 4h (concurrent, max 10)
3. COMPUTE signals:
   a. 16 technical indicators (RSI, MACD, BB, ADX, MFI, Hurst, dll)
   b. XGBoost ML prediction
   c. Multi-factor scoring → BUY/SELL/HOLD
4. COINTEGRATION scan (3 correlated pairs)
5. HMM regime detection (4-state probabilistic)
6. DETECT external positions via Indodax balance
7. MICROSTRUCTURE features (orderbook, VPIN, entropy)
8. CALL DeepSeek CIO → decision (BUY only — no sell)
9. CHECK SL/TP/trailing untuk semua posisi
10. COOLDOWN + BLACKLIST filter untuk BUY baru
11. VALIDATE allocation (Kelly + correlation)
12. EXECUTE market orders
13. PLACE TP limit order di harga target
14. CANCEL orphan orders
15. EVENT-BASED Telegram notification
16. REFRESH deadman switch
17. SLEEP 5 menit → ulang
```

## Mode

| Mode | SL | TP | Min Buy Score | Play Capital |
|---|---|---|---|---|
| **Alpha** 🔴 **(Default)** | min 8% (ATR×1.5) | **ATR×3 dinamis** | ≥+2 | 70-95% |
| Standard | 5% | 5% | ≥+3 | 30-60% |

## Configuration (config.py)

| Parameter | Default | Deskripsi |
|---|---|---|
| PLAY_CAPITAL_IDR | 200000 | Modal bot |
| MIN_ORDER_IDR | 20000 | Minimum order |
| MAX_OPEN_POSITIONS | 4 | Maksimal posisi |
| LOOP_INTERVAL_SECONDS | 300 | Siklus (5 menit) |
| STOP_LOSS_PCT | -0.08 | SL 8% (floor) |
| TAKE_PROFIT_PCT | 0.25 | TP fallback |
| ATR_TP_MULTIPLIER | 3.0 | TP = ATR × 3 |
| ATR_SL_MULTIPLIER | 1.5 | SL = ATR × 1.5 (min 8%) |
| KELLY_FRACTION | 0.25 | Kelly Criterion baseline |
| AUTO_COMPOUND | True | Reinvest 50% profit |
| COINT_Z_ENTRY | 2.0 | Z-score threshold entry |
| COINT_Z_EXIT | 0.5 | Z-score threshold exit |
| HMM_N_STATES | 4 | Jumlah state HMM |
| ML_FORECAST_HORIZON | 5 | XGBoost prediction horizon |
| DAILY_LOSS_FLOOR_IDR | 60000 | Stop trading jika equity < 60k |

## Catatan Penting

- **CIO hanya bisa BUY** — exit cuma via SL/TP/trailing. Gak ada panic selling.
- **Cooldown 12 jam** — pair yang baru closed gak dibeli lagi.
- **Coin blacklist** — otomatis skip pair yang kena SL.
- **Database SQLite** — file `trades.db` di direktori project. Hilang saat Railway restart.
- **API key hanya perlu permission "trade"** — jangan pernah beri permission "withdraw".
- Untuk AI agent lain: baca `config.py` dulu untuk semua parameter.
