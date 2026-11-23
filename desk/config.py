"""All rule numbers in one place. PAPER ONLY."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DESK = DATA / "desk"
STATE_FILE = DESK / "state.json"
DECISIONS_LOG = DESK / "decisions.jsonl"
DB_FILE = DESK / "desk.sqlite"
KILL_FILE = DATA / "KILL"
LIVE_FILE = ROOT / "site" / "live.json"   # "now doing" feed for the phone page (display only)

TZ = "Pacific/Honolulu"            # day boundary for the daily loss limit

# --- money -------------------------------------------------------------------
START_CASH = 100.0                  # paper capital (USD)
SMALL_MODE = True                   # $5-10 per trade
SMALL_MIN, SMALL_MAX = 5.0, 10.0
MAX_POSITION_PCT = 0.10             # design said up to 40%; capped at 10% (gap losses)
MAX_OPEN_POSITIONS = 5
DAILY_LOSS_LIMIT = 10.0             # no new buys for the rest of the Hawaii day
TOTAL_LOSS_LIMIT = 30.0             # full stop
WORST_CASE_GAP = 0.50               # assume a position can gap down 50% past the stop

# --- execution model (paper) ---------------------------------------------------
MAX_SLIPPAGE = 0.02                 # 2%, estimated with x*y=k
FEE_PCT = 0.01                      # assume ~1% total fees per side (pool + priority)

# --- exits --------------------------------------------------------------------
# Live exit profile "E: hybrid ladder" (chosen 2026-11-23 after the bot survey + simulation):
#   before +50%: stop -30%; once it has been +25%, sell everything on a 12% pullback (locks small wins)
#   +50%: sell 1/3, the rest can no longer lose (floor = entry +2%); from +80% trail 25% from the peak
#   +150%: sell another 1/3
#   last 1/3 = moonbag: no ceiling, sold on a 35% drop from its peak (or after 24h)
EXIT_PROFILE = {"sl": -0.30, "arm0": 0.25, "trail0": 0.12,
                "tps": [(0.5, 1 / 3), (1.5, 1 / 3)], "be": 0.02, "arm1": 0.8, "trail1": 0.25,
                "arm": 1.5, "trail": 0.35, "stale_min": None, "max_min": 1440}
# legacy single-rule exits (profile A, kept for comparison only)
STOP_LOSS = -0.06
TAKE_PROFIT_1 = 0.25                # sell half at +25%
TRAIL_ARM = 0.15                    # trailing stop arms at +15%
TRAIL_DIST = 0.12                   # ...and sells if price falls 12% from the peak
MAX_HOLD_MIN = 240                  # time stop

# --- rug rules (design doc) ---------------------------------------------------
MIN_LIQ_USD = 10_000                # was 30k: only 10% of graduates passed (data 2026-11-23)
MIN_AGE_AFTER_GRAD_MIN = 20
MAX_AGE_AFTER_GRAD_MIN = 180        # stop considering very old graduations
MAX_DEV_PCT = 0.05
MAX_TOP10_PCT = 0.30                # excluding pools / program-owned accounts
MIN_LP_LOCKED_PCT = 0.95
# radar-based rules (new)
MAX_BLOCK0_BUNDLERS = 2             # 3+ wallets bought in the creation block -> fail
MAX_BOT_HOLD_PCT = 0.05             # known sniper bots holding 5%+ of supply -> fail

# --- holder-structure rules from commercial terminals (Axiom / GMGN / Jupiter / Trojan) and
#     research write-ups (Mobula sniper/bundler guide, G-BOT), surveyed 2026-11-23
MIN_HOLDERS = 75                    # Axiom "degen" filter: holders >= 75
MAX_SNIPER_HOLD_PCT = 0.20          # supply still held by wallets that bought in blocks 1-3 (red flag > 20%)
MAX_BUNDLE_HOLD_PCT = 0.10          # supply still held by block-0 (bundle) wallets (avoid > 10%)
MAX_INSIDER_PCT = 0.20              # RugCheck-flagged insider wallets in the top holders
SERIAL_MIN_PREV = 3                 # creator launched 3+ earlier coins ...
SERIAL_DEAD_RATIO = 0.67            # ... and 67%+ of them are dead -> serial rugger
DEAD_MCAP_USD = 5_000               # an earlier coin under $5k market cap counts as dead

FAMOUS_TICKERS = {"SOL", "BONK", "WIF", "POPCAT", "TRUMP", "MELANIA", "PENGU", "JUP", "PUMP",
                  "FARTCOIN", "MOODENG", "GOAT", "PNUT", "AI16Z", "BOME", "MEW", "USDC", "USDT",
                  "ANSEM", "USELESS", "SPX", "GIGA", "MICHI", "RETARDIO", "CHILLGUY"}

# --- entry (Sniper) -------------------------------------------------------------
MIN_ENTRY_SCORE = 60

# --- loop -------------------------------------------------------------------------
import os
TICK_SEC = float(os.environ.get("DESK_TICK") or 45)   # live server: 10
GAP_WARN_SEC = 120
MAX_EVALS_PER_CYCLE = 3             # full (credit-using) evaluations per cycle                  # a pause longer than this is logged as a gap
