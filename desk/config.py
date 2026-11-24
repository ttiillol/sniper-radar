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
START_CASH = 1000.0                 # paper capital (USD) - new book from 2026-11-23 (owner's decision)
BOOK = "scalp-1000-2026-11-23"      # changing this starts a fresh paper book (old one is archived)
SMALL_MODE = False                  # was True ($5-10 per trade) for the $100 book
SMALL_MIN, SMALL_MAX = 5.0, 10.0
MAX_POSITION_PCT = 0.10             # normal trade: 10% of the book
# --- conviction sizing (owner: "big only on perfect momentum, otherwise 10-20%")
SIZE_STRONG_PCT = 0.20              # score >= STRONG_SCORE and buy pressure >= STRONG_PRESSURE
STRONG_SCORE = 80
STRONG_PRESSURE = 0.60
SIZE_MAX_PCT = 0.50                 # "perfect momentum": every item of the checklist below
PERFECT_M5 = (3.0, 15.0)            # +3..15% in 5 minutes
PERFECT_H1 = 20.0                   # +20% or more in 1 hour
PERFECT_PRESSURE = 0.65             # buyers' share of USD volume (10 min)
PERFECT_WHALES = 3                  # 3+ whales within 2 minutes, OR a tier-A KOL buy
PERFECT_MIN_LIQ = 150_000
PERFECT_MIN_HOLDERS = 300
MAX_ENTRY_IMPACT = 0.01             # never buy so much that our own buy moves the price more than 1%
WORST_CASE_GAP_DEEP = 0.15          # pools with $150k+ liquidity: assume a 15% gap past the stop (else 25%)
MAX_OPEN_POSITIONS = 5
DAILY_LOSS_LIMIT = 100.0            # 10% of the book: no new buys for the rest of the Hawaii day
TOTAL_LOSS_LIMIT = 300.0            # 30% of the book: full stop
WORST_CASE_GAP = 0.25               # scalp (stop -6%): assume a position can still gap down 25% (was 50%)

# --- execution model (paper) ---------------------------------------------------
MAX_SLIPPAGE = 0.02                 # 2%, estimated with x*y=k
FEE_PCT = 0.01                      # assume ~1% total fees per side (pool + priority)

# --- exits --------------------------------------------------------------------
# Live exit profile "E: hybrid ladder" (chosen 2026-11-23 after the bot survey + simulation):
#   before +50%: stop -30%; once it has been +25%, sell everything on a 12% pullback (locks small wins)
#   +50%: sell 1/3, the rest can no longer lose (floor = entry +2%); from +80% trail 25% from the peak
#   +150%: sell another 1/3
#   last 1/3 = moonbag: no ceiling, sold on a 35% drop from its peak (or after 24h)
EXIT_PROFILE_E = {"sl": -0.30, "arm0": 0.25, "trail0": 0.12,
                  "tps": [(0.5, 1 / 3), (1.5, 1 / 3)], "be": 0.02, "arm1": 0.8, "trail1": 0.25,
                  "arm": 1.5, "trail": 0.35, "stale_min": None, "max_min": 1440}

# --- STRATEGY (2026-11-23, owner's decision): momentum scalping replaces the graduation sniper.
# Trade any Solana memecoin with strong momentum (not only fresh pump.fun graduates), take small
# wins quickly and move on. Rug checks still apply. Costs (~3% round trip) are always deducted.
STRATEGY = "scalp"
# Live exit profile "S: momentum scalp"
#   stop -6% / once +6% was reached, sell everything on a 4% pullback from the peak (locks small wins)
#   +10%: sell half; the rest can no longer lose (floor = entry +1%); from +15% trail 6% from the peak
#   below entry after 20 minutes -> out; never hold longer than 45 minutes
EXIT_PROFILE_S = {"sl": -0.06, "arm0": 0.06, "trail0": 0.04, "tps": [(0.10, 0.5)], "be": 0.01,
                  "arm": 0.15, "trail": 0.06, "stale_min": 20, "max_min": 45}
# Live exit profile "R: runner" (2026-11-23 evening, owner: "let winners run"). Day-1 data with S:
# 15 trades, win 20%, PF 0.21; 8 of 12 losses were stopped within 0-3 minutes by normal noise.
#   stop -12% / from +15%: sell all on a 10% pullback (keeps small wins)
#   +25%: sell 1/3, the rest can't lose (floor entry +2%) / from +40%: trail 20% from the peak
#   +100%: sell another 1/3 / last third: trail 30% from the peak (room for big runs)
#   below entry after 60 minutes -> out; never hold longer than 6 hours
EXIT_PROFILE = {"sl": -0.12, "arm0": 0.15, "trail0": 0.10, "tps": [(0.25, 1 / 3), (1.0, 1 / 3)], "be": 0.02,
                "arm1": 0.40, "trail1": 0.20, "arm": 1.0, "trail": 0.30, "stale_min": 60, "max_min": 360}
SCALP_MIN_M5 = 1.0                  # price change over the last 5 minutes must be at least +1%...
SCALP_MAX_M5 = 8.0                  # ...but not a spike (no chasing; was 25%, two fast stop-outs on day 1)
SCALP_MIN_H1 = 0.0                  # and the last hour must be up (trend)
SCALP_MIN_PRESSURE = 0.55           # buyers' share of USD volume in the last 10 minutes
SCALP_MIN_VOL_M5 = 3_000            # USD traded in the last 5 minutes (activity)
SCALP_RECHECK_SEC = 20              # weak momentum: look again after 20 seconds...
SCALP_MAX_CHECKS = 20               # ...up to 20 times (~7 min), then wait for the coin to trend again
SCALP_RUG_CACHE_SEC = 1800          # a passed rug check is reused for 30 minutes
SCALP_COOLDOWN_SEC = 300            # after selling a coin, wait 5 minutes before trading it again
SCALP_MAX_TRADES_PER_COIN = 3       # per coin per Hawaii day
SCALP_RECHECK_REJECTED_MIN = 15     # a coin rejected earlier is re-examined if it trends again 15+ min later
SCALP_CAND_TTL_SEC = 2 * 3600       # forget candidates that stopped trending
REFINE_EVERY_SEC = 60               # 1-minute-candle re-scoring: one coin per minute (GeckoTerminal budget)
PULLBACK_MIN_H1 = 20.0              # a spike above SCALP_MAX_M5 is watched for a pullback only in a strong 1h trend
PULLBACK_MIN = 0.04                 # buy the dip only after a 4%+ pullback from the spike's high...
PULLBACK_MAX = 0.15                 # ...but a fall of more than 15% means the move broke -> give up
PULLBACK_BOUNCE = 0.01              # and only once price bounced 1% off the dip's low
PULLBACK_PRESSURE = 0.60            # with buyers back in control over the last 2 minutes
PULLBACK_TTL_SEC = 900              # wait at most 15 minutes for the pullback
EARLY_CHECK_MAX_AGE_MIN = 360       # early-buyer (bundle/sniper) analysis only for coins younger than 6h
# legacy single-rule exits (profile A, kept for comparison only)
STOP_LOSS = -0.06
TAKE_PROFIT_1 = 0.25                # sell half at +25%
TRAIL_ARM = 0.15                    # trailing stop arms at +15%
TRAIL_DIST = 0.12                   # ...and sells if price falls 12% from the peak
MAX_HOLD_MIN = 240                  # time stop

# --- rug rules (design doc) ---------------------------------------------------
MIN_LIQ_USD = 25_000                # scalp: $10 trades need some depth; pump LP is burned (can't be pulled)
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

# --- order-flow agents (from the 12-agent Grok desk idea, free data: GeckoTerminal + Jupiter quote)
FLOW_ON = True
FLOW_WINDOW_MIN = 10                # look at the last 10 minutes of trades
WHALE_BUY_USD = 500                 # a single buy this big counts as a whale (pools are only $10k-100k)
FLOW_WEIGHT = 0.4                   # flow score moves the entry score by up to +-20 points
TRENDING_BONUS = 5                  # coin is trending on GeckoTerminal right now
MAX_SELL_IMPACT = 0.05              # sell simulation: selling our size must move price < 5%
DEV_CHECK_SEC = 60                  # while holding: check every minute whether the creator sold
REENTRY_WAIT_SEC = 120              # after a stop-loss, look at the coin once more after 2 minutes...
REENTRY_MIN_WHALES = 3              # ...and only re-buy if 3+ different whales bought within 2 minutes
TRENDING_EVERY_SEC = 30
WATCH_EVERY_SEC = 60                # named wallets (tier W) are checked every minute
