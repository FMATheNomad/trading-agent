# AI Trading Agent — Indodax

Bot trading otomatis untuk Indodax dengan modal Rp100.000,
menggabungkan indikator teknikal (RSI, MACD, EMA, Bollinger Bands)
dengan filter keputusan DeepSeek V4 Flash.

## Setup

1. **Clone & install**
   ```
   pip install -r requirements.txt
   ```

2. **Buat API Key**
   - Indodax: https://indodax.com/trade_api — pilih permission **trade-only**
   - DeepSeek: https://platform.deepseek.com/api_keys
   - Telegram: chat @BotFather, buat bot, dapatkan token

3. **Isi `.env`** — salin dari template:
   ```
   cp .env .env.local   # lalu edit
   ```

4. **Isi minimal `.env`:**
   ```
   INDODAX_API_KEY=...
   INDODAX_SECRET_KEY=...
   DEEPSEEK_API_KEY=...
   TELEGRAM_BOT_TOKEN=...
   TELEGRAM_CHAT_ID=...
   PAPER_TRADING=true
   ```

5. **Jalankan paper trading dulu**
   ```
   python main.py
   ```

## Testing

Sebelum beralih ke live (`PAPER_TRADING=false`):
1. Jalankan backtest: `python backtest.py --days 30`
2. Jalankan paper trading minimal 3-7 hari
3. Bandingkan logika dengan hasil aktual

## Struktur

| File | Fungsi |
|------|--------|
| `main.py` | Orchestrator loop utama |
| `data_layer.py` | Fetch harga, orderbook, OHLCV dari Indodax |
| `indicators.py` | Hitung RSI/MACD/EMA/BB → raw signal |
| `llm_filter.py` | Kirim konteks ke DeepSeek → keputusan final |
| `risk_manager.py` | SL/TP, position sizing, daily loss limit |
| `executor.py` | Signing HMAC-SHA512, kirim order ke Indodax |
| `deadman.py` | Deadman Switch (cancel semua order jika bot mati) |
| `notifier.py` | Kirim notifikasi Telegram |
| `db.py` | SQLite — log trade & keputusan |
| `backtest.py` | Replay data historis untuk evaluasi strategi |
| `config.py` | Semua parameter dari `.env` + default |

## Catatan

- Modal Rp100.000 adalah **testing infrastructure**, bukan profit.
- Pastikan `PAPER_TRADING=true` sebelum live.
- API key hanya perlu permission **trade** — jangan pernah kasih permission withdraw.
