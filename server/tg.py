"""Минимальный клиент Bot API на requests: отправка, правка, опрос обновлений."""
from __future__ import annotations

import json
import re
import time

import requests


class TelegramError(Exception):
    pass


def scrub(s: str) -> str:
    return re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot***", s)


class Telegram:
    def __init__(self, token: str, api: str = "https://api.telegram.org", proxy: str = ""):
        self.token, self.api = token, api.rstrip("/")
        self.proxies = {"https": proxy, "http": proxy} if proxy else None

    def call(self, method: str, data: dict | None = None, files: dict | None = None, timeout: int = 90):
        url = "%s/bot%s/%s" % (self.api, self.token, method)
        data = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
                for k, v in (data or {}).items() if v is not None}
        last = "?"
        for attempt in range(3):
            try:
                r = requests.post(url, data=data, files=files, timeout=timeout, proxies=self.proxies)
            except requests.RequestException as ex:
                last = scrub(str(ex))
                time.sleep(3 * (attempt + 1))
                continue
            try:
                j = r.json()
            except ValueError:
                j = {"ok": False, "description": r.text[:200]}
            if j.get("ok"):
                return j["result"]
            if r.status_code == 429:
                time.sleep(min(60, int(j.get("parameters", {}).get("retry_after", 5))) + 1)
                last = "429"
                continue
            raise TelegramError(j.get("description", "ошибка Telegram"))
        raise TelegramError(last)

    # ---- отправка с запасным вариантом «без разметки», если Telegram не принял HTML
    def send_text(self, chat, text: str, markup: dict | None = None, html_mode: bool = True,
                  preview_url: str = "") -> dict:
        """preview_url — картинка крупным превью над текстом (так у длинной статьи есть фото, а подпись
        не ограничена 1024 знаками). Без него превью ссылок выключено."""
        data = {"chat_id": chat, "text": text, "reply_markup": markup}
        if preview_url:
            data["link_preview_options"] = {"url": preview_url, "prefer_large_media": True, "show_above_text": True}
        else:
            data["link_preview_options"] = {"is_disabled": True}
        if html_mode:
            data["parse_mode"] = "HTML"
        try:
            return self.call("sendMessage", data)
        except TelegramError as ex:
            if html_mode and "parse" in str(ex).lower():
                from posts import plain
                data.pop("parse_mode")
                data["text"] = plain(text)
                return self.call("sendMessage", data)
            raise

    def send_photo(self, chat, photo, caption: str, markup: dict | None = None) -> dict:
        """photo — байты JPEG или file_id уже загруженного фото."""
        data = {"chat_id": chat, "caption": caption, "parse_mode": "HTML", "reply_markup": markup}
        files = None
        if isinstance(photo, (bytes, bytearray)):
            files = {"photo": ("photo.jpg", bytes(photo))}
        else:
            data["photo"] = photo
        try:
            return self.call("sendPhoto", data, files)
        except TelegramError as ex:
            if "parse" in str(ex).lower():
                from posts import plain
                data.pop("parse_mode")
                data["caption"] = plain(caption)
                return self.call("sendPhoto", data, files)
            raise

    def edit_text(self, chat, msg_id: int, text: str, markup: dict | None = None) -> None:
        try:
            self.call("editMessageText", {"chat_id": chat, "message_id": msg_id, "text": text,
                                          "disable_web_page_preview": "true",
                                          "reply_markup": markup or {"inline_keyboard": []}})
        except TelegramError as ex:
            if "not modified" not in str(ex).lower():
                raise

    def answer(self, callback_id: str, text: str = "") -> None:
        try:
            self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text}, timeout=20)
        except TelegramError:
            pass        # кнопка «протухла» (старше 15 минут после рестарта) — не страшно

    def updates(self, offset: int, timeout: int = 50) -> list[dict]:
        return self.call("getUpdates", {"offset": offset, "timeout": timeout,
                                        "allowed_updates": ["message", "callback_query"]},
                         timeout=timeout + 15)


def kb(*rows: list[tuple[str, str]]) -> dict:
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}
