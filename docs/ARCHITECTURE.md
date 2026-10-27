# Arsitektur

Kondisi kode per 2026-10-28, setelah fix list 1-8. Semua nomor baris merujuk
file di repo ini pada commit `fbf9722` + dokumen ini.

## 1. Bentuk proses

Satu proses, satu event loop, satu worker Railway. Tidak ada thread dan tidak
ada multiproses — semua "paralel" di sini adalah `await` yang bergantian di
satu loop. Itu sebabnya satu `asyncio.Lock` cukup untuk menegakkan invarian,
dan itu juga sebabnya `await` di tengah baca-tulis state adalah bug, bukan
gaya penulisan.

```
main.py:728-740  task yang hidup bersamaan:
  market_ws_loop()      tick harga  ──┐
  private_ws_loop()     fill order  ──┤ publish sinkron (nol await)
  _state_consumer()     <─────────────┘ satu-satunya pembaca antrean
  _momentum_scanner()   scan 15 detik (config.MOMENTUM_SCAN_INTERVAL)
  _optimizer_loop()     cek tiap 15 detik; analisis jalan kalau ≥50 sell baru
                        ATAU ≥7 hari sejak run terakhir (AI_OPTIMIZER_INTERVAL_CYCLES
                        sudah tidak dibaca — lihat docs/KNOWN_ISSUES.md)
  _dca_task()           DCA engine, di bawah lock yang sama
  telegram_poller()     getUpdates long-poll
  _balance_poller()     refresh saldo
  portfolio_cycle()     1 cycle / 60 detik (LOOP_INTERVAL_SECONDS)
```

## 2. State: satu pemilik, nol `global`

[`engine/state.py`](../engine/state.py) (102 baris) memegang 50 nama state runtime:
`positions`, `_position_states`, `risk`, `portfolio_risk`, `hmm_detector`,
`momentum_engine`, `optimizer`, `grid_mini`, `dca_smart`, streak regime, cooldown,
circuit breaker, `_latest_balance`, `cycle_counter`, `state_bus`, `_state_lock`.

Konsumen menulis `S.<nama>` — tidak ada satu pun pernyataan `global` yang tersisa
di repo, dan itu dicek mesin (`tests/test_engine_state.py` T2/T3). Alasannya
praktis: sebelum pemecahan, `main.py` punya 11 pernyataan `global` yang merebind
27 nama state berbeda, jadi menulis `positions` tidak terlihat di call site;
sekarang setiap titik tulis terbaca.

`engine/state.py` **tidak** mengimpor `main.py`. Kalau suatu hari ada yang
menambahkan `from main import ...` di dalam `engine/`, itu circular import dan
CI menangkapnya (T5).

## 3. Alur satu siklus keputusan

```
fetch_viable_pairs ─> compute_batch_signals ─> classify_regime (HMM + rules)
        │
        ├── reconcile registry.list(ENTRY)  → order maker mati/grace habis?
        ├── reconcile registry.list(SELL)   → TP/SL terisi atau dibatalkan?
        ├── risk manager: Kelly, daily-loss floor, portfolio stop, circuit breaker
        ├── entry baru  → place_order (v2) → registry.add(pair, ENTRY, …)
        ├── posisi      → _sm_init + _sm_place_tp + _sm_place_sl (limit sell)
        ├── orphan sweep→ hanya order yang TIDAK dimiliki registry ∪ engine id
        └── persist + db (state.json, trades.db WAL)
```

Semuanya ada di [`engine/cycle.py`](../engine/cycle.py) `portfolio_cycle`
(1.290 baris). Fungsi ini adalah **penulis tunggal** keputusan trading; `main.py`
hanya memanggilnya satu kali per cycle dan selalu di dalam lock
([`main.py:744`](../main.py#L744)).

## 4. Registry order (satu sumber kebenaran)

[`orders.py`](../orders.py) menyimpan order yang menunggu, dengan kunci `(pair, kind)`:

| kind | isi |
|------|-----|
| `entry` | buy maker yang menunggu fill (rules engine) |
| `sell` | sell limit TP/SL/auto-sell yang menunggu fill |
| `grid_buy`, `grid_sell` | order resting grid |
| `dca_buy`, `dca_tp` | order resting DCA |

Sebelumnya ada tiga dict terpisah (`_pending_orders`, `_pending_sells`,
`_tp_limit_orders`) dan yang terakhir itu write-only. `_tp_limit_orders`
diperkenalkan `2c390b1` (1 Jul), lalu commit `718fa90` ("hapus TP limit order")
membuang semua **pembaca**-nya tapi meninggalkan penulisnya — akibatnya tujuh blok
"cancel TP order", termasuk yang di jalur shutdown, tidak pernah tereksekusi lagi
sejak 1 Juli sampai registry menggantikannya (`47c136b`). Registry menutup kelas bug
"state tak terpakai tapi masih ditulis" ini dengan test statis (T8).

Orphan cleanup tidak lagi menduga dari "pair ini punya position atau tidak", tapi
menanyakan kepemilikan id: `registry.owns(oid)` = id di registry ∪ id yang dibaca
langsung dari state engine hidup (`_engine_order_ids`, didaftarkan lewat
`registry.add_engine_source`, [`engine/state.py:102`](../engine/state.py#L102)).
Itu memperbaiki bug nyata: order grid/DCA yang resting dulu dianggap yatim dan
dibatalkan tiap 10 cycle.

## 5. Jalur websocket → state (StateBus)

Sebelum #12, tiap tick WS melakukan `asyncio.ensure_future(_realtime_sltp_check(...))`.
Pada 40 pair, itu puluhan task per detik yang semuanya mutate state yang sama dan
saling menyela di setiap `await`. Sekarang:

- task WS memanggil callback **sinkron** — `publish_tick` / `publish_fill`, nol
  `await`, nol spawn ([`market_ws.py:59`](../market_ws.py#L59),
  [`private_ws.py:94`](../private_ws.py#L94));
- tick di-*coalesce*: 1.000 tick satu pair jadi 1 event dengan harga terakhir;
- fill ditampung maksimal `MAX_PENDING_FILLS = 256`, sisanya di-*drop* dan
  **tercacat** (`state_bus.dropped_fills`), tidak hilang diam-diam;
- `_state_consumer` ([`main.py:51`](../main.py#L51)) meng-drain antrean dan
  memprosesnya satu per satu di dalam `S._state_lock`
  ([`main.py:59`](../main.py#L59)).

Empat pemegang lock yang sama: consumer (59), momentum BUY (219), DCA (712),
portfolio cycle (744). Artinya **tidak pernah ada dua jalur yang menulis state
bersamaan** — dan konsekuensi yang sengaja diterima: latency realtime SL/TP sama
dengan durasi cycle terpanjang. Ukur dari baris log
`⏱ Cycle #N finished in Xs`; kalau X membesar, langkah yang disepakati adalah
memperkecil wilayah lock, bukan menghapus serialisasi.

## 6. Gerbang exchange

[`executor.py`](../executor.py) (685 baris) satu-satunya modul yang boleh bicara ke
TAPI. Default **v2**: `https://api.indodax.com`, header `X-APIKEY` + `Sign`
HMAC-SHA256 atas query/body, endpoint `POST/DELETE/GET /api/v2/order`,
`/api/v2/openOrders`, `/api/v2/account`. Response v2 dipetakan balik ke nama field
v1 (`order_id`, `remain_<coin>`, `remain_rp`, status lowercase) supaya call site
tidak perlu tahu versinya.

Yang dijamin di gerbang ini, bukan di pemanggil: rate limit (bucket), parsing
`429` + `Retry-After`, idempotensi lewat `client_order_id`, dan `quantize_price`
/`qty increment` dari metadata `/api/pairs`. Cara pakai yang benar:
`quoteOrderQty` untuk market buy, `timeInForce=MOC` (menggantikan retry
"not maker" v1), `selfTradePreventionMode=EXPIRE_TAKER` supaya order baru tidak
membatalkan TP/SL kita sendiri.

Dua permukaan tidak punya padanan v2 dan tetap butuh kunci legacy: deadman
`POST https://indodax.com/tapi/countdownCancelAll` dan token private-WS
`POST https://indodax.com/api/private_ws/v1/generate_token`.

## 7. Persistensi

| File | isi | ditulis oleh |
|------|-----|--------------|
| `trades.db` (SQLite, WAL) | tabel `trades`, `meta`, `decisions`, `chat_history` | `db.py` |
| `state.json` | positions, cooldown, SM cooldown, circuit breaker, peak equity | `persist.py` (tmp + `os.replace`) |
| `dca_state.json`, `dca_trades.db` | instance & trade DCA | `dca_smart.py` |
| `grid_state.json`, `grid_trades.db` | instance & trade grid | `grid_mini.py` |

Semua ke path `DATA_DIR = STATE_DIR or DATA_DIR or /data atau direktori repo`.
`STATE_DIR` adalah env yang benar untuk Railway volume; tanpa itu state ditulis di
directory sumber dan hilang saat redeploy. Kelima file data di-gitignore
(`*.db`, `*.json`).

## 8. Batas yang tidak boleh dilewati

- Jangan memanggil `portfolio_cycle` dari luar `S._state_lock`.
- Jangan mutate state dari task WS; publish ke `state_bus` saja.
- Jangan tambah `global` — tulis `S.<nama>`.
- Jangan panggil `httpx` ke TAPI dari modul selain `executor.py`.
- Jangan jalankan `dca_main.py` bersamaan dengan `run_all.py`: dua engine DCA
  menulis `dca_state.json` yang sama dan memasang base order ganda
  (alasan di [`run_all.py`](../run_all.py) docstring).
