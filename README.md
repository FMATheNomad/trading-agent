# 🥁 FMA ALPHA QUANT LABS — The Blast Engine

> *"Speed is not about muscle. Speed is about total relaxation. The faster you play, the more relaxed you must be."* — George Kollias

Bot trading kripto **100% engineering, zero AI cost** untuk Indodax.  
Menggabungkan filosofi extreme metal drumming dengan quantitative trading — pendekatan yang **belum pernah dipikirkan oleh ilmuwan trading kuantitatif manapun**.

---

## 📂 File Structure

| File | Fungsi |
|------|--------|
| `main.py` | Orchestrator — 4 async streams (SL/TP, momentum scanner, portfolio cycle, deadman) |
| `rules.py` | **Engine utama** — rank-based trade decision (score 0-100, gantikan DeepSeek CIO) |
| `momentum.py` | **Velocity-aware** momentum engine — signal detection + crossover velocity + volume acceleration |
| `patterns.py` | **🔬 Penemuan** — Paradiddle pattern matching (deteksi pola RLRL untuk reversal) |
| `risk_manager.py` | ATR SL/TP, trailing SL, Kelly sizing, portfolio risk, RR guard |
| `executor.py` | Indodax HMAC-SHA512 signing, market/limit orders |
| `data_layer.py` | Indodax REST API — pairs, tickers, OHLCV, orderbook |
| `market_ws.py` | WebSocket — realtime tickers untuk live SL/TP |
| `private_ws.py` | WebSocket — order fill notification |
| `deadman.py` | `/countdownCancelAll` — safety net kalau bot mati |
| `indicators.py` | Teknikal indikator (RSI, MACD, Bollinger, EMA, ADX, dll) |
| `persist.py` | SQLite state — positions, entry prices, equity snapshot |
| `db.py` | SQLite — trade log, decision log, chat history |
| `notifier.py` | Telegram sender |
| `hmm_regime.py` | HMM regime detector (4 states: BULL/BEAR/HIGH_VOL/SIDEWAYS) |
| `config.py` | Semua konfigurasi — ATR multipliers, mode, API keys |
| `.env` | Environment variables (gitignored) |

---

## 🧠 Filosofi Dasar: 100% ATR-Based

**Tidak ada fixed percentage.** Semua keputusan beli/jual berdasarkan **ATR (Average True Range)**:

```
ATR high (5-8%)  → SL lebar, TP besar, posisi kecil
ATR low  (1-2%)  → SL sempit, TP kecil, trade ditolak kalau tak nutup fee
```

### Konfigurasi ATR (ALPHA mode default)

| Parameter | ALPHA | INSANE | Keterangan |
|-----------|-------|--------|------------|
| ATR_SL_MULTIPLIER | 0.8 | 0.8 | Stop loss = ATR × 0.8 |
| ATR_TP_MULTIPLIER | 1.0 | 1.0 | Take profit = ATR × 1.0 |
| ATR_PROFIT_SELL_MULT | 0.8 | **0.6** | Profit sell threshold (ATR-based) |
| ATR_STAGNANT_MULT | 0.3 | **0.2** | Stagnant detection (ATR×0.2 = ~10min rotate) |
| ATR_CUT_MULT | 0.8 | 0.8 | Cut loss threshold (ATR-based) |
| KELLY_FRACTION | 0.25 | **0.75** | Position sizing aggressiveness |
| MAX_OPEN_POSITIONS | 4 | **6** | Max simultaneous positions |
| MAX_POSITION_PCT | 0.5 | **0.55** | Max % capital per trade |

### Risk-Reward Guard

Setiap entry dicek: potensi profit (ATR×TP) vs potensi rugi (ATR×SL + fee 0.7%).  
**Ditolak kalau RR < 0.8** — gak akan entry kalau gak bisa nutup fee.

---

## 🔬 4 Penemuan Baru (Blast Engine)

### 🔬 Penemuan 1: Triggered Velocity Trading

**Masalah:** Semua sistem trading konvensional mendeteksi sinyal secara **biner**: EMA crossover terjadi/tidak. Ini seperti drummer yang cuma tahu "pukul" atau "tidak pukul" — kehilangan **velocity**.

**Solusi:** Terinspirasi dari **drum trigger** — sensor piezo yang mengukur **kecepatan pukulan**, bukan hanya on/off.

```python
# Biasanya:
if ema9 > ema21: signal = True  # Biner

# Velocity Trading:
velocity = abs(ema9 - ema21) / last_price * 100
# velocity 0.1% = crossover lambat (noise)
# velocity 4.0% = crossover cepat (strong signal, higher allocation)
```

**Implementasi:** `momentum.py` — setiap metode return `(triggered, velocity)`.

---

### 🔬 Penemuan 2: Paradiddle Pattern Matching

**Masalah:** Harga kripto bergerak dalam pola berulang yang tidak ditangkap indikator biasa.  
**Solusi:** Deteksi pola **RLRL** (Resistance/Low) seperti paradiddle drummer.

| Pola | Nama | Aksi |
|------|------|------|
| `RLRR` | Fake Breakout (puncak palsu) | **SELL** |
| `LRLL` | Fake Breakdown (dasar palsu) | **BUY** |
| `RRL` (setelah `RRR`) | Exhaustion (trend lelah) | **SELL** |
| `LLR` (setelah `LLL`) | Exhaustion (trend lelah) | **BUY** |

**Implementasi:** `patterns.py` — `detect_paradiddle(closes) → str | None`

> **Kebaruan ilmiah:** Tidak ada literatur trading yang mendokumentasikan "paradiddle pattern matching." Ini pendekatan orisinal dari analogi drumming.

---

### 🔬 Penemuan 3: Multi-Bin Blast System

**Masalah:** Modal kecil sering idle menunggu satu posisi besar.  
**Solusi:** Seperti **double bass drum** — bergantian tanpa henti:

```
Cash Rp34.561
  ├─ Bin 1: Rp8.640 → Pair A
  ├─ Bin 2: Rp8.640 → Pair B  
  ├─ Bin 3: Rp8.640 → Pair C
  └─ Bin 4: Rp8.640 → Pair D
```

**Implementasi:** `rules.py` — cash dipecah rata ke `n_bins` slots.

---

### 🔬 Penemuan 4: Order Book Pressure Detection

**Masalah:** Entry berdasarkan OHLCV candle yang *udah lewat* — reaktif, bukan proaktif.  
**Solusi:** Cek **tekanan order book real-time** sebelum entry:

```python
imbalance = (bid_vol - ask_vol) / total_vol * 100
if imbalance < -10:  # Seller dominan → skip buy
```

**Implementasi:** Momentum scanner + `rules._score_all_pairs` + buku pressure bonus.

---

## 🎯 Arsitektur: 4-Stream Poliritmik

Seperti drummer progressive metal yang main 4 ritme berbeda secara independen:

| # | Stream | Tempo | Trigger | Fungsi |
|---|--------|-------|---------|--------|
| 1 | **Realtime SL/TP** | ⚡ per tick | WS price update | ATR SL/TP, trailing |
| 2 | **Momentum Scanner** | 🚀 tiap 15s | Signal + pattern + book | Entry cepat |
| 3 | **Portfolio Cycle** | 🎯 tiap 60s | Rank-based | Rebalance + compound |
| 4 | **Deadman Switch** | 🛡️ tiap cycle | Safety | Cancel orders if mati |

Semua async, non-blocking, independen.

---

## ⚙️ Cara Pasang

### 1. Environment Variables (`.env`)

```
INDODAX_API_KEY=...
INDODAX_SECRET_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
DEEPSEEK_API_KEY=...  # Opsional, cuma untuk /ask chat
```

### 2. Mode

| Env | Value | Efek |
|-----|-------|------|
| `ALPHA_MODE=true` | default | 4 posisi, ATR profit 0.8, Kelly 0.25 |
| `INSANE_MODE=true` | agresif | 6 posisi, ATR profit 0.6, Kelly 0.75 |
| `PAPER_TRADING=true` | simulasi | Gak beneran order |

### 3. Deploy (Railway)

```bash
railway up
```

Atau push ke GitHub dengan auto-deploy aktif.

---

## 📊 Perbandingan Performa

| Metrik | DeepSeek Era | Blast Engine |
|--------|-------------|--------------|
| Cycle time | 29s | **5-7s** |
| AI cost/hari | ~$0.50 | **$0** |
| Entry confirmation | TF aligned | TF + Pattern + Book |
| Signal granularity | Biner | **Velocity-aware** |
| Capital utilization | 1-2 posisi | **4-6 bins** |
| Cut loss | ATR×2.0 | **ATR×0.8** |
| Profit sell | fixed 1% | **ATR×0.6** |

---

## 🧠 Ringkasan Discovery

1. **Velocity > Binary** — Kecepatan sinyal lebih penting dari ada/tidaknya sinyal  
2. **Patterns > Indicators** — Pola harga berulang (paradiddle) lebih prediktif dari indikator lagging  
3. **Bins > All-in** — Modal kecil harus dipecah merata  
4. **Order Book > OHLCV** — Tekanan real-time lebih akurat dari candle yang udah lewat  
5. **ATR > Fixed %** — Adaptive position sizing mengalahkan fixed SL/TP  

**Zero AI dependency. Zero API cost. 100% engineering.**

> *"The secret to playing fast is learning to relax while moving at maximum speed."*
