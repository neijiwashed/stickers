import asyncio
import logging
import os
import re
import sqlite3
import tempfile
from io import BytesIO
from typing import Literal, Tuple

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputSticker,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiohttp import web
from PIL import Image

logging.basicConfig(level=logging.INFO)

TOKEN = os.environ.get("TOKEN")
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", "0")

if not TOKEN or ADMIN_ID_RAW == "0":
    ADMIN_ID = 0
else:
    ADMIN_ID = int(ADMIN_ID_RAW)

bot = Bot(token=TOKEN if TOKEN else "DUMMY")
dp = Dispatcher()

DB_NAME = "stickers.db"


def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS user_packs (
                user_id INTEGER,
                pack_name TEXT PRIMARY KEY,
                pack_title TEXT
            )
        """
        )
        conn.commit()


def save_pack_to_db(user_id: int, pack_name: str, pack_title: str):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO user_packs (user_id, pack_name, pack_title)
            VALUES (?, ?, ?)
            ON CONFLICT(pack_name) DO UPDATE SET pack_title=excluded.pack_title
        """,
            (user_id, pack_name, pack_title),
        )
        conn.commit()


def get_user_packs(user_id: int):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT pack_name, pack_title FROM user_packs WHERE user_id = ?",
            (user_id,),
        )
        return cursor.fetchall()


def get_main_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.button(text="➕ Новый пак")
    builder.button(text="✏️ Добавить в пак")
    builder.button(text="📋 Клонировать пак")
    builder.button(text="❌ Отмена")
    builder.adjust(2, 2)
    return builder.as_markup(resize_keyboard=True)


def get_packs_inline_keyboard(user_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    packs = get_user_packs(user_id)

    for pack_name, pack_title in packs:
        builder.button(text=f"📦 {pack_title}", callback_data=f"select_pack:{pack_name}")

    builder.button(text="🔗 Ввести ссылку вручную", callback_data="manual_pack_input")
    builder.adjust(1)
    return builder.as_markup()


class CreatePack(StatesGroup):
    waiting_for_title = State()
    waiting_for_name = State()
    waiting_for_media = State()


class EditPack(StatesGroup):
    waiting_for_pack_link = State()
    waiting_for_media = State()


class ClonePack(StatesGroup):
    waiting_for_source_link = State()


def process_static_image(bio_object: BytesIO) -> bytes:
    bio_object.seek(0)
    img = Image.open(bio_object)

    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        img = img.convert("RGBA")
    else:
        img = img.convert("RGB")

    width, height = img.size
    if width > height:
        new_width = 512
        new_height = max(1, int(round((height * 512) / width)))
    else:
        new_height = 512
        new_width = max(1, int(round((width * 512) / height)))

    img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)

    out_bytes = BytesIO()
    img.save(out_bytes, format="PNG")
    return out_bytes.getvalue()


async def process_video_sticker(input_bytes: bytes) -> bytes:
    with (
        tempfile.NamedTemporaryFile(suffix=".input", delete=False) as in_file,
        tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as out_file,
    ):
        in_path = in_file.name
        out_path = out_file.name
        in_file.write(input_bytes)

    try:
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            in_path,
            "-t",
            "3.0",
            "-c:v",
            "libvpx-vp9",
            "-b:v",
            "256k",
            "-crf",
            "30",
            "-an",
            "-r",
            "30",
            "-vf",
            "scale='if(gt(iw,ih),512,-1)':'if(gt(iw,ih),-1,512)'",
            "-f",
            "webm",
            out_path,
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            raise RuntimeError(f"FFmpeg conversion error: {stderr.decode()}")

        with open(out_path, "rb") as f:
            processed_bytes = f.read()

        return processed_bytes

    finally:
        if os.path.exists(in_path):
            os.remove(in_path)
        if os.path.exists(out_path):
            os.remove(out_path)


async def download_and_process_media(
    message: Message,
) -> Tuple[BufferedInputFile, Literal["static", "video"], str]:
    emoji = "💬"

    if message.photo:
        file_id = message.photo[-1].file_id
        file_info = await bot.get_file(file_id)
        raw_bytes = await bot.download_file(file_info.file_path)
        processed = process_static_image(raw_bytes)
        return BufferedInputFile(processed, filename="sticker.png"), "static", emoji

    elif message.video or message.animation:
        doc = message.video or message.animation
        file_id = doc.file_id
        file_info = await bot.get_file(file_id)
        raw_bytes = await bot.download_file(file_info.file_path)
        processed = await process_video_sticker(raw_bytes.getvalue())
        return BufferedInputFile(processed, filename="sticker.webm"), "video", emoji

    elif message.sticker:
        emoji = message.sticker.emoji or "💬"
        file_id = message.sticker.file_id
        file_info = await bot.get_file(file_id)
        raw_bytes = await bot.download_file(file_info.file_path)

        if message.sticker.is_video or message.sticker.is_animated:
            processed = await process_video_sticker(raw_bytes.getvalue())
            return BufferedInputFile(processed, filename="sticker.webm"), "video", emoji
        else:
            processed = process_static_image(raw_bytes)
            return BufferedInputFile(processed, filename="sticker.png"), "static", emoji

    elif message.document:
        mime = message.document.mime_type or ""
        file_info = await bot.get_file(message.document.file_id)
        raw_bytes = await bot.download_file(file_info.file_path)

        if mime.startswith("video") or mime.startswith("image/gif") or mime == "video/webm":
            processed = await process_video_sticker(raw_bytes.getvalue())
            return BufferedInputFile(processed, filename="sticker.webm"), "video", emoji
        else:
            processed = process_static_image(raw_bytes)
            return BufferedInputFile(processed, filename="sticker.png"), "static", emoji

    raise ValueError("Неподдерживаемый формат медиафайла.")


@dp.message(CommandStart())
async def start_command(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.answer("Привет! К сожалению, этот бот доступен только для администратора.")
        return

    await state.clear()
    await message.answer(
        "👋 **Привет, админ!**\n\n"
        "Выберите нужное действие в меню ниже или воспользуйтесь командами:\n\n"
        "➕ **Новый пак** (`/newpack`)\n"
        "✏️ **Добавить в пак** (`/editpack`)\n"
        "📋 **Клонировать пак** (`/clonepack`)\n"
        "❌ **Отмена** (`/cancel`)\n\n"
        "💡 *Поддерживаются фото, видео, GIF и файлы. Видео дольше 3 сек обрезаются автоматически.*",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard(),
    )


@dp.message(F.text == "❌ Отмена")
@dp.message(Command("cancel"))
async def cancel_handler(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    current_state = await state.get_state()
    await state.clear()
    if current_state is None:
        await message.answer("Нет активного действия.", reply_markup=get_main_keyboard())
    else:
        await message.answer("Действие отменено.", reply_markup=get_main_keyboard())


@dp.message(F.text == "➕ Новый пак")
@dp.message(Command("newpack"))
async def start_pack_creation(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(
        "Начинаем создание стикерпака.\n\n"
        "**Шаг 1:** Введите отображаемое название пака (например: *Мои стикеры*).",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard(),
    )
    await state.set_state(CreatePack.waiting_for_title)


@dp.message(CreatePack.waiting_for_title)
async def process_title(message: Message, state: FSMContext):
    if not message.text or message.text == "❌ Отмена":
        return
    await state.update_data(pack_title=message.text)
    bot_user = await bot.get_me()
    await message.answer(
        "**Шаг 2:** Введите короткое имя для ссылки (только латиница, цифры, подчёркивания).\n\n"
        f"Суффикс добавится автоматически: `_by_{bot_user.username}`",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard(),
    )
    await state.set_state(CreatePack.waiting_for_name)


@dp.message(CreatePack.waiting_for_name)
async def process_name(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        return
    short_name = message.text.strip() if message.text else ""
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9_]*$", short_name):
        await message.answer("Ошибка: Имя должно состоять только из латинских букв, цифр и _.")
        return
    bot_user = await bot.get_me()
    full_pack_name = f"{short_name}_by_{bot_user.username}"
    if len(full_pack_name) > 64:
        await message.answer("Ошибка: Итоговое имя ссылки слишком длинное.")
        return
    await state.update_data(pack_name=full_pack_name)
    await message.answer(
        "**Шаг 3:** Отправьте мне **первый стикер** (Фото, Видео, GIF или файл).\n\n"
        "💡 *Если видео длится более 3 секунд, бот автоматически обрежет его.*",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard(),
    )
    await state.set_state(CreatePack.waiting_for_media)


@dp.message(CreatePack.waiting_for_media, F.photo | F.video | F.animation | F.document | F.sticker)
async def process_media_and_create(message: Message, state: FSMContext):
    status_msg = await message.answer("Обрабатываю медиафайл и создаю стикерпак...")
    user_data = await state.get_data()
    try:
        file, format_type, emoji = await download_and_process_media(message)

        await bot.create_new_sticker_set(
            user_id=ADMIN_ID,
            name=user_data["pack_name"],
            title=user_data["pack_title"],
            stickers=[InputSticker(sticker=file, emoji_list=[emoji], format=format_type)],
            sticker_format=format_type,
        )

        save_pack_to_db(ADMIN_ID, user_data["pack_name"], user_data["pack_title"])

        await status_msg.edit_text(
            f"✅ Стикерпак успешно создан и сохранён в ваш список!\n\n"
            f"Ссылка: t.me/addstickers/{user_data['pack_name']}"
        )
        await state.clear()
    except Exception as e:
        logging.error("Ошибка при создании пака: %s", e)
        if "STICKERSET_INVALID" in str(e) or "name is already taken" in str(e).lower():
            await status_msg.edit_text("Ошибка: Это имя уже занято. Введите другое короткое имя:")
            await state.set_state(CreatePack.waiting_for_name)
        else:
            await status_msg.edit_text(f"Произошла ошибка при обработке: {str(e)[:200]}")
            await state.clear()


@dp.message(F.text == "✏️ Добавить в пак")
@dp.message(Command("editpack"))
async def start_pack_editing(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    packs = get_user_packs(ADMIN_ID)
    if packs:
        await message.answer(
            "Выберите пак из списка ниже или введите ссылку вручную:",
            reply_markup=get_packs_inline_keyboard(ADMIN_ID),
        )
    else:
        await message.answer(
            "Отправьте ссылку на ваш стикерпак (например, `t.me/addstickers/name_by_bot`) "
            "или его короткое имя.",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard(),
        )
        await state.set_state(EditPack.waiting_for_pack_link)


@dp.callback_query(F.data.startswith("select_pack:"))
async def on_pack_selected(callback: CallbackQuery, state: FSMContext):
    pack_name = callback.data.split(":")[1]
    try:
        sticker_set = await bot.get_sticker_set(name=pack_name)
        await state.update_data(edit_pack_name=pack_name)

        await callback.message.edit_text(
            f"Выбран пак: *{sticker_set.title}* (`{pack_name}`)\n\n"
            "Отправляйте медиафайлы (Фото, GIF, Видео) по одному для добавления.\n"
            "Когда закончите, нажмите кнопку **«❌ Отмена»**.",
            parse_mode="Markdown",
        )
        await state.set_state(EditPack.waiting_for_media)
        await callback.answer()
    except Exception as e:
        logging.error("Ошибка при получении пака: %s", e)
        await callback.message.edit_text("Ошибка: Не удалось загрузить выбранный пак.")
        await callback.answer()


@dp.callback_query(F.data == "manual_pack_input")
async def on_manual_pack_input(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "Отправьте ссылку на ваш стикерпак (например, `t.me/addstickers/name_by_bot`) "
        "или его короткое имя.",
        parse_mode="Markdown",
    )
    await state.set_state(EditPack.waiting_for_pack_link)
    await callback.answer()


@dp.message(EditPack.waiting_for_pack_link)
async def process_pack_link(message: Message, state: FSMContext):
    if not message.text or message.text == "❌ Отмена":
        return
    raw_text = message.text.strip()
    pack_name = raw_text.split("/")[-1] if "/" in raw_text else raw_text
    bot_user = await bot.get_me()

    if not pack_name.endswith(f"_by_{bot_user.username}"):
        await message.answer(
            f"Этот пак создан не через меня!\n"
            f"Имя должно заканчиваться на `_by_{bot_user.username}`.",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard(),
        )
        await state.clear()
        return
    try:
        sticker_set = await bot.get_sticker_set(name=pack_name)

        save_pack_to_db(ADMIN_ID, pack_name, sticker_set.title)

        await state.update_data(edit_pack_name=pack_name)
        await message.answer(
            f"Пак *{sticker_set.title}* найден и сохранен в список!\n\n"
            "Отправляйте медиафайлы (Фото, GIF, Видео) по одному для добавления.\n"
            "Когда закончите, нажмите кнопку **«❌ Отмена»**.",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard(),
        )
        await state.set_state(EditPack.waiting_for_media)
    except Exception as e:
        logging.error(e)
        await message.answer("Ошибка: Не удалось найти стикерпак. Проверьте ссылку.")
        await state.clear()


@dp.message(EditPack.waiting_for_media, F.photo | F.video | F.animation | F.document | F.sticker)
async def process_add_sticker(message: Message, state: FSMContext):
    status_msg = await message.answer("Обработка медиа и добавление...")
    user_data = await state.get_data()
    pack_name = user_data["edit_pack_name"]
    try:
        file, format_type, emoji = await download_and_process_media(message)

        await bot.add_sticker_to_set(
            user_id=ADMIN_ID,
            name=pack_name,
            sticker=InputSticker(sticker=file, emoji_list=[emoji], format=format_type),
        )
        await status_msg.edit_text(
            "✅ Стикер успешно добавлен!\n\n"
            "Можете отправить ещё один файл или завершить через кнопку «❌ Отмена»."
        )
    except Exception as e:
        logging.error("Ошибка добавления стикера: %s", e)
        await status_msg.edit_text(f"Не удалось добавить стикер: {str(e)[:200]}")


@dp.message(F.text == "📋 Клонировать пак")
@dp.message(Command("clonepack"))
async def start_pack_cloning(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(
        "Начинаем клонирование стикерпака.\n\n"
        "Отправьте ссылку на любой публичный стикерпак (статичный или видео).",
        reply_markup=get_main_keyboard(),
    )
    await state.set_state(ClonePack.waiting_for_source_link)


@dp.message(ClonePack.waiting_for_source_link)
async def process_clone(message: Message, state: FSMContext):
    if not message.text or message.text == "❌ Отмена":
        return

    raw_text = message.text.strip()
    source_pack_name = raw_text.split("/")[-1] if "/" in raw_text else raw_text
    status_msg = await message.answer("Получение информации о стикерпаке...")

    try:
        source_set = await bot.get_sticker_set(name=source_pack_name)
        if not source_set.stickers:
            await status_msg.edit_text("Этот стикерпак пуст.")
            await state.clear()
            return

        bot_user = await bot.get_me()
        clean_old_name = re.sub(r"[^a-zA-Z0-9_]", "", source_pack_name.split("_by_")[0])
        target_pack_name = f"clone_{clean_old_name}_by_{bot_user.username}"
        if len(target_pack_name) > 64:
            target_pack_name = f"c_{clean_old_name[:30]}_by_{bot_user.username}"

        target_title = f"{source_set.title} (Clone)"
        total_stickers = len(source_set.stickers)
        await status_msg.edit_text(
            f"Найдено стикеров: {total_stickers}. Копирую первый стикер..."
        )

        first_st = source_set.stickers[0]
        first_file_info = await bot.get_file(first_st.file_id)
        first_raw = await bot.download_file(first_file_info.file_path)

        if first_st.is_video or first_st.is_animated:
            processed_first = await process_video_sticker(first_raw.getvalue())
            first_input = BufferedInputFile(processed_first, filename="first.webm")
            pack_format = "video"
        else:
            processed_first = process_static_image(first_raw)
            first_input = BufferedInputFile(processed_first, filename="first.png")
            pack_format = "static"

        await bot.create_new_sticker_set(
            user_id=ADMIN_ID,
            name=target_pack_name,
            title=target_title,
            stickers=[
                InputSticker(
                    sticker=first_input,
                    emoji_list=[first_st.emoji] if first_st.emoji else ["💬"],
                    format=pack_format,
                )
            ],
            sticker_format=pack_format,
        )

        save_pack_to_db(ADMIN_ID, target_pack_name, target_title)

        if total_stickers > 1:
            for index, st in enumerate(source_set.stickers[1:], start=2):
                await status_msg.edit_text(
                    f"Копирование: обработано {index} из {total_stickers}..."
                )
                try:
                    file_info = await bot.get_file(st.file_id)
                    raw_bytes = await bot.download_file(file_info.file_path)

                    if pack_format == "video":
                        processed = await process_video_sticker(raw_bytes.getvalue())
                        st_file = BufferedInputFile(processed, filename=f"st_{index}.webm")
                    else:
                        processed = process_static_image(raw_bytes)
                        st_file = BufferedInputFile(processed, filename=f"st_{index}.png")

                    await bot.add_sticker_to_set(
                        user_id=ADMIN_ID,
                        name=target_pack_name,
                        sticker=InputSticker(
                            sticker=st_file,
                            emoji_list=[st.emoji] if st.emoji else ["💬"],
                            format=pack_format,
                        ),
                    )
                    await asyncio.sleep(0.5)
                except Exception as file_err:
                    logging.error(f"Пропущен стикер {index}: {file_err}")
                    continue

        await status_msg.edit_text(
            "🎉 Стикерпак успешно скопирован и сохранён в ваш список!\n\n"
            f"Ссылка: t.me/addstickers/{target_pack_name}\n\n"
            "Вы можете редактировать его через кнопку «✏️ Добавить в пак»."
        )
        await state.clear()

    except Exception as e:
        logging.exception("Критическая ошибка при клонировании:")
        await status_msg.edit_text(f"Не удалось скопировать пак. Ошибка: {str(e)[:200]}")
        await state.clear()


async def handle_ping(request):
    return web.Response(text="Bot is alive!")


async def main():
    init_db()

    app = web.Application()
    app.router.add_route("*", "/", handle_ping)

    port = int(os.environ.get("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    if TOKEN and ADMIN_ID != 0:
        await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
