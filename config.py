from dotenv import load_dotenv
import os

load_dotenv()

# --- Dana bot — fixed Rp100rb, tidak peduli berapapun deposit user ---
PLAY_CAPITAL_IDR = 100_000
MIN_ORDER_IDR = 25_000
DEFAULT_PLAY_CAPITAL_PCT = 0.5

# --- Portfolio ---
PORTFOLIO_MODE = True
MAX_OPEN_POSITIONS = 2
MIN_24H_VOLUME_IDR = 100_000_000
MAX_SCAN_PAIRS = 40
OHLCV_FETCH_CONCURRENCY = 10
MAX_POSITION_PCT_PER_ASSET = 0.9
MAX_SECTOR_EXPOSURE_PCT = 0.6

# --- Mode Profile ---
ALPHA_MODE = os.getenv("ALPHA_MODE", "false").strip().lower() == "true"

# --- Risk per trade ---
POSITION_SIZE_PCT = 0.85
if ALPHA_MODE:
    STOP_LOSS_PCT = -0.10
    TAKE_PROFIT_PCT = 0.20
    DAILY_LOSS_FLOOR_IDR = 40_000
    PORTFOLIO_STOP_LOSS_PCT = -0.30
else:
    STOP_LOSS_PCT = -0.05
    TAKE_PROFIT_PCT = 0.05
    DAILY_LOSS_FLOOR_IDR = 60_000
    PORTFOLIO_STOP_LOSS_PCT = -0.20
TAKER_FEE_PCT = 0.0035

# --- Hanya koin fundamental — tidak ada meme/shitcoin ---
FUNDAMENTAL_COINS: set[str] = set()
STABLECOINS = {"usdt_idr", "usdc_idr", "busd_idr", "dai_idr", "tusd_idr", "fdusd_idr"}

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
LOOP_INTERVAL_SECONDS = 300
DEADMAN_COUNTDOWN_MS = 900_000
HIGH_VOL_THRESHOLD = 3.5

# --- Senior Quant Settings ---
REGIME_LOOKBACK_CYCLES = 12
CORRELATION_PAIRS = [
    ("btc_idr", "eth_idr"),
    ("sol_idr", "ada_idr"),
    ("bnb_idr", "xrp_idr"),
]

# --- WebSocket ---
WS_MARKET_URL = "wss://ws3.indodax.com/ws/"
WS_MARKET_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJleHAiOjE5NDY2MTg0MTV9.UR1lBM6Eqh0yWz-PVirw1uPCxe60FdchR8eNVdsskeo"
WS_PRIVATE_URL = "wss://pws.indodax.com/ws/?cf_ws_frame_ping_pong=true"
