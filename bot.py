import asyncio
import logging
import os
import re
from io import BytesIO

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, Message
from aiohttp import web
from PIL import Image

logging.basicConfig(level=logging.INFO)

TOKEN = os.environ.get("TOKEN")
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", "0")

logging.info(f"ПРИЛОЖЕНИЕ ЗАПУСКАЕТСЯ...")
logging.info(f"Проверка TOKEN: {'НАЙДЕН' if TOKEN else 'НЕ НАЙДЕН!'}")
logging.info(f"Проверка ADMIN_ID: {ADMIN_ID_RAW}")

if not TOKEN or ADMIN_ID_RAW == "0":
    logging.error("КРИТИЧЕСКАЯ ОШИБКА: Переменные окружения TOKEN или ADMIN_ID пусты или настроены неверно!")
    ADMIN_ID = 0
else:
    ADMIN_ID = int(ADMIN_ID_RAW)

bot = Bot(token=TOKEN if TOKEN else "DUMMY")
dp = Dispatcher()


class CreatePack(StatesGroup):
    waiting_for_title = State()
    waiting_for_name = State()
    waiting_for_photo = State()


@dp.message(CommandStart())
async def start_command(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("Привет! К сожалению, этот бот настроен только для администратора.")
        return
    await message.answer(
        "Привет, админ! Бот успешно запущен на сервере и готов к работе.\n\n"
        "Чтобы создать новый стикерпак, отправь команду: /newpack"
    )


def resize_image(image_bytes: bytes) -> bytes:
    img = Image.open(BytesIO(image_bytes))
    img.thumbnail((512, 512))
    out_bytes = BytesIO()
    img.save(out_bytes, format="PNG")
    return out_bytes.getvalue()


@dp.message(Command("cancel"))
async def cancel_handler(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    current_state = await state.get_state()
    if current_state is None:
        return
    await state.clear()
    await message.answer("Действие отменено.")


@dp.message(Command("newpack"))
async def start_pack_creation(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(
        "Начинаем создание стикерпака. Для отмены в любой момент отправьте /cancel\n\n"
        "Шаг 1: Введите отображаемое название пака (например: Мои стикеры). "
        "Это имя будут видеть пользователи, оно может быть на любом языке."
    )
    await state.set_state(CreatePack.waiting_for_title)


@dp.message(CreatePack.waiting_for_title)
async def process_title(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Пожалуйста, введите название текстом.")
        return
    await state.update_data(pack_title=message.text)

    bot_user = await bot.get_me()
    await message.answer(
        "Шаг 2: Теперь придумайте короткое имя для ссылки. "
        "Оно должно начинаться с английской буквы и содержать только латиницу, цифры или нижние подчеркивания.\n\n"
        f"Обратите внимание: по правилам Telegram к ссылке автоматически добавится _by_{bot_user.username}"
    )
    await state.set_state(CreatePack.waiting_for_name)


@dp.message(CreatePack.waiting_for_name)
async def process_name(message: Message, state: FSMContext):
    short_name = message.text.strip() if message.text else ""
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9_]*$", short_name):
        await message.answer(
            "Ошибка: Имя должно начинаться с латинской буквы и не содержать пробелов или спецсимволов."
        )
        return

    bot_user = await bot.get_me()
    full_pack_name = f"{short_name}_by_{bot_user.username}"
    if len(full_pack_name) > 64:
        await message.answer(
            "Ошибка: Итоговая ссылка получается слишком длинной. Пожалуйста, придумайте имя короче."
        )
        return

    await state.update_data(pack_name=full_pack_name)
    await message.answer(
        f"Шаг 3: Ссылка будет выглядеть так: t.me/addstickers/{full_pack_name}\n\n"
        "Отправьте мне первую фотографию для этого пака."
    )
    await state.set_state(CreatePack.waiting_for_photo)


@dp.message(CreatePack.waiting_for_photo, F.photo)
async def process_photo_and_create(message: Message, state: FSMContext):
    status_msg = await message.answer("Создаю стикерпак, пожалуйста, подождите...")
    user_data = await state.get_data()

    try:
        photo_file = await bot.get_file(message.photo[-1].file_id)
        photo_bytes = await bot.download_file(photo_file.file_path)
        processed_png = resize_image(photo_bytes.read())
        sticker_file = BufferedInputFile(processed_png, filename="sticker.png")

        await bot.create_new_sticker_set(
            user_id=ADMIN_ID,
            name=user_data["pack_name"],
            title=user_data["pack_title"],
            stickers=[{"sticker": sticker_file, "emoji_list": ["✨"], "format": "static"}],
            sticker_format="static",
        )

        await status_msg.edit_text(
            "Стикерпак успешно создан.\n"
            f"Ссылка: t.me/addstickers/{user_data['pack_name']}"
        )
        await state.clear()
    except Exception as e:
        logging.error(e)
        if "STICKERSET_INVALID" in str(e) or "name is already taken" in str(e).lower():
            await status_msg.edit_text(
                "Ошибка: Такое имя ссылки уже занято. Введите другое короткое имя:"
            )
            await state.set_state(CreatePack.waiting_for_name)
        else:
            await status_msg.edit_text(f"Произошла ошибка: {e}")
            await state.clear()


@dp.message(CreatePack.waiting_for_photo)
async def missing_photo(message: Message):
    await message.answer("Пожалуйста, отправьте именно фотографию.")


async def handle_ping(request):
    return web.Response(text="Bot is alive!")


async def main():
    if TOKEN and ADMIN_ID != 0:
        asyncio.create_task(dp.start_polling(bot))

    app = web.Application()
    app.router.add_get("/", handle_ping)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
