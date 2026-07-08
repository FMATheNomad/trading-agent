# 📈 Scaling Guide — FMA Alpha Quant Labs

**Bagaimana cara menaikkan kapasitas bot dari Rp100rb → Rp100jt → Rp1M+**

---

## ⚠️ Prinsip Dasar Scaling

Bot ini dirancang untuk **equity Rp100rb - Rp5jt**. Di atas itu, parameter harus discale secara proporsional. Scaling bukan sekadar "isi duit banyak" — ada 8 dimensi yang harus disesuaikan:

---

## 1. Daily Loss Limit (Paling Kritis)

**Masalah:** `DAILY_LOSS_FLOOR_IDR = 15.000` (fixed). Di equity Rp100jt, ini cuma 0.015% — gak akan pernah kena. Bot bisa loss terus tanpa berhenti.

| Equity | Daily Loss Harusnya | Config |
|--------|-------------------|--------|
| Rp100rb | Rp15rb (15%) | ✅ Default |
| Rp1jt | Rp30rb (3%) | Ubah `DAILY_LOSS_FLOOR_IDR = 30000` |
| Rp10jt | Rp300rb (3%) | Ubah `DAILY_LOSS_FLOOR_IDR = 300000` |
| Rp100jt | Rp3jt (3%) | Ubah `DAILY_LOSS_FLOOR_IDR = 3000000` |
| Rp1M | Rp30jt (3%) | Ubah `DAILY_LOSS_FLOOR_IDR = 30000000` |

**Formula:** `DAILY_LOSS_FLOOR_IDR = equity × 0.03`

---

## 2. Position Sizing & Likuiditas

**Masalah:** `MAX_POSITION_PCT_PER_ASSET = 0.25` (25%). Di equity Rp100jt, 1 posisi = Rp25jt. Banyak altcoin Indodax gak bisa handle order sebesar itu — slippage gede.

| Ekuitas | 1 Posisi | Pair Aman | Pair Berisiko |
|---------|----------|-----------|---------------|
| Rp100rb | Rp25rb | Semua | - |
| Rp1jt | Rp250rb | BTC, ETH, USDT, SOL | Altcoin kecil |
| Rp10jt | Rp2.5jt | BTC, ETH, USDT | SOL, ADA, altcoin |
| Rp100jt | Rp25jt | BTC, ETH, USDT | Semua altcoin ❌ |

**Aturan:** Jangan pernah trade lebih dari **20% volume 24h pair**. Contoh:
- BTC/IDR volume 24h: ~Rp500M → maks Rp100jt/order ✅
- Altcoin volume 24h: Rp1M → maks Rp200rb/order ❌

**Implementasi di `rules.py`:**
```python
max_order_vol = min(equity * MAX_POSITION_PCT, vol_idr * 0.2)
```

---

## 3. Fee Scaling

**Sekarang:** Market buy 0.31% + maker sell 0.20% + PPh 0.21% = 0.73% round trip.

Di equity gede, fee bisa dinego sama Indodax. Hubungi `partner@indodax.com` untuk:
- **Volume > Rp100jt/bulan** → kemungkinan dapet diskon fee maker/taker
- **Market maker** → fee bisa 0% kalau provide likuiditas

**Kalau fee turun 50%:** Round trip dari 0.73% → 0.36%. EV langsung naik drastis.

---

## 4. n_bins Divisor

**Masalah:** `n_bins = max(1, int(actual_idr_balance / 40000))`. Di Rp100jt: `2500` → di-cap ke `MAX_OPEN_POSITIONS`.

**Harusnya discale:**
```python
n_bins = max(1, min(int(actual_idr_balance / MIN_POSITION_SIZE), MAX_OPEN_POSITIONS))
MIN_POSITION_SIZE = max(20000, total_equity * 0.05)
```

---

## 5. Min Order & Entry

**Sekarang:** Minimum entry Rp20rb. Di equity gede, ini terlalu kecil.

| Equity | Min Entry Ideal | Alasan |
|--------|----------------|--------|
| < Rp5jt | Rp20rb | Default ✅ |
| Rp5-50jt | Rp100rb | Biar gak pecah posisi terlalu banyak |
| Rp50-500jt | Rp1jt | Likuiditas minimum |
| > Rp500jt | Rp5jt | Order size meaningful |

---

## 6. Circuit Breaker Scaling

**Sekarang:** `EQUITY_FLOOR_IDR = 10.000`. Di equity Rp100jt, gak relevan.

| Equity | Floor | 5 Hari Loss Maks | Cooldown |
|--------|-------|-------------------|----------|
| Rp100rb | Rp10rb | 5 × Rp15rb = Rp75rb | 72 jam ✅ |
| Rp10jt | Rp200rb | 5 × Rp300rb = Rp1.5jt | 72 jam ✅ |
| Rp100jt | Rp3jt | 5 × Rp3jt = Rp15jt | 72 jam ✅ |
| Rp1M | Rp30jt | 5 × Rp30jt = Rp150jt | 72 jam ✅ |

---

## 7. Infrastruktur

| Level | Hosting | Biaya | Cocok Untuk |
|-------|---------|-------|-------------|
| **Sekarang** | Railway | ~$5-20/bln | Equity < Rp50jt |
| **Bronze** | VPS Jakarta (IDN, Biznet) | ~Rp200rb/bln | Equity Rp50-500jt |
| **Silver** | Dedicated server, colocation | ~Rp2-5jt/bln | Equity Rp500jt-5M |
| **Gold** | Multi-VPS, load balancer, failover | ~Rp10-50jt/bln | Equity > Rp5M |

**Kenapa VPS penting untuk equity gede:**
- Latensi lebih rendah (Jakarta ke server Indodax ≈ 2-5ms vs Railway ≈ 50-200ms)
- WS tick lebih stabil (realtime SL/TP lebih responsif)
- Gak kena RAM/CPU limit (Railway free tier terbatas)

---

## 8. Diversifikasi Venue

Equity > Rp500jt → **jangan taruh semua di Indodax.** Butuh diversifikasi exchange juga:

| Platform | Kelebihan | Untuk |
|----------|-----------|-------|
| Indodax | 🇮🇩 Indonesia, fiat IDR | Strategi utama |
| Binance | 🌐 Global, pair USDT, volume gede | Hedging, arbitrase |
| Bybit | 📈 Derivatif, leverage | Hedging spot-futures |

**Distribusi ideal:**
- 60% Indodax (strategi utama)
- 30% Binance/Bybit (hedging + diversifikasi)
- 10% Cash/stablecoin (darurat)

---

## 9. Risk Management Tambahan

Di equity gede, butuh lapisan risk tambahan:

**A. VaR (Value at Risk):**
```python
var_95 = portfolio_value * daily_vol * 1.645
# Kalau daily var > 5% equity → kurangi posisi
```

**B. Pair correlation matrix:**
```python
# Jangan ambil 3 posisi sekaligus di pair yang berkorelasi tinggi
# Contoh: SOL, RAY, SRM — semuanya Solana ecosystem
```

**C. Weekly circuit breaker:**
```python
# Total loss mingguan > 10% → stop trading, review parameter
```

**D. Manual override:**
```python
# Telegram command: /stop — berhenti trading, cancel semua order
# /resume — lanjut lagi
```

---

## 📋 Checklist Scaling

### Rp100rb → Rp10jt (Minor Tuning)
- [ ] Gak perlu banyak perubahan
- [ ] `MIN_ORDER_IDR` bisa naik ke Rp20rb (udah)
- [ ] `DAILY_LOSS_FLOOR_IDR` naikin ke Rp30-50rb

### Rp10jt → Rp100jt (Moderate)
- [ ] `DAILY_LOSS_FLOOR_IDR = equity × 0.03`
- [ ] `MAX_POSITION_PCT_PER_ASSET = 0.10` (10%, bukan 25%)
- [ ] `n_bins` divisor naik ke 500rb
- [ ] `MIN_ORDER_IDR` naik ke Rp100rb
- [ ] Pindah ke VPS Jakarta
- [ ] Filter pair berdasarkan volume: jangan trade pair dengan volume < Rp5M/hari
- [ ] Nego fee Indodax

### Rp100jt → Rp1M+ (Major)
- [ ] Semua parameter discale proporsional
- [ ] Diversifikasi venue (Binance, Bybit)
- [ ] VaR, correlation, weekly circuit breaker
- [ ] Costum VPS / dedicated server
- [ ] Audit risk pihak ketiga
- [ ] Legal entity (PT / CV)
- [ ] Tax compliance (PPh final, laporan pajak)

---

## Kontak Indodax untuk Volume Besar

```
Email: partner@indodax.com
WhatsApp: +62-xxx-xxxx-xxxx (hubungi via website resmi)
```

**Yang bisa dinego:**
- Fee maker/taker diskon
- Rate limit dinaikkan (default 20 req/s per pair)
- Withdraw limit dinaikkan
- Dedicated support

---

> *"Scaling is not just adding money. It's adding responsibility."*

© 2026 FMA ALPHA QUANT LABS
