```python
import asyncio
import logging
import os
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.enums import ParseMode

# ==========================================
# CONFIGURATION 
# ==========================================
# Securely fetch token from Render Environment Variables
BOT_TOKEN = os.getenv("BOT_TOKEN") 

# Securely fetch channel ID
CHANNEL_ID = os.getenv("CHANNEL_ID", "@alans_deals_test")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - [%(levelname)s] - %(message)s")
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()

# ==========================================
# MOCK DATABASE
# ==========================================
pending_deals = {
    "deal_001": {
        "title": "Apple MacBook Air M3 (2024)",
        "original_price": "1.349,00 €",
        "deal_price": "1.149,00 €",
        "discount": "-15%",
        "url": "https://www.amazon.it/dp/B0CX2345?tag=tuo_codice-21",
        "image": "https://m.media-amazon.com/images/I/71jG+e7roXL._AC_SX679_.jpg"
    }
}

# ==========================================
# UI KEYBOARDS & COPY
# ==========================================
def build_admin_keyboard(deal_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Approva & Pubblica", callback_data=f"approve_{deal_id}")
    builder.button(text="❌ Rifiuta Offerta", callback_data=f"reject_{deal_id}")
    builder.adjust(2)
    return builder.as_markup()

def build_public_keyboard(url: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🛒 Acquista Ora su Amazon", url=url)
    return builder.as_markup()

def format_deal_copy(deal: dict) -> str:
    return (
        f"🔥 <b>MINIMO STORICO AMAZON</b> 🔥\n\n"
        f"💻 <b>{deal['title']}</b>\n\n"
        f"❌ Prezzo precedente: <s>{deal['original_price']}</s>\n"
        f"✅ <b>Prezzo Scontato: {deal['deal_price']}</b> ({deal['discount']})\n\n"
        f"⏳ <i>Scade a breve! Clicca il link in basso per acquistarlo:</i>"
    )

# ==========================================
# BOT HANDLERS
# ==========================================
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("🛠 <b>Amazon Deal Engine Online.</b>\nType /admin to view pending deals.")

@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    await message.answer("🔄 <i>Scansione API Amazon PA 5.0 in corso...</i>")
    await asyncio.sleep(1.5) 
    
    deal_id = "deal_001"
    deal = pending_deals[deal_id]
    
    await message.answer_photo(
        photo=deal['image'],
        caption=f"⚠️ <b>DA APPROVARE</b> ⚠️\n\n" + format_deal_copy(deal),
        reply_markup=build_admin_keyboard(deal_id)
    )

@dp.callback_query(F.data.startswith("approve_"))
async def process_approval(callback: CallbackQuery):
    deal_id = callback.data.split("_")[1]
    deal = pending_deals[deal_id]
    
    # 1. Update Admin panel to show success
    await callback.message.edit_caption(
        caption=f"✅ <b>PUBBLICATO NEL CANALE IT!</b>\n\n" + format_deal_copy(deal),
        reply_markup=None
    )
    
    # 2. ACTUALLY PUSH TO THE PUBLIC CHANNEL
    try:
        await bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=deal['image'],
            caption=format_deal_copy(deal),
            reply_markup=build_public_keyboard(deal['url'])
        )
        await callback.answer("Inviato al canale con successo!")
    except Exception as e:
        logger.error(f"Failed to post to channel: {e}")
        await callback.answer("Errore di pubblicazione! Check channel permissions.", show_alert=True)

@dp.callback_query(F.data.startswith("reject_"))
async def process_rejection(callback: CallbackQuery):
    await callback.message.edit_caption(
        caption="❌ <b>Offerta Scartata.</b> Non verrà pubblicata.",
        reply_markup=None
    )
    await callback.answer("Offerta cancellata.")

# ==========================================
# EXECUTION
# ==========================================
async def handle_ping(request):
    """Dummy web server response to keep Render health checks happy."""
    return web.Response(text="Amazon Bot is alive and running!")

async def main():
    logger.info("Starting Premium Bot Architecture...")
    
    # Start a dummy web server so Render doesn't crash the deploy
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



```
