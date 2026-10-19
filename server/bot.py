"""Сервер Aion: точка входа. Три потока: API для ноутбука, тик раз в 20 секунд (карточки, сроки,
публикация по слотам) и опрос Telegram (кнопки и /status).

    python bot.py
"""
from __future__ import annotations

import logging
import sys
import threading
import time

import api
from config import Config
from db import DB
from service import Service
from tg import Telegram, TelegramError, scrub
from vk import VK, group_from_link
from vk import scrub as vk_scrub

TICK_SEC = 20


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stdout)
    log = logging.getLogger("aion")
    cfg = Config.from_env()
    miss = cfg.check()
    if miss:
        log.error("не заданы: %s (см. .env.example)", ", ".join(miss))
        return 2
    db = DB(cfg.data_dir / "aion.db")
    tg = Telegram(cfg.telegram_token, cfg.telegram_api, cfg.telegram_proxy)
    vk = None
    if cfg.vk_enabled:
        vk = VK(cfg.vk_token, group_from_link(cfg.vk_group), cfg.vk_proxy)
        try:
            note = "✅ ВК подключён: группа %s. Утверждённые посты будут выходить и туда." % vk.check()
        except Exception as ex:
            note = ("⚠️ ВК: %s\nПроверьте VK_TOKEN (нужен токен пользователя-админа группы с правами wall, photos, "
                    "offline) и VK_GROUP. Пока не исправите, ВК будет показывать ошибку в карточках." % vk_scrub(str(ex)))
        log.info(vk_scrub(note))
        try:
            tg.send_text(cfg.admin_chat_id, note, html_mode=False)
        except TelegramError as ex:
            log.warning("не отправил сообщение о ВК: %s", scrub(str(ex)))
    service = Service(cfg, db, tg, vk=vk)
    try:
        me = tg.call("getMe").get("username")
    except TelegramError as ex:          # сеть могла ещё не подняться: работаем дальше, тик и опрос повторят
        me = "?"
        log.warning("getMe: %s", scrub(str(ex)))
    log.info("бот @%s, канал %s, слоты %s", me, cfg.channel_id, ", ".join("%d:%02d" % s for s in cfg.slots))

    api.start_in_thread(cfg.api_host, cfg.api_port, cfg.api_token, service)

    def ticker():
        while True:
            service.tick()
            time.sleep(TICK_SEC)

    threading.Thread(target=ticker, name="aion-tick", daemon=True).start()

    offset = int(db.kv_get("offset", "0"))
    while True:
        try:
            for u in tg.updates(offset):
                offset = u["update_id"] + 1
                db.kv_set("offset", str(offset))
                try:
                    service.handle_update(u)
                except Exception:
                    log.exception("ошибка обработки обновления")
        except TelegramError as ex:
            log.warning("getUpdates: %s", scrub(str(ex)))
            time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
