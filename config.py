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
MIN_ORDER_IDR = 15_000
DEFAULT_PLAY_CAPITAL_PCT = 0.5

PORTFOLIO_MODE = True
MAX_OPEN_POSITIONS = 3
def max_positions_for_equity(equity: float) -> int:
    if equity >= 10_000_000: return 3
    if equity >= 5_000_000: return 2
    if equity >= 1_000_000: return 2
    return 1

MIN_24H_VOLUME_IDR = 50_000_000
MAX_SCAN_PAIRS = 60
OHLCV_FETCH_CONCURRENCY = 15
MAX_POSITION_PCT_PER_ASSET = 0.35
MAX_SECTOR_EXPOSURE_PCT = 0.5

KELLY_FRACTION = 0.15
MAX_KELLY_ALLOC = 0.6
MIN_KELLY_ALLOC = 0.2

ALPHA_MODE = os.getenv("ALPHA_MODE", "true").strip().lower() == "true"
INSANE_MODE = os.getenv("INSANE_MODE", "false").strip().lower() == "true"

POSITION_SIZE_PCT = 0.50
if INSANE_MODE:
    DAILY_LOSS_FLOOR_IDR = 0
    PORTFOLIO_STOP_LOSS_PCT = -0.50
    MAX_SCAN_PAIRS = 50
    MAX_OPEN_POSITIONS = 6
    MAX_POSITION_PCT_PER_ASSET = 0.55
    MAX_DAILY_TRADES = 999999
    KELLY_FRACTION = 0.75
elif ALPHA_MODE:
    DAILY_LOSS_FLOOR_IDR = 15_000
    PORTFOLIO_STOP_LOSS_PCT = -0.15
    MAX_DAILY_TRADES = 5
    MAX_OPEN_POSITIONS = 1
    MAX_POSITION_PCT_PER_ASSET = 0.25
    KELLY_FRACTION = 0.10
    ATR_TP_MULTIPLIER = 1.5
    ATR_SL_MULTIPLIER = 2.0
    ATR_PROFIT_SELL_MULT = 1.2
    ATR_CUT_MULT = 2.0
    ATR_STAGNANT_MULT = 1.2
else:
    DAILY_LOSS_FLOOR_IDR = 15_000
    PORTFOLIO_STOP_LOSS_PCT = -0.10
    MAX_DAILY_TRADES = 3
    MAX_OPEN_POSITIONS = 1
TAKER_FEE_PCT = 0.0035
MAKER_FEE_PCT = 0.0020
FEE_CLEARANCE_RATIO = 2.5

FUNDAMENTAL_COINS: set[str] = {
    "btc_idr", "eth_idr", "sol_idr", "xrp_idr", "ada_idr",
    "doge_idr", "avax_idr", "dot_idr", "link_idr", "bnb_idr",
    "trx_idr", "bch_idr", "shib_idr", "near_idr", "ltc_idr",
    "xlm_idr", "sui_idr", "pepe_idr", "uni_idr", "aave_idr",
    "atom_idr", "algo_idr", "fil_idr", "icp_idr", "xtz_idr",
    "arb_idr", "op_idr", "inj_idr", "grt_idr", "sand_idr",
    "mana_idr", "crv_idr", "fet_idr", "etc_idr", "hbar_idr",
    "vet_idr", "theta_idr", "iota_idr", "ksm_idr", "yfi_idr",
    "axs_idr", "cake_idr", "enj_idr", "celo_idr", "imx_idr",
    "pendle_idr", "jup_idr", "ondo_idr", "ldo_idr", "bonk_idr",
    "wif_idr", "mnt_idr", "trump_idr", "hype_idr", "render_idr",
    "strk_idr", "tia_idr",
}
RECOVERY_TOP: set[str] = {
    "btc_idr", "eth_idr", "sol_idr", "xrp_idr", "bnb_idr",
    "ada_idr", "doge_idr", "avax_idr", "dot_idr", "link_idr",
    "sui_idr", "near_idr", "trx_idr",
}
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

ATR_TP_MULTIPLIER = 2.0
ATR_SL_MULTIPLIER = 1.5
ATR_MIN_MOVE_MULTIPLIER = 0.5
MAX_ATR_PCT = 100.0
ATR_PROFIT_SELL_MULT = 1.5
ATR_STAGNANT_MULT = 0.8
ATR_CUT_MULT = 1.5
if INSANE_MODE:
    ATR_PROFIT_SELL_MULT = 1.0
    ATR_STAGNANT_MULT = 0.5
    ATR_CUT_MULT = 1.0

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

AUTO_COMPOUND = False
CAPITAL_GROWTH_MULTIPLIER = 1.0
COMPOUND_CAP_IDR = 1_000_000

WS_MARKET_URL = "wss://ws3.indodax.com/ws/"
WS_MARKET_TOKEN = os.getenv("WS_MARKET_TOKEN", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJleHAiOjE5NDY2MTg0MTV9.UR1lBM6Eqh0yWz-PVirw1uPCxe60FdchR8eNVdsskeo")
WS_PRIVATE_URL = "wss://pws.indodax.com/ws/?cf_ws_frame_ping_pong=true"
