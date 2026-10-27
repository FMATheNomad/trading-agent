# Konfigurasi

Semua konfigurasi lewat satu modul: [`config.py`](../config.py) (172 baris, 94 nama
tingkat modul — 92 konstanta publik + 2 variabel antara). Ia memanggil
`load_dotenv()` di baris 9, jadi variabel di `.env` (gitignored) mengisi nilai
sebelum konstanta dihitung. Tidak ada layer config lain — tidak ada `pyproject`
settings, tidak ada file YAML.

Urutan precedence sederhana: **env var → default di `config.py`**. Nilai yang
tidak ada `os.getenv`-nya tidak bisa diubah dari lingkungan; kalau perlu diubah,
ubah kodenya.

## 28 env var dibaca, 26 di antaranya berpengaruh

26 tabel di bawah yang punya efek nyata. Dua sisanya (`SENTRY_DSN`,
`AI_OPTIMIZER_INTERVAL_CYCLES`) dibaca `config.py` lalu tidak dipakai — masuk
daftar [knob mati](#19-konstanta-yang-tidak-dibaca-siapa-pun). Tabel terpisah
per kelompok:

### Kredensial & mode exchange

| Env | Default | Efek |
|-----|---------|------|
| `PAPER_TRADING` | `true` | `true` = tidak ada satu pun panggilan TAPI. Lihat [`docs/SECURITY.md`](SECURITY.md) |
| `INDODAX_V2_API_KEY` | `""` | kunci v2; tanpa ini `TAPI_VERSION=auto` jatuh ke v1 |
| `INDODAX_V2_SECRET_KEY` | `""` | secret v2 (HMAC-SHA256) |
| `INDODAX_API_KEY` | `""` | kunci legacy — **masih wajib** untuk deadman + token private-WS |
| `INDODAX_SECRET_KEY` | `""` | secret legacy |
| `INDODAX_TAPI_V2_URL` | `https://api.indodax.com` | basis URL v2 |
| `TAPI_VERSION` | `auto` | `auto` \| `v1` \| `v2`; `auto` pakai v2 kalau kedua kunci v2 ada ([`config.py:105-111`](../config.py#L105)) |
| `PAIR` | `btc_idr` | pair deadman (`SYMBOL` turunannya tidak dibaca) |
| `MAKER_FIRST` | `false` | entry pakai limit maker + slippage 0,001 |

### Risiko & ukuran posisi

| Env | Default | Efek |
|-----|---------|------|
| `PLAY_CAPITAL_IDR` | `300000` | saldo awal paper; juga dasar `DAILY_LOSS_FLOOR` dan compound cap |
| `MAX_DAILY_TRADES` | tidak di-set | kalau di-set, **menimpa nilai dari mode** ([`config.py:147-149`](../config.py#L147)) |
| `ALPHA_MODE` | `true` | profil risiko ALPHA (lihat tabel mode) |
| `INSANE_MODE` | `false` | profil risiko INSANE; menang atas ALPHA |
| `CLEAR_LOSS_HOLD` | tidak di-set | satu kali jalan: reset flag daily-loss dari `state.json` saat boot ([`main.py:278`](../main.py#L278)) |

### Scan & data

| Env | Default | Efek |
|-----|---------|------|
| `MAX_SCAN_PAIRS` | `40` | top-N pair berdasarkan volume 24 jam (INSANE memaksa 50) |
| `MIN_24H_VOLUME_IDR` | `500000000` | ambang likuiditas pair yang boleh dibiayai |
| `OHLCV_FETCH_CONCURRENCY` | `15` | parallelisme fetch OHLCV di `data_layer` |
| `WS_MARKET_TOKEN` | token publik bawaan Indodax | hanya perlu di-override kalau Indodax ganti token market WS |

### Notifikasi, AI, storage

| Env | Default | Efek |
|-----|---------|------|
| `TELEGRAM_BOT_TOKEN` | `""` | kirim notifikasi + long-poll perintah |
| `TELEGRAM_CHAT_ID` | `""` | chat tujuan |
| `DEEPSEEK_API_KEY` | `""` | AI optimizer; kosong = optimizer nonaktif dengan satu peringatan |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | endpoint kompatibel-OpenAI |
| `DEEPSEEK_MODEL` | `deepseek-v4-flash` | model optimizer |
| `AI_OPTIMIZER_AUTO_APPLY` | `true` | terapkan usulan parameter tanpa konfirmasi |
| `STATE_DIR` | — | **isi ini di Railway.** Direktori `trades.db`, `state.json`, `dca_*`, `grid_*`. Fallback: `DATA_DIR` → `/data` → direktori repo |
| `DATA_DIR` | — | alias lama `STATE_DIR`, tetap dibaca |

## Profil mode (bukan env, tapi menentukan banyak nilai)

`ALPHA_MODE=true` adalah default, jadi baris ALPHA adalah yang jalan kalau kamu
tidak men-set apa pun. `INSANE_MODE=true` menang atas ALPHA.

| Konstanta | STANDARD | **ALPHA (default)** | INSANE |
|-----------|---------|---------------------|--------|
| `MAX_OPEN_POSITIONS` | 1 | 4 | 6 |
| `MAX_POSITION_PCT_PER_ASSET` | 0.50 | 0.25 | 0.55 |
| `DAILY_LOSS_FLOOR_IDR` | 15.000 | 15.000 | 0 |
| `PORTFOLIO_STOP_LOSS_PCT` | −10% | −15% | −50% |
| `MAX_DAILY_TRADES` | 3 | 999999 | 999999 |
| `KELLY_FRACTION` | 0.15 | 0.10 | 0.75 |
| `ATR_SL_MULTIPLIER` | 1.5 | 1.2 | 1.5 |
| `ATR_PROFIT_SELL_MULT` | 1.5 | 1.5 | 1.0 |
| `ATR_CUT_MULT` | 1.5 | 1.5 | 1.0 |
| `ATR_STAGNANT_MULT` † | 0.8 | 1.0 | 0.5 |

`†` baris itu nilainya berubah per mode, tapi tidak ada kode yang membacanya —
perubahan ini kosong. `MAX_SCAN_PAIRS` (INSANE 50) dan konstanta `ROTHSCHILD_*`
juga ikut berubah.
Catatan: `max_positions_for_equity()` (4 kalau equity ≥ Rp5 jt) dipakai terpisah
dari `MAX_OPEN_POSITIONS`, jadi keduanya bisa tidak konsisten — dibaca di
`engine/cycle.py`.

## Nilai tetap yang paling menentukan perilaku

| Konstanta | Nilai | Fungsi |
|-----------|-------|--------|
| `MIN_ORDER_IDR` | 25.000 | order di bawah ini ditolak (hardcoded, bukan env) |
| `LOOP_INTERVAL_SECONDS` | 60 | jeda antar `portfolio_cycle` |
| `MOMENTUM_SCAN_INTERVAL` | 15 | jeda `_momentum_scanner` |
| `DEADMAN_COUNTDOWN_MS` | 900.000 | deadman: semua order batal kalau tidak diperpanjang 15 menit |
| `HIGH_VOL_THRESHOLD` | 3.5 | ATR rata-rata di atas ini → regime HIGH_VOL |
| `HMM_N_STATES` / `HMM_RETRAIN_INTERVAL` | 4 / 50 | regime detector |
| `REGIME_STABILITY_CYCLES` / `_BEAR_` | 5 / 3 | streak sebelum regime dianggap stabil |
| `LIMIT_GRACE_CYCLE` / `SELL_GRACE_CYCLE` | 3 / 3 | cycle tunggu order maker/sell sebelum dianggap mati |
| `CIRCUIT_BREAKER_LIMIT` / `_HOURS` | 5 / 72 | 5 hari loss berturut → berhenti 72 jam |
| `EQUITY_FLOOR_IDR` | 10.000 | equity di bawah ini: tidak ada entry, log `🏴 EQUITY FLOOR` |
| `AUTO_COMPOUND` / `COMPOUND_CAP_IDR` | true / 1.000.000 | `PLAY_CAPITAL_IDR` dinaikkan mengikuti equity, dibatasi 1 jt |
| `POSITION_SIZE_PCT` | 0.50 | porsi kas per entry di jalur non-Kelly |
| `KELLY_FRACTION` + `MIN/MAX_KELLY_ALLOC` | 0.10 + 0.2/0.6 | pemotongan Kelly dan batas alokasi |
| `ROTHSCHILD_INITIAL_SL_ATR` / `TRAILING_SL_ATR` / `PYRAMID_TRIGGER` / `PYRAMID_MULT` | 0.5 / 2.0 / 0.5 / 0.5 | mesin trailing & pyramid |

## Biaya (Indodax IDR spot)

| Konstanta | Nilai | |
|-----------|-------|---|
| `TAKER_FEE_PCT` | 0,0031 | 0,31% buy taker |
| `TAKER_SELL_FEE_PCT` | 0,0030 | 0,30% sell taker |
| `MAKER_FEE_PCT` | 0,0020 | 0,20% dua sisi |
| `SELL_TAX_PCT` | 0,0021 | 0,21% pajak jual |
| `CFX_FEE_PCT` | 0,000111 | 0,0111% |

Roundtrip: maker 0,6211%, taker 0,8311%. **Tapi hati-hati:** konstanta
`TOTAL_MAKER_ROUNDTRIP_PCT`, `TOTAL_TAKER_ROUNDTRIP_PCT`, `TAKER_SELL_FEE_PCT`,
`CFX_FEE_PCT` dan `FEE_CLEARANCE_RATIO` tidak dibaca kode mana pun (lihat bawah).
Biaya yang benar-benar dipakai perhitungan ada di `risk_manager.py`/`rules.py`.

## 19 konstanta yang tidak dibaca siapa pun

Mengubah ini **tidak berpengaruh apa pun**. Terdaftar di
[`docs/KNOWN_ISSUES.md`](KNOWN_ISSUES.md#knob-mati):

```
DEFAULT_PLAY_CAPITAL_PCT  PORTFOLIO_MODE          MAX_SECTOR_EXPOSURE_PCT
ATR_MIN_MOVE_MULTIPLIER   MAX_ATR_PCT             ATR_STAGNANT_MULT
TAKER_SELL_FEE_PCT        CFX_FEE_PCT             TOTAL_MAKER_ROUNDTRIP_PCT
TOTAL_TAKER_ROUNDTRIP_PCT FEE_CLEARANCE_RATIO     SYMBOL
DEEPSEEK_THINKING_MODE    SENTRY_DSN              ROTHSCHILD_LIMIT_GRACE_SEC
CAPITAL_GROWTH_MULTIPLIER AI_OPTIMIZER_INTERVAL_CYCLES
FUNDAMENTAL_COINS         RECOVERY_TOP            (set kosong, tidak dibaca)
```

Ditambah 2 variabel antara di dalam `config.py` (`_tapi_version`,
`_MAX_DAILY_TRADES_ENV`) yang memang hanya dipakai modul itu sendiri.
Angka ini dihasilkan audit AST: setiap nama `config.py` dicari pemanggilannya
(`config.NAMA` atau `from config import NAMA`) di seluruh `*.py` dan
`engine/*.py`, test dikecualikan.

`SENTRY_DSN` menarik karena `sentry-sdk` ada di `requirements.txt` tapi tidak ada
satu pun file yang mengimpornya: crash hari ini tidak dilaporkan ke mana-mana,
hanya ke log worker.
