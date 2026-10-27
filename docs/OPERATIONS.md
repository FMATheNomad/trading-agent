# Operasi

Cara menjalankan, membaca, dan memulihkan bot. Semua perintah dari direktori repo,
dengan interpreter virtualenv (`./.venv/bin/python`) supaya dependensi terbaca —
sistem Python kosong tidak punya `httpx`/`dotenv`.

## Menjalankan

| Tujuan | Perintah |
|--------|----------|
| Produksi (yang dipakai Railway) | `python run_all.py` — Procfile: `worker: python run_all.py` |
| Bot saja, tanpa wrapper | `python main.py` (guard `__main__` di baris 781) |
| Paper trading | `PAPER_TRADING=false` tidak di-set → default `true`, nol panggilan TAPI |
| Backtest strategi | `python backtest.py --pairs btc_idr,eth_idr --days 30` |
| Uji walk-forward / Monte Carlo | `python backtest.py --walkforward --windows 6` · `--mc --mc_runs 2000` |
| Suite test | `./.venv/bin/python tests/test_<nama>.py` — lihat [`TESTING.md`](TESTING.md) |

Flag `backtest.py`: `--pairs` (`btc_idr,sol_idr`), `--days` (30), `--tf` (60),
`--capital` (200000), `--regime {BULL,BEAR,SIDEWAYS,HIGH_VOL}`, `--walkforward`,
`--windows` (6), `--mc`, `--mc_runs` (2000).

**Jangan jalankan `dca_main.py` berdampingan dengan `run_all.py`.** DCA sudah
terintegrasi di `main.py` lewat `_dca_task` ([`main.py:733`](../main.py#L733)); dua
proses berarti dua engine menulis `dca_state.json` yang sama dan memasang base
order ganda. `dca_main.py` tinggal untuk debug DCA terisolasi.

## Yang muncul di log saat boot

Urutan baris yang normal (live mode, kunci lengkap):

```
Restored N positions / cooldowns / SM cooldowns / grid instances
Circuit breaker: ... | 🛑 CIRCUIT BREAKER aktif — sisa X jam cooldown
CLEANUP: cancelled K stale orders — fresh start for SM & Grid
  WARN: no legacy key — deadman switch is OFF (v2 has no countdownCancelAll)
🤖 FMA ALPHA QUANT LABS started (Engineering Mode) ... Mode: LIVE | ALPHA 🔴 | TAPI v2
```

Yang wajib diperhatikan:

- **`WARN: no legacy key`** — kalau ini muncul, deadman switch mati. Bot tetap
  trading, tapi tidak ada pembatalan otomatis saat proses hilang. Jangan hapus
  baris ini ([`main.py:347`](../main.py#L347)).
- **`CLEANUP: cancelled K stale orders`** — sapuan ini buta: dia membatalkan
  *semua* open order, termasuk order resting grid/DCA, karena `load_instances()`
  grid jalan duluan (305) tapi DCA baru setelahnya (700) sementara sapuan di 325.
  Ini isu aktif, lihat [`KNOWN_ISSUES.md`](KNOWN_ISSUES.md).
- Baris `⏱ Cycle #N finished in Xs` tiap cycle — **X adalah latency SL/TP
  realtime terburukmu**. X yang membesar = lock terlalu lebar, bukan sinyal bagus.

## Membaca kondisi berjalan

Lewat Telegram (long-poll `getUpdates`, handler di [`main.py`](../main.py)):

`/status` `/cycle` `/project` `/today` `/week` `/month` `/year` `/perf` `/log`
`/risk` `/why` `/atr [COIN]` `/ask <COIN>` `/greed` `/commands` `/help` `/start`

Notifikasi hanya event-based (entry, fill, SL/TP, regime berubah, circuit breaker,
optimizer, shutdown) — tidak ada spam berkala.

## Menghentikan

`SIGINT`/`SIGTERM` → `handle_sig` hanya men-set `S.shutdown_flag`
([`main.py`](../main.py#L140)); loop berhenti di titik aman, lalu berurutan:

1. `poller`/`bal_poller` cancel, `mws_stop()`/`pws_stop()`, `state_task.cancel()`;
2. `cancel_deadman()` — matikan countdown supaya order tidak dibatalkan paksa;
3. batalkan **semua** order di `registry` (entry/sell/grid/dca) lalu `pop`;
4. batalkan `tp_order_id`/`sl_order_id` dari `_position_states` (langkah ini
   rangkap dengan 3 — lihat [`KNOWN_ISSUES.md`](KNOWN_ISSUES.md#perilaku-rangkap));
5. `Shutdown complete.`

Kalau proses **tidak** berhenti rapi (crash, OOM, redeploy Railway): order yang
sudah terkirim ke exchange tetap hidup, dan tanpa perpanjangan deadman
(`DEADMAN_COUNTDOWN_MS` = 15 menit) exchange membatalkan semuanya sendiri. Itu
bukan bug — itu jaring pengamannya.

## Persistensi & restart

State di `STATE_DIR`: `trades.db` (+WAL), `state.json`, `dca_state.json`,
`dca_trades.db`, `grid_state.json`, `grid_trades.db`. `state.json` ditulis
lewat tmp + `os.replace` (`persist.py:12`) jadi tidak mungkin separuh rusak.

Kalau `STATE_DIR` tidak diset di Railway, path jatuh ke direktori repo dan
**semua state hilang saat redeploy** — posisi terlihat hilang padahal order masih
hidup di exchange. Set `STATE_DIR=/data` dan pasang volume.

Untuk memaksa reset flag daily-loss saat boot: `CLEAR_LOSS_HOLD=1 python main.py`
([`main.py:278`](../main.py#L278)) — hanya reset flag, bukan posisi.

## Menguji koneksi live tanpa risiko dana

Urutan yang sudah terbukti (10-28 Okt, read-only + order tak terdanai):

1. `GET /api/v2/account` dan `/api/v2/openOrders` — membuktikan signature dan
   field mapping bekerja;
2. satu **MARKET BUY tak terdanai** (`quoteOrderQty` di bawah minimum) — exchange
   menolak karena dana, tapi penolakan itu membuktikan parameter + `Sign` diterima;
3. kalau mau melihat `FILLED` sungguhan, itu satu-satunya jalur yang **belum**
   pernah terverifikasi di setup ini (saldo IDR 0 saat migrasi v2).

## Railway

Deploy otomatis dari branch `main` — **push = deploy**. Tidak ada
`railway.json`/`Dockerfile`/healthcheck; build pakai `requirements.txt` dan
`runtime.txt` (`python-3.12`).

Variabel minimum untuk live: `PAPER_TRADING=false`, `INDODAX_V2_API_KEY`,
`INDODAX_V2_SECRET_KEY`, `INDODAX_API_KEY`, `INDODAX_SECRET_KEY` (deadman +
token WS), `STATE_DIR=/data`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
Opsional: `DEEPSEEK_API_KEY` (AI optimizer), `MAX_SCAN_PAIRS`, `MAKER_FIRST`.
