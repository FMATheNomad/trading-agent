# Keamanan

## Yang boleh dilakukan bot ini dengan kunci API

Hanya tiga hal, semuanya lewat [`executor.py`](../executor.py): pasang order,
batalkan order, baca (akun, open orders, trades). Tidak ada satu pun jalur
withdrawal/deposit di kode — `can_withdraw` hanya **dibaca** dari respons akun
sebagai info ([`executor.py:420`](../executor.py#L420)), tidak pernah dipanggil.
Kunci dengan scope "read + spot trading" sudah cukup; jangan kasih izin penarikan.

Key yang dipakai saat migrasi v2 dibuat dengan izin read-only + spot trading.
Kalau izin itu diubah, semua order gagal dengan `{"code": -N, "msg": ...}`.

## Kredensial: di mana boleh dan tidak boleh ada

| Boleh | Tidak boleh |
|-------|-------------|
| `.env` di disk lokal / Railway Variables | `.env` di-commit (CI gagal kalau ini terjadi) |
| `INDODAX_V2_API_KEY` dll. sebagai env var runtime | nilai kredensial di source code |
| output `/status` berupa angka equity & posisi | screenshot log yang mengandung header `X-APIKEY` |

`.gitignore` menutup `.env`, `*.db`, `*.json`, `logs/`, `.venv/` — jadi file state
(`state.json`, `trades.db`) juga tidak pernah masuk repo. Langkah CI
"Fail if credentials are tracked" menjalankan
`git ls-files --error-unmatch .env` dan menggagalkan build kalau keesokan ada yang
menambah `.env` dengan `git add -f`.

**Cara kerja yang diterima untuk credential:** tulis/diisi tanpa pernah ditampilkan.
Kunci tidak pernah dicetak oleh kode (tidak ada satu pun `print(headers)`), dan
`Sign`/`X-APIKEY` tidak pernah masuk log.

## Dua versi API, dua kunci

| | v2 (default) | v1 legacy |
|---|---|---|
| Base | `https://api.indodax.com` | `https://indodax.com/tapi` |
| Header key | `X-APIKEY` | `Key` |
| Signature | HMAC-**SHA256** atas query/body | HMAC-**SHA512** atas body |
| Env | `INDODAX_V2_API_KEY` / `INDODAX_V2_SECRET_KEY` | `INDODAX_API_KEY` / `INDODAX_SECRET_KEY` |
| Dipakai untuk | order, openOrders, account | **deadman** + token private-WS |

`TAPI_VERSION=auto` memilih v2 kalau kedua kunci v2 ada; `v1` memaksa legacy.
Perhatikan: **dua permukaan tidak punya padanan v2**, jadi kunci v1 masih wajib
kalau kamu mau tetap terlindungi:

- `POST https://indodax.com/tapi/countdownCancelAll` — deadman switch. Tanpa
  perpanjangan tiap cycle, exchange membatalkan semua order dalam 15 menit
  (`DEADMAN_COUNTDOWN_MS`). Ini jaring pengaman saat proses hilang.
- `POST https://indodax.com/api/private_ws/v1/generate_token` — token untuk WS
  private (push fill).

Kalau kunci v1 dihapus, deadman diam-diam berhenti melindungi order terbuka.
`main.py` mencetak peringatan saat boot —
`WARN: no legacy key — deadman switch is OFF` ([`main.py:347`](../main.py#L347)) —
dan itu jangan dihapus dari kode.

## Rotasi kunci

Regenerasi kunci Indodax **mengunci penarikan API selama 24 jam**. Kunci v2 yang
pernah tertempel di percakapan (28 Okt 2026) sebaiknya dirotasi saat kamu siap;
urutan yang aman:

1. buat kunci baru, salin ke `.env` (atau Railway Variables) **tanpa** menempelnya
   ke percakapan/screenshot;
2. restart worker, pastikan boot tidak `WARN` dan `/status` menampilkan saldo;
3. baru hapus kunci lama di dashboard Indodax.

Kunci lama tetap hidup sampai dihapus — menimpanya di `.env` saja tidak
mencabut aksesnya.

## Token market WS di repo

[`config.py:160`](../config.py#L160) menyimpan default `WS_MARKET_TOKEN` berupa JWT
yang diterbitkan Indodax untuk websocket publik. Ini **bukan kredensial pengguna**:
dia memberi akses data pasar, bukan saldo atau order, dan tidak bisa dipakai
mengeksekusi apa pun. Tetap ada jalan override lewat env kalau Indodax
menggantinya dan WS mulai menolak koneksi.

## Kalau pakai `.venv` lokal

Dependensi di `requirements.txt` tidak dipin per versi (semua `>=`), jadi build
hari esok bisa berbeda. Untuk lingkungan live, pertimbangkan lockfile
(`pip freeze > constraints.txt`) supaya perubahan paket disengaja, bukan kejutan.
Empat paket di `requirements.txt` tidak diimpor kode mana pun
(`xgboost`, `scikit-learn`, `statsmodels`, `sentry-sdk`) — termasuk `sentry-sdk`,
yang berarti **crash tidak dilaporkan ke Sentry walau `SENTRY_DSN` tersedia**;
dia hanya masuk log worker.

## Laporan kerentanan

Untuk proyek AGPL ini: buka issue privat dulu kalau bug bisa merugikan dana
pengguna (mis. order ganda, pembatalan TP/SL yang salah). Jangan publikasikan
repro yang melibatkan kunci atau `chat_id` Telegram.
