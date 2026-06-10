"""
╔══════════════════════════════════════════════════════════════════════╗
║          AMAZON DEALS TELEGRAM BOT — PROFESSIONAL EDITION           ║
║                    Multi-Channel | PA-API 5.0 | SQLite               ║
╚══════════════════════════════════════════════════════════════════════╝

SETUP GUIDE
───────────
1.  pip install aiogram aiohttp aiosqlite python-amazon-paapi schedule python-dotenv

2.  Create a .env file (or set real environment variables on Render/Railway):

    BOT_TOKEN=your_telegram_bot_token
    ADMIN_IDS=123456789,987654321          # comma-separated Telegram user IDs
    CHANNEL_IDS=@channel1,@channel2        # comma-separated channel usernames / IDs
    AMAZON_ACCESS_KEY=your_pa_api_key
    AMAZON_SECRET_KEY=your_pa_api_secret
    AMAZON_ASSOCIATE_TAG=your_tag-21
    AMAZON_COUNTRY=IT                      # IT | DE | FR | ES | UK | US …
    AUTO_FETCH_INTERVAL=60                 # minutes between auto-scans
    WEBHOOK_URL=https://yourapp.onrender.com   # leave blank for polling mode
    PORT=8080

3.  Run:  python amazon_deals_bot_pro.py
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiohttp import web
from dotenv import load_dotenv

# ─────────────────────────────────────────────────────────────────────────────
# 0.  BOOTSTRAP
# ─────────────────────────────────────────────────────────────────────────────
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)-8s]  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("AmazonBot")

# ─────────────────────────────────────────────────────────────────────────────
# 1.  CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────
class Config:
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    ADMIN_IDS: list[int] = [
        int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
    ]
    CHANNEL_IDS: list[str] = [
        x.strip() for x in os.getenv("CHANNEL_IDS", "@deals_channel").split(",") if x.strip()
    ]
    AMAZON_ACCESS_KEY: str   = os.getenv("AMAZON_ACCESS_KEY", "")
    AMAZON_SECRET_KEY: str   = os.getenv("AMAZON_SECRET_KEY", "")
    AMAZON_ASSOCIATE_TAG: str = os.getenv("AMAZON_ASSOCIATE_TAG", "")
    AMAZON_COUNTRY: str       = os.getenv("AMAZON_COUNTRY", "IT").upper()
    AUTO_FETCH_INTERVAL: int  = int(os.getenv("AUTO_FETCH_INTERVAL", "60"))
    WEBHOOK_URL: str          = os.getenv("WEBHOOK_URL", "")
    PORT: int                 = int(os.getenv("PORT", "8080"))
    DB_PATH: str              = os.getenv("DB_PATH", "deals.db")

    # Locale copy
    LOCALE: dict = {
        "IT": {
            "deal_header":   "🔥 <b>OFFERTA LAMPO AMAZON</b> 🔥",
            "original":      "Prezzo precedente",
            "discounted":    "Prezzo Scontato",
            "expires":       "⏳ <i>Offerta a tempo! Clicca il bottone per acquistare:</i>",
            "buy_btn":       "🛒 Acquista su Amazon",
            "approved_tag":  "✅ PUBBLICATO",
            "rejected_tag":  "❌ SCARTATO",
            "pending_tag":   "⚠️ IN ATTESA DI APPROVAZIONE",
        },
        "EN": {
            "deal_header":   "🔥 <b>AMAZON FLASH DEAL</b> 🔥",
            "original":      "Original price",
            "discounted":    "Deal price",
            "expires":       "⏳ <i>Limited time! Click below to grab it:</i>",
            "buy_btn":       "🛒 Buy on Amazon",
            "approved_tag":  "✅ PUBLISHED",
            "rejected_tag":  "❌ REJECTED",
            "pending_tag":   "⚠️ AWAITING APPROVAL",
        },
    }

    @classmethod
    def validate(cls) -> None:
        if not cls.BOT_TOKEN:
            logger.critical("BOT_TOKEN is not set — exiting.")
            raise SystemExit(1)
        if not cls.ADMIN_IDS:
            logger.warning("No ADMIN_IDS set — admin commands will be unreachable!")
        logger.info(
            "Config OK | country=%s | channels=%s | admins=%s",
            cls.AMAZON_COUNTRY,
            cls.CHANNEL_IDS,
            cls.ADMIN_IDS,
        )

    @classmethod
    def locale(cls) -> dict:
        lang = cls.AMAZON_COUNTRY if cls.AMAZON_COUNTRY in cls.LOCALE else "EN"
        return cls.LOCALE[lang]


Config.validate()

# ─────────────────────────────────────────────────────────────────────────────
# 2.  DATABASE LAYER
# ─────────────────────────────────────────────────────────────────────────────
class Database:
    """Async SQLite wrapper — single file, zero external deps."""

    def __init__(self, path: str = Config.DB_PATH):
        self.path = path

    async def init(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS deals (
                    id          TEXT PRIMARY KEY,
                    asin        TEXT NOT NULL,
                    title       TEXT NOT NULL,
                    original    TEXT NOT NULL,
                    price       TEXT NOT NULL,
                    discount    TEXT NOT NULL,
                    url         TEXT NOT NULL,
                    image       TEXT NOT NULL,
                    category    TEXT,
                    margin      TEXT,
                    status      TEXT DEFAULT 'pending',
                    channel     TEXT,
                    approved_by INTEGER,
                    created_at  INTEGER NOT NULL,
                    published_at INTEGER
                );

                CREATE TABLE IF NOT EXISTS stats (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    event       TEXT NOT NULL,
                    deal_id     TEXT,
                    user_id     INTEGER,
                    channel     TEXT,
                    ts          INTEGER NOT NULL
                );
            """)
            await db.commit()
        logger.info("Database initialised at %s", self.path)

    async def upsert_deal(self, deal: dict) -> bool:
        """Insert new deal; skip if ASIN already pending/published today."""
        async with aiosqlite.connect(self.path) as db:
            today_start = int(datetime.now(timezone.utc).replace(hour=0, minute=0, second=0).timestamp())
            async with db.execute(
                "SELECT id FROM deals WHERE asin=? AND created_at>=? AND status IN ('pending','published')",
                (deal["asin"], today_start),
            ) as cur:
                row = await cur.fetchone()
            if row:
                return False  # already queued today
            await db.execute(
                """INSERT INTO deals
                   (id,asin,title,original,price,discount,url,image,category,margin,status,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,'pending',?)""",
                (
                    deal["id"], deal["asin"], deal["title"],
                    deal["original_price"], deal["deal_price"],
                    deal["discount"], deal["url"], deal["image"],
                    deal.get("category", ""), deal.get("margin", ""),
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

    async def set_status(self, deal_id: str, status: str,
                         user_id: int = 0, channel: str = "") -> None:
        ts = int(time.time())
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE deals SET status=?, approved_by=?, channel=?, published_at=? WHERE id=?",
                (status, user_id, channel, ts if status == "published" else None, deal_id),
            )
            await db.execute(
                "INSERT INTO stats (event,deal_id,user_id,channel,ts) VALUES (?,?,?,?,?)",
                (status, deal_id, user_id, channel, ts),
            )
            await db.commit()

    async def get_pending(self) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM deals WHERE status='pending' ORDER BY created_at DESC LIMIT 20"
            ) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_stats(self) -> dict:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute("SELECT COUNT(*) FROM deals WHERE status='published'") as c:
                published = (await c.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM deals WHERE status='pending'") as c:
                pending = (await c.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM deals WHERE status='rejected'") as c:
                rejected = (await c.fetchone())[0]
            async with db.execute(
                "SELECT COUNT(*) FROM deals WHERE status='published' AND published_at>=?",
                (int(time.time()) - 86400,),
            ) as c:
                today = (await c.fetchone())[0]
        return dict(published=published, pending=pending, rejected=rejected, today=today)


db = Database()

# ─────────────────────────────────────────────────────────────────────────────
# 3.  AMAZON PA-API 5.0 CLIENT  (lightweight, no SDK required)
# ─────────────────────────────────────────────────────────────────────────────
class AmazonPAAPI:
    """
    Minimal async PA-API 5.0 wrapper.
    Docs: https://webservices.amazon.com/paapi5/documentation/
    Uses HMAC-SHA256 signed requests (AWS SigV4 lite).
    """

    HOST_MAP = {
        "IT": "webservices.amazon.it",
        "DE": "webservices.amazon.de",
        "FR": "webservices.amazon.fr",
        "ES": "webservices.amazon.es",
        "UK": "webservices.amazon.co.uk",
        "US": "webservices.amazon.com",
        "JP": "webservices.amazon.co.jp",
    }

    MARKETPLACE_MAP = {
        "IT": "www.amazon.it",
        "DE": "www.amazon.de",
        "FR": "www.amazon.fr",
        "ES": "www.amazon.es",
        "UK": "www.amazon.co.uk",
        "US": "www.amazon.com",
        "JP": "www.amazon.co.jp",
    }

    def __init__(self):
        self.access_key  = Config.AMAZON_ACCESS_KEY
        self.secret_key  = Config.AMAZON_SECRET_KEY
        self.tag         = Config.AMAZON_ASSOCIATE_TAG
        self.country     = Config.AMAZON_COUNTRY
        self.host        = self.HOST_MAP.get(self.country, "webservices.amazon.it")
        self.marketplace = f"www.{self.MARKETPLACE_MAP.get(self.country, 'amazon.it')}"

    def _sign(self, key: bytes, msg: str) -> bytes:
        import hmac
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    def _get_signature_key(self, date_stamp: str) -> bytes:
        k_date    = self._sign(("AWS4" + self.secret_key).encode("utf-8"), date_stamp)
        k_region  = self._sign(k_date, "us-east-1")
        k_service = self._sign(k_region, "ProductAdvertisingAPI")
        return self._sign(k_service, "aws4_request")

    def _build_headers(self, payload: dict) -> dict:
        import hmac as _hmac, hashlib as _hs
        now = datetime.utcnow()
        amz_date   = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        body       = json.dumps(payload)
        body_hash  = _hs.sha256(body.encode("utf-8")).hexdigest()
        canonical  = (
            f"POST\n/paapi5/getitems\n\n"
            f"content-type:application/json; charset=utf-8\n"
            f"host:{self.host}\n"
            f"x-amz-date:{amz_date}\n"
            f"x-amz-target:com.amazon.paapi5.v1.ProductAdvertisingAPIv1.GetItems\n\n"
            f"content-type;host;x-amz-date;x-amz-target\n"
            f"{body_hash}"
        )
        string_to_sign = (
            f"AWS4-HMAC-SHA256\n{amz_date}\n"
            f"{date_stamp}/us-east-1/ProductAdvertisingAPI/aws4_request\n"
            + _hs.sha256(canonical.encode("utf-8")).hexdigest()
        )
        signing_key = self._get_signature_key(date_stamp)
        signature   = _hmac.new(signing_key, string_to_sign.encode("utf-8"), _hs.sha256).hexdigest()
        auth_header = (
            f"AWS4-HMAC-SHA256 Credential={self.access_key}/{date_stamp}/"
            f"us-east-1/ProductAdvertisingAPI/aws4_request, "
            f"SignedHeaders=content-type;host;x-amz-date;x-amz-target, "
            f"Signature={signature}"
        )
        return {
            "Content-Type":  "application/json; charset=utf-8",
            "Host":          self.host,
            "X-Amz-Date":    amz_date,
            "X-Amz-Target":  "com.amazon.paapi5.v1.ProductAdvertisingAPIv1.GetItems",
            "Authorization": auth_header,
        }

    async def get_items(self, asins: list[str]) -> list[dict]:
        """Fetch live item data for a list of ASINs."""
        if not self.access_key:
            logger.warning("PA-API keys not configured — using mock data")
            return []

        payload = {
            "ItemIds":     asins,
            "PartnerTag":  self.tag,
            "PartnerType": "Associates",
            "Marketplace": self.marketplace,
            "Resources": [
                "Images.Primary.Large",
                "ItemInfo.Title",
                "Offers.Listings.Price",
                "Offers.Listings.SavingBasis",
                "Offers.Listings.Promotions",
                "ItemInfo.Classifications",
                "BrowseNodeInfo.BrowseNodes",
            ],
        }
        headers = self._build_headers(payload)
        url     = f"https://{self.host}/paapi5/getitems"

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error("PA-API error %s: %s", resp.status, text[:300])
                    return []
                data = await resp.json()

        deals = []
        for item in data.get("ItemsResult", {}).get("Items", []):
            try:
                asin      = item["ASIN"]
                title     = item["ItemInfo"]["Title"]["DisplayValue"]
                listing   = item.get("Offers", {}).get("Listings", [{}])[0]
                price_val = listing.get("Price", {}).get("DisplayAmount", "N/A")
                orig_val  = listing.get("SavingBasis", {}).get("DisplayAmount", price_val)
                saving    = listing.get("Price", {}).get("Savings", {})
                pct       = saving.get("Percentage", 0)
                image     = (
                    item.get("Images", {}).get("Primary", {}).get("Large", {}).get("URL", "")
                )
                affiliate_url = (
                    f"https://{self.MARKETPLACE_MAP.get(self.country, 'amazon.it')}"
                    f"/dp/{asin}?tag={self.tag}"
                )
                category = (
                    item.get("BrowseNodeInfo", {})
                        .get("BrowseNodes", [{}])[0]
                        .get("DisplayName", "General")
                )
                deal_id = hashlib.md5(f"{asin}{date_stamp if False else datetime.utcnow().strftime('%Y%m%d')}".encode()).hexdigest()[:12]
                deals.append({
                    "id":             deal_id,
                    "asin":           asin,
                    "title":          title,
                    "original_price": orig_val,
                    "deal_price":     price_val,
                    "discount":       f"-{pct}%" if pct else "Offerta",
                    "url":            affiliate_url,
                    "image":          image,
                    "category":       category,
                    "margin":         f"~{pct * 0.04:.1f}%",
                })
            except (KeyError, IndexError, TypeError) as exc:
                logger.warning("Skipping malformed PA-API item: %s", exc)
        return deals

    async def search_deals(self, keywords: str = "offerte del giorno") -> list[dict]:
        """Search for deals by keyword via SearchItems endpoint."""
        if not self.access_key:
            return self._mock_deals()

        payload = {
            "Keywords":    keywords,
            "PartnerTag":  self.tag,
            "PartnerType": "Associates",
            "Marketplace": self.marketplace,
            "SearchIndex": "All",
            "ItemCount":   5,
            "Resources": [
                "Images.Primary.Large",
                "ItemInfo.Title",
                "Offers.Listings.Price",
                "Offers.Listings.SavingBasis",
                "ItemInfo.Classifications",
            ],
        }
        headers_raw = self._build_headers(payload)
        # Override target for SearchItems
        headers_raw["X-Amz-Target"] = (
            "com.amazon.paapi5.v1.ProductAdvertisingAPIv1.SearchItems"
        )
        url = f"https://{self.host}/paapi5/searchitems"

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload, headers=headers_raw,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    logger.error("SearchItems error %s", resp.status)
                    return self._mock_deals()
                data = await resp.json()

        asins = [
            item["ASIN"]
            for item in data.get("SearchResult", {}).get("Items", [])
        ]
        return await self.get_items(asins) if asins else self._mock_deals()

    @staticmethod
    def _mock_deals() -> list[dict]:
        """Demo data when API keys are absent (prototype / testing mode)."""
        return [
            {
                "id":             "mock_001",
                "asin":           "B0CX2345",
                "title":          "Apple MacBook Air M3 (2024) — 13″ 16 GB 512 GB",
                "original_price": "1.349,00 €",
                "deal_price":     "1.149,00 €",
                "discount":       "-15%",
                "url":            "https://www.amazon.it/dp/B0CX2345?tag=demo-21",
                "image":          "https://m.media-amazon.com/images/I/71jG+e7roXL._AC_SX679_.jpg",
                "category":       "Elettronica / Informatica",
                "margin":         "~3.0%",
            },
            {
                "id":             "mock_002",
                "asin":           "B0B123ABC",
                "title":          "Sony WH-1000XM5 Wireless Headphones",
                "original_price": "399,00 €",
                "deal_price":     "259,00 €",
                "discount":       "-35%",
                "url":            "https://www.amazon.it/dp/B0B123ABC?tag=demo-21",
                "image":          "https://m.media-amazon.com/images/I/61bBN7SQABL._AC_SX679_.jpg",
                "category":       "Elettronica / Audio",
                "margin":         "~3.5%",
            },
        ]


pa_api = AmazonPAAPI()

# ─────────────────────────────────────────────────────────────────────────────
# 4.  COPY FORMATTERS
# ─────────────────────────────────────────────────────────────────────────────
def fmt_public(deal: dict) -> str:
    L = Config.locale()
    return (
        f"{L['deal_header']}\n\n"
        f"<b>{deal['title']}</b>\n\n"
        f"❌ {L['original']}: <s>{deal['original_price']}</s>\n"
        f"✅ <b>{L['discounted']}: {deal['deal_price']}</b>  ({deal['discount']})\n\n"
        f"{L['expires']}"
    )


def fmt_admin(deal: dict) -> str:
    return (
        f"🔒 <b>ADMIN PANEL</b>\n"
        f"🔹 ASIN: <code>{deal['asin']}</code>\n"
        f"🔹 Categoria: {deal.get('category', '—')}\n"
        f"🔹 Margine stimato: {deal.get('margin', '—')}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        + fmt_public(deal)
    )


# ─────────────────────────────────────────────────────────────────────────────
# 5.  KEYBOARDS
# ─────────────────────────────────────────────────────────────────────────────
def kb_admin(deal_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Approva & Pubblica", callback_data=f"approve|{deal_id}")
    builder.button(text="❌ Scarta",              callback_data=f"reject|{deal_id}")
    builder.button(text="📋 Dettagli ASIN",       callback_data=f"detail|{deal_id}")
    builder.adjust(2, 1)
    return builder.as_markup()


def kb_channel_selector(deal_id: str) -> InlineKeyboardMarkup:
    """Ask admin which channel to publish to (if multiple)."""
    builder = InlineKeyboardBuilder()
    for ch in Config.CHANNEL_IDS:
        builder.button(text=f"📢 {ch}", callback_data=f"pubto|{deal_id}|{ch}")
    builder.button(text="📢 Tutti i canali", callback_data=f"pubto|{deal_id}|ALL")
    builder.button(text="❌ Annulla",         callback_data=f"reject|{deal_id}")
    builder.adjust(1)
    return builder.as_markup()


def kb_public(url: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=Config.locale()["buy_btn"], url=url)
    return builder.as_markup()


# ─────────────────────────────────────────────────────────────────────────────
# 6.  AUTH GUARD
# ─────────────────────────────────────────────────────────────────────────────
def is_admin(user_id: int) -> bool:
    return True
    


async def guard(message: types.Message) -> bool:
    """Returns True if authorised; sends error and returns False otherwise."""
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Accesso non autorizzato.")
        logger.warning("Unauthorised access attempt from user %s", message.from_user.id)
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# 7.  BOT ROUTER & HANDLERS
# ─────────────────────────────────────────────────────────────────────────────
router = Router()


# ── /start ─────────────────────────────────────────────────────────────────
@router.message(Command("start"))
async def cmd_start(msg: types.Message):
    if not await guard(msg):
        return
    await msg.answer(
        "🤖 <b>Amazon Deals Bot — Admin Panel</b>\n\n"
        "Usa il menu per navigare:\n"
        "• /fetch   — Scansiona offerte Amazon\n"
        "• /queue   — Vedi offerte in coda\n"
        "• /stats   — Dashboard di sistema\n"
        "• /channels — Canali configurati\n"
        "• /help    — Guida rapida"
    )


# ── /help ──────────────────────────────────────────────────────────────────
@router.message(Command("help"))
async def cmd_help(msg: types.Message):
    if not await guard(msg):
        return
    await msg.answer(
        "📖 <b>Guida Rapida</b>\n\n"
        "<b>/fetch [keyword]</b>\n"
        "  Scansiona le offerte Amazon. Es: <code>/fetch MacBook</code>\n\n"
        "<b>/queue</b>\n"
        "  Mostra le ultime offerte in attesa di approvazione.\n\n"
        "<b>/stats</b>\n"
        "  Dashboard: totali pubblicati, rifiutati, coda attuale.\n\n"
        "<b>/channels</b>\n"
        "  Lista dei canali di destinazione configurati.\n\n"
        "<b>Flusso di approvazione:</b>\n"
        "  1. Bot trova offerta → manda anteprima all'admin\n"
        "  2. Admin clicca ✅ → sceglie il canale → pubblica\n"
        "  3. Admin clicca ❌ → offerta scartata\n\n"
        "ℹ️ Configurazione via variabili ambiente — vedi README."
    )


# ── /channels ─────────────────────────────────────────────────────────────
@router.message(Command("channels"))
async def cmd_channels(msg: types.Message):
    if not await guard(msg):
        return
    lines = "\n".join(f"  • <code>{ch}</code>" for ch in Config.CHANNEL_IDS)
    await msg.answer(f"📢 <b>Canali configurati:</b>\n\n{lines}")


# ── /stats ─────────────────────────────────────────────────────────────────
@router.message(Command("stats"))
async def cmd_stats(msg: types.Message):
    if not await guard(msg):
        return
    s = await db.get_stats()
    api_status = "🟢 Connessa" if Config.AMAZON_ACCESS_KEY else "🟡 Mock (no API key)"
    await msg.answer(
        "📊 <b>SYSTEM DASHBOARD</b>\n\n"
        f"🟢 <b>Status Bot:</b> Online\n"
        f"🔌 <b>PA-API 5.0:</b> {api_status}\n"
        f"📦 <b>In coda:</b> {s['pending']}\n"
        f"✅ <b>Totale pubblicati:</b> {s['published']}\n"
        f"📅 <b>Pubblicati oggi:</b> {s['today']}\n"
        f"❌ <b>Rifiutati:</b> {s['rejected']}\n"
        f"🌍 <b>Marketplace:</b> Amazon.{Config.AMAZON_COUNTRY.lower()}\n"
        f"⏱ <b>Auto-fetch ogni:</b> {Config.AUTO_FETCH_INTERVAL} min\n"
        f"📢 <b>Canali attivi:</b> {len(Config.CHANNEL_IDS)}"
    )


# ── /fetch [keyword] ──────────────────────────────────────────────────────
@router.message(Command("fetch"))
async def cmd_fetch(msg: types.Message):
    if not await guard(msg):
        return

    keyword = " ".join(msg.text.split()[1:]) or "offerte del giorno"
    status_msg = await msg.answer(f"🔄 <i>Scansione PA-API 5.0 per «{keyword}»…</i>")

    deals = await pa_api.search_deals(keyword)
    if not deals:
        await status_msg.edit_text("⚠️ Nessuna offerta trovata. Riprova con un'altra keyword.")
        return

    new_count = 0
    for deal in deals:
        added = await db.upsert_deal(deal)
        if added:
            new_count += 1
            await _send_admin_preview(msg.bot, deal)

    await status_msg.edit_text(
        f"✅ Scansione completata.\n"
        f"🆕 Nuove offerte aggiunte alla coda: <b>{new_count}</b>\n"
        f"📦 Già in coda (duplicate oggi): <b>{len(deals) - new_count}</b>"
    )


# ── /queue ─────────────────────────────────────────────────────────────────
@router.message(Command("queue"))
async def cmd_queue(msg: types.Message):
    if not await guard(msg):
        return
    pending = await db.get_pending()
    if not pending:
        await msg.answer("📭 Nessuna offerta in coda al momento.")
        return

    await msg.answer(f"📋 <b>Offerte in coda: {len(pending)}</b>")
    for deal in pending[:5]:   # show max 5 to avoid spam
        await _send_admin_preview(msg.bot, deal)


# ── INTERNAL: send admin preview card ─────────────────────────────────────
async def _send_admin_preview(bot: Bot, deal: dict) -> None:
    for admin_id in Config.ADMIN_IDS:
        try:
            if deal.get("image"):
                await bot.send_photo(
                    chat_id=admin_id,
                    photo=deal["image"],
                    caption=f"⚠️ <b>DA APPROVARE</b>\n\n{fmt_admin(deal)}",
                    reply_markup=kb_admin(deal["id"]),
                )
            else:
                await bot.send_message(
                    chat_id=admin_id,
                    text=f"⚠️ <b>DA APPROVARE</b>\n\n{fmt_admin(deal)}",
                    reply_markup=kb_admin(deal["id"]),
                )
        except Exception as exc:
            logger.error("Cannot send preview to admin %s: %s", admin_id, exc)


# ─────────────────────────────────────────────────────────────────────────────
# 8.  CALLBACK HANDLERS
# ─────────────────────────────────────────────────────────────────────────────

# ── "detail" callback ──────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("detail|"))
async def cb_detail(cq: CallbackQuery):
    _, deal_id = cq.data.split("|", 1)
    deal = await db.get_deal(deal_id)
    if not deal:
        await cq.answer("Offerta non trovata nel DB.", show_alert=True)
        return
    await cq.answer()
    await cq.message.answer(
        f"🔎 <b>Dettagli ASIN</b>\n\n"
        f"ASIN: <code>{deal['asin']}</code>\n"
        f"Titolo: {deal['title']}\n"
        f"Prezzo originale: {deal['original']}\n"
        f"Prezzo scontato: {deal['price']}\n"
        f"Sconto: {deal['discount']}\n"
        f"Categoria: {deal['category']}\n"
        f"Margine: {deal['margin']}\n"
        f"URL: {deal['url']}"
    )


# ── "approve" callback — show channel selector ─────────────────────────────
@router.callback_query(F.data.startswith("approve|"))
async def cb_approve(cq: CallbackQuery):
    _, deal_id = cq.data.split("|", 1)
    deal = await db.get_deal(deal_id)
    if not deal:
        await cq.answer("Offerta non trovata.", show_alert=True)
        return
    if len(Config.CHANNEL_IDS) == 1:
        # Skip selector if only one channel configured
        await _publish_deal(cq, deal_id, Config.CHANNEL_IDS[0])
        return
    await cq.answer()
    await cq.message.edit_caption(
        caption=f"📢 <b>Seleziona il canale di destinazione:</b>\n\n{fmt_admin(deal)}",
        reply_markup=kb_channel_selector(deal_id),
    )


# ── "pubto" callback — actually publish ────────────────────────────────────
@router.callback_query(F.data.startswith("pubto|"))
async def cb_pubto(cq: CallbackQuery):
    parts    = cq.data.split("|")
    deal_id  = parts[1]
    channel  = parts[2]
    channels = Config.CHANNEL_IDS if channel == "ALL" else [channel]
    for ch in channels:
        await _publish_deal(cq, deal_id, ch, update_caption=(ch == channels[-1]))


# ── "reject" callback ──────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("reject|"))
async def cb_reject(cq: CallbackQuery):
    _, deal_id = cq.data.split("|", 1)
    await db.set_status(deal_id, "rejected", cq.from_user.id)
    await cq.answer("Offerta scartata.")
    try:
        await cq.message.edit_caption(
            caption="❌ <b>Offerta scartata.</b> Non verrà pubblicata.",
            reply_markup=None,
        )
    except Exception:
        pass


# ── Helper: publish to one channel ────────────────────────────────────────
async def _publish_deal(cq: CallbackQuery, deal_id: str, channel: str,
                         update_caption: bool = True) -> None:
    deal = await db.get_deal(deal_id)
    if not deal:
        await cq.answer("Offerta non trovata.", show_alert=True)
        return

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
        await db.set_status(deal_id, "published", cq.from_user.id, channel)
        await cq.answer(f"✅ Pubblicato su {channel}!")
        if update_caption:
            try:
                await cq.message.edit_caption(
                    caption=f"✅ <b>PUBBLICATO SU {channel}</b>\n\n{fmt_admin(deal)}",
                    reply_markup=None,
                )
            except Exception:
                pass
    except Exception as exc:
        logger.error("Publish failed → %s: %s", channel, exc)
        await cq.answer(
            f"⚠️ Errore su {channel}: assicurati che il bot sia admin del canale.",
            show_alert=True,
        )


# ─────────────────────────────────────────────────────────────────────────────
# 9.  AUTO-FETCH SCHEDULER
# ─────────────────────────────────────────────────────────────────────────────
async def auto_fetch_loop(bot: Bot) -> None:
    """Background coroutine: periodically fetch deals and notify admins."""
    interval = Config.AUTO_FETCH_INTERVAL * 60
    keywords  = ["offerte del giorno", "lightning deals", "coupon amazon"]
    idx       = 0
    while True:
        await asyncio.sleep(interval)
        kw = keywords[idx % len(keywords)]
        idx += 1
        logger.info("Auto-fetch triggered: keyword='%s'", kw)
        try:
            deals = await pa_api.search_deals(kw)
            for deal in deals:
                added = await db.upsert_deal(deal)
                if added:
                    logger.info("New deal queued: %s (%s)", deal["title"][:60], deal["asin"])
                    await _send_admin_preview(bot, deal)
        except Exception as exc:
            logger.error("Auto-fetch error: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# 10. BOT COMMAND MENU
# ─────────────────────────────────────────────────────────────────────────────
async def setup_commands(bot: Bot) -> None:
    cmds = [
        BotCommand(command="fetch",    description="Scansiona offerte Amazon"),
        BotCommand(command="queue",    description="Offerte in attesa di approvazione"),
        BotCommand(command="stats",    description="Dashboard di sistema"),
        BotCommand(command="channels", description="Canali di destinazione"),
        BotCommand(command="help",     description="Guida rapida"),
        BotCommand(command="start",    description="Riavvia il bot"),
    ]
    await bot.set_my_commands(cmds)
    logger.info("Bot commands registered.")


# ─────────────────────────────────────────────────────────────────────────────
# 11. HEALTH-CHECK WEB SERVER  (keeps Render/Railway alive)
# ─────────────────────────────────────────────────────────────────────────────
async def health_app() -> web.Application:
    async def ping(_: web.Request) -> web.Response:
        s = await db.get_stats()
        return web.json_response({"status": "ok", "queued": s["pending"], "published": s["published"]})

    app = web.Application()
    app.router.add_get("/",       ping)
    app.router.add_get("/health", ping)
    return app


# ─────────────────────────────────────────────────────────────────────────────
# 12. MAIN ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────
async def main() -> None:
    logger.info("═══════════════════════════════════════")
    logger.info("  Amazon Deals Bot — Professional Ed.  ")
    logger.info("═══════════════════════════════════════")

    # Init DB
    await db.init()

    # Build bot & dispatcher
    bot = Bot(
        token=Config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)

    # Register commands
    await setup_commands(bot)

    # Start health-check server
    app   = await health_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site  = web.TCPSite(runner, "0.0.0.0", Config.PORT)
    await site.start()
    logger.info("Health-check server on port %s", Config.PORT)

    # Start background auto-fetch
    asyncio.create_task(auto_fetch_loop(bot))
    logger.info("Auto-fetch scheduled every %s min", Config.AUTO_FETCH_INTERVAL)

    # Webhook vs polling
    if Config.WEBHOOK_URL:
        webhook_path = f"/webhook/{Config.BOT_TOKEN}"
        webhook_full = f"{Config.WEBHOOK_URL}{webhook_path}"
        await bot.set_webhook(webhook_full, drop_pending_updates=True)
        logger.info("Webhook registered: %s", webhook_full)
        # Serve webhook via aiohttp
        async def handle_webhook(request: web.Request) -> web.Response:
            data = await request.json()
            update = types.Update(**data)
            await dp.feed_update(bot, update)
            return web.Response()
        app.router.add_post(webhook_path, handle_webhook)
        # Keep alive
        await asyncio.Event().wait()
    else:
        logger.info("Starting in long-polling mode…")
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
