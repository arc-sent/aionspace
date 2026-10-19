"""Публикация поста с фото на стену группы ВК. Схема как в omega/vk.py, только фото вместо видео:

    photos.getWallUploadServer -> загрузка JPEG -> photos.saveWallPhoto -> wall.post (от имени группы)

Нужен токен пользователя-админа группы с правами wall, photos, offline (токен сообщества эти методы не пускает).
VKError несёт код и этап: временные ошибки повторяются, а сетевой сбой на самом wall.post — нет
(запись могла уже создаться, повтор дал бы дубль)."""
from __future__ import annotations

import logging
import re
import time

import requests

log = logging.getLogger("aion.vk")
API = "https://api.vk.com/method/"
VERSION = "5.199"
RETRYABLE_CODES = {1, 6, 9, 10}         # неизвестная / слишком часто / flood control / внутренняя


class VKError(RuntimeError):
    def __init__(self, code: int | None, message: str, *, stage: str = "", network: bool = False,
                 no_retry: bool = False):
        self.code, self.stage, self.network, self.no_retry = code, stage, network, no_retry
        super().__init__(message)

    @property
    def retryable(self) -> bool:
        return (self.network or self.code in RETRYABLE_CODES) and not self.no_retry


def scrub(s: str) -> str:
    s = re.sub(r"access_token=[^&\s]+", "access_token=***", s)
    return re.sub(r"vk1\.a\.[A-Za-z0-9_-]+", "vk1.a.***", s)


def group_from_link(text: str) -> str:
    """«https://vk.com/aion_space», «vk.com/club123», «public123», «123» -> короткое имя или число."""
    t = text.strip().rstrip("/")
    t = re.sub(r"^(https?://)?(m\.)?vk\.(com|ru)/", "", t)
    m = re.fullmatch(r"(?:club|public|event)(\d+)", t)
    if m:
        return m.group(1)
    return t.lstrip("-")


class VK:
    def __init__(self, token: str, group: str, proxy: str = "", retries: int = 3, base_delay: float = 3.0):
        self.token, self.group = token, group
        self.proxies = {"https": proxy, "http": proxy} if proxy else None
        self.retries, self.base_delay = retries, base_delay
        self.group_id: int | None = int(group) if str(group).isdigit() else None
        self.group_name = ""

    def call(self, method: str, stage: str, **params):
        data = {"access_token": self.token, "v": VERSION, **params}
        try:
            r = requests.post(API + method, data=data, timeout=30, proxies=self.proxies).json()
        except (requests.RequestException, ValueError) as ex:
            raise VKError(None, "сетевая ошибка: %s" % scrub(str(ex)), stage=stage, network=True) from ex
        if "error" in r:
            e = r["error"]
            raise VKError(e.get("error_code"), "VK %s: %s" % (e.get("error_code"), e.get("error_msg")), stage=stage)
        return r["response"]

    # ---- группа
    def resolve(self) -> int:
        """Числовой id группы и её название (groups.getById понимает и короткое имя)."""
        if self.group_id and self.group_name:
            return self.group_id
        res = self.call("groups.getById", "поиск группы", group_id=self.group)
        g = (res.get("groups") if isinstance(res, dict) else res)[0]
        self.group_id, self.group_name = int(g["id"]), g.get("name", "")
        return self.group_id

    def check(self) -> str:
        """Проверка при старте: группа найдена и токен пускает к загрузке фото на её стену."""
        gid = self.resolve()
        self.call("photos.getWallUploadServer", "проверка прав на фото", group_id=gid)
        return "«%s» (id %d)" % (self.group_name, gid)

    def wall_link(self, post_id: int) -> str:
        return "https://vk.com/wall-%d_%d" % (self.group_id or 0, post_id)

    # ---- публикация
    def _upload_photo(self, gid: int, image: bytes) -> str:
        server = self.call("photos.getWallUploadServer", "VK photos.getWallUploadServer", group_id=gid)
        stage = "загрузка фото в VK"
        try:
            up = requests.post(server["upload_url"], files={"photo": ("photo.jpg", image, "image/jpeg")},
                               timeout=120, proxies=self.proxies)
            up.raise_for_status()
            up = up.json()
        except (requests.RequestException, ValueError) as ex:
            raise VKError(None, "сетевая ошибка: %s" % scrub(str(ex)), stage=stage, network=True) from ex
        if not up.get("photo") or up.get("photo") == "[]":
            raise VKError(None, "VK не принял файл фото", stage=stage)
        saved = self.call("photos.saveWallPhoto", "VK photos.saveWallPhoto", group_id=gid,
                          photo=up["photo"], server=up["server"], hash=up["hash"])[0]
        return "photo%s_%s" % (saved["owner_id"], saved["id"])

    def _post_once(self, text: str, image: bytes | None) -> int:
        gid = self.resolve()
        attachments = self._upload_photo(gid, image) if image else ""
        stage = "VK wall.post"
        data = {"access_token": self.token, "v": VERSION, "owner_id": -gid, "from_group": 1,
                "message": text, "attachments": attachments}
        try:
            r = requests.post(API + "wall.post", data=data, timeout=30, proxies=self.proxies).json()
        except (requests.RequestException, ValueError) as ex:
            raise VKError(None, "ответ VK потерян, запись могла выйти: %s" % scrub(str(ex)),
                          stage=stage, network=True, no_retry=True) from ex
        if "error" in r:
            e = r["error"]
            raise VKError(e.get("error_code"), "VK %s: %s" % (e.get("error_code"), e.get("error_msg")), stage=stage)
        return int(r["response"]["post_id"])

    def post(self, text: str, image: bytes | None) -> int:
        """Опубликовать запись. Возвращает post_id. Временные ошибки повторяются с растущей паузой."""
        for attempt in range(1, self.retries + 1):
            try:
                return self._post_once(text, image)
            except VKError as ex:
                if not ex.retryable or attempt == self.retries:
                    raise
                delay = self.base_delay * (2 ** (attempt - 1))
                log.warning("ВК, попытка %d/%d: %s; повтор через %.0f с", attempt, self.retries, ex, delay)
                time.sleep(delay)
        raise AssertionError("unreachable")
