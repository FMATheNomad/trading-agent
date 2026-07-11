# FMA Alpha Quant Labs — Onboarding Brief

**Jangan edit file ini tanpa persetujuan.** Ini adalah dokumen konteks untuk sesi AI baru.

---

## Ringkasan

Bot trading kripto sistematis untuk Indodax (exchange Indonesia). Fully autonomous. 430+ commits. Arsitektur:

```
HMM Regime Gate → Strategi per Regime → State Machine → Market Buy + Limit Sell
```

## File Structure

| File | Fungsi | Keterangan |
|------|--------|-----------|
| `main.py` | Orchestrator — cycle, momentum scanner, state machine, Telegram | ~2200 baris |
| `rules.py` | Decision engine — regime gate + momentum + mean-reversion | ~300 baris |
| `config.py` | Semua parameter | ~150 baris |
| `executor.py` | HMAC-SHA512 signing, order execution | ~140 baris |
| `risk_manager.py` | ATR SL/TP, Kelly per regime, validate_allocation | ~260 baris |
| `persist.py` | JSON state persistence | ~135 baris |
| `db.py` | SQLite trade log | ~195 baris |
| `data_layer.py` | Indodax REST API | ~135 baris |
| `indicators.py` | Teknikal indikator (RSI, MACD, BB, dll) | ~275 baris |
| `hmm_regime.py` | HMM 4-state regime detector | ~86 baris |
| `momentum.py` | Velocity-aware momentum engine | ~42 baris |
| `patterns.py` | Paradiddle pattern matching | ~58 baris |
| `market_ws.py` | WebSocket ticker + callback | ~70 baris |
| `private_ws.py` | WebSocket order fill notification | ~80 baris |
| `deadman.py` | countdownCancelAll safety net | ~65 baris |
| `notifier.py` | Telegram sender | ~33 baris |
| `backtest.py` | Backtest dengan rules.decide() + state machine | ~300 baris |
| `pairs.py` | Pair trading signals (z-score) + terintegrasi | ~95 baris |
| `SCALING.md` | Scaling guide Rp100rb → Rp1M+ | — |
| `ONBOARDING.md` | Dokumen ini | — |

## Arsitektur Inti

### Regime Detection (HMM)
4 state: BULL, BEAR, SIDEWAYS, HIGH_VOL. Feature: log return, volatility, ATR, range, skew.
HMM sebagai **hard gate** — menentukan strategi mana yang aktif.

| Regime | Strategi | Entry | Exit |
|--------|----------|-------|------|
| BULL (≥5 cycle → Rothschild) | Momentum + trailing | Market buy | Trailing SL ATR×2.0 |
| BEAR | Momentum konservatif | Market buy | TP fix ATR×2 |
| SIDEWAYS | Mean-reversion (BB + RSI) | Market buy | TP fix ATR×0.5 |
| HIGH_VOL | Survival | ❌ No entry | Exit only |

### Rothschild
- **Aktivasi:** 5 cycle BULL berturut-turut
- **Deaktivasi:** SIDEWAYS terdeteksi, atau 3 cycle BEAR
- **Mode:** 6 slot, Kelly 25%, pyramid ON
- **Initial SL:** ATR×0.5 (config: `ROTHSCHILD_INITIAL_SL_ATR`)
- **Trailing SL:** ATR×2.0 (config: `ROTHSCHILD_TRAILING_SL_ATR`)

### State Machine — Exit Management
Set posisi memiliki 6 state: `NEW → PENDING → TP_ACTIVE ↔ SL_ACTIVE → closed`

- **TP_ACTIVE:** TP limit order di exchange @ ATR×2 (SIDEWAYS default)
- **SL_ACTIVE:** SL limit order (jika harga turun dari TP_ACTIVE)
- **TRAILING:** No TP, trailing SL (BULL mode, activated after >2.5% profit)
- Transisi via WS tick (`_realtime_sltp_check`) + manual cycle check (line ~920)

**Flow:**
```
SM INIT → TP first @ ATR×2
  ├── harga turun ke SL? → cancel TP, place SL → SL_ACTIVE
  │   └── harga balik? → cancel SL, place TP → TP_ACTIVE
  └── profit >2.5% di BULL? → cancel TP, TRAILING ON
```

### Entry
- Market buy (keisi pasti, fee 0.31%)
- Minimum entry: Rp20.000
- **Cooldown:** 24 jam setelah SM FILLED, entry diblokir (kecuali score > 90)
- **Range filter:** harga >70% dari 14-candle range → skip (cegah FOMO)
- **ATR minimum filter:** ATR pair < 1.5% → skip

### Circuit Breaker (3 lapis)
| Lapis | Trigger | Aksi |
|-------|---------|------|
| Daily loss | Equity turun Rp15k dari peak | Stop entry, TP-only |
| Consecutive loss | 5 hari loss berturut | Stop 72 jam |
| Equity floor | Equity < Rp10k | Hold (no shutdown) |

### Fee Structure (Juli 2026)
| Komponen | Nilai |
|----------|-------|
| BUY market | 0.31% (taker) |
| SELL limit | 0.20% (maker) |
| PPh final (sell) | 0.21% |
| CFX fee | 0.0111% |
| **Round trip** | **~0.73%** |

## Key Parameters

| Parameter | Value | File |
|-----------|-------|------|
| `MIN_ORDER_IDR` | 15.000 | config.py |
| Min entry | 20.000 | rules.py |
| `ATR_TP_MULTIPLIER` | 2.0 | config.py |
| `ATR_SL_MULTIPLIER` | 1.2 | config.py |
| `ROTHSCHILD_INITIAL_SL_ATR` | 0.5 | config.py |
| `ROTHSCHILD_TRAILING_SL_ATR` | 2.0 | config.py |
| SL minimum floor | 1.5% | main.py `_sm_place_sl` |
| `DAILY_LOSS_FLOOR_IDR` | 15.000 | config.py |
| `CIRCUIT_BREAKER_LIMIT` | 5 hari | config.py |
| `CIRCUIT_BREAKER_HOURS` | 72 jam | config.py |
| `MAX_SCAN_PAIRS` | 40 | config.py |
| `MIN_24H_VOLUME_IDR` | 500.000.000 | config.py |

## Key Variables in main.py

| Variable | Type | Fungsi |
|----------|------|--------|
| `positions` | list[dict] | Posisi aktif |
| `_position_states` | dict[str, dict] | SM state per pair |
| `_sm_cooldown` | dict[str, float] | Cooldown timestamp (24 jam) |
| `_coin_blacklist` | set[str] | Pair diblokir setelah SL |
| `_pending_orders` | dict[str, dict] | Limit buy pending (maker_first) |
| `_pending_sells` | dict[str, dict] | Limit sell unfilled |
| `_realtime_sold` | set[str] | Baru di-SM FILLED (cegah double sell) |
| `_daily_loss_hit_today` | bool | Daily loss hold |
| `_realized_pnl_idr` | float | Accumulated profit for compound |
| `_rothschild_active` | bool | Rothschild mode ON/OFF |
| `_regime_bull_streak` | int | Consecutive BULL cycles |
| `_regime_bear_streak` | int | Consecutive BEAR cycles |
| `_latest_regime` | dict | Regime info (regime, confidence) |
| `_latest_ohlcv_map_1h` | dict | OHLCV cache |
| `LIVE_TICKERS` | dict[str, dict] | WS realtime prices |

## Command Telegram
`/status`, `/ask <coin>`, `/atr`, `/greed`, `/why`, `/commands`,
`/project`, `/today`, `/perf`, `/cycle`, `/risk`

## Common User Preferences
- Emosi: mudah frustrasi, butuh penjelasan konkret.
- Gaya komunikasi: langsung, suka panggilan "tolol" saat kesal. Jangan baper.
- Tidak suka spekulasi tanpa data. Cek API/log dulu sebelum jawab.
- "Pikirkan efek domino" — setiap perubahan harus dipertimbangkan dampak ke seluruh sistem.

## Prioritas Implementasi (Belum Dilakukan)

| Item | Status |
|------|--------|
| RVM (Reflexive Vortex Model) | 💤 Konsep |
| Pre-maintenance risk reduction | 💤 Ide |
| `/wd` command | 💤 Ide |
| 1-day timeframe | 💤 Diskusi |
| Polytonal Trading (Stravinsky) | 💤 Konsep |
| BKRG (Baum-Kelly Governor) | 💤 Konsep |
| LTPL (Livermore Pivotal Ladder) | 💤 Konsep |
| CFCG (Correlation Fracture Governor) | 💤 Konsep |
| Semua tercatat di `/home/fariz/Destop/fma-unimplemented-ideas.md` | — |

## Catatan Penting
- Jangan pernah push tanpa persetujuan eksplisit.
- Jangan menghapus HARD SL atau force sell — sudah dibahas & dihapus permanen (commit 4e45cea).
- Jangan mengubah struktur file tanpa diskusi.
- Bot berjalan di Railway, auto-deploy dari GitHub main branch.
- Equity terkini bisa dicek via `getInfo` API Indodax.
- Semua path entry (rules.py + momentum scanner) harus konsisten — cek semuanya kalo ada perubahan parameter.
