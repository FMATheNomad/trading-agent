# Known issues

Semua entri di sini diverifikasi ulang terhadap kode pada commit `fbf9722`
(28 Okt 2026), dan setiap klaim punya nomor baris yang bisa kamu cek sendiri.
Urut dari yang paling mahal. Kalau kamu hanya punya waktu untuk satu fix,
ambil yang nomor 1.

Istilah: "yatim" = order yang resting di exchange tapi tidak ada satu pun
struktur data di dalam proses yang tahu id-nya.

---

## 1. Sweep startup membatalkan order grid/DCA sebelum keduanya dimuat

**Gejala (live).** Bot restart → semua limit order grid dan DCA yang sedang
resting dibatalkan, tapi engine-nya kemudian me-load instance dari disk dan
mengira ordernya masih ada.

**Sebab.** Urutan eksekusi di `main()`:

| Baris | Yang terjadi |
|-------|--------------|
| [`main.py:305`](../main.py#L305) | `S.grid_mini.load_instances()` — id order grid ikut dipulihkan |
| [`main.py:308-321`](../main.py#L308) | load circuit breaker |
| [`main.py:325-342`](../main.py#L325) | **cancel semua open order di semua pair**, tanpa bertanya siapa pemiliknya |
| [`main.py:700`](../main.py#L700) | `S.dca_smart.load_instances()` — DCA belum tahu ordernya sudah hilang |

Sweep itu lahir sebelum registry ada, dan ia tidak pakai `registry.owns()`
seperti orphan cleanup di cycle. Registry saat itu masih kosong, jadi
sebenarnya tidak ada cara untuk mengenali kepemilikan di titik itu — yang ada
hanya `grid_mini.instances` yang sudah terisi sejak baris 305.

**Dampak, per engine (dibaca dari kode, bukan dugaan):**
- **Grid** ([`grid_mini.py:221-224`](../grid_mini.py#L221)): instance dengan
  status order `cancelled` dibuang dari `self.instances` **dan** pair-nya
  masuk `_pair_blacklist` untuk sisa sesi. Jadi kaki grid hilang senyap dan
  pair itu tidak bisa di-grid lagi sampai restart.
- **DCA safety order** ([`dca_smart.py:200-201`](../dca_smart.py#L200)):
  `so["order_id"] = None`, lalu `place_safety_and_tp()`
  ([`dca_smart.py:205`](../dca_smart.py#L205)) memasang ulang order yang sama.
  Hasilnya churn: sweep membatalkan, engine memasang ulang, dan id order lama
  hilang dari catatan engine. Tidak merusak posisi, tapi boros panggilan API
  dan harga basis tidak pernah dihitung ulang.
- **DCA base order**: `base_order_id` yang ikut dibatalkan tetap tersimpan di
  instance (`main.py:700` memuatnya *setelah* sweep), jadi tidak ada yang tahu
  order induknya sudah mati sampai reconcile berikutnya.
- **Position SM**: TP/SL yang resting ikut hilang; `_position_states` masih
  menunjuk id lama sampai cycle berikutnya memanggil `_sm_get`/`_sm_cleanup`.

**Fix yang benar.** Pindahkan sweep ke setelah kedua `load_instances()`, lalu
ganti loop buta dengan `registry.owns(oid)` ∪ id dari `_engine_order_ids()`
([`engine/state.py:85-99`](../engine/state.py#L85)) — persis seperti yang sudah
dilakukan orphan cleanup di [`engine/cycle.py:1336-1352`](../engine/cycle.py#L1336).

---

## 2. State circuit breaker tidak pernah sampai ke cycle

**Gejala.** Setelah restart, log bilang `🛑 CIRCUIT BREAKER aktif — 5 hari loss
berturut, sisa 41 jam cooldown`, tapi bot tetap membuka entry baru.

**Sebab.** Blok restore menulis variabel **lokal**, bukan state:

```python
# main.py:309-311 — tanpa `global`, dan tanpa prefiks S.
_cb_consecutive_loss_days = cb.get("consecutive_loss_days", 0)
_cb_last_loss_date = cb.get("last_loss_date", "")
_cb_active_until = cb.get("active_until", 0)
```

Pemilik nama-nama itu adalah `engine/state.py`
([`engine/state.py:73-76`](../engine/state.py#L73)), dan
`portfolio_cycle` membacanya sebagai `S._cb_active_until`
([`engine/cycle.py:125`](../engine/cycle.py#L125)). Karena `main()` tidak
pernah menulis `S._cb_active_until`, nilai yang dilihat cycle selalu `0` →
gerbang cooldown tidak pernah aktif sesudah restart. Persist-nya sendiri benar
(`portfolio_cycle` menulis cooldown lewat `persist.save_circuit_breaker` di
[`engine/cycle.py:133`](../engine/cycle.py#L133)); yang putus hanya jalur baca.

**Kenapa ini lolos test.** Tidak ada test yang men-simulasikan restart dengan
`circuit_breaker.json` berisi cooldown aktif. `tests/test_cycle_module.py`
hanya menguji jalur cycle yang hidup.

**Fix.** Tiga baris itu jadi `S._cb_* = ...`. (Catatan: repo ini sengaja tidak
punya `global`, jadi `S.` adalah satu-satunya cara yang benar.) Perlu test
baru: tulis `circuit_breaker.json` dengan `active_until` di masa depan,
panggil blok boot, assert `S._cb_active_until != 0`.

---

## 3. Order sell-retry tidak pernah punya pemilik

**Gejala (live).** Sell limit maker tidak terisi 3 cycle → cycle membatalkannya
dan memasang ulang di bid. Order hasil pasang-ulang itu tidak terpantau oleh
siapa pun.

**Sebab.** Di jalur retry
([`engine/cycle.py:202-214`](../engine/cycle.py#L202)):

```python
if registry.bump(pid, SELL) >= config.SELL_GRACE_CYCLE:
    await cancel_order(client, oid, pair=pid, side="sell")
    bid = int(S._latest_ticker_map.get(pid, {}).get("buy", 0))
    if bid > 20:
        sell_ret = await _sm_place_sell(client, pid, ps.qty, bid)   # ← id-nya dibuang
    registry.pop(pid, SELL)
```

`_sm_place_sell` mengembalikan dict order
([`engine/position_sm.py:67-72`](../engine/position_sm.py#L67)), tapi hanya
`None`/bukan-`None` yang dipakai untuk log. Id order baru tidak masuk registry
(`registry.add` untuk SELL hanya dipanggil di
[`engine/cycle.py:1002`](../engine/cycle.py#L1002)), tidak masuk
`_position_states` (field `tp_order_id`/`sl_order_id` tidak disentuh), dan
`registry.pop(pid, SELL)` sudah menghapus catatan lama.

**Dampak.** Dua hal, tergantung nasib order:
- Kalau terisi: aman, fill ditangkap private WS / reconcile posisi.
- Kalau tidak terisi: tidak ada yang membatalkan atau memantau. Order itu
  bertahan sampai (a) harga lewat dan terisi sendiri, atau (b) pair itu keluar
  dari `tracked` di [`engine/cycle.py:1337`](../engine/cycle.py#L1337) — yaitu
  saat posisi hilang dari `S.positions` dan registry kosong untuk pair itu —
  lalu orphan sweep membatalkannya sebagai "yatim"
  ([`engine/cycle.py:1349`](../engine/cycle.py#L1349)). Untuk pair yang
  posisinya sudah terjual sebagian, itu berarti order jual kedua bisa tetap
  resting tanpa pemilik selama posisinya masih tercatat.

**Fix.** Catat id-nya: `registry.add(pid, SELL, sell_ret["order_id"],
side="sell", ...)` dan hapus `registry.pop(pid, SELL)` yang di akhir — `add()`
sudah menimpa keyed `(pair, kind)` ([`orders.py:79-84`](../orders.py#L79)), jadi
pop justru membuang catatan yang baru dibuat.

---

## 4. Perilaku rangkap

<a id="perilaku-rangkap"></a>

Dua tempat mengerjakan hal yang sama, tanpa yang satu tahu:

**(a) Shutdown membatalkan dua kali.**
[`main.py:762-778`](../main.py#L762):
1. loop `registry.list()` → `cancel_order(...)` untuk setiap tracked order;
2. langsung setelahnya, loop `S._position_states` → `_sm_cancel(...)` untuk
   `tp_order_id`/`sl_order_id`.

Kedua loop itu mengenai order yang sama (TP/SL SM juga tercatat di registry),
jadi tiap shutdown menghasilkan panggilan API ganda. Tidak merusak state, tapi
menambah latensi shutdown dan memakan rate limit. Arahkan satu loop saja —
registry sudah mencakup id SM.

**(b) Dua tempat menyimpan id order yang sama.**
Registry keyed `(pair, kind)` dan `_position_states[pair]["tp_order_id"]` /
`["sl_order_id"]` adalah salinan yang harus dijaga sinkron manual. Karena itu
`_engine_order_ids()` harus membaca keduanya
([`engine/state.py:92-93`](../engine/state.py#L92)). Selama dual-write ini ada,
selalu ada satu jalur (lihat isu #3) yang lupa menulis salah satunya.

---

## 5. Yang belum terbukti di produksi

Ini bukan bug, ini **belum diuji**. Bedakan keduanya.

- **Fill live dengan dana sungguhan.** Semua jalur fill
  (`_handle_fill_event`, debounce, pyramid, reconcile sell) dijalankan dengan
  client dan order id palsu di test. Belum ada satu pun order berdana yang
  dikirim → diterima → di-update lewat private WS. Migrasi v2
  (`f3f8700`) menguji tanda tangan dan respons terhadap sandbox/key read-only,
  tidak terhadap fill.
- **CI: tertutup, dengan catatan.** `.github/workflows/test.yml` dibuat di
  `0598e06` tapi belum pernah di-push sampai 27 Okt. Begitu di-push, run
  pertamanya mati dalam 0 detik (`workflow file issue`, 0 job) karena
  `${{ runner.temp }}` dipasang di `env:` level job — context `runner` hanya
  sah di level step, dan `yaml.safe_load` tidak menangkapnya. Diperbaiki di
  `198bb8b`; run [40584759444](https://github.com/FMATheNomad/trading-agent/actions/runs/40584759444)
  hijau: 10/10 suite, 82 `OK:`, 1m13s pada Python 3.12. Yang masih terbuka:
  workflow belum pernah gagal-test dilihat di CI (kita tahu ia bisa lulus, belum
  tahu apakah ia benar-benar menangkap regresi), dan `actionlint` belum masuk
  langkah CI.
- **Restart dengan state rusak.** Belum ada uji putus-listrik: `state.json`
  separuh tertulis, atau `trades.db` WAL yang tidak ter-checkpoint.

---

## 6. Knob mati

<a id="knob-mati"></a>

19 konstanta di `config.py` tidak dibaca satu file pun. Mengubahnya = tidak
mengubah apa pun. Audit: setiap nama tingkat modul di `config.py` dicari
pemakaiannya (`config.NAMA` / `from config import NAMA`) di seluruh `*.py` dan
`engine/*.py`, test dikecualikan.

```
DEFAULT_PLAY_CAPITAL_PCT  PORTFOLIO_MODE          MAX_SECTOR_EXPOSURE_PCT
ATR_MIN_MOVE_MULTIPLIER   MAX_ATR_PCT             ATR_STAGNANT_MULT
TAKER_SELL_FEE_PCT        CFX_FEE_PCT             TOTAL_MAKER_ROUNDTRIP_PCT
TOTAL_TAKER_ROUNDTRIP_PCT FEE_CLEARANCE_RATIO     SYMBOL
DEEPSEEK_THINKING_MODE    SENTRY_DSN              ROTHSCHILD_LIMIT_GRACE_SEC
CAPITAL_GROWTH_MULTIPLIER AI_OPTIMIZER_INTERVAL_CYCLES
FUNDAMENTAL_COINS         RECOVERY_TOP
```

Yang paling menyesatkan:

- **`SENTRY_DSN`.** `sentry-sdk>=2.0` ada di `requirements.txt`, tidak ada satu
  pun `import sentry`. Crash hari ini hanya masuk log worker. Kamu pikir ada
  alerting — tidak ada.
- **`AI_OPTIMIZER_INTERVAL_CYCLES` (default 50).** Nilainya dibaca dari env lalu
  dibuang. `_optimizer_loop` ([`main.py:73-101`](../main.py#L73)) sebenarnya
  polling tiap 15 detik dan baru jalan kalau ada ≥50 sell baru **atau** ≥7 hari
  sejak terakhir. Meng-set `AI_OPTIMIZER_INTERVAL_CYCLES=1` tidak membuat
  optimizer lebih sering.
- **Lima konstanta fee** (`TAKER_SELL_FEE_PCT`, `CFX_FEE_PCT`,
  `TOTAL_*_ROUNDTRIP_PCT`, `FEE_CLEARANCE_RATIO`). Angka roundtrip maker
  0,6211% / taker 0,8311% di [`docs/CONFIGURATION.md`](CONFIGURATION.md) adalah
  kosakata, bukan perhitungan. Biaya yang benar-benar dipakai ada di
  `risk_manager.py` dan `rules.py`.
- **`FUNDAMENTAL_COINS`, `RECOVERY_TOP`, `SKIP_COINS`.** Semua `set()` kosong —
  sisa dari desain screening lama. (`SKIP_COINS` memang dibaca; dua yang lain
  tidak.)
- **`ATR_STAGNANT_MULT`.** Ini satu-satunya knob mode-table yang nilainya
  berubah per profil (STANDARD 0.8 [`config.py:42`](../config.py#L42) / ALPHA
  1.0 / INSANE 0.5) — jadi pembaca config akan mengira ia
  mengatur perilaku. Ia tidak mengatur apa pun.

**Sikap yang disarankan:** jangan hapus dulu (dokumen lama dan hasil backtest
mereferensinya), tapi tandai tiap nama dengan komentar `# DEAD:` supaya orang
berikutnya tidak membuang waktu menyetelnya. Untuk `SENTRY_DSN` ada dua pilihan
jujur: pasang `sentry_sdk.init(dsn=...)` di `main()` atau buang dependensinya.

---

## 7. Trade-off yang diterima sadar

Bukan bug — ini harga yang dibayar sengaja, dan dokumentasi ini tidak akan
pura-pura bilang sebaliknya.

| Hal | Harga | Alasan |
|-----|-------|--------|
| SL/TP realtime punya latensi = durasi satu cycle (bisa puluhan detik saat market penuh) | Order SL/TP baru terpasang setelah cycle selesai, bukan saat tick masuk | Task WS tidak boleh mutate state ([`docs/ARCHITECTURE.md`](ARCHITECTURE.md#5-jalur-websocket--state-statebus)). Memakai `ensure_future` per tick itu bug yang kita hapus di `ac1b70f` |
| Satu worker, satu loop asyncio | Tidak ada skal horizontal; `portfolio_cycle` 1.290 baris blocking-ish CPU ikut menahan loop | Konsistensi state lebih mahal daripada throughput. Two-process = dua writer = butuh lock lintas proses |
| `MAX_OPEN_POSITIONS` vs `max_positions_for_equity()` | Dua sumber kebenaran untuk satu konsep | Belum disatukan; dibaca di cycle yang berbeda. Selama mode ALPHA keduanya memberi angka sama (4) |
| Requirements pakai `>=` | Build hari ini tidak reproducible; hasil besok bisa beda | Meng-pin butuh uji ulang di interpreter produksi; belum dilakukan |
| State in-memory + `state.json` snapshot | Crash di antara dua snapshot = kehilangan perubahan kecil | Snapshot-based persistence lebih sederhana daripada event log; belum perlu replay |
| Heuristics nama vs audit AST | Penghapusan import pakai audit undefined-name AST, bukan hitung nama | Hitung nama salah: ia pernah menyebut `fetch_orderbook`, `get_trades_by_period`, `portfolio_cycle` tidak terpakai dan menghasilkan 3 `NameError`. Lihat [`docs/TESTING.md`](TESTING.md) |

---

## 8. Sudah ditutup

Daftar lengkap dengan commit-nya ada di [`docs/CHANGELOG.md`](CHANGELOG.md).
Yang paling struktural:

- **Tiga dict state order yang write-only** (`_pending_orders`, `_pending_sells`,
  `_tp_limit_orders`) → satu registry keyed `(pair, kind)` (`47c136b`).
  `_tp_limit_orders` ditinggalkan tanpa pembaca oleh `718fa90` dan tetap ditulis
  sampai `47c136b`; sejak itu orphan cleanup bertanya ke pemilik id, bukan
  menduga dari "pair ini punya position atau tidak".
- **Puluhan task WS per detik yang mutate state bersama** → `StateBus`
  coalescing + satu `_state_consumer` + satu `S._state_lock` (`ac1b70f`).
- **`main.py` 2.471 baris dengan 27 nama `global`** → 789 baris +
  `engine/state.py` / `engine/position_sm.py` / `engine/cycle.py`, dengan body
  fungsi dibuktikan AST-identik saat dipindahkan.
- **Double DCA engine** dan order tanpa idempotensi (`f3f8700`), rate limit dan
  backoff 429 di satu gerbang, paper/live parity, validasi tick size dari
  `/api/pairs`.
