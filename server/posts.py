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


TEXT_LIMIT = 4096


def validate_articles(data: object, seen_sources: set[str]) -> tuple[list[dict], list[str]]:
    """Статьи для Дзена: {"articles": [{title, lead, sections: [{heading, paragraphs}], outro, cta, ...}]}."""
    problems: list[str] = []
    items = data.get("articles") if isinstance(data, dict) else data
    if not items:
        return [], []
    if not isinstance(items, list):
        return [], ["Дзен: поле articles не список"]
    seen = set(seen_sources)
    out: list[dict] = []
    for i, a in enumerate(items, 1):
        if not isinstance(a, dict):
            problems.append("Дзен, статья %d: неверный формат" % i)
            continue
        title = str(a.get("title", "")).strip()
        src = str(a.get("source_url", "")).strip()
        sections = []
        for s in a.get("sections") or []:
            if not isinstance(s, dict):
                continue
            paras = [str(x).strip() for x in (s.get("paragraphs") or []) if str(x).strip()]
            if paras:
                sections.append({"heading": str(s.get("heading", "")).strip(), "paragraphs": paras})
        if not title or not sections:
            problems.append("Дзен, статья %d: нет заголовка или разделов" % i)
            continue
        if not src.startswith("https://"):
            problems.append("Дзен, статья %d («%s»): нет ссылки на первоисточник, пропущена" % (i, title[:40]))
            continue
        if src in seen:
            problems.append("Дзен, статья %d («%s»): этот источник уже был, пропущена" % (i, title[:40]))
            continue
        tags = []
        for t in (a.get("hashtags") or [])[:3]:
            t = re.sub(r"\s+", "", str(t))
            if t:
                tags.append(t if t.startswith("#") else "#" + t)
        out.append({
            "kind": "dzen",
            "rubric": str(a.get("rubric", "")).strip() or "Статья",
            "title": title, "lead": str(a.get("lead", "")).strip(), "sections": sections,
            "outro": str(a.get("outro", "")).strip(), "cta": str(a.get("cta", "")).strip(),
            "hashtags": tags,
            "image_url": str(a.get("image_url", "")).strip(),
            "image_page_url": str(a.get("image_page_url", "")).strip(),
            "credit": str(a.get("credit", "")).strip(),
            "source_url": src,
            "source_label": str(a.get("source_label", "")).strip() or (urlparse(src).hostname or "источник"),
            "check": str(a.get("check", "")).strip(),
            "interactive": str(a.get("interactive", "")).strip(),
        })
        seen.add(src)
    return out, problems


def build_article(a: dict, default_cta: str, cta_marker: str, with_credit: bool = True) -> tuple[str, list[str]]:
    """Статья одним сообщением (до 4096 знаков): жирный заголовок, лид, разделы с жирными подзаголовками,
    концовка, крючок на основной канал, фото/источник, хештеги. Если длиннее лимита — с конца убираются
    разделы (минимум два остаются). Возвращает (HTML, предупреждения)."""
    e = html.escape
    warns: list[str] = []
    cta = a.get("cta") or ""
    if cta_marker and cta_marker.lower() not in cta.lower():
        cta = (cta + "\n\n" + default_cta).strip() if cta else default_cta
    sections = list(a["sections"])

    def assemble(secs):
        parts = ["<b>%s</b>" % e(a["title"])]
        if a.get("lead"):
            parts.append("<i>%s</i>" % e(a["lead"]))
        for s in secs:
            block = ("<b>%s</b>\n" % e(s["heading"]) if s.get("heading") else "") + "\n\n".join(e(p) for p in s["paragraphs"])
            parts.append(block)
        if a.get("outro"):
            parts.append(e(a["outro"]))
        if cta:
            parts.append(e(cta))
        tail = []
        if with_credit and a.get("credit"):
            tail.append("<i>Фото: %s</i>" % e(a["credit"]))
        tail.append('<i>Источник: <a href="%s">%s</a></i>' % (e(a["source_url"], quote=True), e(a["source_label"])))
        if a.get("hashtags"):
            tail.append(e(" ".join(a["hashtags"])))
        return "\n\n".join(parts) + "\n\n" + "\n".join(tail)

    text = assemble(sections)
    while utf16_len(plain(text)) > TEXT_LIMIT - 50 and len(sections) > 2:
        dropped = sections.pop()
        warns.append("«%s»: длиннее лимита Telegram, убран раздел «%s»" % (a["title"][:40], dropped.get("heading", "")[:40]))
        text = assemble(sections)
    if utf16_len(plain(text)) > TEXT_LIMIT:
        raise ValueError("статья длиннее 4096 знаков даже после сокращения")
    return text, warns


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


def vk_text(p: dict, has_image: bool) -> str:
    """Тот же пост для стены ВК: разметки там нет, поэтому простой текст, заголовок первой строкой,
    ссылка на источник адресом (ВК сам делает её кликабельной)."""
    parts = [p["title"]] + list(p["paragraphs"])
    if p.get("outro"):
        parts.append(p["outro"])
    tail = []
    if has_image and p.get("credit"):
        tail.append("Фото: %s" % p["credit"])
    tail.append("Источник: %s" % p["source_url"])
    if p.get("hashtags"):
        tail.append(" ".join(p["hashtags"]))
    return "\n\n".join(parts) + "\n\n" + "\n".join(tail)


def build_digest(day: str, posts: list[dict], problems: list[str], slots: list[tuple[int, int]],
                 articles: list[dict] | None = None, dzen_slots: list[tuple[int, int]] | None = None,
                 dzen_channel: str = "") -> str:
    when = ", ".join("%d:%02d" % s for s in slots)
    lines = ["Aion Space, подборка за %s" % day, "Постов на утверждение: %d" % len(posts),
             "Утверждённые выходят в канал в %s, по одному." % when, ""]
    if articles:
        lines.insert(2, "Статей для Дзена: %d (%s, в %s)" % (
            len(articles), dzen_channel, ", ".join("%d:%02d" % s for s in dzen_slots or [])))
    for i, p in enumerate(posts, 1):
        lines.append("%d. %s: %s" % (i, p["rubric"], p["title"]))
        if p.get("image_page_url"):
            lines.append("   Снимок: " + p["image_page_url"])
        if p.get("check"):
            lines.append("   Проверка: " + p["check"])
        if p.get("video_idea"):
            lines.append("   Видео: " + p["video_idea"])
        lines.append("")
    for i, a in enumerate(articles or [], 1):
        lines.append("Дзен %d. %s: %s" % (i, a["rubric"], a["title"]))
        if a.get("interactive"):
            lines.append("   Интерактив: " + a["interactive"])
        if a.get("check"):
            lines.append("   Проверка: " + a["check"])
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


def fetch_image(p: dict, domains: list[str]) -> tuple[bytes | None, str, str]:
    """(байты JPEG | None, пояснение, почему картинки нет, адрес картинки, с которого она скачана)."""
    if not p.get("credit"):
        return None, "нет подписи автора фото, картинка не использована", ""
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
            return prepare_image(download(url)), "", url
        except Exception as ex:
            last = "картинка не скачалась (%s): %s" % (kind, ex)
    return None, last, ""
