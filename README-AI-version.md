# 🏛️ FMA ALPHA QUANT LABS — Arsip: Dari AI ke Engineering

> *Dokumen ini mencatat transformasi bot dari sistem yang bergantung pada DeepSeek AI menjadi 100% engineering murni.*

---

## 📜 Sejarah Singkat

### Fase 1: DeepSeek CIO (Hari 1-2)
Bot berjalan dengan **DeepSeek CIO** sebagai otak utama:
- Setiap 60 detik, bot kirim data pasar ke DeepSeek → AI memutuskan beli/jual
- Mahal: ~$0.50/hari untuk API calls
- Lambat: cycle time 29-41s karena nunggu response AI
- Bermasalah: waktu API balance habis, bot cuma bisa HOLD

### Fase 2: Hybrid (Hari 2-3)
- Momentum engine untuk entry cepat (15s scan)
- DeepSeek hanya untuk portfolio rebalance tiap cycle
- Mulai ada ATR-based SL/TP
- Tapi masih bergantung AI untuk keputusan strategis

### Fase 3: Engineering Dominance (Hari 3)
- **`rules.py`** lahir — menggantikan DeepSeek CIO sepenuhnya
- Rank-based scoring system (0-100)
- Semua keputusan berdasarkan aturan engineering, bukan AI
- Cycle time turun dari 29s → **5-7s**
- **Biaya API: Rp0/hari**

### Fase 4: The Blast Engine (Hari 3-4)
Inovasi baru dari filosofi drummer metal extreme:

| # | Penemuan | File | Kebaruan |
|---|----------|------|----------|
| 1 | **Velocity Trading** | `momentum.py` | Signal diukur kecepatannya, bukan biner |
| 2 | **Paradiddle Patterns** | `patterns.py` | Pola RLRL untuk prediksi reversal |
| 3 | **Multi-Bin System** | `rules.py` | Modal dipecah rata ke 4-6 bin independen |
| 4 | **Book Pressure** | `main.py` + `rules.py` | Orderbook imbalance sebagai konfirmasi entry |

---

## 📊 Perbandingan Lengkap

### Biaya

| Komponen | DeepSeek Era | Blast Engine |
|----------|-------------|--------------|
| API trading decision | ~$0.50/hari | **$0** |
| DeepSeek chat | $0 | $0 (jarang dipakai) |
| **Total/hari** | **~Rp8.000** | **Rp0** |

Dalam sebulan: **Rp240.000 VS Rp0** — ini setara 60% dari modal!

### Performa

| Metrik | DeepSeek | Hybrid | Blast Engine |
|--------|----------|--------|-------------|
| Cycle time | 29-41s | 11-16s | **5-7s** |
| Entry confirmation | AI memilih | TF aligned | TF + Pattern + Book |
| Exit logic | AI memutuskan | ATR SL/TP | ATR + rules engine |
| Signal granularity | Biner | Biner | **Velocity-aware** |
| Capital utilization | 1-2 posisi | 2-3 posisi | **4-6 bins** |
| AI dependency | **Total** | Parsial | **Nol** |

### Teknologi yang Dihapus

| File | Fungsi | Alasan Dihapus |
|------|--------|----------------|
| `llm_filter.py` | Prompt builder untuk DeepSeek | Diganti `rules.py` |
| `features.py` | Microstructure features | Hanya untuk konteks AI |
| `ml_signal.py` | XGBoost training | Lambat, tidak dipakai decision |
| `cointegration.py` | Pair trading signals | Tidak relevan untuk modal kecil |

### Konfigurasi yang Dihilangkan (Dead Code)

| Variable | Dulu | Sekarang |
|----------|------|----------|
| `STOP_LOSS_PCT` | -8% fixed | **ATR×0.8** |
| `TAKE_PROFIT_PCT` | 25% fixed | **ATR×1.0** |
| `PARTIAL_TP_*` | Scaling out | **Dihapus** |
| `PROFIT_SELL_THRESHOLD` | 1% fixed | **ATR×0.6** |
| `MOMENTUM_MIN_SIGNALS` | 2 | **Hardcoded** |

---

## 📈 Evolusi Rules Engine

### `rules.py` v1 (DeepSeek replacement)
- Score 0-100 berdasarkan signal + RSI + volume
- Sell jika cut -8% atau rank drop
- Buy jika score ≥8 dan TF aligned

### `rules.py` v2 (Blast Engine)
- **Velocity scoring** — signal dengan velocity tinggi dapat bonus
- **Book pressure** — orderbook imbalance mempengaruhi score
- **Bin system** — cash dipecah rata ke n_bins
- **ATR-based cuts** — semua threshold pake ATR multiplier

---

## 🧠 Ringkasan Filosofi

```
DeepSeek Era:   "AI, beliin saya koin yang bagus"     → mahal, lambat, error-prone
Hybrid:         "AI bantu saya aja yang susah-susah"   → lebih murah, masih bergantung
Blast Engine:   "Saya tahu cara main, biar saya urus"  → murah, cepat, presisi
```

**Prinsip utama:**
1. **Tidak ada AI dalam trading decisions** — AI hanya untuk chat
2. **100% ATR-based** — tidak ada fixed percentage apapun
3. **Multi-layer confirmation** — TF alignment → pattern → book pressure
4. **Compounding otomatis** — tiap profit nambah ke modal
5. **Risk-reward guard** — entry hanya jika potensi profit > potensi rugi × 0.8

---

*Arsip ini dibuat untuk dokumentasi evolusi bot.  
Versi terbaru dan termutakhir selalu ada di `README.md`.*
