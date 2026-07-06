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
