# Copyright (C) 2026 FMA ALPHA QUANT LABS
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# See the LICENSE file for more details.

from dotenv import load_dotenv
import os

load_dotenv()

PLAY_CAPITAL_IDR = int(os.getenv("PLAY_CAPITAL_IDR", "300000"))
MIN_ORDER_IDR = 10_000
DEFAULT_PLAY_CAPITAL_PCT = 0.8

PORTFOLIO_MODE = True
MAX_OPEN_POSITIONS = 4
def max_positions_for_equity(equity: float) -> int:
    if equity >= 10_000_000: return 6
    if equity >= 5_000_000: return 5
    return 4

MIN_24H_VOLUME_IDR = 100_000_000
MAX_SCAN_PAIRS = 40
OHLCV_FETCH_CONCURRENCY = 20
MAX_POSITION_PCT_PER_ASSET = 0.5
MAX_SECTOR_EXPOSURE_PCT = 0.7

KELLY_FRACTION = 0.25
MAX_KELLY_ALLOC = 0.95
MIN_KELLY_ALLOC = 0.3

ALPHA_MODE = os.getenv("ALPHA_MODE", "true").strip().lower() == "true"
INSANE_MODE = os.getenv("INSANE_MODE", "false").strip().lower() == "true"

POSITION_SIZE_PCT = 0.85
if INSANE_MODE:
    DAILY_LOSS_FLOOR_IDR = 0
    PORTFOLIO_STOP_LOSS_PCT = -0.50
    MAX_SCAN_PAIRS = 30
    MAX_OPEN_POSITIONS = 6
    MAX_POSITION_PCT_PER_ASSET = 0.55
    MAX_DAILY_TRADES = 999999
    KELLY_FRACTION = 0.75
elif ALPHA_MODE:
    DAILY_LOSS_FLOOR_IDR = 60_000
    PORTFOLIO_STOP_LOSS_PCT = -0.35
    MAX_DAILY_TRADES = 999999
else:
    DAILY_LOSS_FLOOR_IDR = 60_000
    PORTFOLIO_STOP_LOSS_PCT = -0.20
    MAX_DAILY_TRADES = 20
TAKER_FEE_PCT = 0.0035

FUNDAMENTAL_COINS: set[str] = set()
STABLECOINS = {"usdt_idr", "usdc_idr", "busd_idr", "dai_idr", "tusd_idr", "fdusd_idr"}
SKIP_COINS: set[str] = set()

PAIR = os.getenv("PAIR", "btc_idr")
SYMBOL = PAIR.replace("_", "").upper()

PAPER_TRADING = os.getenv("PAPER_TRADING", "true").strip().lower() == "true"

INDODAX_API_KEY = os.getenv("INDODAX_API_KEY", "")
INDODAX_SECRET_KEY = os.getenv("INDODAX_SECRET_KEY", "")
INDODAX_BASE_URL = "https://indodax.com"
INDODAX_TAPI_URL = f"{INDODAX_BASE_URL}/tapi"
INDODAX_TAPI_V2_URL = "https://tapi.indodax.com"

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
DEEPSEEK_THINKING_MODE = False

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

LOOP_INTERVAL_SECONDS = 60
DEADMAN_COUNTDOWN_MS = 900_000
HIGH_VOL_THRESHOLD = 3.5

MOMENTUM_SCAN_INTERVAL = 15
HMM_N_STATES = 4
HMM_RETRAIN_INTERVAL = 50

ATR_TP_MULTIPLIER = 0.8
ATR_SL_MULTIPLIER = 0.5
ATR_MIN_MOVE_MULTIPLIER = 0.0
MAX_ATR_PCT = 100.0
ATR_PROFIT_SELL_MULT = 0.6
ATR_STAGNANT_MULT = 0.3
ATR_CUT_MULT = 0.5
if INSANE_MODE:
    ATR_PROFIT_SELL_MULT = 0.5
    ATR_STAGNANT_MULT = 0.2
    ATR_CUT_MULT = 0.5

ROTHSCHILD_INITIAL_SL_ATR = 0.3
ROTHSCHILD_TRAILING_SL_ATR = 1.5
ROTHSCHILD_PYRAMID_TRIGGER = 0.5
ROTHSCHILD_PYRAMID_MULT = 0.5
ROTHSCHILD_LIMIT_GRACE_SEC = 15
ROTHSCHILD_ACTIVE = False

MAKER_FIRST = os.getenv("MAKER_FIRST", "false").strip().lower() == "true"
MAKER_SLIPPAGE = 0.001

_MAX_DAILY_TRADES_ENV = os.getenv("MAX_DAILY_TRADES")
if _MAX_DAILY_TRADES_ENV is not None:
    MAX_DAILY_TRADES = int(_MAX_DAILY_TRADES_ENV)

AUTO_COMPOUND = True
CAPITAL_GROWTH_MULTIPLIER = 1.0
COMPOUND_CAP_IDR = 5_000_000

WS_MARKET_URL = "wss://ws3.indodax.com/ws/"
WS_MARKET_TOKEN = os.getenv("WS_MARKET_TOKEN", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJleHAiOjE5NDY2MTg0MTV9.UR1lBM6Eqh0yWz-PVirw1uPCxe60FdchR8eNVdsskeo")
WS_PRIVATE_URL = "wss://pws.indodax.com/ws/?cf_ws_frame_ping_pong=true"
