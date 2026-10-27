# Arsip dokumen

Folder ini berisi dokumen yang **tidak lagi menggambarkan kode hari ini**. isinya
dibiarkan utuh (tidak disunting selain banner penanda di kepala tiap file) supaya
alasan keputusan lama masih bisa dibaca.

| File | Ditutup | Digantikan oleh | Yang berubah sejak dokumen itu ditulis |
|------|---------|-----------------|----------------------------------------|
| [`README.md`](README.md) | 2026-10-28 | [`../README.md`](../README.md), [`ARCHITECTURE.md`](ARCHITECTURE.md) | entrypoint `run_all.py`, `main.py` 789 baris, 10 suite test |
| [`ONBOARDING.md`](ONBOARDING.md) | 2026-10-28 | [`CONFIGURATION.md`](CONFIGURATION.md), [`ARCHITECTURE.md`](ARCHITECTURE.md) | `MIN_ORDER_IDR` 25.000, TAPI v2, `orders.registry`, `engine/state.py` |
| [`README-AI-version.md`](README-AI-version.md) | 2026-10-28 | [`ARCHITECTURE.md`](ARCHITECTURE.md) | fix list 1-8 belum ada saat ditulis |
| [`SCALING.md`](SCALING.md) | 2026-10-28 | [`KNOWN_ISSUES.md`](KNOWN_ISSUES.md) | pemecahan modul & kunci state sudah terjadi; rencana worker belum |
| [`SECURITY.md`](SECURITY.md) | 2026-10-28 | [`../SECURITY.md`](../SECURITY.md) | kredensial v2, deadman v1-only, konsekuensi rotasi key |

## Cara membaca arsip ini

Tiap file dimulai dengan blockquote `# ARSIP — ditutup <tanggal>` yang menyebut
apa yang sudah bergeser. Jangan pakai angkanya untuk mengambil keputusan; angka
yang benar ada di dokumen pengganti yang ditunjuk banner itu.

## Kalau perlu versi aslinya

Lima dokumen ini pernah hidup di root repo. Versi terakhir sebelum diarsipkan:

```
git log --follow --oneline -- README.md        # atau nama file lain
git show 37402e3:SECURITY.md                   # isi persis seperti saat itu
```
