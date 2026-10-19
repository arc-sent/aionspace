"""Посты Aion: проверка JSON с ноутбука, оформление для Telegram, картинки, слоты публикации.
Без БД и без Telegram — чистые функции (кроме скачивания картинки)."""
from __future__ import annotations

import datetime as dt
import html
import io
import re
from urllib.parse import urlparse

import requests

MSK = dt.timezone(dt.timedelta(hours=3))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AionSpaceBot/1.0"
CAPTION_LIMIT = 1024
PHOTO_MAX_BYTES = 9_500_000


def now_msk() -> dt.datetime:
    return dt.datetime.now(MSK).replace(microsecond=0)


def utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def plain(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s))


def host_allowed(url: str, domains: list[str]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in domains)


# --------------------------------------------------------------------------- проверка
def validate(data: object, seen_sources: set[str]) -> tuple[list[dict], list[str]]:
    """Те же правила, что у aion_daily.py: заголовок, текст и https-источник обязательны, повторы источников — нет."""
    problems: list[str] = []
    posts = data.get("posts") if isinstance(data, dict) else data
    if not isinstance(posts, list):
        return [], ["в JSON нет списка posts"]
    seen = set(seen_sources)
    out: list[dict] = []
    for i, p in enumerate(posts, 1):
        if not isinstance(p, dict):
            problems.append("пост %d: неверный формат" % i)
            continue
        title = str(p.get("title", "")).strip()
        paras = [str(x).strip() for x in (p.get("paragraphs") or []) if str(x).strip()]
        src = str(p.get("source_url", "")).strip()
        if not title or not paras:
            problems.append("пост %d: нет заголовка или текста" % i)
            continue
        if not src.startswith("https://"):
            problems.append("пост %d («%s»): нет ссылки на первоисточник, пропущен" % (i, title[:40]))
            continue
        if src in seen:
            problems.append("пост %d («%s»): этот источник уже был, пропущен" % (i, title[:40]))
            continue
        tags = []
        for t in (p.get("hashtags") or [])[:3]:
            t = re.sub(r"\s+", "", str(t))
            if t:
                tags.append(t if t.startswith("#") else "#" + t)
        out.append({
            "rubric": str(p.get("rubric", "")).strip() or "Пост",
            "title": title, "paragraphs": paras,
            "outro": str(p.get("outro", "")).strip(),
            "hashtags": tags,
            "image_url": str(p.get("image_url", "")).strip(),
            "image_page_url": str(p.get("image_page_url", "")).strip(),
            "credit": str(p.get("credit", "")).strip(),
            "source_url": src,
            "source_label": str(p.get("source_label", "")).strip() or (urlparse(src).hostname or "источник"),
            "check": str(p.get("check", "")).strip(),
            "video_idea": str(p.get("video_idea", "")).strip(),
        })
        seen.add(src)
    return out, problems


# --------------------------------------------------------------------------- оформление
def build_caption(p: dict, with_credit: bool = True) -> str:
    e = html.escape
    parts = ["<b>%s</b>" % e(p["title"])]
    parts += [e(x) for x in p["paragraphs"]]
    if p.get("outro"):
        parts.append(e(p["outro"]))
    tail = []
    if with_credit and p.get("credit"):
        tail.append("<i>Фото: %s</i>" % e(p["credit"]))
    tail.append('<i>Источник: <a href="%s">%s</a></i>' % (e(p["source_url"], quote=True), e(p["source_label"])))
    if p.get("hashtags"):
        tail.append(e(" ".join(p["hashtags"])))
    return "\n\n".join(parts) + "\n\n" + "\n".join(tail)


def render(p: dict, has_image: bool) -> list[tuple[str, str]]:
    """Как пост выглядит в Telegram: [("photo", подпись)] или [("photo", заголовок), ("text", остальное)]
    или [("text", текст)], если картинки нет. Одинаково для карточки на утверждение и для канала."""
    caption = build_caption(p, with_credit=has_image)
    if not has_image:
        return [("text", caption)]
    if utf16_len(plain(caption)) > CAPTION_LIMIT:
        rest = caption.split("\n\n", 1)[1] if "\n\n" in caption else caption
        return [("photo", "<b>%s</b>" % html.escape(p["title"])), ("text", rest)]
    return [("photo", caption)]


def build_digest(day: str, posts: list[dict], problems: list[str], slots: list[tuple[int, int]]) -> str:
    when = ", ".join("%d:%02d" % s for s in slots)
    lines = ["Aion Space, подборка за %s" % day, "Постов на утверждение: %d" % len(posts),
             "Утверждённые выходят в канал в %s, по одному." % when, ""]
    for i, p in enumerate(posts, 1):
        lines.append("%d. %s: %s" % (i, p["rubric"], p["title"]))
        if p.get("image_page_url"):
            lines.append("   Снимок: " + p["image_page_url"])
        if p.get("check"):
            lines.append("   Проверка: " + p["check"])
        if p.get("video_idea"):
            lines.append("   Видео: " + p["video_idea"])
        lines.append("")
    if problems:
        lines.append("Предупреждения:")
        lines += ["- " + w for w in problems]
    return "\n".join(lines).strip()[:4000]


# --------------------------------------------------------------------------- слоты
def next_slot(now: dt.datetime, slots: list[tuple[int, int]], taken: set[str]) -> dt.datetime:
    """Ближайший свободный слот не раньше now. taken — уже занятые слоты в формате isoformat()."""
    day = now.astimezone(MSK).date()
    for d in range(0, 366):
        for h, m in slots:
            t = dt.datetime.combine(day + dt.timedelta(days=d), dt.time(h, m), MSK)
            if t >= now and t.isoformat() not in taken:
                return t
    raise RuntimeError("нет свободного слота")


def human_time(iso: str, now: dt.datetime | None = None) -> str:
    t = dt.datetime.fromisoformat(iso).astimezone(MSK)
    now = now or now_msk()
    if t.date() == now.date():
        return "сегодня в %s" % t.strftime("%H:%M")
    if t.date() == now.date() + dt.timedelta(days=1):
        return "завтра в %s" % t.strftime("%H:%M")
    return t.strftime("%d.%m в %H:%M")


# --------------------------------------------------------------------------- картинки
def og_image(page_url: str) -> str:
    r = requests.get(page_url, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    for pat in (r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']'):
        m = re.search(pat, r.text, re.I)
        if m:
            return html.unescape(m.group(1))
    raise ValueError("на странице нет og:image")


def download(url: str) -> bytes:
    r = requests.get(url, headers={"User-Agent": UA, "Accept": "image/*,*/*;q=0.8"}, timeout=90)
    r.raise_for_status()
    if not r.headers.get("content-type", "").lower().startswith("image/"):
        raise ValueError("по ссылке не картинка (%s)" % r.headers.get("content-type", "?"))
    if len(r.content) < 2000:
        raise ValueError("слишком маленький файл")
    return r.content


def prepare_image(data: bytes) -> bytes:
    """Telegram принимает фото до 10 МБ, ширина+высота до 10000, пропорции до 1:20."""
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = 300_000_000
    im = Image.open(io.BytesIO(data))
    im.load()
    if im.mode != "RGB":
        im = im.convert("RGB")
    w, h = im.size
    if max(w, h) / max(1, min(w, h)) > 20:
        raise ValueError("слишком вытянутая картинка")
    if len(data) <= PHOTO_MAX_BYTES and max(w, h) <= 4000 and data[:2] == b"\xff\xd8":
        return data
    scale = min(1.0, 2560 / max(w, h))
    if scale < 1.0:
        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    for q in (90, 85, 78, 70):
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=q, optimize=True)
        if buf.tell() <= PHOTO_MAX_BYTES:
            return buf.getvalue()
    raise ValueError("не удалось сжать до 9,5 МБ")


def fetch_image(p: dict, domains: list[str]) -> tuple[bytes | None, str]:
    """(байты JPEG | None, пояснение, почему картинки нет)."""
    if not p.get("credit"):
        return None, "нет подписи автора фото, картинка не использована"
    candidates = [("image_url", p["image_url"])] if p.get("image_url") else []
    last = "нет ссылки на картинку"
    tried_og = False
    while True:
        if not candidates:
            if p.get("image_page_url") and not tried_og:
                tried_og = True
                try:
                    candidates.append(("og:image", og_image(p["image_page_url"])))
                except Exception as ex:
                    last = "не нашёл картинку на странице снимка: %s" % ex
                    break
            else:
                break
        kind, url = candidates.pop(0)
        if not host_allowed(url, domains):
            last = "домен картинки не из списка открытых источников: %s" % (urlparse(url).hostname or url)
            continue
        try:
            return prepare_image(download(url)), ""
        except Exception as ex:
            last = "картинка не скачалась (%s): %s" % (kind, ex)
    return None, last
