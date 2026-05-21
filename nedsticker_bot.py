await bot.create_new_sticker_set(
            user_id=ADMIN_ID,
            name=user_data["pack_name"],
            title=user_data["pack_title"],
            stickers=[{"sticker": sticker_file, "emoji_list": ["✨"]}],
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