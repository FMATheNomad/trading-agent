# 🥁 FMA ALPHA QUANT LABS — The Blast Engine v3

> *"I don't need to know which way the market will move. I just need to be the ONLY ONE who knows when it moves."* — Nathan Mayer Rothschild

Bot trading kripto **100% engineering, zero AI cost** untuk Indodax.  
**396 commits.** Fully autonomous. Zero human intervention.

---

## 📂 File Structure

| File | Fungsi |
|------|--------|
| `main.py` | Orchestrator — portfolio cycle, momentum scanner, **state machine**, Telegram poller, deadman |
| `rules.py` | **Regime-gated decision engine** — BULL/BEAR → momentum, SIDEWAYS → mean-reversion, HIGH_VOL → survival |
| `risk_manager.py` | ATR SL/TP, **per-regime Kelly**, circuit breaker, validate_allocation |
| `executor.py` | Indodax HMAC-SHA512 signing, market/maker_first/maker orders |
| `data_layer.py` | Indodax REST API (ticker, pairs, depth, OHLCV) |
| `market_ws.py` | WebSocket realtime tickers (24h summary) |
| `private_ws.py` | WebSocket order fill notification |
| `deadman.py` | `/countdownCancelAll` safety net (15 menit) |
| `indicators.py` | Teknikal indikator (RSI, MACD, BB, EMA, ADX, MFI, Hurst, Skew) |
| `hmm_regime.py` | HMM regime detector (4 states) |
| `momentum.py` | Velocity-aware momentum engine |
| `patterns.py` | Paradiddle pattern matching (RLRL reversal detection) |
| `persist.py` | JSON state persistence (semua survive restart) |
| `db.py` | SQLite trade log + chat history |
| `notifier.py` | Telegram sender |
| `config.py` | Semua konfigurasi |
| `.env` | Environment variables (gitignored) |

---

## 🧠 Arsitektur — State Machine + Regime Gate

```
BUY → MARKET (keisi pasti, fee taker 0.31%)
  ↓
STATE MACHINE (kelola exit)
  ├── SIDEWAYS → TP_ACTIVE: pasang TP limit @ ATR×2, SL @ ATR×1.2
  ├── BULL → TRAILING: initial SL @ ATR×0.5, trailing SL @ ATR×1.5
  └── HIGH_VOL → survival (no entry)
       ↓
Transisi otomatis via WS tick:
  TP_ACTIVE → harga turun ke SL? → cancel TP, place SL
  SL_ACTIVE → harga balik? → cancel SL, place TP
  TRAILING  → harga naik? → trailing SL ikut naik
       ↓
FILLED → closed, cooldown 24h (gak ambil coin user)
```

### HMM sebagai HARD Gate

| State | Strategi | Entry | Exit | Kelly |
|-------|----------|-------|------|-------|
| BULL (≥5 cycle → 🔴 Rothschild) | Momentum + trailing | Market buy | **Trailing SL** (ATR×1.5) + pyramid | 25% |
| BEAR | Momentum defensif | Market buy | TP limit @ ATR×2 | 15% |
| SIDEWAYS | **Mean-reversion** (BB fade, RSI) | Market buy | TP limit @ ATR×0.5 | 10% |
| HIGH_VOL | Survival | ❌ No entry | Exit only | 5% |

---

## 🔬 State Machine — Mekanisme Utama

Setiap posisi punya **state** yang di-track di `_position_states`:

| State | Deskripsi |
|-------|-----------|
| `NEW` | Init, segera overwrite |
| `PENDING` | TP/SL gagal → retry tiap cycle |
| `TP_ACTIVE` | **TP fixed** @ entry + ATR×2. Harga turun? → cancel TP, place SL |
| `SL_ACTIVE` | **SL active**. Harga balik? → cancel SL, place TP |
| `TRAILING` | **No TP, trailing SL**. Harga naik? → SL ikut naik. Pyramid ON |

**Transisi realtime via WS tick** (bukan nunggu cycle 60 detik):
- TP limit order nangkring di exchange — keisi otomatis
- SL limit order nangkring — keisi kalau harga turun
- HARD SL: kalau harga >3% di bawah SL → force close di bid

---

## 🔬 Penemuan 1: Mean-Reversion Module

Aktif saat HMM = SIDEWAYS:

```
Entry:  Close ≤ BB_lower + RSI < 35 + volume surge
Exit:   TP ATR×0.5 (0.5-1%)
SL:     ATR×0.8
WR:     65-70%
Maks:   2 posisi
```

---

## 🔬 Penemuan 2: Paradiddle Pattern

Deteksi pola RLRL (Resistance/Low):

| Pola | Arti | Aksi |
|------|------|------|
| `RLRR` | Fake breakout | **SELL** |
| `LRLL` | Fake breakdown | **BUY** |
| `RRL` after `RRR` | Exhaustion | **SELL** |
| `LLR` after `LLL` | Exhaustion | **BUY** |

---

## 🔬 Penemuan 3: The Rothschild Asymmetry

### Power Law: 90% loss rate, 10% home run

```
Initial SL: ATR×0.5 (2.5%) — tight cut
Trailing SL: ATR×1.5 — ride runner
Pyramid: profit ATR×0.5 → tambah 50% market buy
Entry_mode per posisi: exit pake aturan saat entry, bukan mode aktif
```

Matematika:
- 9 trade rugi 2.5% × 9 = -22.5%
- 1 trade profit 10% × 1.5 (pyramid) = +15%
- **Masih negatif tanpa trailing.** Trailing SL bikin winner bisa 15-20% → **NET POSITIF.**

---

## 🔬 Penemuan 4: Reflexive Vortex Model (RVM)

Bukan ngukur magnitude sinyal, tapi **phase alignment** 4 loop:

1. Price acceleration — velocity makin cepat
2. Volume surge — volume > price velocity
3. Order book imbalance — tekanan beli/jual real-time
4. HMM confidence — BULL/BEAR > 0.9

| Loop Aligned | Aksi | Size |
|---|---|---|
| ≥3 (VORTEX) | ALL-IN, trailing ATR×3, no TP | 50%+ |
| 2 | Rothschild normal | 15-25% |
| 0-1 | Skip | 0-10% |

---

## 🛡️ Circuit Breaker — 3 Lapis

| Lapis | Trigger | Aksi | Persist |
|-------|---------|------|---------|
| **Daily loss** | Equity turun Rp15k dari peak | Stop entry, TP-only | ✅ |
| **Consecutive loss** | 5 hari loss berturut | **Stop total 72 jam** | ✅ |
| **Equity floor** | Equity < Rp10k | **Shutdown total** | ✅ |
| **Drawdown** | 15% dari all-time peak | Warning | ✅ |
| **Deadman** | Bot crash 15 menit | Exchange cancel order | Exchange |
| **SM cooldown** | 24h setelah SM FILLED | Skip re-init (aman buat user) | ❌ |

---

## 💰 Fee Struktur (Market Buy + Maker Sell)

| Transaksi | Fee |
|-----------|-----|
| BUY market | 0.31% (taker) |
| SELL limit (TP) | 0.20% + PPh 0.21% + CFX 0.0111% |
| **Round trip** | **~0.73%** |

---

## 📦 Deployment

```
Procfile:  worker: python main.py
Runtime:   python-3.12
Platform:  Railway (auto-deploy from GitHub)
Volume:    /data (state.json + trades.db)
Total:     396 commits, ~4000 baris kode
```

---

## 🤖 Telegram Commands

| Command | Fungsi |
|---------|--------|
| `/status` | Portfolio & posisi |
| `/ask <coin>` | Detail sinyal & ATR koin |
| `/atr` | ATR, SL, TP semua posisi |
| `/greed` | Bypass daily loss (1×/hari) |
| `/why` | Alasan bot gak trading |
| `/commands` | Daftar lengkap |
| `/project` | Proyeksi harian, bulanan, tahunan |
| `/today` | Performa hari ini |
| `/perf` | Win rate & stats |

---

## 🏆 Benchmark

Dari riset independen: **"Boutique systematic prop-trading system"** — secara risk-management dan signal architecture, bot ini **jauh di atas retail bot** (3Commas/Bitsgap/Cryptohopper/Gunbot) dan setara **junior prop desk** institusional.

**Unik:** Satu-satunya bot dengan **Full Indodax API Coverage** — tidak ada platform retail besar yang support Indodax.

---

## ⚖️ License

**Copyright (C) 2026 FMA ALPHA QUANT LABS** — AGPL-3.0

---

> *"Yang penting bukan profit di bear market. Yang penting masih idup pas bull datang."* — FMA
