# Changelog

Semua commit sejak review kritis sampai sekarang, berurutan terbalik. Kolom
**bukti** berisi hal yang bisa kamu jalankan sendiri — bukan klaim.

Arah baca: fix list lahir dari review kode terhadap Indodax official API docs
(`github.com/btcid/indodax-official-api-docs`) dan `main.py` versi 2.471 baris.
Nomor fix list mengacu ke daftar review itu.

| Fix | Isi | Commit |
|-----|-----|--------|
| 1 | Double DCA engine + idempotensi order (`client_order_id`) | `f3f8700` |
| 2 | Rate limiter + handling 429/backoff di satu gerbang TAPI | `f3f8700` |
| 3 | Paper/live parity: layer risiko identik di paper mode | `f3f8700` |
| 4 | Validasi price/qty dari metadata `/api/pairs` | `f3f8700` |
| 5 | Golden tests executor/rules/SM + perbaikan `backtest.py` | `f3f8700` |
| 6 | Migrasi TAPI v1 → v2 | `f3f8700` |
| 7 | CI workflow | `0598e06` |
| 11 | Registry order tunggal (ganti 3 dict) | `47c136b` |
| 12 | Antrean state: task WS tidak mutate langsung | `ac1b70f` |
| 8 | Pecah `main.py` jadi modul `engine/` | `37402e3` → `6df110a` → `fbf9722` |
| 9–10 | Tidak dikerjakan (review ulang menunjukkan keduanya sudah tertutup #11/#12) | — |

---

## 2026-10-28 — dokumen baru

Tiga commit, berturut dari yang paling tua: `10b8608` (arsip), `a7cc68b`
(perbaikan test), commit ini (dokumen).

- **`10b8608`** — dokumen lama (`README.md`, `ONBOARDING.md`,
  `README-AI-version.md`, `SCALING.md`, `SECURITY.md`) dipindah via `git mv` ke
  [`docs/archive/`](archive/) dan diberi banner arsip di baris pertama; isi
  aslinya tidak diubah satu karakter pun. Indeks:
  [`docs/archive/INDEX.md`](archive/INDEX.md).
- **`a7cc68b`** — `tests/test_position_sm.py` T10: sebelumnya membandingkan AST
  dengan backup `/tmp/main_stage2_before.py` (test yang tidak bisa jalan dua
  kali, dan diam-diam jadi SKIP), sekarang mengunci kontrak lock di docstring
  `engine/position_sm.py`.
- **commit ini** — dokumen baru, semua klaimnya diverifikasi ulang terhadap kode
  di `fbf9722`: [`README.md`](../README.md),
  [`docs/ARCHITECTURE.md`](ARCHITECTURE.md),
  [`docs/CONFIGURATION.md`](CONFIGURATION.md), [`docs/OPERATIONS.md`](OPERATIONS.md),
  [`docs/TESTING.md`](TESTING.md), [`docs/SECURITY.md`](SECURITY.md),
  [`docs/KNOWN_ISSUES.md`](KNOWN_ISSUES.md), dokumen ini. Plus `.env.example`:
  26 env var yang berpengaruh, tanpa satu pun nilai rahasia.
- Yang **tidak** berubah: kode produksi. Nol baris logika bot disentuh di ketiga
  commit ini.

## `fbf9722` — refactor(engine): potong `portfolio_cycle` + regime classifier ke `engine/cycle.py`

Fix list #8, tahap 3 (terakhir).

- `engine/cycle.py` baru, 1.404 baris: `classify_regime`, `correlation_risk`,
  `portfolio_cycle` (1.290 baris, mulai [`engine/cycle.py:116`](../engine/cycle.py#L116)).
- `main.py` jadi 789 baris: entri proses, consumer state, loop pendukung,
  shutdown. `from engine.cycle import portfolio_cycle` satu baris.
- Total setelah tiga tahap: `main.py` 2.471 → 789 baris, **nol** statement
  `global` (sebelumnya 11 statement me-rebind 27 nama).
- Test baru `tests/test_cycle_module.py` (8 kasus): struktur definisi, audit
  nama-bocor antar modul, subprocess tanpa import `main`, klasifikasi regime
  BULL/BEAR/SIDEWAYS_LOW_VOL/HIGH_VOL dengan `FakeHMM`, rasio
  `correlation_risk`, lokasi pemakaian lock, dan `portfolio_cycle` yang benar-benar
  dijalankan dengan client `None` untuk membuktikan circuit-breaker early-return
  dan jalur error hidup.
- Bukti: `python tests/test_cycle_module.py` → 8 `OK:`; 10 suite → 82 kasus
  (`run_all.py` di root adalah entri produksi, bukan runner test — jalankan
  loop `for f in tests/test_*.py`, lihat [`docs/TESTING.md`](TESTING.md)).
- Catatan proses: pruning import di tahap ini hampir merusak kode. Heuristik
  "nama yang jumlah kemunculannya 1" menyebut `fetch_orderbook`,
  `get_trades_by_period`, `portfolio_cycle` tidak terpakai; menghapusnya
  menghasilkan 3 `NameError` saat runtime. Authority untuk pruning adalah audit
  nama-tak-terdefinisi dari AST, bukan penghitung nama. Aturan itu sekarang
  tertulis di [`docs/TESTING.md`](TESTING.md).

## `6df110a` — refactor(engine): potong position state machine ke `engine/position_sm.py`

Fix list #8, tahap 2.

- `engine/position_sm.py` baru, 265 baris, 11 fungsi: `pnl_pct`, `add_position`,
  `_sm_cancel`, `_sm_place_sell`, `_sm_place_tp`, `_sm_place_sl`, `_sm_init`,
  `_sm_cleanup`, `_sm_get`, `_handle_fill_event`, `_realtime_sltp_check`.
- Kontrak lock ditulis di docstring modul: fungsi di sini **hanya** boleh
  dipanggil dari dalam `S._state_lock`. Itu sekarang di-assert oleh test (T10),
  jadi menghapus docstring-nya menggagalkan suite.
- `main.py` kehilangan 250 baris; ia tinggal konsumen
  (`from engine.position_sm import ...`). Nol circular import, dibuktikan dengan
  subprocess: import `engine.position_sm` tidak memuat `main`.
- Test baru `tests/test_position_sm.py` (10 kasus) dengan harness stub sendiri
  (`FakeClient`, `fake_place`, `fake_cancel`, `fake_notify`) — jalur transisi SM,
  debounce `_realtime_sltp_last`, dan gating pyramid sebelumnya tidak teruji
  sama sekali.
- Setiap body fungsi dibuktikan identik lewat `ast.dump()` terhadap
  `main.py` pra-ekstraksi pada saat pemindahan. (Test yang menyimpan perbandingan
  itu ke `/tmp` kemudian diganti karena tidak dapat dijalankan ulang — lihat
  commit dokumentasi.)

## `37402e3` — refactor(engine): pindahkan 50 state runtime ke `engine/state.py`

Fix list #8, tahap 1.

- `engine/state.py` baru, 102 baris, jadi pemilik tunggal 50 nama state
  (`positions`, `_position_states`, `risk`, `grid_mini`, `dca_smart`, `_cb_*`,
  `_realtime_sltp_last`, `state_bus`, `_state_lock`, `cycle_counter`, …).
- Modul ini tidak mengimpor `main` → tidak ada siklus impor.
- `_engine_order_ids()` ditambahkan di
  [`engine/state.py:85-99`](../engine/state.py#L85) dan didaftarkan ke registry
  lewat `registry.add_engine_source()`
  ([`engine/state.py:102`](../engine/state.py#L102)). Registry sekarang bisa
  menjawab "order ini punya kita?" tanpa merampas state grid/DCA/SM.
- Test baru `tests/test_engine_state.py` (7 kasus). Daftarnya bukan hardcode:
  `STATE_NAMES` diturunkan dari `dir(S)`, jadi menambah state tanpa memperbarui
  test ketahuan.

## `ac1b70f` — fix race: event WS antre di StateBus, mutasi state dikunci satu consumer

Fix list #12.

- Masalah: setiap tick websocket memanggil
  `asyncio.ensure_future(_realtime_sltp_check(...))`. Pada 40 pair itu puluhan
  task per detik, semuanya mutate state yang sama, diinterleave oleh `await` di
  tengah-tengah.
- Sekarang: `state_bus.py` (82 baris) — antrean event dengan coalescing
  latest-wins per pair, buffer fill bounded 256 + counter drop; satu
  `_state_consumer` ([`main.py:51`](../main.py#L51)) yang mengambil snapshot dan
  memprosesnya di dalam `async with S._state_lock`.
- Semua titik mutasi pakai lock yang sama: consumer
  [`main.py:59`](../main.py#L59), momentum BUY
  [`main.py:219`](../main.py#L219), DCA [`main.py:712`](../main.py#L712),
  cycle [`main.py:744`](../main.py#L744).
- Trade-off yang diterima: SL/TP realtime kini berlatensi satu durasi cycle.
  Lihat [`docs/KNOWN_ISSUES.md`](KNOWN_ISSUES.md#7-trade-off-yang-diterima-sadar).
- Test baru `tests/test_state_queue.py` (7 kasus): publish sinkron tanpa
  `await`, drain snapshot, latest-wins, bounded buffer + pencacah drop, dan
  check statis bahwa `ensure_future(_realtime_sltp_check` tidak boleh muncul lagi.

## `47c136b` — refactor: registry order tunggal ganti 3 dict state

Fix list #11.

- Sebelum: `_pending_orders`, `_pending_sells`, `_tp_limit_orders`. Yang
  terakhir itu write-only: diperkenalkan `2c390b1` (1 Jul), lalu `718fa90`
  ("hapus TP limit order") membuang semua **pembaca**-nya tapi meninggalkan
  penulisnya. Akibatnya tujuh blok "cancel TP order", termasuk yang di jalur
  shutdown, mati tanpa suara sampai registry menggantikannya.
- Sesudah: `orders.py` (132 baris), keyed `(pair, kind)` dengan kind
  `entry/sell/grid_buy/grid_sell/dca_buy/dca_tp`; `owns(id)` = id registry ∪ id
  yang dibaca live dari state engine.
- Perbaikan bug nyata: order grid/DCA yang resting sebelumnya dianggap yatim
  hanya karena pair-nya tidak punya posisi, lalu dibatalkan setiap 10 cycle
  ([`engine/cycle.py:1336-1352`](../engine/cycle.py#L1336)).
- Test baru `tests/test_order_registry.py`; termasuk check statis T8 yang
  menolak kemunculan lagi pola "dict state yang ditulis tapi tidak dibaca".
- Sisa yang sengaja ditinggalkan: `_position_states[pair]["tp_order_id"]` masih
  salinan paralel registry. Efeknya ada di
  [`docs/KNOWN_ISSUES.md`](KNOWN_ISSUES.md#4-perilaku-rangkap).

## `0598e06` — ci: jalankan suite test tiap push/PR ke main

Fix list #7.

- `.github/workflows/test.yml`: Python 3.12 (sama dengan `runtime.txt`),
  `pip install -r requirements.txt`, `.env` dilacak → build gagal,
  `python -m py_compile *.py engine/*.py tests/*.py`, lalu setiap
  `tests/test_*.py` dijalankan dengan `timeout 300` dan diakumulasikan
  (non-fail-fast, supaya satu suite mati tidak menyembunyikan suite lain).
- `STATE_DIR: ${{ runner.temp }}/fma-state` — test tidak boleh menulis ke
  direktori repo.
- **Status jujur:** workflow ini belum pernah benar-benar berjalan. `git push`
  belum dilakukan (`main` auto-deploy ke Railway, dan push bukan keputusan
  agent). Hijau di laptop ≠ hijau di runner.

## `f3f8700` — migrasi TAPI v2 + hardening gerbang order (fix list 1–6)

Commit terbesar: 15 file, +1.448/−399.

- **#6 TAPI v2.** `https://api.indodax.com` dengan header `X-APIKEY` dan
  `Sign` HMAC-SHA256 atas payload urlencoded (`POST/DELETE/GET
  /api/v2/order|openOrders|account`), `recvWindow=5000`. Adapter memetakan
  respons v2 ke nama field v1 supaya `engine/` tidak perlu berubah.
  `TAPI_VERSION=auto` memilih v2 hanya kalau kedua kunci v2 ada
  ([`config.py:105-111`](../config.py#L105)). Deadman switch dan token
  private-WS **hanya** ada di endpoint legacy, jadi `INDODAX_API_KEY` tetap
  wajib meski trading sudah v2 ([`config.py:114-116`](../config.py#L114)).
- **#1 Idempotensi.** Dua DCA engine pernah jalan bersamaan dan memasang order
  ganda. Satu engine (`dca_smart.py`), dan order diberi `client_order_id`.
- **#2 Rate limit.** Semua panggilan TAPI lewat satu gerbang dengan limiter +
  backoff untuk 429; sebelumnya retry tersebar di call site.
- **#3 Paper parity.** Layer risiko di paper mode kini memakai kode yang sama
  dengan live mode (sebelumnya jalur paper memotong beberapa gerbang, sehingga
  hasil backtest/paper tidak membuktikan apa pun tentang perilaku live).
- **#4 Tick size.** Harga/qty divalidasi dari metadata `/api/pairs`
  (`rules.py`, `quantize_price`) — order tidak bisa lagi ditolak exchange karena
  desimal salah.
- **#5 Golden tests.** `tests/test_golden.py`, `test_pair_rules.py`,
  `test_executor_idempotency.py`, `test_paper_parity.py`, `test_tapi_v2.py`
  (263 baris) ditambahkan di sini; `backtest.py` ikut diperbaiki supaya bisa
  dijalankan dengan argumen.
- **Konsekuensi yang belum ditutup:** fill live dengan dana sungguhan belum
  pernah diuji nyata. Tanda tangan dan parsing teruji terhadap respons
  read-only, bukan terhadap urutan order→fill→update WS.

## Sebelum ini

`8a4f250` (18 Jul) adalah commit terakhir era lama: "fix paper mode: skip SM agar
sell tidak terblokir selamanya". Rantai `99b2186`, `45035fb`, `e90fd5e`,
`8bd07ce` (15 Jul) semuanya adalah tambal-sulang paper mode yang mendorong bot
ke arah yang sekarang dibalik oleh #3 — tiap tambalan justru membuang gerbang
risiko supaya paper mode jalan. Riwayat itu alasan [`docs/ARCHITECTURE.md`](ARCHITECTURE.md)
menolak konsep "jalur paper terpisah".
