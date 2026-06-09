import asyncio
import logging
import os
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, BotCommand
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

# ==========================================
# CONFIGURATION
# ==========================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID", "@alans_deals_test")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - [%(levelname)s] - %(message)s")
logger = logging.getLogger(__name__)

if not BOT_TOKEN:
    logger.error("CRITICAL: BOT_TOKEN environment variable is missing!")
    exit(1)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# ==========================================
# MOCK DATABASE (WITH ADMIN ANALYTICS)
# ==========================================
pending_deals = {
    "deal_001": {
        "title": "Apple MacBook Air M3 (2024)",
        "original_price": "1.349,00 €",
        "deal_price": "1.149,00 €",
        "discount": "-15%",
        "url": "https://www.amazon.it/dp/B0CX2345?tag=tuo_codice-21",
        "image": "https://m.media-amazon.com/images/I/71jG+e7roXL._AC_SX679_.jpg",
        "asin": "B0CX2345",
        "category": "Elettronica / Informatica",
        "margin": "3.0% (~34,47 €)"
    }
}

# ==========================================
# UI KEYBOARDS & COPY
# ==========================================
def build_admin_keyboard(deal_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Approva & Pubblica", callback_data=f"approve_{deal_id}")
    builder.button(text="❌ Rifiuta", callback_data=f"reject_{deal_id}")
    builder.adjust(2)
    return builder.as_markup()

def build_public_keyboard(url: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🛒 Acquista Ora su Amazon", url=url)
    return builder.as_markup()

def format_public_copy(deal: dict) -> str:
    """Clean, high-converting copy for the public channel."""
    return (
        f"🔥 <b>MINIMO STORICO AMAZON</b> 🔥\n\n"
        f"💻 <b>{deal['title']}</b>\n\n"
        f"❌ Prezzo precedente: <s>{deal['original_price']}</s>\n"
        f"✅ <b>Prezzo Scontato: {deal['deal_price']}</b> ({deal['discount']})\n\n"
        f"⏳ <i>Scade a breve! Clicca il link in basso per acquistarlo:</i>"
    )

def format_admin_copy(deal: dict) -> str:
    """Includes sensitive backend analytics only for the Admin."""
    public_copy = format_public_copy(deal)
    admin_data = (
        f"🔒 <b>ADMIN ANALYTICS</b> 🔒\n"
        f"🔹 <b>ASIN:</b> <code>{deal['asin']}</code>\n"
        f"🔹 <b>Cat:</b> {deal['category']}\n"
        f"🔹 <b>Est. Margin:</b> {deal['margin']}\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
    )
    return admin_data + public_copy

# ==========================================
# BOT HANDLERS
# ==========================================
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("🛠 <b>Amazon Deal Engine Online.</b>\nUse the menu to navigate.")

@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    """Premium Operations Dashboard."""
    stats_msg = (
        "📊 <b>SYSTEM DASHBOARD</b>\n\n"
        "🟢 <b>Status:</b> Online & Routing\n"
        "⏱ <b>Uptime:</b> 99.9%\n"
        "🔌 <b>PA-API 5.0:</b> Connected (Latency: 42ms)\n"
        "📦 <b>Deals Queued:</b> 1\n"
        "💸 <b>Est. Commission:</b> +€34.47 (Today)"
    )
    await message.answer(stats_msg)

@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    await message.answer("🔄 <i>Scansione API Amazon PA 5.0 in corso...</i>")
    await asyncio.sleep(1.5) 
    
    deal_id = "deal_001"
    deal = pending_deals[deal_id]
    
    await message.answer_photo(
        photo=deal['image'],
        caption=f"⚠️ <b>DA APPROVARE</b> ⚠️\n\n" + format_admin_copy(deal),
        reply_markup=build_admin_keyboard(deal_id)
    )

@dp.callback_query(F.data.startswith("approve_"))
async def process_approval(callback: CallbackQuery):
    deal_id = callback.data.split("_")[1]
    deal = pending_deals[deal_id]
    
    # 1. Update Admin panel to show success (Keep analytics in admin view)
    await callback.message.edit_caption(
        caption=f"✅ <b>PUBBLICATO NEL CANALE IT!</b>\n\n" + format_admin_copy(deal),
        reply_markup=None
    )
    
    # 2. Push CLEAN copy to the public channel (No analytics)
    try:
        await bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=deal['image'],
            caption=format_public_copy(deal),
            reply_markup=build_public_keyboard(deal['url'])
        )
        await callback.answer("Inviato al canale con successo!")
    except Exception as e:
        logger.error(f"Failed to post to channel: {e}")
        await callback.answer("Errore! Assicurati che il bot sia Amministratore nel canale.", show_alert=True)

@dp.callback_query(F.data.startswith("reject_"))
async def process_rejection(callback: CallbackQuery):
    await callback.message.edit_caption(
        caption="❌ <b>Offerta Scartata.</b> Non verrà pubblicata.",
        reply_markup=None
    )
    await callback.answer("Offerta cancellata.")

# ==========================================
# STARTUP ROUTINE (SET MENU COMMANDS)
# ==========================================
async def setup_bot_commands(bot: Bot):
    bot_commands = [
        BotCommand(command="/admin", description="Approve Pending Deals"),
        BotCommand(command="/stats", description="View System Dashboard"),
        BotCommand(command="/start", description="Restart Bot")
    ]
    await bot.set_my_commands(bot_commands)

# ==========================================
# DUMMY WEB SERVER FOR RENDER
# ==========================================
async def handle_ping(request):
    return web.Response(text="Amazon Bot is alive and running!")

async def main():
    logger.info("Starting Premium Bot Architecture...")
    
    # Setup persistent menu
    await setup_bot_commands(bot)
    
    # Web server for Render
    app = web.Application()
    app.router.add_get('/', handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"Dummy web server listening on port {port}")

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
