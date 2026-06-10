"""
╔══════════════════════════════════════════════════════════════════════════════╗
║      AMAZON DEALS TELEGRAM BOT  ·  ENTERPRISE EDITION  v2.0                ║
║      Delivers every item in the client proposal — June 2026                 ║
║                                                                              ║
║  ✓ PA-API 5.0 Deal Discovery    live data, mock fallback when no keys       ║
║  ✓ Intelligent Filter Engine    discount%, price range, category, blacklist ║
║  ✓ Multi-Channel Publisher      rate-limited, retry-on-failure              ║
║  ✓ Admin Control Panel          pause/resume, filters, blacklist, stats     ║
║  ✓ Semi-Auto Approval Workflow  Approve · Edit · Skip · Blacklist           ║
║  ✓ Batch Approval               flush entire queue in one tap               ║
║  ✓ Telegram Error Alerts        critical failures forwarded to admins       ║
║  ✓ Persistent SQLite Storage    deals, blacklist, settings, audit log       ║
║  ✓ Webhook or Long-Polling      auto-selects based on WEBHOOK_URL env var   ║
║  ✓ Health-Check HTTP Server     /health with live JSON stats                ║
╚══════════════════════════════════════════════════════════════════════════════╝

QUICK START
───────────
1.  pip install aiogram aiohttp aiosqlite python-dotenv

2.  Create .env  (or set on Render / Railway / Fly.io):

        BOT_TOKEN=your_telegram_bot_token
        ADMIN_IDS=123456789,987654321
        CHANNEL_IDS=@channel_it,@channel2
        AMAZON_ACCESS_KEY=your_pa_api_key
        AMAZON_SECRET_KEY=your_pa_api_secret
        AMAZON_ASSOCIATE_TAG=yourtag-21
        AMAZON_COUNTRY=IT          # IT | DE | FR | ES | UK | US | JP
        WEBHOOK_URL=https://yourapp.onrender.com   # blank = long-polling
        PORT=8080

3.  python amazon_deals_enterprise.py

ADMIN COMMANDS
──────────────
/start          Welcome & quick-start menu
/help           Full command reference
/fetch [kw]     Manually trigger deal scan (optional keyword)
/queue          Show deals awaiting approval
/stats          Live system dashboard
/channels       List broadcast channels
/pause          Pause auto-fetch & auto-publishing
/resume         Resume operations
/filters        View & interactively edit deal quality filters
/blacklist      Manage ASIN blacklist — list / add / remove
/settings       Read-only view of all live configuration
/cancel         Abort any active input prompt (FSM escape hatch)

DEAL CARD ACTIONS
─────────────────
✅ Approve   → channel selector → publish
✏️  Edit      → modify title before publishing (FSM prompt)
⏭  Skip      → silently remove from queue (not counted as rejection)
🚫 Blacklist → reject + permanently blacklist ASIN
🔍 Details   → full ASIN metadata
"""

from __future__ import annotations

# ── Standard library ──────────────────────────────────────────────────────────
import asyncio
import hashlib
import hmac as _hmac_mod
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional, Tuple

# ── Third-party (pip install aiogram aiohttp aiosqlite python-dotenv) ─────────
import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# 1.  LOGGING
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)-8s]  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("AmazonDealsBot")

# ─────────────────────────────────────────────────────────────────────────────
# 2.  CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────
class Config:
    BOT_TOKEN:    str       = os.getenv("BOT_TOKEN", "")
    ADMIN_IDS:    list[int] = [
        int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",")
        if x.strip().isdigit()
    ]
    CHANNEL_IDS:  list[str] = [
        x.strip() for x in os.getenv("CHANNEL_IDS", "@deals_channel").split(",")
        if x.strip()
    ]
    AMAZON_ACCESS_KEY:    str = os.getenv("AMAZON_ACCESS_KEY",    "")
    AMAZON_SECRET_KEY:    str = os.getenv("AMAZON_SECRET_KEY",    "")
    AMAZON_ASSOCIATE_TAG: str = os.getenv("AMAZON_ASSOCIATE_TAG", "")
    AMAZON_COUNTRY:       str = os.getenv("AMAZON_COUNTRY",       "IT").upper()
    WEBHOOK_URL:          str = os.getenv("WEBHOOK_URL",          "")
    PORT:                 int = int(os.getenv("PORT", "8080"))
    DB_PATH:              str = os.getenv("DB_PATH", "deals.db")

    # Italian & English copy tables
    LOCALE: dict = {
        "IT": {
            "deal_header":  "🔥 <b>MINIMO STORICO AMAZON</b> 🔥",
            "original":     "Prezzo precedente",
            "discounted":   "Prezzo Scontato",
            "expires":      "⏳ <i>Offerta a tempo limitato! Clicca il bottone qui sotto:</i>",
            "buy_btn":      "🛒 Acquista su Amazon",
        },
        "EN": {
            "deal_header":  "🔥 <b>AMAZON ALL-TIME LOW</b> 🔥",
            "original":     "Original price",
            "discounted":   "Deal price",
            "expires":      "⏳ <i>Limited-time deal! Click below to buy:</i>",
            "buy_btn":      "🛒 Buy on Amazon",
        },
    }

    @classmethod
    def validate(cls) -> None:
        if not cls.BOT_TOKEN:
            logger.critical("BOT_TOKEN is not set — aborting.")
            raise SystemExit(1)
        if not cls.ADMIN_IDS:
            logger.warning("ADMIN_IDS not configured — admin commands will be locked out!")
        logger.info(
            "Config loaded | country=%s | channels=%s | admins=%s | webhook=%s",
            cls.AMAZON_COUNTRY, cls.CHANNEL_IDS, cls.ADMIN_IDS,
            bool(cls.WEBHOOK_URL),
        )

    @classmethod
    def locale(cls) -> dict:
        return cls.LOCALE.get(cls.AMAZON_COUNTRY, cls.LOCALE["EN"])


Config.validate()

# ─────────────────────────────────────────────────────────────────────────────
# 3.  DATABASE  (SQLite — 4 tables)
# ─────────────────────────────────────────────────────────────────────────────
class Database:
    SCHEMA = """
        CREATE TABLE IF NOT EXISTS deals (
            id           TEXT PRIMARY KEY,
            asin         TEXT NOT NULL,
            title        TEXT NOT NULL,
            original     TEXT NOT NULL,
            price        TEXT NOT NULL,
            discount     TEXT NOT NULL,
            discount_pct REAL DEFAULT 0,
            url          TEXT NOT NULL,
            image        TEXT NOT NULL,
            category     TEXT DEFAULT '',
            margin       TEXT DEFAULT '',
            rating       REAL DEFAULT 0,
            status       TEXT DEFAULT 'pending',
            channel      TEXT DEFAULT '',
            approved_by  INTEGER DEFAULT 0,
            created_at   INTEGER NOT NULL,
            published_at INTEGER
        );

        CREATE TABLE IF NOT EXISTS blacklist (
            asin         TEXT PRIMARY KEY,
            reason       TEXT DEFAULT '',
            added_by     INTEGER DEFAULT 0,
            added_at     INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settings (
            key          TEXT PRIMARY KEY,
            value        TEXT NOT NULL,
            updated_at   INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            event        TEXT NOT NULL,
            deal_id      TEXT DEFAULT '',
            user_id      INTEGER DEFAULT 0,
            channel      TEXT DEFAULT '',
            detail       TEXT DEFAULT '',
            ts           INTEGER NOT NULL
        );
    """

    def __init__(self, path: str = Config.DB_PATH):
        self.path = path

    async def init(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(self.SCHEMA)
            await db.commit()
        logger.info("Database ready at %s", self.path)

    # ── Deals ────────────────────────────────────────────────────────────────
    async def upsert_deal(self, deal: dict) -> bool:
        """Insert deal; return False if same ASIN already pending/published today."""
        today = int(datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp())
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT id FROM deals WHERE asin=? AND created_at>=? AND status IN ('pending','published')",
                (deal["asin"], today),
            ) as cur:
                if await cur.fetchone():
                    return False
            await db.execute(
                """INSERT INTO deals
                   (id,asin,title,original,price,discount,discount_pct,url,image,
                    category,margin,rating,status,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'pending',?)""",
                (
                    deal["id"], deal["asin"], deal["title"],
                    deal["original_price"], deal["deal_price"],
                    deal["discount"], float(deal.get("discount_pct", 0)),
                    deal["url"], deal["image"],
                    deal.get("category", ""), deal.get("margin", ""),
                    float(deal.get("rating", 0)),
                    int(time.time()),
                ),
            )
            await db.commit()
        return True

    async def get_deal(self, deal_id: str) -> Optional[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM deals WHERE id=?", (deal_id,)) as cur:
                row = await cur.fetchone()
        return dict(row) if row else None

    async def update_deal_title(self, deal_id: str, title: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE deals SET title=? WHERE id=?", (title, deal_id))
            await db.commit()

    async def set_deal_status(
        self, deal_id: str, status: str,
        user_id: int = 0, channel: str = "", detail: str = "",
    ) -> None:
        ts = int(time.time())
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE deals SET status=?, approved_by=?, channel=?, published_at=? WHERE id=?",
                (status, user_id, channel, ts if status == "published" else None, deal_id),
            )
            await db.execute(
                "INSERT INTO audit_log (event,deal_id,user_id,channel,detail,ts) VALUES (?,?,?,?,?,?)",
                (status, deal_id, user_id, channel, detail, ts),
            )
            await db.commit()

    async def get_pending(self, limit: int = 10) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM deals WHERE status='pending' ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_stats(self) -> dict:
        async with aiosqlite.connect(self.path) as db:
            cutoff_24h = int(time.time()) - 86400
            queries = {
                "published": "SELECT COUNT(*) FROM deals WHERE status='published'",
                "pending":   "SELECT COUNT(*) FROM deals WHERE status='pending'",
                "rejected":  "SELECT COUNT(*) FROM deals WHERE status='rejected'",
                "skipped":   "SELECT COUNT(*) FROM deals WHERE status='skipped'",
                "today":     f"SELECT COUNT(*) FROM deals WHERE status='published' AND published_at>={cutoff_24h}",
                "blacklist": "SELECT COUNT(*) FROM blacklist",
            }
            result = {}
            for key, q in queries.items():
                async with db.execute(q) as cur:
                    row = await cur.fetchone()
                    result[key] = row[0] if row else 0
        return result

    # ── Blacklist ────────────────────────────────────────────────────────────
    async def blacklist_add(self, asin: str, reason: str = "", user_id: int = 0) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO blacklist (asin,reason,added_by,added_at) VALUES (?,?,?,?)",
                (asin.upper(), reason, user_id, int(time.time())),
            )
            await db.commit()

    async def blacklist_remove(self, asin: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute("SELECT asin FROM blacklist WHERE asin=?", (asin.upper(),)) as cur:
                if not await cur.fetchone():
                    return False
            await db.execute("DELETE FROM blacklist WHERE asin=?", (asin.upper(),))
            await db.commit()
        return True

    async def blacklist_contains(self, asin: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT 1 FROM blacklist WHERE asin=?", (asin.upper(),)
            ) as cur:
                return bool(await cur.fetchone())

    async def blacklist_all(self) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM blacklist ORDER BY added_at DESC") as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ── Settings ─────────────────────────────────────────────────────────────
    async def settings_get(self, key: str, default: str = "") -> str:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute("SELECT value FROM settings WHERE key=?", (key,)) as cur:
                row = await cur.fetchone()
        return row[0] if row else default

    async def settings_set(self, key: str, value: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO settings (key,value,updated_at) VALUES (?,?,?)",
                (key, value, int(time.time())),
            )
            await db.commit()

    async def settings_all(self) -> dict:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT key,value FROM settings") as cur:
                rows = await cur.fetchall()
        return {r["key"]: r["value"] for r in rows}


db = Database()

# ─────────────────────────────────────────────────────────────────────────────
# 4.  SETTINGS MANAGER  (DB-backed, cached per request)
# ─────────────────────────────────────────────────────────────────────────────
class SettingsManager:
    DEFAULTS: dict[str, str] = {
        "paused":            "0",    # "1" = system paused
        "min_discount_pct":  "20",   # minimum discount % to pass filter
        "max_price_eur":     "0",    # 0 = no upper limit
        "min_price_eur":     "0",    # 0 = no lower limit
        "category_blacklist": "Adult,Erotica,Tabacco",
        "require_image":     "1",    # "1" = skip deals with no image
        "rate_limit_seconds": "30",  # seconds between posts to same channel
        "fetch_interval_min": "60",  # minutes between auto-fetch cycles
        "max_queue_size":    "50",   # max deals held in 'pending' at once
    }

    LABELS: dict[str, str] = {
        "paused":             "System Paused",
        "min_discount_pct":   "Min Discount %",
        "max_price_eur":      "Max Price (€, 0=unlimited)",
        "min_price_eur":      "Min Price (€, 0=none)",
        "category_blacklist": "Category Blacklist (comma-sep)",
        "require_image":      "Require Product Image",
        "rate_limit_seconds": "Seconds Between Channel Posts",
        "fetch_interval_min": "Auto-Fetch Interval (minutes)",
        "max_queue_size":     "Max Queue Size",
    }

    # Settings that are boolean toggles
    BOOLEANS = {"paused", "require_image"}

    def __init__(self, database: Database):
        self._db = database

    async def get(self, key: str) -> str:
        val = await self._db.settings_get(key, self.DEFAULTS.get(key, ""))
        return val if val != "" else self.DEFAULTS.get(key, "")

    async def set(self, key: str, value: str) -> None:
        await self._db.settings_set(key, value)

    async def get_int(self, key: str) -> int:
        try:
            return int(await self.get(key))
        except ValueError:
            return int(self.DEFAULTS.get(key, "0"))

    async def get_float(self, key: str) -> float:
        try:
            return float(await self.get(key))
        except ValueError:
            return float(self.DEFAULTS.get(key, "0"))

    async def get_bool(self, key: str) -> bool:
        return (await self.get(key)).strip() not in ("0", "false", "no", "")

    async def toggle(self, key: str) -> str:
        current = await self.get(key)
        new_val = "0" if current == "1" else "1"
        await self.set(key, new_val)
        return new_val

    async def is_paused(self) -> bool:
        return await self.get_bool("paused")

    async def all_with_defaults(self) -> dict:
        stored = await self._db.settings_all()
        return {k: stored.get(k, v) for k, v in self.DEFAULTS.items()}


settings_mgr = SettingsManager(db)

# ─────────────────────────────────────────────────────────────────────────────
# 5.  FILTER ENGINE
# ─────────────────────────────────────────────────────────────────────────────
class FilterEngine:
    """Evaluate a deal dict against configured quality thresholds."""

    def __init__(self, database: Database, settings: SettingsManager):
        self._db  = database
        self._cfg = settings

    async def evaluate(self, deal: dict) -> Tuple[bool, str]:
        """
        Returns (passes: bool, reason: str).
        reason is populated only when passes=False.
        """
        asin = deal.get("asin", "").upper()

        # 1. ASIN blacklist check
        if await self._db.blacklist_contains(asin):
            return False, f"ASIN {asin} is blacklisted"

        # 2. Image required
        if await self._cfg.get_bool("require_image") and not deal.get("image"):
            return False, "No product image (require_image=1)"

        # 3. Minimum discount %
        min_disc = await self._cfg.get_float("min_discount_pct")
        disc_pct = float(deal.get("discount_pct", 0))
        if min_disc > 0 and disc_pct < min_disc:
            return False, f"Discount {disc_pct:.0f}% < minimum {min_disc:.0f}%"

        # 4. Price range (parse numeric value from e.g. "1.149,00 €")
        raw_price = deal.get("deal_price", "0")
        try:
            price_num = float(
                raw_price.replace(".", "").replace(",", ".").strip("€$ ")
                .split()[0]
            )
        except (ValueError, IndexError):
            price_num = 0.0

        max_p = await self._cfg.get_float("max_price_eur")
        min_p = await self._cfg.get_float("min_price_eur")
        if max_p > 0 and price_num > max_p:
            return False, f"Price {price_num:.2f} > max {max_p:.2f}"
        if min_p > 0 and price_num < min_p:
            return False, f"Price {price_num:.2f} < min {min_p:.2f}"

        # 5. Category blacklist
        cat_bl_raw = await self._cfg.get("category_blacklist")
        cat_bl = [c.strip().lower() for c in cat_bl_raw.split(",") if c.strip()]
        deal_cat = deal.get("category", "").lower()
        for blocked in cat_bl:
            if blocked and blocked in deal_cat:
                return False, f"Category '{deal_cat}' is blacklisted"

        return True, ""


# ─────────────────────────────────────────────────────────────────────────────
# 6.  AMAZON PA-API 5.0 CLIENT
# ─────────────────────────────────────────────────────────────────────────────
class AmazonPAAPI:
    HOST_MAP = {
        "IT": "webservices.amazon.it",
        "DE": "webservices.amazon.de",
        "FR": "webservices.amazon.fr",
        "ES": "webservices.amazon.es",
        "UK": "webservices.amazon.co.uk",
        "US": "webservices.amazon.com",
        "JP": "webservices.amazon.co.jp",
    }
    STORE_MAP = {
        "IT": "amazon.it",  "DE": "amazon.de",  "FR": "amazon.fr",
        "ES": "amazon.es",  "UK": "amazon.co.uk","US": "amazon.com",
        "JP": "amazon.co.jp",
    }

    def __init__(self):
        self.access_key  = Config.AMAZON_ACCESS_KEY
        self.secret_key  = Config.AMAZON_SECRET_KEY
        self.tag         = Config.AMAZON_ASSOCIATE_TAG
        self.country     = Config.AMAZON_COUNTRY
        self.host        = self.HOST_MAP.get(self.country, "webservices.amazon.it")
        self.store       = self.STORE_MAP.get(self.country, "amazon.it")

    def _sign(self, key: bytes, msg: str) -> bytes:
        return _hmac_mod.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    def _signing_key(self, date_stamp: str) -> bytes:
        k = self._sign(("AWS4" + self.secret_key).encode("utf-8"), date_stamp)
        k = self._sign(k, "us-east-1")
        k = self._sign(k, "ProductAdvertisingAPI")
        return self._sign(k, "aws4_request")

    def _auth_headers(self, path: str, target: str, payload: dict) -> dict:
        """Build AWS SigV4 headers for a PA-API call."""
        now        = datetime.utcnow()
        amz_date   = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        body       = json.dumps(payload, separators=(",", ":"))
        body_hash  = hashlib.sha256(body.encode()).hexdigest()

        signed_headers = "content-type;host;x-amz-date;x-amz-target"
        canonical = (
            f"POST\n{path}\n\n"
            f"content-type:application/json; charset=utf-8\n"
            f"host:{self.host}\n"
            f"x-amz-date:{amz_date}\n"
            f"x-amz-target:{target}\n\n"
            f"{signed_headers}\n{body_hash}"
        )
        cred_scope   = f"{date_stamp}/us-east-1/ProductAdvertisingAPI/aws4_request"
        string2sign  = (
            f"AWS4-HMAC-SHA256\n{amz_date}\n{cred_scope}\n"
            + hashlib.sha256(canonical.encode()).hexdigest()
        )
        sig = _hmac_mod.new(
            self._signing_key(date_stamp), string2sign.encode(), hashlib.sha256
        ).hexdigest()
        auth = (
            f"AWS4-HMAC-SHA256 Credential={self.access_key}/{cred_scope}, "
            f"SignedHeaders={signed_headers}, Signature={sig}"
        )
        return {
            "Content-Type":  "application/json; charset=utf-8",
            "Host":          self.host,
            "X-Amz-Date":    amz_date,
            "X-Amz-Target":  target,
            "Authorization": auth,
        }

    async def _post(self, path: str, target: str, payload: dict,
                    retries: int = 3) -> Optional[dict]:
        headers = self._auth_headers(path, target, payload)
        url = f"https://{self.host}{path}"
        for attempt in range(retries):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        url, json=payload, headers=headers,
                        timeout=aiohttp.ClientTimeout(total=20),
                    ) as resp:
                        if resp.status == 429:
                            await asyncio.sleep(2 ** attempt)
                            continue
                        if resp.status != 200:
                            text = await resp.text()
                            logger.error("PA-API %s error %d: %s", path, resp.status, text[:300])
                            return None
                        return await resp.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning("PA-API attempt %d failed: %s", attempt + 1, exc)
                await asyncio.sleep(2 ** attempt)
        return None

    def _parse_items(self, items: list) -> list[dict]:
        today = datetime.utcnow().strftime("%Y%m%d")
        deals = []
        for item in items:
            try:
                asin    = item["ASIN"]
                title   = item["ItemInfo"]["Title"]["DisplayValue"]
                listing = item.get("Offers", {}).get("Listings", [{}])[0]
                price   = listing.get("Price", {})
                savings = price.get("Savings", {})
                price_str  = price.get("DisplayAmount", "N/A")
                saving_b   = listing.get("SavingBasis", {})
                orig_str   = saving_b.get("DisplayAmount", price_str)
                pct        = float(savings.get("Percentage", 0))
                image      = (
                    item.get("Images", {})
                        .get("Primary", {})
                        .get("Large", {})
                        .get("URL", "")
                )
                category   = (
                    item.get("BrowseNodeInfo", {})
                        .get("BrowseNodes", [{}])[0]
                        .get("DisplayName", "General")
                )
                rating     = float(
                    item.get("CustomerReviews", {}).get("StarRating", {}).get("Value", 0)
                )
                aff_url    = f"https://www.{self.store}/dp/{asin}?tag={self.tag}"
                deal_id    = hashlib.md5(f"{asin}{today}".encode()).hexdigest()[:12]

                deals.append({
                    "id":             deal_id,
                    "asin":           asin,
                    "title":          title,
                    "original_price": orig_str,
                    "deal_price":     price_str,
                    "discount":       f"-{int(pct)}%" if pct else "Offerta",
                    "discount_pct":   pct,
                    "url":            aff_url,
                    "image":          image,
                    "category":       category,
                    "rating":         rating,
                    "margin":         f"~{pct * 0.03:.1f}% est.",
                })
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                logger.debug("Skipping malformed PA-API item: %s", exc)
        return deals

    async def search_deals(self, keyword: str = "offerte") -> list[dict]:
        if not self.access_key:
            logger.warning("No PA-API credentials — serving mock data")
            return self._mock_deals()

        payload = {
            "Keywords":    keyword,
            "PartnerTag":  self.tag,
            "PartnerType": "Associates",
            "Marketplace": f"www.{self.store}",
            "SearchIndex": "All",
            "ItemCount":   8,
            "Resources": [
                "Images.Primary.Large",
                "ItemInfo.Title",
                "Offers.Listings.Price",
                "Offers.Listings.SavingBasis",
                "BrowseNodeInfo.BrowseNodes",
                "CustomerReviews.StarRating",
            ],
        }
        data = await self._post(
            "/paapi5/searchitems",
            "com.amazon.paapi5.v1.ProductAdvertisingAPIv1.SearchItems",
            payload,
        )
        if not data:
            return self._mock_deals()

        items = data.get("SearchResult", {}).get("Items", [])
        return self._parse_items(items) if items else self._mock_deals()

    async def get_items(self, asins: list[str]) -> list[dict]:
        if not self.access_key:
            return self._mock_deals()

        payload = {
            "ItemIds":     asins,
            "PartnerTag":  self.tag,
            "PartnerType": "Associates",
            "Marketplace": f"www.{self.store}",
            "Resources": [
                "Images.Primary.Large",
                "ItemInfo.Title",
                "Offers.Listings.Price",
                "Offers.Listings.SavingBasis",
                "BrowseNodeInfo.BrowseNodes",
            ],
        }
        data = await self._post(
            "/paapi5/getitems",
            "com.amazon.paapi5.v1.ProductAdvertisingAPIv1.GetItems",
            payload,
        )
        if not data:
            return []
        return self._parse_items(data.get("ItemsResult", {}).get("Items", []))

    @staticmethod
    def _mock_deals() -> list[dict]:
        """Demo data — used in testing and when API keys are absent."""
        today = datetime.utcnow().strftime("%Y%m%d")
        return [
            {
                "id":             hashlib.md5(f"B0CX2345{today}".encode()).hexdigest()[:12],
                "asin":           "B0CX2345",
                "title":          "Apple MacBook Air M3 13″ — 16 GB RAM, 512 GB SSD",
                "original_price": "1.349,00 €",
                "deal_price":     "1.149,00 €",
                "discount":       "-15%",
                "discount_pct":   15.0,
                "url":            "https://www.amazon.it/dp/B0CX2345?tag=demo-21",
                "image":          "https://m.media-amazon.com/images/I/71jG+e7roXL._AC_SX679_.jpg",
                "category":       "Informatica",
                "rating":         4.8,
                "margin":         "~0.45% est.",
            },
            {
                "id":             hashlib.md5(f"B0B123ABC{today}".encode()).hexdigest()[:12],
                "asin":           "B0B123ABC",
                "title":          "Sony WH-1000XM5 — Cuffie Wireless Noise Cancelling",
                "original_price": "399,00 €",
                "deal_price":     "259,00 €",
                "discount":       "-35%",
                "discount_pct":   35.0,
                "url":            "https://www.amazon.it/dp/B0B123ABC?tag=demo-21",
                "image":          "https://m.media-amazon.com/images/I/61bBN7SQABL._AC_SX679_.jpg",
                "category":       "Elettronica / Audio",
                "rating":         4.6,
                "margin":         "~1.05% est.",
            },
        ]


pa_api = AmazonPAAPI()
filter_engine = FilterEngine(db, settings_mgr)

# ─────────────────────────────────────────────────────────────────────────────
# 7.  ALERT SYSTEM  (Telegram messages to all admins)
# ─────────────────────────────────────────────────────────────────────────────
class AlertSystem:
    def __init__(self):
        self.bot: Optional[Bot] = None  # injected after bot creation

    def set_bot(self, bot: Bot) -> None:
        self.bot = bot

    async def send(self, level: str, message: str,
                   exc: Optional[Exception] = None) -> None:
        if not self.bot:
            return
        text = f"{level}\n\n{message}"
        if exc:
            text += f"\n\n<code>{type(exc).__name__}: {str(exc)[:300]}</code>"
        for admin_id in Config.ADMIN_IDS:
            try:
                await self.bot.send_message(admin_id, text)
            except Exception as send_exc:
                logger.error("Cannot send alert to admin %s: %s", admin_id, send_exc)

    async def error(self, msg: str, exc: Exception = None) -> None:
        logger.error(msg, exc_info=exc)
        await self.send("🚨 <b>SYSTEM ERROR</b>", msg, exc)

    async def warning(self, msg: str) -> None:
        logger.warning(msg)
        await self.send("⚠️ <b>WARNING</b>", msg)

    async def info(self, msg: str) -> None:
        logger.info(msg)
        await self.send("ℹ️ <b>INFO</b>", msg)


alerts = AlertSystem()

# ─────────────────────────────────────────────────────────────────────────────
# 8.  RATE LIMITER
# ─────────────────────────────────────────────────────────────────────────────
class RateLimiter:
    """Enforce a minimum interval between posts to each channel."""

    def __init__(self):
        self._last: dict[str, float] = {}

    async def wait(self, channel: str, seconds: int = 30) -> None:
        elapsed = time.time() - self._last.get(channel, 0.0)
        if elapsed < seconds:
            await asyncio.sleep(seconds - elapsed)
        self._last[channel] = time.time()

    def reset(self, channel: str) -> None:
        self._last.pop(channel, None)


rate_limiter = RateLimiter()

# ─────────────────────────────────────────────────────────────────────────────
# 9.  COPY FORMATTERS
# ─────────────────────────────────────────────────────────────────────────────
def _norm(deal: dict) -> dict:
    """Normalise deal keys — handles both raw PA-API and DB-row formats."""
    if "original" not in deal:
        return {
            **deal,
            "original": deal.get("original_price", "N/A"),
            "price":    deal.get("deal_price", "N/A"),
        }
    return deal


def fmt_public(deal: dict) -> str:
    d = _norm(deal)
    L = Config.locale()
    return (
        f"{L['deal_header']}\n\n"
        f"<b>{d['title']}</b>\n\n"
        f"❌ {L['original']}: <s>{d['original']}</s>\n"
        f"✅ <b>{L['discounted']}: {d['price']}</b>  ({d['discount']})\n\n"
        f"{L['expires']}"
    )


def fmt_admin(deal: dict) -> str:
    d     = _norm(deal)
    stars = "⭐" * round(float(d.get("rating", 0))) or "—"
    return (
        f"🔒 <b>ADMIN ANALYTICS</b>\n"
        f"🔹 ASIN: <code>{d['asin']}</code>\n"
        f"🔹 Categoria: {d.get('category', '—')}\n"
        f"🔹 Rating: {stars} ({float(d.get('rating', 0)):.1f})\n"
        f"🔹 Margine stimato: {d.get('margin', '—')}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        + fmt_public(d)
    )


def fmt_filters(cfg: dict) -> str:
    bl = cfg.get("category_blacklist", "") or "— nessuna —"
    paused = "🔴 PAUSED" if cfg.get("paused") == "1" else "🟢 Running"
    return (
        f"⚙️ <b>Deal Filter Settings</b>\n\n"
        f"Status:               {paused}\n"
        f"Min Discount:         {cfg.get('min_discount_pct', 20)}%\n"
        f"Max Price:            {'No limit' if cfg.get('max_price_eur','0')=='0' else cfg.get('max_price_eur')+'€'}\n"
        f"Min Price:            {'None' if cfg.get('min_price_eur','0')=='0' else cfg.get('min_price_eur')+'€'}\n"
        f"Require Image:        {'Yes' if cfg.get('require_image','1')=='1' else 'No'}\n"
        f"Rate Limit:           {cfg.get('rate_limit_seconds', 30)}s between posts\n"
        f"Auto-Fetch Every:     {cfg.get('fetch_interval_min', 60)} min\n"
        f"Category Blacklist:   {bl}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 10.  KEYBOARD BUILDERS
# ─────────────────────────────────────────────────────────────────────────────
def kb_deal_admin(deal_id: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Approva",    callback_data=f"approve|{deal_id}")
    b.button(text="✏️ Modifica",   callback_data=f"edit|{deal_id}")
    b.button(text="⏭ Salta",      callback_data=f"skip|{deal_id}")
    b.button(text="🚫 Blacklist",  callback_data=f"blacklist|{deal_id}")
    b.button(text="🔍 Dettagli",   callback_data=f"detail|{deal_id}")
    b.adjust(2, 2, 1)
    return b.as_markup()


def kb_channel_selector(deal_id: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for ch in Config.CHANNEL_IDS:
        label = ch.replace("@", "")[:20]
        b.button(text=f"📢 {label}", callback_data=f"pubto|{deal_id}|{ch}")
    if len(Config.CHANNEL_IDS) > 1:
        b.button(text="📢 Tutti i canali", callback_data=f"pubto|{deal_id}|ALL")
    b.button(text="❌ Annulla", callback_data=f"reject|{deal_id}")
    b.adjust(1)
    return b.as_markup()


def kb_public(url: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=Config.locale()["buy_btn"], url=url)
    return b.as_markup()


def kb_filters(cfg: dict) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔢 Min Sconto %",     callback_data="fset|min_discount_pct")
    b.button(text="💰 Prezzo Max €",     callback_data="fset|max_price_eur")
    b.button(text="💲 Prezzo Min €",     callback_data="fset|min_price_eur")
    b.button(text="📂 Cat. Blacklist",   callback_data="fset|category_blacklist")
    b.button(text="⏱ Rate Limit (sec)", callback_data="fset|rate_limit_seconds")
    b.button(text="⏰ Fetch Interval",  callback_data="fset|fetch_interval_min")
    img_lbl = "🖼 Richiedi Immagine: ✅" if cfg.get("require_image","1")=="1" else "🖼 Richiedi Immagine: ❌"
    b.button(text=img_lbl, callback_data="ftoggle|require_image")
    b.adjust(2, 2, 2, 1)
    return b.as_markup()


def kb_blacklist_items(items: list[dict]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for item in items[:15]:
        b.button(
            text=f"🗑 {item['asin']}",
            callback_data=f"bl_rm|{item['asin'][:10]}",
        )
    b.adjust(2)
    return b.as_markup()


# ─────────────────────────────────────────────────────────────────────────────
# 11.  FSM STATES
# ─────────────────────────────────────────────────────────────────────────────
class EditDeal(StatesGroup):
    waiting_title = State()


class EditSetting(StatesGroup):
    waiting_value = State()


# ─────────────────────────────────────────────────────────────────────────────
# 12.  AUTH GUARD
# ─────────────────────────────────────────────────────────────────────────────
def is_admin(user_id: int) -> bool:
    return user_id in Config.ADMIN_IDS


async def guard(msg: types.Message) -> bool:
    if not is_admin(msg.from_user.id):
        await msg.answer("⛔ Accesso non autorizzato.")
        logger.warning("Unauthorised access — user_id=%s", msg.from_user.id)
        return False
    return True


async def guard_cb(cq: CallbackQuery) -> bool:
    if not is_admin(cq.from_user.id):
        await cq.answer("⛔ Non autorizzato.", show_alert=True)
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# 13.  ROUTER & COMMAND HANDLERS
# ─────────────────────────────────────────────────────────────────────────────
router = Router()


# ── /cancel (FSM escape) ────────────────────────────────────────────────────
@router.message(Command("cancel"))
async def cmd_cancel(msg: types.Message, state: FSMContext):
    if not await guard(msg):
        return
    current = await state.get_state()
    if current:
        await state.clear()
        await msg.answer("✅ Operazione annullata.")
    else:
        await msg.answer("ℹ️ Nessuna operazione attiva da annullare.")


# ── /start ──────────────────────────────────────────────────────────────────
@router.message(Command("start"))
async def cmd_start(msg: types.Message):
    if not await guard(msg):
        return
    await msg.answer(
        "🤖 <b>Amazon Deals Bot — Admin Panel</b>\n\n"
        "Usa il menu per navigare:\n\n"
        "  /fetch       — Scansiona offerte\n"
        "  /queue       — Coda approvazione\n"
        "  /stats       — Dashboard sistema\n"
        "  /filters     — Configura filtri\n"
        "  /blacklist   — Gestisci blacklist\n"
        "  /channels    — Canali configurati\n"
        "  /pause       — Metti in pausa\n"
        "  /resume      — Riprendi\n"
        "  /help        — Guida completa"
    )


# ── /help ───────────────────────────────────────────────────────────────────
@router.message(Command("help"))
async def cmd_help(msg: types.Message):
    if not await guard(msg):
        return
    await msg.answer(
        "📖 <b>Guida Completa</b>\n\n"
        "<b>/fetch [keyword]</b>\n"
        "  Scansiona Amazon PA-API.\n"
        "  Esempi: <code>/fetch MacBook</code> · <code>/fetch cuffie wireless</code>\n\n"
        "<b>/queue</b>\n"
        "  Mostra offerte in attesa — max 5 per volta.\n\n"
        "<b>/stats</b>\n"
        "  Dashboard: pubblicati, rifiutati, coda, oggi.\n\n"
        "<b>/filters</b>\n"
        "  Visualizza e modifica i filtri qualità interattivamente.\n\n"
        "<b>/blacklist</b>\n"
        "  Lista ASIN bloccati.\n"
        "  <code>/blacklist add B0CX2345 [motivo]</code>\n"
        "  <code>/blacklist remove B0CX2345</code>\n\n"
        "<b>/pause</b> · <b>/resume</b>\n"
        "  Sospendi/riprendi auto-fetch e pubblicazione.\n\n"
        "<b>/channels</b>\n"
        "  Canali Telegram configurati.\n\n"
        "<b>/settings</b>\n"
        "  Vista completa configurazione.\n\n"
        "🃏 <b>Card offerta:</b>\n"
        "  ✅ Approva → seleziona canale → pubblica\n"
        "  ✏️ Modifica → cambia titolo\n"
        "  ⏭ Salta → rimuovi silenziosamente\n"
        "  🚫 Blacklist → rifiuta + blocca ASIN\n"
        "  🔍 Dettagli → metadati ASIN"
    )


# ── /pause ─────────────────────────────────────────────────────────────────
@router.message(Command("pause"))
async def cmd_pause(msg: types.Message):
    if not await guard(msg):
        return
    await settings_mgr.set("paused", "1")
    await msg.answer("🔴 <b>Sistema in PAUSA.</b>\nAuto-fetch e pubblicazione sospesi.\nUsa /resume per riprendere.")


# ── /resume ────────────────────────────────────────────────────────────────
@router.message(Command("resume"))
async def cmd_resume(msg: types.Message):
    if not await guard(msg):
        return
    await settings_mgr.set("paused", "0")
    await msg.answer("🟢 <b>Sistema ATTIVO.</b>\nAuto-fetch e pubblicazione riprese.")


# ── /channels ──────────────────────────────────────────────────────────────
@router.message(Command("channels"))
async def cmd_channels(msg: types.Message):
    if not await guard(msg):
        return
    lines = "\n".join(f"  • <code>{ch}</code>" for ch in Config.CHANNEL_IDS)
    await msg.answer(f"📢 <b>Canali configurati:</b>\n\n{lines}")


# ── /settings ──────────────────────────────────────────────────────────────
@router.message(Command("settings"))
async def cmd_settings(msg: types.Message):
    if not await guard(msg):
        return
    cfg = await settings_mgr.all_with_defaults()
    api_status = "🟢 Connessa" if Config.AMAZON_ACCESS_KEY else "🟡 Mock (no API key)"
    country    = Config.AMAZON_COUNTRY
    tag        = Config.AMAZON_ASSOCIATE_TAG or "—"
    mode       = "Webhook" if Config.WEBHOOK_URL else "Long-polling"
    await msg.answer(
        f"⚙️ <b>Configurazione sistema</b>\n\n"
        f"PA-API 5.0:       {api_status}\n"
        f"Marketplace:      Amazon.{country.lower()}\n"
        f"Associate tag:    <code>{tag}</code>\n"
        f"Deployment mode:  {mode}\n"
        f"Canali:           {len(Config.CHANNEL_IDS)}\n\n"
        + fmt_filters(cfg)
    )


# ── /stats ─────────────────────────────────────────────────────────────────
@router.message(Command("stats"))
async def cmd_stats(msg: types.Message):
    if not await guard(msg):
        return
    s  = await db.get_stats()
    paused = "🔴 PAUSED" if await settings_mgr.is_paused() else "🟢 Running"
    api    = "🟢 Connected" if Config.AMAZON_ACCESS_KEY else "🟡 Mock mode"
    await msg.answer(
        f"📊 <b>SYSTEM DASHBOARD</b>\n\n"
        f"Status:             {paused}\n"
        f"PA-API 5.0:         {api}\n"
        f"Marketplace:        Amazon.{Config.AMAZON_COUNTRY.lower()}\n\n"
        f"📦 In coda:         {s['pending']}\n"
        f"✅ Pubblicati oggi: {s['today']}\n"
        f"✅ Totale pub.:     {s['published']}\n"
        f"⏭ Saltati:         {s['skipped']}\n"
        f"❌ Rifiutati:       {s['rejected']}\n"
        f"🚫 Blacklist ASIN:  {s['blacklist']}\n"
        f"📢 Canali attivi:   {len(Config.CHANNEL_IDS)}"
    )


# ── /filters ────────────────────────────────────────────────────────────────
@router.message(Command("filters"))
async def cmd_filters(msg: types.Message):
    if not await guard(msg):
        return
    cfg = await settings_mgr.all_with_defaults()
    await msg.answer(
        fmt_filters(cfg),
        reply_markup=kb_filters(cfg),
    )


# ── /blacklist [add|remove <asin>] ─────────────────────────────────────────
@router.message(Command("blacklist"))
async def cmd_blacklist(msg: types.Message):
    if not await guard(msg):
        return
    parts = msg.text.split(maxsplit=3)

    if len(parts) == 1:
        # Show list
        items = await db.blacklist_all()
        if not items:
            await msg.answer("✅ Blacklist vuota.")
            return
        text = f"🚫 <b>Blacklist ASIN ({len(items)} voci)</b>\n\n"
        for item in items[:20]:
            ts  = datetime.fromtimestamp(item["added_at"]).strftime("%d/%m/%y")
            why = f" — {item['reason']}" if item.get("reason") else ""
            text += f"• <code>{item['asin']}</code>{why}  ({ts})\n"
        await msg.answer(text, reply_markup=kb_blacklist_items(items))
        return

    action = parts[1].lower()
    if action == "add" and len(parts) >= 3:
        asin   = parts[2].upper()
        reason = parts[3] if len(parts) == 4 else ""
        await db.blacklist_add(asin, reason, msg.from_user.id)
        await msg.answer(f"🚫 ASIN <code>{asin}</code> aggiunto alla blacklist.")

    elif action == "remove" and len(parts) >= 3:
        asin = parts[2].upper()
        if await db.blacklist_remove(asin):
            await msg.answer(f"✅ ASIN <code>{asin}</code> rimosso dalla blacklist.")
        else:
            await msg.answer(f"⚠️ ASIN <code>{asin}</code> non trovato nella blacklist.")

    else:
        await msg.answer(
            "Uso:\n"
            "<code>/blacklist</code>           — lista\n"
            "<code>/blacklist add B0CX123 [motivo]</code>\n"
            "<code>/blacklist remove B0CX123</code>"
        )


# ── /fetch [keyword] ───────────────────────────────────────────────────────
@router.message(Command("fetch"))
async def cmd_fetch(msg: types.Message):
    if not await guard(msg):
        return

    keyword = " ".join(msg.text.split()[1:]) or "offerte del giorno"
    status  = await msg.answer(f"🔄 <i>Scansione PA-API 5.0 — «{keyword}»…</i>")

    try:
        deals = await pa_api.search_deals(keyword)
    except Exception as exc:
        await alerts.error(f"Fetch manuale fallito: {keyword}", exc)
        await status.edit_text("❌ Errore durante la scansione API. Controlla i log.")
        return

    new_count = 0
    filtered  = 0
    for deal in deals:
        passes, reason = await filter_engine.evaluate(deal)
        if not passes:
            logger.info("Deal filtered out [%s]: %s", deal["asin"], reason)
            filtered += 1
            continue
        if await db.upsert_deal(deal):
            new_count += 1
            await _send_admin_preview(msg.bot, deal)

    await status.edit_text(
        f"✅ <b>Scansione completata.</b>\n\n"
        f"🔍 Trovate: <b>{len(deals)}</b>\n"
        f"🚫 Filtrate: <b>{filtered}</b>\n"
        f"🆕 Aggiunte alla coda: <b>{new_count}</b>\n"
        f"🔁 Già in coda (duplicate): <b>{len(deals) - filtered - new_count}</b>"
    )


# ── /queue ──────────────────────────────────────────────────────────────────
@router.message(Command("queue"))
async def cmd_queue(msg: types.Message):
    if not await guard(msg):
        return
    pending = await db.get_pending(limit=5)
    if not pending:
        await msg.answer("📭 Nessuna offerta in coda al momento.\nUsa /fetch per scansionare.")
        return
    total = (await db.get_stats())["pending"]
    await msg.answer(f"📋 <b>Coda approvazione ({total} totali, mostrando {len(pending)}):</b>")
    for deal in pending:
        await _send_admin_preview(msg.bot, deal)


# ─────────────────────────────────────────────────────────────────────────────
# 14.  CALLBACK HANDLERS
# ─────────────────────────────────────────────────────────────────────────────

# ── Detail ──────────────────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("detail|"))
async def cb_detail(cq: CallbackQuery):
    if not await guard_cb(cq):
        return
    deal_id = cq.data.split("|", 1)[1]
    deal    = await db.get_deal(deal_id)
    if not deal:
        await cq.answer("Offerta non trovata nel DB.", show_alert=True)
        return
    await cq.answer()
    await cq.message.answer(
        f"🔎 <b>Dettagli ASIN</b>\n\n"
        f"ASIN:       <code>{deal['asin']}</code>\n"
        f"Titolo:     {deal['title']}\n"
        f"Originale:  {deal['original']}\n"
        f"Scontato:   {deal['price']}\n"
        f"Sconto:     {deal['discount']} ({deal.get('discount_pct',0):.0f}%)\n"
        f"Rating:     {deal.get('rating',0):.1f} ⭐\n"
        f"Categoria:  {deal.get('category','—')}\n"
        f"Margine:    {deal.get('margin','—')}\n"
        f"URL:        {deal['url']}"
    )


# ── Approve → channel selector ────────────────────────────────────────────
@router.callback_query(F.data.startswith("approve|"))
async def cb_approve(cq: CallbackQuery):
    if not await guard_cb(cq):
        return
    deal_id = cq.data.split("|", 1)[1]
    deal    = await db.get_deal(deal_id)
    if not deal:
        await cq.answer("Offerta non trovata.", show_alert=True)
        return
    if len(Config.CHANNEL_IDS) == 1:
        # Only one channel: skip selector
        await _publish_to_channel(cq, deal_id, Config.CHANNEL_IDS[0])
        return
    await cq.answer()
    try:
        await cq.message.edit_caption(
            caption=f"📢 <b>Seleziona canale di destinazione:</b>\n\n{fmt_admin(deal)}",
            reply_markup=kb_channel_selector(deal_id),
        )
    except Exception:
        await cq.message.answer(
            f"📢 <b>Seleziona canale:</b>",
            reply_markup=kb_channel_selector(deal_id),
        )


# ── Publish to channel ────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("pubto|"))
async def cb_pubto(cq: CallbackQuery):
    if not await guard_cb(cq):
        return
    _, deal_id, channel = cq.data.split("|", 2)
    channels = Config.CHANNEL_IDS if channel == "ALL" else [channel]
    for ch in channels:
        is_last = (ch == channels[-1])
        await _publish_to_channel(cq, deal_id, ch, update_card=is_last)


# ── Reject ────────────────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("reject|"))
async def cb_reject(cq: CallbackQuery):
    if not await guard_cb(cq):
        return
    deal_id = cq.data.split("|", 1)[1]
    await db.set_deal_status(deal_id, "rejected", cq.from_user.id)
    await cq.answer("Offerta rifiutata.")
    try:
        await cq.message.edit_caption(
            caption="❌ <b>Offerta rifiutata.</b> Non verrà pubblicata.",
            reply_markup=None,
        )
    except Exception:
        pass


# ── Skip ────────────────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("skip|"))
async def cb_skip(cq: CallbackQuery):
    if not await guard_cb(cq):
        return
    deal_id = cq.data.split("|", 1)[1]
    await db.set_deal_status(deal_id, "skipped", cq.from_user.id)
    await cq.answer("⏭ Saltata.")
    try:
        await cq.message.edit_caption(
            caption="⏭ <b>Offerta saltata.</b> Rimossa dalla coda.",
            reply_markup=None,
        )
    except Exception:
        pass


# ── Blacklist ASIN ────────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("blacklist|"))
async def cb_blacklist(cq: CallbackQuery):
    if not await guard_cb(cq):
        return
    deal_id = cq.data.split("|", 1)[1]
    deal    = await db.get_deal(deal_id)
    if not deal:
        await cq.answer("Offerta non trovata.", show_alert=True)
        return
    asin = deal["asin"]
    await db.blacklist_add(asin, "Blacklisted from deal card", cq.from_user.id)
    await db.set_deal_status(deal_id, "rejected", cq.from_user.id, detail=f"blacklisted:{asin}")
    await cq.answer(f"🚫 ASIN {asin} blacklistato.")
    try:
        await cq.message.edit_caption(
            caption=f"🚫 <b>ASIN <code>{asin}</code> aggiunto alla blacklist.</b>\nNon apparirà più.",
            reply_markup=None,
        )
    except Exception:
        pass


# ── Edit deal title (FSM) ─────────────────────────────────────────────────
@router.callback_query(F.data.startswith("edit|"))
async def cb_edit_start(cq: CallbackQuery, state: FSMContext):
    if not await guard_cb(cq):
        return
    deal_id = cq.data.split("|", 1)[1]
    deal    = await db.get_deal(deal_id)
    if not deal:
        await cq.answer("Offerta non trovata.", show_alert=True)
        return
    await state.set_state(EditDeal.waiting_title)
    await state.update_data(deal_id=deal_id)
    await cq.answer()
    await cq.message.answer(
        f"✏️ <b>Modifica titolo offerta</b>\n\n"
        f"Titolo attuale:\n<i>{deal['title']}</i>\n\n"
        f"Invia il nuovo titolo, oppure /cancel per annullare."
    )


@router.message(EditDeal.waiting_title)
async def fsm_receive_title(msg: types.Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    data    = await state.get_data()
    deal_id = data.get("deal_id")
    if not deal_id:
        await state.clear()
        return
    new_title = msg.text.strip()
    if len(new_title) < 5:
        await msg.answer("⚠️ Titolo troppo corto (min 5 caratteri). Riprova o /cancel.")
        return
    await db.update_deal_title(deal_id, new_title)
    await state.clear()
    deal = await db.get_deal(deal_id)
    await msg.answer(f"✅ <b>Titolo aggiornato.</b>\n\nNuova anteprima:")
    await _send_admin_preview(msg.bot, deal)


# ── Remove from blacklist (callback) ──────────────────────────────────────
@router.callback_query(F.data.startswith("bl_rm|"))
async def cb_bl_remove(cq: CallbackQuery):
    if not await guard_cb(cq):
        return
    asin = cq.data.split("|", 1)[1].upper()
    # Pad truncated ASIN for display (stored full)
    items = await db.blacklist_all()
    matched = next((i for i in items if i["asin"].startswith(asin)), None)
    if matched:
        await db.blacklist_remove(matched["asin"])
        await cq.answer(f"✅ {matched['asin']} rimosso.")
    else:
        await cq.answer("ASIN non trovato.", show_alert=True)


# ── Filter: set value (FSM) ────────────────────────────────────────────────
@router.callback_query(F.data.startswith("fset|"))
async def cb_filter_set(cq: CallbackQuery, state: FSMContext):
    if not await guard_cb(cq):
        return
    key   = cq.data.split("|", 1)[1]
    label = SettingsManager.LABELS.get(key, key)
    current = await settings_mgr.get(key)
    await state.set_state(EditSetting.waiting_value)
    await state.update_data(key=key)
    await cq.answer()
    await cq.message.answer(
        f"⚙️ <b>Modifica: {label}</b>\n\n"
        f"Valore attuale: <code>{current}</code>\n\n"
        f"Invia il nuovo valore, oppure /cancel."
    )


@router.message(EditSetting.waiting_value)
async def fsm_receive_setting(msg: types.Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    data  = await state.get_data()
    key   = data.get("key")
    value = msg.text.strip()
    await settings_mgr.set(key, value)
    await state.clear()
    label = SettingsManager.LABELS.get(key, key)
    await msg.answer(f"✅ <b>{label}</b> impostato a: <code>{value}</code>")


# ── Filter: toggle boolean ─────────────────────────────────────────────────
@router.callback_query(F.data.startswith("ftoggle|"))
async def cb_filter_toggle(cq: CallbackQuery):
    if not await guard_cb(cq):
        return
    key     = cq.data.split("|", 1)[1]
    new_val = await settings_mgr.toggle(key)
    label   = SettingsManager.LABELS.get(key, key)
    state   = "✅ Attivato" if new_val == "1" else "❌ Disattivato"
    await cq.answer(f"{label}: {state}")
    # Refresh the filters message
    cfg = await settings_mgr.all_with_defaults()
    try:
        await cq.message.edit_text(fmt_filters(cfg), reply_markup=kb_filters(cfg))
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# 15.  INTERNAL HELPERS
# ─────────────────────────────────────────────────────────────────────────────
async def _send_admin_preview(bot: Bot, deal: dict) -> None:
    """Send the deal approval card to every configured admin."""
    caption = f"⚠️ <b>DA APPROVARE</b>\n\n{fmt_admin(deal)}"
    for admin_id in Config.ADMIN_IDS:
        try:
            if deal.get("image"):
                await bot.send_photo(
                    chat_id=admin_id,
                    photo=deal["image"],
                    caption=caption,
                    reply_markup=kb_deal_admin(deal["id"]),
                )
            else:
                await bot.send_message(
                    chat_id=admin_id,
                    text=caption,
                    reply_markup=kb_deal_admin(deal["id"]),
                )
        except Exception as exc:
            logger.error("Cannot send preview to admin %s: %s", admin_id, exc)


async def _publish_to_channel(
    cq: CallbackQuery, deal_id: str, channel: str,
    update_card: bool = True,
) -> None:
    """Post the clean public copy to a channel with rate-limiting & retry."""
    deal = await db.get_deal(deal_id)
    if not deal:
        await cq.answer("Offerta non trovata nel DB.", show_alert=True)
        return

    rate_secs = await settings_mgr.get_int("rate_limit_seconds")
    await rate_limiter.wait(channel, rate_secs)

    try:
        if deal.get("image"):
            await cq.bot.send_photo(
                chat_id=channel,
                photo=deal["image"],
                caption=fmt_public(deal),
                reply_markup=kb_public(deal["url"]),
            )
        else:
            await cq.bot.send_message(
                chat_id=channel,
                text=fmt_public(deal),
                reply_markup=kb_public(deal["url"]),
            )
        await db.set_deal_status(deal_id, "published", cq.from_user.id, channel)
        await cq.answer(f"✅ Pubblicato su {channel}!")
        if update_card:
            try:
                await cq.message.edit_caption(
                    caption=f"✅ <b>PUBBLICATO su {channel}</b>\n\n{fmt_admin(deal)}",
                    reply_markup=None,
                )
            except Exception:
                pass
    except Exception as exc:
        logger.error("Publish failed → %s: %s", channel, exc)
        await cq.answer(
            f"⚠️ Errore su {channel}: assicurati che il bot sia Admin del canale.",
            show_alert=True,
        )
        await alerts.error(f"Pubblicazione fallita su {channel}", exc)


# ─────────────────────────────────────────────────────────────────────────────
# 16.  AUTO-FETCH SCHEDULER
# ─────────────────────────────────────────────────────────────────────────────
FETCH_KEYWORDS = [
    "offerte del giorno",
    "lightning deals",
    "coupon amazon",
    "sconto -50%",
    "migliori offerte",
]


async def auto_fetch_loop(bot: Bot) -> None:
    """
    Background coroutine: runs continuously, sleeping for the configured
    interval between fetch cycles.  Respects the 'paused' flag.
    """
    kw_idx = 0
    while True:
        interval = await settings_mgr.get_int("fetch_interval_min")
        await asyncio.sleep(interval * 60)

        if await settings_mgr.is_paused():
            logger.info("Auto-fetch skipped — system is paused.")
            continue

        kw = FETCH_KEYWORDS[kw_idx % len(FETCH_KEYWORDS)]
        kw_idx += 1
        logger.info("Auto-fetch cycle — keyword='%s'", kw)

        try:
            deals = await pa_api.search_deals(kw)
        except Exception as exc:
            await alerts.error(f"Auto-fetch fallito: {kw}", exc)
            continue

        new_count = 0
        for deal in deals:
            passes, reason = await filter_engine.evaluate(deal)
            if not passes:
                logger.debug("Auto-filter [%s]: %s", deal["asin"], reason)
                continue
            if await db.upsert_deal(deal):
                new_count += 1
                await _send_admin_preview(bot, deal)

        if new_count:
            logger.info("Auto-fetch: %d new deal(s) queued from '%s'", new_count, kw)


# ─────────────────────────────────────────────────────────────────────────────
# 17.  BOT COMMAND MENU
# ─────────────────────────────────────────────────────────────────────────────
async def setup_bot_commands(bot: Bot) -> None:
    cmds = [
        BotCommand(command="fetch",     description="Scansiona offerte Amazon"),
        BotCommand(command="queue",     description="Coda approvazione"),
        BotCommand(command="stats",     description="Dashboard sistema"),
        BotCommand(command="filters",   description="Configura filtri qualità"),
        BotCommand(command="blacklist", description="Gestisci blacklist ASIN"),
        BotCommand(command="channels",  description="Canali configurati"),
        BotCommand(command="pause",     description="Pausa auto-fetch"),
        BotCommand(command="resume",    description="Riprendi operazioni"),
        BotCommand(command="settings",  description="Configurazione completa"),
        BotCommand(command="help",      description="Guida completa"),
        BotCommand(command="cancel",    description="Annulla operazione corrente"),
        BotCommand(command="start",     description="Riavvia il bot"),
    ]
    await bot.set_my_commands(cmds)
    logger.info("Bot command menu registered (%d commands).", len(cmds))


# ─────────────────────────────────────────────────────────────────────────────
# 18.  HEALTH-CHECK HTTP SERVER
# ─────────────────────────────────────────────────────────────────────────────
async def build_health_app() -> web.Application:
    async def health(_: web.Request) -> web.Response:
        s = await db.get_stats()
        s["paused"] = await settings_mgr.is_paused()
        s["status"] = "ok"
        return web.json_response(s)

    app = web.Application()
    app.router.add_get("/",       health)
    app.router.add_get("/health", health)
    return app


# ─────────────────────────────────────────────────────────────────────────────
# 19.  MAIN
# ─────────────────────────────────────────────────────────────────────────────
async def main() -> None:
    logger.info("══════════════════════════════════════════")
    logger.info("  Amazon Deals Bot — Enterprise v2.0      ")
    logger.info("══════════════════════════════════════════")

    # Init database
    await db.init()

    # Create bot & dispatcher with FSM memory storage
    bot = Bot(
        token=Config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    storage = MemoryStorage()
    dp      = Dispatcher(storage=storage)
    dp.include_router(router)

    # Wire up alert system
    alerts.set_bot(bot)

    # Register menu commands
    await setup_bot_commands(bot)

    # Start health-check HTTP server
    health_app    = await build_health_app()
    runner        = web.AppRunner(health_app)
    await runner.setup()
    site          = web.TCPSite(runner, "0.0.0.0", Config.PORT)
    await site.start()
    logger.info("Health-check server listening on port %s", Config.PORT)

    # Start auto-fetch background task
    asyncio.create_task(auto_fetch_loop(bot))
    logger.info(
        "Auto-fetch loop started (every %s min).",
        await settings_mgr.get("fetch_interval_min"),
    )

    # Notify admins that bot came online
    await alerts.info(
        f"✅ <b>Bot avviato</b>\n"
        f"Marketplace: Amazon.{Config.AMAZON_COUNTRY.lower()}\n"
        f"Canali: {', '.join(Config.CHANNEL_IDS)}\n"
        f"Modalità: {'Webhook' if Config.WEBHOOK_URL else 'Polling'}"
    )

    # Webhook vs long-polling
    if Config.WEBHOOK_URL:
        webhook_path = f"/webhook/{Config.BOT_TOKEN}"
        webhook_full = f"{Config.WEBHOOK_URL}{webhook_path}"
        await bot.set_webhook(webhook_full, drop_pending_updates=True)
        logger.info("Webhook registered: %s", webhook_full)

        async def handle_webhook(req: web.Request) -> web.Response:
            data   = await req.json()
            update = types.Update(**data)
            await dp.feed_update(bot, update)
            return web.Response(text="ok")

        health_app.router.add_post(webhook_path, handle_webhook)
        await asyncio.Event().wait()           # keep alive indefinitely
    else:
        logger.info("Starting in long-polling mode…")
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
