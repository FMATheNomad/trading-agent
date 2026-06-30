from dotenv import load_dotenv
import os

load_dotenv()

# --- Modal & Risk ---
INITIAL_CAPITAL_IDR = 100_000
MAX_OPEN_POSITIONS = 1
POSITION_SIZE_PCT = 0.85
STOP_LOSS_PCT = -0.03
TAKE_PROFIT_PCT = 0.03
DAILY_LOSS_FLOOR_IDR = 80_000
TAKER_FEE_PCT = 0.003

# --- Pair ---
PAIR = os.getenv("PAIR", "btc_idr")
SYMBOL = PAIR.replace("_", "").upper()

# --- Mode ---
PAPER_TRADING = os.getenv("PAPER_TRADING", "true").strip().lower() == "true"

# --- Indodax API ---
INDODAX_API_KEY = os.getenv("INDODAX_API_KEY", "")
INDODAX_SECRET_KEY = os.getenv("INDODAX_SECRET_KEY", "")
INDODAX_BASE_URL = "https://indodax.com"
INDODAX_TAPI_URL = f"{INDODAX_BASE_URL}/tapi"
INDODAX_TAPI_V2_URL = "https://tapi.indodax.com"

# --- DeepSeek API ---
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
DEEPSEEK_THINKING_MODE = False

# --- Telegram ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# --- Scheduler ---
LOOP_INTERVAL_SECONDS = 120
