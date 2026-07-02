from dotenv import load_dotenv
import os

load_dotenv()

# --- Dana bot — Rp200rb target -> Rp500rb dalam beberapa hari ---
PLAY_CAPITAL_IDR = 300_000
MIN_ORDER_IDR = 10_000
DEFAULT_PLAY_CAPITAL_PCT = 0.8

# --- Portfolio ---
PORTFOLIO_MODE = True
MAX_OPEN_POSITIONS = 4
def max_positions_for_equity(equity: float) -> int:
    if equity >= 10_000_000:
        return 6
    if equity >= 5_000_000:
        return 5
    return 4

MIN_24H_VOLUME_IDR = 100_000_000
MAX_SCAN_PAIRS = 40
OHLCV_FETCH_CONCURRENCY = 10
MAX_POSITION_PCT_PER_ASSET = 0.95
MAX_SECTOR_EXPOSURE_PCT = 0.7

# --- Mode Profile ---
ALPHA_MODE = os.getenv("ALPHA_MODE", "true").strip().lower() == "true"
INSANE_MODE = os.getenv("INSANE_MODE", "false").strip().lower() == "true"

# --- Risk per trade ---
POSITION_SIZE_PCT = 0.85
if INSANE_MODE:
    STOP_LOSS_PCT = -0.12
    TAKE_PROFIT_PCT = 0.35
    DAILY_LOSS_FLOOR_IDR = 0
    PORTFOLIO_STOP_LOSS_PCT = -0.50
    MIN_24H_VOLUME_IDR = 100_000_000
    MAX_SCAN_PAIRS = 50
    MAX_OPEN_POSITIONS = 6
    POSITION_SIZE_PCT = 0.95
    MAX_POSITION_PCT_PER_ASSET = 1.0
    MAX_DAILY_TRADES = 20
    KELLY_FRACTION = 0.5
    PROFIT_SELL_THRESHOLD = -99
elif ALPHA_MODE:
    STOP_LOSS_PCT = -0.08
    TAKE_PROFIT_PCT = 0.25
    DAILY_LOSS_FLOOR_IDR = 60_000
    PORTFOLIO_STOP_LOSS_PCT = -0.35
    MAX_DAILY_TRADES = 10
    PROFIT_SELL_THRESHOLD = 2.0
else:
    STOP_LOSS_PCT = -0.05
    TAKE_PROFIT_PCT = 0.05
    DAILY_LOSS_FLOOR_IDR = 60_000
    PORTFOLIO_STOP_LOSS_PCT = -0.20
    MAX_DAILY_TRADES = 10
    PROFIT_SELL_THRESHOLD = 2.0
TAKER_FEE_PCT = 0.0035

# --- Hanya koin fundamental — tidak ada meme/shitcoin ---
FUNDAMENTAL_COINS: set[str] = set()
STABLECOINS = {"usdt_idr", "usdc_idr", "busd_idr", "dai_idr", "tusd_idr", "fdusd_idr"}
SKIP_COINS: set[str] = set()

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

# --- HMM Regime Detection ---
HMM_N_STATES = 4
HMM_RETRAIN_INTERVAL = 20

# --- Cointegration Engine ---
COINT_Z_ENTRY = 2.0
COINT_Z_EXIT = 0.5
COINT_MIN_HALF_LIFE_HOURS = 2
CORRELATION_PAIRS = [
    ("btc_idr", "eth_idr"),
    ("sol_idr", "ada_idr"),
    ("bnb_idr", "xrp_idr"),
]

# --- ML Signal ---
ML_TRAIN_MIN_SAMPLES = 100
ML_FORECAST_HORIZON = 5
ML_BUY_THRESHOLD = 0.65

# --- Kelly Criterion ---
KELLY_FRACTION = 0.25
MAX_KELLY_ALLOC = 0.95
MIN_KELLY_ALLOC = 0.3

# --- ATR-Based TP ---
ATR_TP_MULTIPLIER = 3.0
ATR_SL_MULTIPLIER = 2.0
ATR_MIN_MOVE_MULTIPLIER = 0.75
MAX_ATR_PCT = 10.0

# --- Partial TP (Scaling Out) ---
PARTIAL_TP_ENABLED = True
PARTIAL_TP_FIRST_PCT = 0.5
PARTIAL_TP_FIRST_MULTIPLIER = 2.5
PARTIAL_TP_RUNNER_TRAIL_MULTIPLIER = 0.5

# --- Maker-First (save fees: limit → market fallback) ---
MAKER_FIRST = os.getenv("MAKER_FIRST", "false").strip().lower() == "true"
MAKER_SLIPPAGE = 0.001

# --- Max trades/day (bisa di-override via env) ---
_MAX_DAILY_TRADES_ENV = os.getenv("MAX_DAILY_TRADES")
if _MAX_DAILY_TRADES_ENV is not None:
    MAX_DAILY_TRADES = int(_MAX_DAILY_TRADES_ENV)

# --- Auto Compound ---
AUTO_COMPOUND = True
CAPITAL_GROWTH_MULTIPLIER = 1.0

# --- WebSocket ---
WS_MARKET_URL = "wss://ws3.indodax.com/ws/"
WS_MARKET_TOKEN = os.getenv("WS_MARKET_TOKEN", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJleHAiOjE5NDY2MTg0MTV9.UR1lBM6Eqh0yWz-PVirw1uPCxe60FdchR8eNVdsskeo")
WS_PRIVATE_URL = "wss://pws.indodax.com/ws/?cf_ws_frame_ping_pong=true"
