# FMA Alpha Quant Labs — AI Hedge Fund Manager

**Auto-trading bot untuk Indodax** dengan AI Chief Investment Officer (CIO) berbasis DeepSeek V4 Flash. Menggabungkan multi-timeframe technical analysis, statistical arbitrage, regime detection, dan portfolio risk management.

## Arsitektur

```
┌─────────────────────────────────────────────────────────┐
│                    main.py (Orchestrator)               │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐  │
│  │ Scan 40  │ │ OHLCV    │ │Indicators│ │  DeepSeek │  │
│  │ Pairs    │→│ 1h + 4h  │→│ Scoring  │→│    CIO    │  │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘  │
│                                    ↓                   │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐                │
│  │ Risk     │→│Validate  │→│ Execute  │                │
│  │ Manager  │ │Allocation│ │ Order    │                │
│  └──────────┘ └──────────┘ └──────────┘                │
│                                    ↓                   │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐                │
│  │ Telegram │ │ SQLite   │ │ Deadman  │                │
│  │ Notif    │ │ Logging  │ │ Switch   │                │
│  └──────────┘ └──────────┘ └──────────┘                │
└─────────────────────────────────────────────────────────┘
```

## Fitur

### 📊 Market Analysis
- **40+ pairs scan** by 24h volume (setiap 5 menit)
- **Multi-timeframe**: 1h entry timing + 4h macro trend
- **Technical indicators**: RSI(14), EMA(9/21/50), MACD, Bollinger Bands
- **Volume spike detection** (ratio vs 20-candle average)
- **Momentum streak** (consecutive candles direction)
- **Multi-factor scoring**: -8 to +8 (BUY ≥+3, SELL ≤-3)
- **Order book imbalance** (bid/ask pressure)
- **Regime classifier**: BULL/BEAR/HIGH_VOL/SIDEWAYS

### 🧠 AI Chief Investment Officer (DeepSeek)
- **Portfolio manager** yang menganalisis SEMUA data pasar
- **Regime-based strategy**: perilaku berubah sesuai kondisi pasar
- **Multi-timeframe conviction** (TF alignment = HIGH conviction)
- **Auto-adjust play capital** berdasarkan market conviction
- **Dynamic SL/TP** berdasarkan ATR (Average True Range)
- **Alpha Mode**: SL 10% TP 20%, lebih agresif

### 📈 Pairs Trading (Stat-Arb)
- **Z-score mean reversion** pada pasangan korelasi: BTC/ETH, SOL/ADA, BNB/XRP
- Signal LONG_SPREAD (z < -2) / SHORT_SPREAD (z > 2)
- Market-neutral strategy

### 📉 Risk Management
| Parameter | Standard | Alpha |
|-----------|----------|-------|
| Stop Loss | 5% | 10% |
| Take Profit | 5% | 20% |
| Daily Loss Floor | Rp60.000 | Rp40.000 |
| Portfolio Drawdown | 20% | 30% |
| Max Positions | 3 | 3 |
| Position Cap | 90% per asset | 90% per asset |
| Fee Estimate | 0.35% taker | 0.35% taker |

### 🔌 Koneksi Real-Time
- **Market WebSocket**: streaming 24h summary semua pair
- **Private WebSocket**: notifikasi order FILL/DONE real-time
- **Balance Poller**: update saldo tiap 30 detik
- **Deadman Switch** (15 menit): cancel semua order jika bot mati

### 📱 Telegram Bot
- Notifikasi tiap 5 menit: `CIO: HOLD/REBALANCE | Regime: X | Play: X%`
- `/status` — cek portfolio, posisi, mode
- SL/TP trigger notifications
- Order execution reports

## Struktur File

```
trading-agent/
├── main.py          # Orchestrator — startup → loop → shutdown
├── config.py        # Semua parameter (81 baris)
├── data_layer.py    # Fetch Indodax API (ticker, OHLCV, orderbook)
├── indicators.py    # Teknikal indikator + multi-factor scoring
├── llm_filter.py    # DeepSeek API prompt builder + response parser
├── executor.py      # HMAC-SHA512 signing, TAPI v1 + v2
├── risk_manager.py  # SL/TP, ATR, portfolio drawdown, fee
├── pairs.py         # Pairs trading z-score engine
├── db.py            # SQLite (trades, meta, positions, decisions)
├── deadman.py       # Deadman Switch (countdown cancel)
├── market_ws.py     # Market data WebSocket client
├── private_ws.py    # Private WebSocket client (order status)
├── notifier.py      # Telegram sender
├── backtest.py      # Backtest engine
├── Procfile         # Railway: worker → python main.py
├── runtime.txt      # Python 3.12
├── requirements.txt # Dependencies
└── .env             # API keys (gitignored)
```

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```
### 2. Buat API Key
- **Indodax**: https://indodax.com/trade_api → permission **trade-only**
- **DeepSeek**: https://platform.deepseek.com/api_keys
- **Telegram**: chat @BotFather → `/newbot` → dapatkan token

### 3. Konfigurasi `.env`
```env
INDODAX_API_KEY=...
INDODAX_SECRET_KEY=...
DEEPSEEK_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
PAPER_TRADING=true            # true = simulasi, false = LIVE
ALPHA_MODE=false              # true = SL10% TP20%
```

### 4. Jalankan
```bash
python main.py
```

## Deployment (Railway)

### 1. Build & Deploy
```bash
railway up --service trading-agent
```

### 2. Set Environment Variables
```bash
railway variables set KEY=VALUE --service trading-agent
```

### 3. Auto-deploy dari GitHub
Service sudah terhubung ke `FMATheNomad/trading-agent`. Push ke `main` → auto-deploy.

## Mode

| Mode | SL | TP | Min Buy Score | Play Capital | Cocok Untuk |
|------|----|----|---------------|-------------|-------------|
| **Standard** | 5% | 5% | ≥+3 | 30-60% | Konservatif |
| **Alpha** 🔴 | 10% | 20% | ≥+2 | 60-90% | Risk taker |

Aktifkan Alpha Mode: `railway variables set ALPHA_MODE=true --service trading-agent`

## Cara Kerja (1 Cycle = 5 menit)

```
1. FETCH 40 viable IDR pairs (volume >100jt/24h)
2. FETCH OHLCV 1h + 4h untuk setiap pair
3. COMPUTE indicators: RSI, EMA, MACD, BB, momentum, volume ratio
4. COMPUTE pairs trading z-score
5. CLASSIFY regime (BULL/BEAR/SIDEWAYS/HIGH_VOL)
6. DETECT external positions dari balance Indodax
7. CALL DeepSeek CIO → decision + play capital + trades
8. CHECK SL/TP untuk semua posisi
9. VALIDATE allocation + risk
10. EXECUTE orders via Indodax TAPI
11. SEND Telegram notification
12. REFRESH deadman switch (jika ada posisi)
13. SLEEP 5 menit → ulang
```

## AI Prompt Structure

`llm_filter.py` berisi:
- **BASE_PROMPT**: instruksi dasar untuk DeepSeek sebagai crypto quant trader
- **ALPHA_PROMPT**: tambahan instruksi agresif (aktif jika ALPHA_MODE=true)
- **SYSTEM_PROMPT**: BASE_PROMPT + ALPHA_PROMPT (dikombinasikan saat startup)

DeepSeek return JSON:
```json
{
  "decision": "HOLD | REBALANCE",
  "play_capital_pct": 50,
  "reasoning": "...",
  "trades": [
    {"pair": "btc_idr", "action": "BUY", "allocation_pct": 80, "reason": "..."}
  ]
}
```

## Catatan Penting

- **Modal Rp105.204** (cash) + INJ + HUMANITY + ETH = **~Rp235.000 equity**
- **Posisi saat ini:** INJ (0.324), HUMANITY (55.28), ETH (0.00028) — STIK & ZEREBRO sudah dijual (cut loss)
- **API key hanya perlu permission "trade"** — jangan pernah beri permission "withdraw"
- **Telegram bot** — jika ganti nama/token, update env Railway
- **Database SQLite** — file `trades.db` di direktori project
- **Logging** — Railway logs via `railway service logs`
- Untuk AI agent lain: baca `config.py` dulu untuk semua parameter yang bisa di-tuning
