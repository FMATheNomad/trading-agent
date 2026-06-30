import httpx
import config

TG_API = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"

async def send_message(text: str) -> bool:
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        print("Telegram: missing token or chat_id", flush=True)
        return False
    try:
        async with httpx.AsyncClient() as client:
            print(f"Telegram: sending to {config.TELEGRAM_CHAT_ID[:4]}...", flush=True)
            r = await client.post(
                f"{TG_API}/sendMessage",
                json={
                    "chat_id": config.TELEGRAM_CHAT_ID,
                    "text": text,
                    "parse_mode": "HTML",
                },
            )
            if r.status_code != 200:
                print(f"Telegram: HTTP {r.status_code} {r.text[:200]}", flush=True)
            return r.status_code == 200
    except Exception as e:
        print(f"Telegram error: {e}", flush=True)
        return False
