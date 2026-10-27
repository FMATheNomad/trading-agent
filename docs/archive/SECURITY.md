> # ARSIP — ditutup 2026-10-28
>
> Dokumen ini menjelaskan project **sebelum** migrasi TAPI v2, registry order tunggal,
> antrean event WS, dan pemecahan `main.py` menjadi paket `engine/`. Disimpan apa adanya
> supaya keputusan lama tetap terbaca; **bukan sumber kebenaran** untuk kode hari ini.
>
> Dibuat sebelum migrasi TAPI v2. Tidak menyebut `INDODAX_V2_API_KEY`/`INDODAX_V2_SECRET_KEY`,
> keharusan kunci v1 untuk deadman + private-WS token, dan konsekuensi rotasi key (penarikan
> API terkunci 24 jam). Isinya digantikan [`../SECURITY.md`](../SECURITY.md).
>
> Sumber kebenaran sekarang: [`../../README.md`](../../README.md) dan [`../`](../).

---
# Security Policy

## 🔐 Reporting a Vulnerability

This project is an open-source crypto trading bot. If you discover a security vulnerability, please report it privately.

**Do not** report security issues via public GitHub issues. Instead, contact the maintainer directly.

### Contact

- **Email:** [fmasoftwarelabs@gmail.com](mailto:security@fmaquantlabs.com) *(placeholder)*
- **GitHub Issues:** For non-security bugs and feature requests

### What to Include

- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

## 🔑 API Key Safety

This bot requires Indodax API keys with `trade` permission. Store them **only** in environment variables or Railway secrets. Never commit API keys to the repository.

The `.env` file is gitignored — do not remove it from `.gitignore`.

## 🛡️ Security Best Practices

1. Use a **dedicated Indodax account** with limited funds for trading
2. Set API key permissions to **minimum required** (`trade` + `view`, NOT `withdraw`)
3. Regularly rotate API keys
4. Monitor bot activity via Telegram notifications
5. The deadman switch (`/countdownCancelAll`) will cancel all open orders if the bot stops

## 📜 License

AGPL-3.0 — see `LICENSE` for details.
