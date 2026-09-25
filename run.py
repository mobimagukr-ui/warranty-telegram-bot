import asyncio
import logging
import os
import secrets

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Update
from dotenv import load_dotenv

load_dotenv()

from bot import (
    ADMIN_IDS,
    BOT_TOKEN,
    init_db,
    np_track,
    overdue_notify,
    router,
)

logging.basicConfig(level=logging.INFO)

PORT = int(os.getenv("PORT", "10000"))
BASE_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

WEBHOOK_PATH = "/webhook/" + secrets.token_urlsafe(32)


async def health(request):
    return web.Response(text="OK")


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задано в Render Environment")

    init_db()

    bot = Bot(
        BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dp = Dispatcher()
    dp.include_router(router)

    if not BASE_URL:
        raise RuntimeError("RENDER_EXTERNAL_URL не знайдено")

    webhook_url = BASE_URL + WEBHOOK_PATH

    await bot.set_webhook(
        webhook_url,
        drop_pending_updates=True,
    )

    app = web.Application()

    async def telegram_webhook(request):
        try:
            data = await request.json()
            update = Update.model_validate(data)
            await dp.feed_update(bot, update)
            return web.Response(text="OK")
        except Exception:
            logging.exception("Telegram webhook error")
            return web.Response(status=500, text="ERROR")

    app.router.add_get("/", health)
    app.router.add_post(WEBHOOK_PATH, telegram_webhook)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT,
    )

    tasks = []
    tasks.append(asyncio.create_task(overdue_notify(bot)))

    if os.getenv("NP_API_KEY"):
        tasks.append(asyncio.create_task(np_track()))

    await site.start()

    logging.info("Warranty bot started on port %s", PORT)
    logging.info("Telegram webhook configured")

    try:
        await asyncio.Event().wait()
    finally:
        for task in tasks:
            task.cancel()

        await bot.delete_webhook(
            drop_pending_updates=False
        )

        await bot.session.close()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
