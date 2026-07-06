# 🥁 FMA ALPHA QUANT LABS — The Blast Engine v2

> *"I don't need to know which way the market will move. I just need to be the ONLY ONE who knows when it moves."* — Nathan Mayer Rothschild

Bot trading kripto **100% engineering, zero AI cost** untuk Indodax.  
Menggabungkan filosofi extreme metal drumming, quantitative trading, regime-switching HMM, dan **asymmetric capitalism**.

---

## 📂 File Structure

| File | Fungsi |
|------|--------|
| `main.py` | Orchestrator — portfolio cycle, momentum scanner, realtime SL/TP, Telegram poller, deadman |
| `rules.py` | **Regime-gated decision engine** — BULL/BEAR → momentum, SIDEWAYS → mean-reversion, HIGH_VOL → survival |
| `momentum.py` | Velocity-aware momentum engine |
| `patterns.py` | Paradiddle pattern matching (RLRL reversal detection) |
| `risk_manager.py` | ATR SL/TP, two-tier stop, pyramid trigger, **per-regime Kelly**, circuit breaker |
| `executor.py` | Indodax HMAC-SHA512 signing, maker_first/market orders |
| `data_layer.py` | Indodax REST API (ticker, pairs, depth, OHLCV) |
| `market_ws.py` | WebSocket realtime tickers (24h summary) |
| `private_ws.py` | WebSocket order fill notification |
| `deadman.py` | `/countdownCancelAll` safety net (15 menit) |
| `indicators.py` | Teknikal indikator (RSI, MACD, BB, EMA, ADX, MFI, Hurst, Skew) |
| `hmm_regime.py` | HMM regime detector (4 states: BULL/BEAR/SIDEWAYS/HIGH_VOL) |
| `persist.py` | JSON state persistence (positions, blacklist, cooldown, circuit breaker, peak capital) |
| `db.py` | SQLite trade log + chat history |
| `notifier.py` | Telegram sender |
| `config.py` | Semua konfigurasi + parameter |
| `.env` | Environment variables (gitignored) |

---

## 🧠 Arsitektur — Regime-Gated Engine

```
HMM GATE (classify_regime)
    │
    ├── BULL/BEAR ───→ Rothschild Mode (momentum + trailing TP + pyramid)
    │
    ├── SIDEWAYS ────→ Mean-Reversion Mode (BB fade + RSI extreme + grid ringan)
    │
    └── HIGH_VOL ────→ Survival Mode (no entry, kurangi posisi)
```

### HMM sebagai HARD Gate

Bukan sekadar informasi — HMM **menentukan strategi mana yang aktif**.

| State | Mode | Strategi | WR Target | Position Size |
|-------|------|----------|-----------|---------------|
| BULL (≥5 cycle) | 🔴 Rothschild | Momentum + trailing TP + pyramid | 35-40% | Kelly 25% |
| BEAR | 🔴 Rothschild | Momentum defensif, cut ketat | 35-40% | Kelly 15% |
| SIDEWAYS | 🟢 Konservatif | **Mean-reversion** (BB fade, RSI < 35 > 70) | **65-70%** | Kelly 10% |
| HIGH_VOL | ⚪ Survival | No entry, exit only | - | Kelly 5% |

### 🔬 Penemuan: Stability Buffer

Market kripto sangat labil. Buffer anti-flip:
- **BULL streak ≥ 5 cycle** → Rothschild ON (terverifikasi)
- **Bear streak ≥ 3 cycle** → Rothschild OFF (sideway/bear terverifikasi)
- **SIDEWAYS/HIGH_VOL** → kedua streak reset ke 0

---

## 🔬 Penemuan 1: Mean-Reversion Module

**Masalah:** Momentum/trend-following kalah di SIDEWAYS — ini properti bawaan, bukan bug.

**Solusi:** Modul mean-reversion yang HANYA aktif saat HMM = SIDEWAYS:

```
Entry:  Close ≤ BB_lower + RSI < 35 + volume confirmation
Exit:   TP ATR×0.5 (0.5-1%) atau RSI > 70 + BB_upper
SL:     ATR×0.8
WR:     65-70%
Maks:   2 posisi
```

**Fee edge:** Entry via limit order (maker) — fee hanya 0.20% buy + 0.41% sell (termasuk PPh) = round trip ~0.62%. Dengan target 0.5-1%, profit masih positif.

---

## 🔬 Penemuan 2: Paradiddle Pattern Matching

Deteksi pola RLRL (Resistance/Low) untuk prediksi reversal.

| Pola | Arti | Aksi |
|------|------|------|
| `RLRR` | Fake breakout (puncak palsu) | **SELL** |
| `LRLL` | Fake breakdown (dasar palsu) | **BUY** |
| `RRL` after `RRR` | Exhaustion | **SELL** |
| `LLR` after `LLL` | Exhaustion | **BUY** |

**Implementasi:** `patterns.py` — dipakai momentum scanner sebelum entry.

---

## 🔬 Penemuan 3: The Rothschild Asymmetry

### 3 Pilar:

#### 3a. Limit Order Entry (Toll Booth)
```
BUKAN: beli market kapan pun ada sinyal → kena spread + fee taker 0.31%
TAPI:  pasang limit order di support ATR → fee maker 0.20%
       kalau 60 detik gak keisi → retry 3x naik harga → market order
```

#### 3b. Ultra-Tight Initial SL (Power Law)
**90% trade boleh rugi asal 10% sisanya home run.**

```
Tier 1 (0-30 menit): SL ATR×0.3 — rugi 1-2%, cut cepat
Tier 2 (>30 menit):  Trailing SL ATR×1.5
Pyramid:             Kalau profit ATR×0.5, tambah 50% posisi
```

Matematika:
- 9 trade rugi 1.5% × 9 = -13.5%
- 1 trade profit 10% × 1.5 (pyramid) = +15%
- **NET: +1.5% walau 90% gagal!**

#### 3c. Entry_Mode per Posisi

Setiap posisi mencatat `entry_mode` (ROTHSCHILD/KONSERVATIF).  
Exit menggunakan aturan sesuai mode entry, **bukan mode aktif sekarang**.

---

## 🔬 Penemuan 4: Adaptive Regime Switching

**Masalah:** Trend-following unggul di BULL, rugi di SIDEWAYS.  
**Solusi:** HMM gate + stability buffer.

```
BULL 5 cycle berturut → Rothschild 🔴 (6 slot @15%)
Non-BULL 3 cycle      → Konservatif 🟢 (4 slot @25%)
SIDEWAYS              → Mean-reversion   (2 slot @10%)
HIGH_VOL              → Survival          (no entry)
```

---

## 🔬 Penemuan 5: The Reflexive Vortex Model (RVM)

Terinspirasi George Soros — pasar itu reflexive, harga↔fundamental saling mempengaruhi.

Bukan ngukur **magnitude** sinyal, tapi **PHASE ALIGNMENT** dari 4 loop:

1. **Price acceleration** — velocity makin cepat
2. **Volume surge** — volume > price velocity
3. **Order book imbalance** — tekanan beli/jual real-time
4. **HMM confidence** — BULL/BEAR dengan confidence > 0.9

| Jumlah Loop Aligned | Aksi | Position Size |
|---------------------|------|---------------|
| ≥3 (VORTEX) | ALL-IN, trailing ATR×3, no TP | 50%+ |
| 2 | Rothschild normal | 15-25% |
| 0-1 | Konservatif/skip | 0-10% |

---

## 🛡️ Risk Management & Circuit Breaker

| Lapis | Trigger | Aksi | Persist? |
|-------|---------|------|----------|
| **Daily loss** | Equity turun Rp15k dari peak harian | Stop entry, TP-only | ✅ |
| **Consecutive loss** | 5 hari loss berturut-turut | Stop total 72 jam | ✅ |
| **Equity floor** | Equity < Rp10k | Stop total (shutdown) | ✅ |
| **Portfolio drawdown** | 15% dari all-time peak | Print warning | ✅ |
| **Deadman switch** | Bot crash 15 menit | Exchange cancel semua order | ❌ (exchange-side) |
| **Blacklist** | Kena SL | Pair diblokir (FIFO max 50) | ✅ |
| **Cooldown** | Kena SL | 12h / 60min (Rothschild) | ✅ |
| **ATR filter** | ATR > 25% atau vol < 500jt | Skip entry | ❌ |
| **Fee viability** | Fee makan profit | Skip trade | ❌ |
| **Orphan cleanup** | Tiap 10 cycle + startup | Cancel order liar | ❌ |

---

## ⚙️ Parameter per Regime

| Parameter | BULL Rothschild | SIDEWAYS MeanRev | HIGH_VOL |
|-----------|----------------|-------------------|----------|
| Max positions | 6 | 2 | 0 |
| Position size | 15% | 10% | 5% |
| Entry SL | ATR×0.3 | ATR×0.8 | - |
| Trailing SL | ATR×1.5 | ATR×0.4 | - |
| TP | Trailing (no cap) | ATR×0.5 | Exit only |
| Kelly | 25% | 10% | 5% |

---

## 💰 Struktur Fee (per Juli 2026)

| Order | Buy | Sell |
|-------|-----|------|
| Maker (limit) | 0.20% | 0.20% + PPh 0.21% |
| Taker (market) | 0.31% | 0.30% + PPh 0.21% |
| CFX | 0.0111% | 0.0111% |

**Round trip maker:** ~0.62%  
**Round trip taker:** ~0.83%

---

## 🚀 Circuit Breaker — Final Layer

```
Equity Floor (Rp10k)
    ↓ kena?
Consecutive Loss Counter (max 5 hari)
    ↓ ≥5?
CIRCUIT BREAKER — Stop 72 jam
    ↓
Reset otomatis setelah cooldown
```

**Persist:** Semua state circuit breaker disimpan ke `state.json`.  
**Telegram:** Notifikasi setiap trigger.  
**Greed override:** `/greed` bypass daily loss hold (1×/hari, max Rp30k).

---

## 📊 Skenario Realistis (Rp80k → Rp360k)

| Skenario | Waktu | Catatan |
|----------|-------|---------|
| BULL terus (Rothschild ON) | ~5 bulan | Pyramid + compound |
| BULL 3bln → SIDEWAYS | 12-18 bulan | Mean-rev stabil |
| Deposit Rp280k | Instant + 1 bulan | Bot optimal |

---

## 🧪 Indodax API Coverage

| API | Metode | Status |
|-----|--------|--------|
| `/api/ticker/{pair}` | GET | ✅ |
| `/api/ticker_all` | GET | ✅ |
| `/api/pairs` | GET | ✅ |
| `/api/depth/{pair}` | GET | ✅ |
| `/tradingview/history_v2` | GET | ✅ |
| `/tapi` — getInfo | POST | ✅ |
| `/tapi` — trade | POST | ✅ |
| `/tapi` — getOrder | POST | ✅ |
| `/tapi` — openOrders | POST | ✅ |
| `/tapi` — cancelOrder | POST | ✅ |
| `/tapi` — /countdownCancelAll | POST | ✅ |
| WS market:summary-24h | WSS | ✅ |
| WS private order_update | WSS | ✅ |

---

## 📦 Deployment

```
Procfile:  worker: python main.py
Runtime:   python-3.12
Platform:  Railway (auto-deploy from GitHub)
Volume:    /data (state.json + trades.db)
```

---

## ⚖️ License

**Copyright (C) 2026 FMA ALPHA QUANT LABS** — AGPL-3.0

---

> *"The secret to playing fast is learning to relax while moving at maximum speed."* — George Kollias  
> *"I don't need to be right. I just need to be right ONCE."* — N.M. Rothschild  
> *"Yang penting bukan profit di bear market. Yang penting masih idup pas bull datang."* — FMA
