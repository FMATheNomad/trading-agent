# Testing

10 suite, 82 kasus terverifikasi, tanpa framework (tidak ada pytest — tiap file
standalone, `assert` langsung, keluar nonzero kalau gagal).

```bash
./.venv/bin/python tests/test_golden.py            # satu suite
for f in tests/test_*.py; do ./.venv/bin/python "$f" || echo "FAIL $f"; done   # semua
```

Lokal (Python 3.14, venv repo): **1 menit 25 detik** untuk 10 suite (diukur lagi
setelah dokumen ini ditulis; 1m37s pada run sebelumnya). Sistem
Python biasa tidak punya `httpx`/`dotenv`, jadi `python tests/...` tanpa venv
selalu gagal `ModuleNotFoundError` — itu bukan kegagalan test.

| Suite | Kasus | Yang dikunci |
|-------|-------|--------------|
| [`test_tapi_v2.py`](../tests/test_tapi_v2.py) | 13 | signature v2, mapping field balik ke nama v1, `quoteOrderQty`, `MOC`, retry lebih dalam dengan `client_order_id` baru, zero-call di paper mode |
| [`test_golden.py`](../tests/test_golden.py) | 13 | perilaku ujung-ke-ujung: TP fill + fee, arming SL, trailing ratchet, entry momentum BULL, blokir BEAR/HIGH_VOL, mean-reversion, `backtest.py` selesai penuh |
| [`test_position_sm.py`](../tests/test_position_sm.py) | 10 | lifecycle SM, matematika ATR + floor 0,5%/1,5%, transisi TP→SL→TP, debounce 10 detik (50 tick = 1 reaksi), tidak ada order hantu untuk pair asing, daily-loss halt di atas pyramid |
| [`test_order_registry.py`](../tests/test_order_registry.py) | 9 | kekuncian `(pair, kind)`, kind tak dikenal → `ValueError`, snapshot `list()` aman untuk pola hapus-sambil-iterasi, `owns()` = registry ∪ id engine, order grid tanpa position tetap terlindungi |
| [`test_cycle_module.py`](../tests/test_cycle_module.py) | 8 | definisi keputusan hanya di `engine/cycle.py`, **nol nama yang bocor dari `main.py`**, `classify_regime` 4 jalur + override HMM, circuit breaker aktif/kedaluwarsa dieksekusi betulan |
| [`test_engine_state.py`](../tests/test_engine_state.py) | 7 | 50 state hanya ada di `engine/state.py`, nol `global` di `main.py`, nol referensi state telanjang di fungsi mana pun, `main` dan `engine.state` berbagi objek yang sama |
| [`test_state_queue.py`](../tests/test_state_queue.py) | 7 | 1.000 tick → 1 event (harga terakhir), cap 256 fill + pencacah drop, consumer memproses serial di dalam lock, task WS cuma publisher |
| [`test_executor_idempotency.py`](../tests/test_executor_idempotency.py) | 6 | order hilang → raises "uncertain" dengan nol retry buta, `429` dibaca `Retry-After`, token bucket 15/s membatasi 20 cancel |
| [`test_pair_rules.py`](../tests/test_pair_rules.py) | 6 | harga/qty dari metadata `/api/pairs`, `qty increment` truncation, order di bawah minimum / pair suspended ditolak tanpa panggilan API |
| [`test_paper_parity.py`](../tests/test_paper_parity.py) | 3 | paper mode = nol HTTP di semua fungsi gerbang; live masih kirim request bertanda tangan; 6 bypass risk paper lama benar-benar hilang |

## CI

[`.github/workflows/test.yml`](../.github/workflows/test.yml) — nama `tests`:

- trigger: `push` ke `main`, semua `pull_request`, dan `workflow_dispatch`;
- `concurrency` per ref dengan `cancel-in-progress`, `timeout-minutes: 20`;
- Python **3.12** (= `runtime.txt`), `pip install -r requirements.txt`;
- langkah "Fail if credentials are tracked": `.env` yang ter-commit = gagal;
- `python -m py_compile *.py engine/*.py tests/*.py` (paket `engine/` ikut);
- loop `tests/test_*.py` dengan `timeout 300` per suite, `status` diakumulasi
  supaya **suite yang gagal belakangan tetap jalan** (bukan fail-fast);
- `STATE_DIR` di-set **per step** (`runner.temp`) → test tidak pernah menulis
  `trades.db` di repository. Jangan pindahkan ini ke `env:` level job: context
  `runner` hanya tersedia di level step, dan GitHub menolak seluruh file kalau
  tidak (lihat blok di bawah).

**Status: sudah berjalan dan hijau.** Run pertama (`0598e06` dikirim ke GitHub
pada 27 Okt) mati dalam 0 detik dengan `workflow file issue` dan **0 job** —
`${{ runner.temp }}` dipasang di `env:` level job. Parser YAML biasa (termasuk
`yaml.safe_load`) menerima ini; hanya runner Actions yang menolak. Diperbaiki di
`198bb8b`, dan run berikutnya lolos: [run 40584759444](https://github.com/FMATheNomad/trading-agent/actions/runs/40584759444)
→ `suite in 1m13s`, 10/10 suite `PASS`, 82 baris `OK:` di log pada Python 3.12.

> Pelajaran yang lebih umum: **`hijau di laptop ≠ hijau di CI`, dan kegagalan
> CI bisa terjadi sebelum satu baris test pun jalan.** Karena itu: validasi
> perubahan workflow dengan `actionlint` (bukan cuma `yaml.safe_load`) sebelum
> push, dan perlakukan "workflow belum pernah punya run hijau" sebagai bug, bukan
> sebagai detail kecil. Perbedaan interpreter 3.12 vs 3.14 juga tetap perlu
> diwaspadai: di 3.14 kolom AST di dalam f-string dilaporkan berbeda, jadi alat
> refactor memberi hasil tak identik kalau versinya tertukar.

## Kalau menambah suite

1. Nama file `tests/test_<topik>.py` — CI mengambilnya otomatis dari glob.
2. Tidak ada `conftest`, tidak ada runner: path masuk sendiri via
   `sys.path.insert(0, ROOT)` lalu `assert` dan `print("Tn OK: ...")`.
3. Set `config.HAS_TRADING_KEYS = False` **sebelum** `import main`, supaya tidak
   ada jalur yang menyentuh TAPI.
4. Jangan menulis ke `trades.db` repo. Kalau menyentuh `db`, redirect
   `db.DB_PATH` ke `/tmp/...` seperti `test_golden.py:92`, atau andalkan
   `STATE_DIR` dari CI.
5. Cek statis yang menjangkau jalur keputusan harus memindai `main.py` **plus**
   `engine/*.py` — pola `decision_code()` di
   [`test_order_registry.py`](../tests/test_order_registry.py). Setelah #8,
   memindai `main.py` saja berarti bypass bisa dipindah ke `engine/cycle.py` dan
   CI diam.
6. Suite tidak boleh bergantung file di luar repo. Backup `/tmp` yang dipakai saat
   refactor #8 sudah dihapus dari test; verifikasi ekuivalensi AST cukup dicatat di
   pesan commit.
