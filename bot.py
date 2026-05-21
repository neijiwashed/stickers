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

logging.info("ПРИЛОЖЕНИЕ ЗАПУСКАЕТСЯ...")
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


class EditPack(StatesGroup):
    waiting_for_pack_link = State()
    waiting_for_sticker_photo = State()


class ClonePack(StatesGroup):
    waiting_for_source_link = State()


@dp.message(CommandStart())
async def start_command(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("Привет! К сожалению, этот бот настроен только для администратора.")
        return
    await message.answer(
        "Привет, админ! Бот готов к работе со стикерами.\n\n"
        "Доступные команды:\n"
        "/newpack — Создать совершенно новый стикерпак на вашем аккаунте\n"
        "/editpack — Добавить новые изображения в ваш существующий стикерпак\n"
        "/clonepack — Полностью скопировать чужой статический пак под ваше управление\n"
        "/cancel — Отменить текущий процесс ввода данных"
    )


def resize_image(image_bytes: bytes) -> bytes:
    img = Image.open(BytesIO(image_bytes))
    if img.format == "WEBP":
        img = img.convert("RGBA")
    width, height = img.size
    if width > height:
        new_width = 512
        new_height = int(round((height * 512) / width))
    else:
        new_height = 512
        new_width = int(round((width * 512) / height))
    img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
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
        "Шаг 1: Введите отображаемое название пака (например: Мои стикеры)."
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
        "Шаг 2: Теперь придумайте короткое имя для ссылки (только латиница, цифры, подчёркивания).\n\n"
        f"Автоматический суффикс: _by_{bot_user.username}"
    )
    await state.set_state(CreatePack.waiting_for_name)


@dp.message(CreatePack.waiting_for_name)
async def process_name(message: Message, state: FSMContext):
    short_name = message.text.strip() if message.text else ""
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9_]*$", short_name):
        await message.answer("Ошибка: Имя должно состоять из латинских букв и цифр.")
        return
    bot_user = await bot.get_me()
    full_pack_name = f"{short_name}_by_{bot_user.username}"
    if len(full_pack_name) > 64:
        await message.answer("Ошибка: Итоговая ссылка слишком длинная.")
        return
    await state.update_data(pack_name=full_pack_name)
    await message.answer("Шаг 3: Отправьте мне первую фотографию для этого пака.")
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
            stickers=[{"sticker": sticker_file, "emoji_list": ["💬"], "format": "static"}],
            sticker_format="static",
        )
        await status_msg.edit_text(f"Стикерпак успешно создан.\nСсылка: t.me/addstickers/{user_data['pack_name']}")
        await state.clear()
    except Exception as e:
        logging.error(e)
        if "STICKERSET_INVALID" in str(e) or "name is already taken" in str(e).lower():
            await status_msg.edit_text("Ошибка: Такое имя ссылки уже занято. Введите другое короткое имя:")
            await state.set_state(CreatePack.waiting_for_name)
        else:
            await status_msg.edit_text(f"Произошла ошибка: {str(e)[:200]}")
            await state.clear()


@dp.message(CreatePack.waiting_for_photo)
async def missing_photo(message: Message):
    await message.answer("Пожалуйста, отправьте именно фотографию.")


@dp.message(Command("editpack"))
async def start_pack_editing(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(
        "Отправьте мне ссылку на ваш стикерпак (например, t.me/addstickers/name_by_bot) "
        "или просто пришлите его техническое имя."
    )
    await state.set_state(EditPack.waiting_for_pack_link)


@dp.message(EditPack.waiting_for_pack_link)
async def process_pack_link(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Пожалуйста, отправьте ссылку текстом.")
        return
    raw_text = message.text.strip()
    pack_name = raw_text.split("/")[-1] if "/" in raw_text else raw_text
    bot_user = await bot.get_me()
    if not pack_name.endswith(f"_by_{bot_user.username}"):
        await message.answer(
            f"Этот пак создан не через меня!\n\n"
            f"Имя пака должно заканчиваться на _by_{bot_user.username}. "
            f"Я могу редактировать только те паки, которые я сам создал."
        )
        await state.clear()
        return
    try:
        await bot.get_sticker_set(name=pack_name)
        await state.update_data(edit_pack_name=pack_name)
        await message.answer(
            f"Пак {pack_name} успешно найден!\n\n"
            "Теперь отправьте мне фотографию, которую хотите добавить. "
            "Вы можете отправлять новые фото по одному. Когда закончите, просто введите /cancel"
        )
        await state.set_state(EditPack.waiting_for_sticker_photo)
    except Exception as e:
        logging.error(e)
        await message.answer("Ошибка: Не удалось найти этот стикерпак. Проверьте правильность ссылки.")
        await state.clear()


@dp.message(EditPack.waiting_for_sticker_photo, F.photo)
async def process_add_sticker(message: Message, state: FSMContext):
    status_msg = await message.answer("Добавляю стикер в пак, пожалуйста, подождите...")
    user_data = await state.get_data()
    pack_name = user_data["edit_pack_name"]
    try:
        photo_file = await bot.get_file(message.photo[-1].file_id)
        photo_bytes = await bot.download_file(photo_file.file_path)
        processed_png = resize_image(photo_bytes.read())
        sticker_file = BufferedInputFile(processed_png, filename="sticker.png")
        await bot.add_sticker_to_set(
            user_id=ADMIN_ID,
            name=pack_name,
            sticker={"sticker": sticker_file, "emoji_list": ["💬"], "format": "static"}
        )
        await status_msg.edit_text("Стикер успешно добавлен!\n\nВы можете отправить следующую фотографию или выйти через /cancel")
    except Exception as e:
        logging.error(e)
        await status_msg.edit_text(f"Не удалось добавить стикер. Ошибка: {str(e)[:200]}")


@dp.message(EditPack.waiting_for_sticker_photo)
async def missing_photo_edit(message: Message):
    await message.answer("Пожалуйста, отправьте фотографию для нового стикера или нажмите /cancel для выхода.")


@dp.message(Command("clonepack"))
async def start_pack_cloning(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(
        "Начинаем клонирование стикерпака.\n\n"
        "Отправьте мне ссылку на любой существующий статический стикерпак, который вы хотите скопировать."
    )
    await state.set_state(ClonePack.waiting_for_source_link)


@dp.message(ClonePack.waiting_for_source_link)
async def process_clone(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Пожалуйста, отправьте ссылку текстом.")
        return
    raw_text = message.text.strip()
    source_pack_name = raw_text.split("/")[-1] if "/" in raw_text else raw_text
    status_msg = await message.answer("Получаю информацию о стикерпаке...")
    try:
        source_set = await bot.get_sticker_set(name=source_pack_name)
        if source_set.is_animated or source_set.is_video:
            await status_msg.edit_text("Извините, бот поддерживает клонирование только обычных статических стикерпаков.")
            await state.clear()
            return
        if not source_set.stickers:
            await status_msg.edit_text("Этот стикерпак пуст.")
            await state.clear()
            return
        bot_user = await bot.get_me()
        clean_old_name = re.sub(r'[^a-zA-Z0-9_]', '', source_pack_name.split("_by_")[0])
        target_pack_name = f"clone_{clean_old_name}_by_{bot_user.username}"
        if len(target_pack_name) > 64:
            target_pack_name = f"c_{clean_old_name[:30]}_by_{bot_user.username}"
        await status_msg.edit_text(f"Найдено стикеров: {len(source_set.stickers)}. Начинаю копирование на ваш аккаунт, это займет какое-то время...")
        prepared_stickers = []
        for index, sticker in enumerate(source_set.stickers):
            try:
                file_info = await bot.get_file(sticker.file_id)
                file_bytes = await bot.download_file(file_info.file_path)
                processed_png = resize_image(file_bytes.read())
                sticker_file = BufferedInputFile(processed_png, filename=f"sticker_{index}.png")
                prepared_stickers.append({
                    "sticker": sticker_file,
                    "emoji_list": sticker.emoji if sticker.emoji else ["💬"],
                    "format": "static"
                })
            except Exception as file_err:
                logging.error(f"Ошибка при обработке отдельного стикера: {file_err}")
                continue
        if not prepared_stickers:
            await status_msg.edit_text("Не удалось обработать ни один стикер из этого пака.")
            await state.clear()
            return
        await bot.create_new_sticker_set(
            user_id=ADMIN_ID,
            name=target_pack_name,
            title=source_set.title,
            stickers=prepared_stickers,
            sticker_format="static"
        )
        await status_msg.edit_text(
            "Стикерпак успешно скопирован под ваше управление!\n\n"
            f"Новая ссылка: t.me/addstickers/{target_pack_name}\n\n"
            "Теперь вы сможете редактировать его через команду /editpack !"
        )
        await state.clear()
    except Exception as e:
        logging.exception("Критическая ошибка при клонировании пака:")
        await status_msg.edit_text(f"Не удалось скопировать пак. Короткая ошибка: {str(e)[:200]}")
        await state.clear()


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
