# FMA ALPHA QUANT LABS — trading-agent

Bot trading spot IDR di Indodax. asyncio, berbasis aturan (bukan LLM yang
mengeksekusi order), jalan sebagai satu worker di Railway.

Tesis mesinnya: **HMM Regime Gate → Strategi per Regime → State Machine →
Market Buy + Limit Sell**. Keputusan entry dibuat rules engine [`rules.py`](rules.py)
(354 baris); yang menahan risiko bukan AI, tapi state machine TP/SL di
[`engine/position_sm.py`](engine/position_sm.py).

> ⚠️ **Default-nya paper trading.** `PAPER_TRADING` = `true` kalau tidak di-set
> ([`config.py:95`](config.py#L95)). Untuk LIVE kamu harus menulis
> `PAPER_TRADING=false` **dan** memasang kunci API. Sebelum itu terjadi, baca
> [`docs/OPERATIONS.md`](docs/OPERATIONS.md) bagian "menyalakan live".

## Dokumen

| Baca ini | Untuk |
|----------|-------|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | siapa memanggil siapa, di mana state hidup, aturan lock |
| [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) | 28 env var + nilai parameter per mode |
| [`docs/OPERATIONS.md`](docs/OPERATIONS.md) | menjalankan, membaca log, perintah Telegram, restart, shutdown |
| [`docs/TESTING.md`](docs/TESTING.md) | 10 suite / 82 kasus dan cara menjalankan tiap satunya |
| [`docs/SECURITY.md`](docs/SECURITY.md) | kredensial v1 vs v2, deadman, rotasi key |
| [`docs/KNOWN_ISSUES.md`](docs/KNOWN_ISSUES.md) | lubang yang diketahui, dengan nomor baris |
| [`docs/CHANGELOG.md`](docs/CHANGELOG.md) | fix list 1-8 dan commit-nya |
| [`docs/archive/`](docs/archive/) | dokumen lama, sudah diberi penanda arsip |

## Peta modul

```
run_all.py ──> main.py            entri + orkestrasi proses (789 baris)
                │
                ├─ engine/state.py          50 state runtime, pemilik tunggal (102)
                ├─ engine/cycle.py          portfolio_cycle 1 siklus keputusan (1.404)
                ├─ engine/position_sm.py    TP/SL/trailing/pyramid per posisi (265)
                ├─ orders.py                registry order (pair, kind) (132)
                ├─ state_bus.py             antrean event WS (82)
                │
                ├─ market_ws.py ── publish tick ─┐
                ├─ private_ws.py ─ publish fill ─┴─> _state_consumer ──> S._state_lock
                ├─ executor.py                  gerbang TAPI v2 + adapter v1 (685)
                ├─ data_layer.py / indicators.py / rules.py / risk_manager.py
                ├─ dca_smart.py / grid_mini.py  engine independen (DCA & grid)
                └─ db.py / persist.py           trades.db (WAL) + state.json
```

Dua hal yang paling sering salah dipahami orang baru, dan sengaja dibuat tidak
bisa salah lagi:

1. **Task WS tidak menyentuh state.** Tick dan fill hanya menulis ke
   `state_bus`; satu `_state_consumer` yang mengubah dunia, dan ia memakai
   `S._state_lock` yang sama dengan `portfolio_cycle` ([`main.py:51`](main.py#L51)).
2. **Tidak ada `global` di repo ini.** Semua state dibaca/ditulis lewat
   `S.<nama>`, jadi setiap mutasi terlihat di call site.

## Mulai cepat

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env           # isi sendiri; file .env tidak pernah di-commit
python backtest.py --pairs btc_idr,eth_idr --days 30   # uji strategi tanpa uang
python main.py                 # paper mode: nol panggilan TAPI
./.venv/bin/python tests/test_golden.py                # salah satu dari 10 suite
```

Python 3.12 di produksi ([`runtime.txt`](runtime.txt)); CI juga 3.12.
Pengembangan lokal boleh 3.14 — tapi jalankan test dengan interpreter yang sama
dengan yang kamu akan deploy.

## Lisensi

AGPL-3.0 — lihat [`LICENSE`](LICENSE). Setiap file sumber membawa header AGPL;
kalau menambah file `.py`, header itu ikut ditambahkan.
