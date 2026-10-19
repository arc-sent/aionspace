#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Aion Space: ежедневная подборка постов -> бот в Telegram (с фото, жирным шрифтом и отступами).

Цепочка:  Claude (claude -p, ваша подписка, только WebSearch/WebFetch) ищет свежие темы и возвращает JSON
          -> проверка (источник, подпись, домен картинки, длина) -> скачивание картинок
          -> отправка в бота (sendPhoto, HTML-форматирование) -> итоговая сводка -> журнал тем.

Команды:
    python aion_daily.py run [--dry-run] [--force] [--from-json файл]
    python aion_daily.py test-telegram
    python aion_daily.py chat-id
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

HERE = Path(__file__).resolve().parent
LOGDIR = HERE / "logs"
STATE = HERE / "state"
OUT = HERE / "out"
HISTORY = HERE / "history.json"
LOCK = HERE / ".running"
MSK = dt.timezone(dt.timedelta(hours=3))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AionSpaceBot/1.0"
CAPTION_LIMIT = 1024
PHOTO_MAX_BYTES = 9_500_000
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря"]

DEFAULTS = {
    "run_time": "08:00",
    "fallback_after_minutes": 15,
    "posts_per_day": 4,
    "min_posts": 3,
    "claude_model": "claude-sonnet-5-5",
    "claude_path": "",
    "claude_timeout_minutes": 30,
    "claude_attempts": 2,
    "history_days": 30,
    "send_digest": True,
    # "server": подборка уходит на сервер Aion (утверждение кнопками и публикация в канал по слотам);
    # "telegram": как раньше, посты сразу приходят в бота
    "delivery": "telegram",
    # статьи для Дзена (канал t.me/interesOKosmose -> @zen_sync_bot -> Дзен); только при delivery = server
    "dzen_articles_per_day": 1,
    "dzen_prompt": "dzen_prompt.txt",
    "pause_between_messages_sec": 1.5,
    "telegram_api": "https://api.telegram.org",
    "image_domains": ["nasa.gov", "esawebb.org", "esahubble.org", "eso.org", "esa.int",
                      "webbtelescope.org", "hubblesite.org", "stsci.edu",
                      "chandra.si.edu", "chandra.harvard.edu"],
    "vpn": {"mode": "none", "dir": "", "check_url": "https://api.anthropic.com/v1/models",
            "prefer": [], "max_probe": 8},
    "wake_pc": True,
    "task_hours": 2,
    "sleep_after": True,
    "sleep_only_if_idle_minutes": 10,
    "python": "",
}

_SECRETS: list[str] = []


# --------------------------------------------------------------------------- утилиты
def now_msk() -> dt.datetime:
    return dt.datetime.now(MSK)


def scrub(s: str) -> str:
    for sec in _SECRETS:
        if sec:
            s = s.replace(sec, "***")
    return re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot***", s)


def log(msg: object) -> None:
    line = "[%s] %s" % (now_msk().strftime("%Y-%m-%d %H:%M:%S"), scrub(str(msg)))
    try:
        LOGDIR.mkdir(exist_ok=True)
        with open(LOGDIR / ("daily_%s.log" % now_msk().strftime("%Y-%m-%d")), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    try:
        print(line, flush=True)
    except Exception:
        pass


def load_settings() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    p = HERE / "settings.json"
    if p.exists():
        user = json.loads(p.read_text(encoding="utf-8-sig"))
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


def load_env() -> dict:
    env: dict[str, str] = {}
    p = HERE / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_PROXY", "AION_SERVER_URL", "AION_SERVER_TOKEN"):
        env.setdefault(k, os.environ.get(k, ""))
    return env


def keep_awake(on: bool) -> None:
    """Пока цепочка работает, Windows не должна усыпить ПК."""
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001 if on else 0x80000000)
    except Exception:
        pass


def utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def plain(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s))


def host_allowed(url: str, domains: list[str]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in domains)


# --------------------------------------------------------------------------- журнал тем
def load_history() -> list[dict]:
    try:
        return json.loads(HISTORY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def save_history(items: list[dict]) -> None:
    HISTORY.write_text(json.dumps(items[-200:], ensure_ascii=False, indent=1), encoding="utf-8")


def server_history(cfg: dict, env: dict) -> list[dict]:
    """Темы с сервера Aion (в том числе отклонённые и ещё не вышедшие). [] — сервер не настроен или недоступен."""
    url, token = env.get("AION_SERVER_URL", "").rstrip("/"), env.get("AION_SERVER_TOKEN", "")
    if cfg.get("delivery") != "server" or not url or not token:
        return []
    try:
        r = requests.get(url + "/history", params={"days": cfg["history_days"]},
                         headers={"Authorization": "Bearer " + token}, timeout=30)
        r.raise_for_status()
        return list(r.json().get("items") or [])
    except (requests.RequestException, ValueError) as ex:
        log("история с сервера не получена: %s" % ex)
        return []


def recent_topics(cfg: dict, today: dt.date, target: str = "main") -> str:
    since = today - dt.timedelta(days=int(cfg["history_days"]))
    lines = []
    items, seen = [], set()
    local = load_history() if target == "main" else []
    remote = [h for h in server_history(cfg, load_env()) if (h.get("target") or "main") == target]
    for h in local + remote:
        if h.get("source_url") in seen:
            continue
        seen.add(h.get("source_url"))
        items.append(h)
    for h in items:
        try:
            d = dt.date.fromisoformat(h["date"])
        except (KeyError, ValueError):
            continue
        if d >= since:
            lines.append("- %s: %s (%s)" % (h["date"], h.get("title", ""), h.get("source_url", "")))
    return "\n".join(lines) if lines else "(пока ничего не публиковалось)"


# --------------------------------------------------------------------------- Claude
def build_prompt(cfg: dict, today: dt.date, dzen: bool = False) -> str:
    text = (HERE / (cfg["dzen_prompt"] if dzen else "prompt.txt")).read_text(encoding="utf-8-sig")
    repl = {
        "{TODAY}": "%d %s %d" % (today.day, MONTHS[today.month - 1], today.year),
        "{TODAY_ISO}": today.isoformat(),
        "{N_POSTS}": str(cfg["posts_per_day"]),
        "{N_ARTICLES}": str(cfg["dzen_articles_per_day"]),
        "{RECENT_TOPICS}": recent_topics(cfg, today, "dzen" if dzen else "main"),
    }
    for k, v in repl.items():
        text = text.replace(k, v)
    return text


def find_claude(cfg: dict) -> str | None:
    if cfg.get("claude_path"):
        return cfg["claude_path"]
    for n in ("claude.exe", "claude.cmd", "claude"):
        p = shutil.which(n)
        if p:
            return p
    return None


def claude_reachable(url: str) -> bool:
    try:
        return requests.get(url, timeout=10).status_code != 403
    except requests.RequestException:
        return False


def reachable(url: str, proxies: dict | None = None) -> bool:
    try:
        requests.get(url, timeout=10, proxies=proxies)
        return True
    except requests.RequestException:
        return False


def start_vpn(cfg: dict, check_url: str = "") -> str:
    """Свой лёгкий VPN из Marionette (vpnclient.py): локальный прокси только для запросов этого скрипта."""
    v = cfg["vpn"]
    vdir = Path(v["dir"])
    client = vdir / "vpnclient.py"
    if not client.exists():
        log("vpn: не найден %s" % client)
        return ""
    args = [sys.executable, str(client), "up", "--check-url", check_url or v["check_url"]]
    if v.get("max_probe"):
        args += ["--max-probe", str(v["max_probe"])]
    if v.get("prefer"):
        args += ["--prefer", ",".join(v["prefer"])]
    log("запускаю свой VPN-прокси (vpnclient.py up)")
    try:
        rc = subprocess.run(args, cwd=str(vdir), timeout=300).returncode
    except subprocess.TimeoutExpired:
        log("vpn: таймаут")
        return ""
    state = vdir / "run" / "state.json"
    if rc != 0 or not state.exists():
        log("vpn: не поднялся (код %s)" % rc)
        return ""
    return str(json.loads(state.read_text(encoding="utf-8")).get("proxy", ""))


def stop_vpn(cfg: dict) -> None:
    client = Path(cfg["vpn"]["dir"]) / "vpnclient.py"
    try:
        subprocess.run([sys.executable, str(client), "down"], cwd=str(client.parent), timeout=60)
    except Exception as ex:
        log("vpn down: %s" % ex)


def parse_json(text: str) -> dict:
    t = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if fence:
        t = fence.group(1).strip()
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("в ответе нет JSON")
    return json.loads(t[a:b + 1])


def generate(cfg: dict, today: dt.date, dzen: bool = False) -> dict:
    claude = find_claude(cfg)
    if not claude:
        raise RuntimeError("консольный claude не найден в PATH (npm i -g @anthropic-ai/claude-code, затем claude -> /login)")
    prompt = build_prompt(cfg, today, dzen)
    check = cfg["vpn"]["check_url"]
    proxy = ""
    env = os.environ.copy()
    vpn_started = False
    try:
        if claude_reachable(check):
            log("Claude API доступен напрямую: VPN не нужен")
        elif cfg["vpn"]["mode"] == "own" and cfg["vpn"]["dir"]:
            proxy = start_vpn(cfg)
            vpn_started = True
            if not proxy:
                raise RuntimeError("нет доступа к Claude (VPN не поднялся)")
            env.update(HTTPS_PROXY=proxy, HTTP_PROXY=proxy, NO_PROXY="127.0.0.1,localhost")
            log("claude идёт через прокси %s" % proxy)
        else:
            raise RuntimeError("Claude API недоступен, а vpn.mode не 'own'")

        last_err = "неизвестно"
        for attempt in range(1, int(cfg["claude_attempts"]) + 1):
            cmd = [claude, "-p", "--model", cfg["claude_model"], "--no-session-persistence",
                   "--allowedTools", "WebSearch", "WebFetch"]
            log("запуск claude (%s%s), попытка %d" % (cfg["claude_model"], ", статьи для Дзена" if dzen else "", attempt))
            try:
                p = subprocess.run(cmd, input=prompt.encode("utf-8"), capture_output=True, env=env,
                                   timeout=int(cfg["claude_timeout_minutes"]) * 60)
            except subprocess.TimeoutExpired:
                last_err = "таймаут claude"
                log(last_err)
                continue
            out = p.stdout.decode("utf-8", "replace")
            OUT.mkdir(exist_ok=True)
            (OUT / ("claude_%s%s.txt" % ("dzen_" if dzen else "", today.isoformat()))).write_text(out, encoding="utf-8")
            if p.returncode != 0:
                last_err = "claude завершился с кодом %s: %s" % (p.returncode, p.stderr.decode("utf-8", "replace")[-300:])
                log(last_err)
                continue
            try:
                return parse_json(out)
            except ValueError as ex:
                last_err = "не удалось разобрать ответ: %s" % ex
                log(last_err)
        raise RuntimeError(last_err)
    finally:
        if vpn_started:
            stop_vpn(cfg)
            log("VPN-прокси выключен")


# --------------------------------------------------------------------------- проверка постов
def validate(data: object, history: list[dict]) -> tuple[list[dict], list[str]]:
    problems: list[str] = []
    posts = data.get("posts") if isinstance(data, dict) else data
    if not isinstance(posts, list):
        return [], ["в ответе нет списка posts"]
    seen = {h.get("source_url") for h in history}
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
            problems.append("пост %d («%s»): этот источник уже использовался, пропущен" % (i, title[:40]))
            continue
        tags = []
        for t in (p.get("hashtags") or [])[:3]:
            t = re.sub(r"\s+", "", str(t))
            if t:
                tags.append(t if t.startswith("#") else "#" + t)
        credit = str(p.get("credit", "")).strip()
        post = {
            "rubric": str(p.get("rubric", "")).strip() or "Пост",
            "title": title, "paragraphs": paras,
            "outro": str(p.get("outro", "")).strip(),
            "hashtags": tags,
            "image_url": str(p.get("image_url", "")).strip(),
            "image_page_url": str(p.get("image_page_url", "")).strip(),
            "credit": credit,
            "source_url": src,
            "source_label": str(p.get("source_label", "")).strip() or (urlparse(src).hostname or "источник"),
            "check": str(p.get("check", "")).strip(),
            "video_idea": str(p.get("video_idea", "")).strip(),
        }
        seen.add(src)
        out.append(post)
    return out, problems


def build_caption(p: dict) -> str:
    e = html.escape
    parts = ["<b>%s</b>" % e(p["title"])]
    parts += [e(x) for x in p["paragraphs"]]
    if p["outro"]:
        parts.append(e(p["outro"]))
    tail = []
    if p["credit"]:
        tail.append("<i>Фото: %s</i>" % e(p["credit"]))
    tail.append('<i>Источник: <a href="%s">%s</a></i>' % (e(p["source_url"], quote=True), e(p["source_label"])))
    if p["hashtags"]:
        tail.append(e(" ".join(p["hashtags"])))
    return "\n\n".join(parts) + "\n\n" + "\n".join(tail)


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
    try:
        from PIL import Image
    except ImportError:
        if len(data) > PHOTO_MAX_BYTES:
            raise ValueError("файл больше 9,5 МБ, а Pillow не установлен (pip install pillow)")
        return data
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


def get_image(p: dict, cfg: dict) -> tuple[bytes | None, str]:
    """Возвращает (jpeg/png-байты | None, пояснение, если картинки нет)."""
    if not p["credit"]:
        return None, "нет подписи автора фото, картинка не использована"
    domains = cfg["image_domains"]
    candidates = []
    if p["image_url"]:
        candidates.append(("image_url", p["image_url"]))
    last = "нет ссылки на картинку"
    tried_og = False
    while True:
        if not candidates:
            if p["image_page_url"] and not tried_og:
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


# --------------------------------------------------------------------------- Telegram
class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, token: str, chats: list[str], api: str, proxy: str = ""):
        self.token, self.chats, self.api = token, chats, api.rstrip("/")
        self.proxies = {"https": proxy, "http": proxy} if proxy else None

    def call(self, method: str, data: dict | None = None, files: dict | None = None):
        url = "%s/bot%s/%s" % (self.api, self.token, method)
        last = "?"
        for attempt in range(3):
            try:
                r = requests.post(url, data=data, files=files, timeout=90, proxies=self.proxies)
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

    def message(self, text: str, html_mode: bool = True) -> None:
        for chat in self.chats:
            data = {"chat_id": chat, "text": text, "disable_web_page_preview": "true"}
            if html_mode:
                data["parse_mode"] = "HTML"
            try:
                self.call("sendMessage", data)
            except TelegramError as ex:
                if html_mode and "parse" in str(ex).lower():
                    data.pop("parse_mode")
                    data["text"] = plain(text)
                    self.call("sendMessage", data)
                else:
                    raise

    def photo(self, image: bytes, caption: str) -> None:
        for chat in self.chats:
            data = {"chat_id": chat, "caption": caption, "parse_mode": "HTML"}
            try:
                self.call("sendPhoto", data, {"photo": ("photo.jpg", image)})
            except TelegramError as ex:
                if "parse" in str(ex).lower():
                    data.pop("parse_mode")
                    data["caption"] = plain(caption)
                    self.call("sendPhoto", data, {"photo": ("photo.jpg", image)})
                else:
                    raise


def send_post(tg: Telegram, p: dict, cfg: dict) -> list[str]:
    """Шлёт один пост. Возвращает предупреждения."""
    warns: list[str] = []
    caption = build_caption(p)
    image, why = get_image(p, cfg)
    if image is None:
        warns.append("«%s»: без фото — %s" % (p["title"][:40], why))
        note = "\n\n⚠️ Картинка не добавлена."
        if p["image_page_url"]:
            note += " Страница снимка: " + html.escape(p["image_page_url"])
        tg.message(caption + note)
        return warns
    if utf16_len(plain(caption)) > CAPTION_LIMIT:
        warns.append("«%s»: текст длиннее 1024 знаков, отправлен отдельным сообщением" % p["title"][:40])
        tg.photo(image, "<b>%s</b>" % html.escape(p["title"]))
        rest = caption.split("\n\n", 1)[1] if "\n\n" in caption else caption
        tg.message(rest)
        return warns
    tg.photo(image, caption)
    return warns


def build_digest(day: dt.date, posts: list[dict], warns: list[str]) -> str:
    lines = ["Aion Space, %s" % day.strftime("%d.%m.%Y"), "Постов: %d" % len(posts), ""]
    for i, p in enumerate(posts, 1):
        lines.append("%d. %s: %s" % (i, p["rubric"], p["title"]))
        if p["image_page_url"]:
            lines.append("   Снимок: " + p["image_page_url"])
        if p["check"]:
            lines.append("   Проверка: " + p["check"])
        if p["video_idea"]:
            lines.append("   Видео: " + p["video_idea"])
        lines.append("")
    if warns:
        lines.append("Предупреждения:")
        lines += ["- " + w for w in warns]
    return "\n".join(lines).strip()[:4000]


# --------------------------------------------------------------------------- команды
def make_tg(env: dict, cfg: dict) -> Telegram | None:
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    chats = [c.strip() for c in env.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
    _SECRETS.append(token)
    if not token or not chats:
        return None
    return Telegram(token, chats, cfg["telegram_api"], env.get("TELEGRAM_PROXY", ""))


def connect_tg(tg: Telegram, cfg: dict) -> bool:
    """Маршрут до Telegram: TELEGRAM_PROXY из .env, напрямую или через свой VPN.
    Возвращает True, если VPN поднят здесь (его нужно выключить через stop_vpn)."""
    url = tg.api
    if tg.proxies and reachable(url, tg.proxies):
        log("Telegram: через прокси из .env")
        return False
    if reachable(url):
        tg.proxies = None
        log("Telegram доступен напрямую")
        return False
    if cfg["vpn"]["mode"] == "own" and cfg["vpn"]["dir"]:
        proxy = start_vpn(cfg, url)
        if proxy:
            tg.proxies = {"https": proxy, "http": proxy}
            log("Telegram идёт через свой VPN-прокси %s" % proxy)
        else:
            log("Telegram: свой VPN не поднялся")
        return True
    log("Telegram недоступен, а vpn.mode не 'own'")
    return False


def get_articles(args, cfg: dict, data: dict, today: dt.date, problems: list[str]) -> list[dict]:
    """Статьи для Дзена: вторым запуском Claude по dzen_prompt.txt (или из --from-json). Сбой не мешает постам."""
    if cfg.get("delivery") != "server" or int(cfg.get("dzen_articles_per_day") or 0) <= 0:
        return []
    if args.from_json:
        items = data.get("articles") if isinstance(data, dict) else None
    else:
        try:
            items = generate(cfg, today, dzen=True).get("articles")
        except Exception as ex:
            problems.append("Дзен: статьи не получились: %s" % scrub(str(ex))[:300])
            log(problems[-1])
            return []
    items = [a for a in (items or []) if isinstance(a, dict)][: int(cfg["dzen_articles_per_day"])]
    OUT.mkdir(exist_ok=True)
    (OUT / ("articles_%s.json" % today.isoformat())).write_text(
        json.dumps({"articles": items}, ensure_ascii=False, indent=1), encoding="utf-8")
    log("статей для Дзена: %d" % len(items))
    return items


def push_batch(env: dict, today: dt.date, posts: list[dict], problems: list[str],
               articles: list[dict] | None = None) -> dict:
    """Отправить проверенную подборку на сервер Aion. Сервер сам пришлёт карточки на утверждение.
    Повторная отправка той же подборки безопасна: сервер пропускает источники, которые у него уже есть."""
    url, token = env.get("AION_SERVER_URL", "").rstrip("/"), env.get("AION_SERVER_TOKEN", "")
    if not url or not token:
        raise RuntimeError("в .env не заданы AION_SERVER_URL и AION_SERVER_TOKEN")
    _SECRETS.append(token)
    body = {"day": today.isoformat(), "posts": posts, "problems": problems, "articles": articles or []}
    last = "?"
    for attempt in range(1, 4):
        try:
            r = requests.post(url + "/batch", json=body, headers={"Authorization": "Bearer " + token}, timeout=120)
            if r.status_code == 200:
                return r.json()
            last = "HTTP %s: %s" % (r.status_code, r.text[:200])
            if r.status_code in (400, 401):
                break
        except requests.RequestException as ex:
            last = str(ex)
        log("сервер Aion, попытка %d: %s" % (attempt, last))
        time.sleep(10 * attempt)
    raise RuntimeError(last)


def cmd_run(args, cfg: dict, env: dict) -> int:
    today = now_msk().date()
    tg = make_tg(env, cfg)
    if tg is None and not args.dry_run:
        log("в .env не заданы TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID")
        return 2
    done = STATE / ("done_%s.flag" % today.isoformat())
    if done.exists() and not args.force and not args.dry_run:
        log("подборка за сегодня уже отправлена")
        return 10
    if LOCK.exists() and time.time() - LOCK.stat().st_mtime < 4 * 3600 and not args.force:
        log("другой запуск ещё идёт (.running моложе 4 часов)")
        return 11
    LOCK.write_text(now_msk().isoformat(), encoding="utf-8")
    keep_awake(True)
    tg_vpn = False
    try:
        log("=== подборка Aion Space %s ===" % today.isoformat())
        try:
            if args.from_json:
                data = json.loads(Path(args.from_json).read_text(encoding="utf-8-sig"))
            else:
                data = generate(cfg, today)
        except Exception as ex:
            log("подборка не получилась: %s" % ex)
            if tg and not args.dry_run:
                tg_vpn = connect_tg(tg, cfg)
                try:
                    tg.message("❌ Aion Space: подборка за %s не вышла.\n%s" % (today.strftime("%d.%m"), scrub(str(ex))[:600]), html_mode=False)
                except Exception as ex2:
                    log("и уведомление не ушло: %s" % ex2)
            return 1
        history = load_history()
        posts, problems = validate(data, history)
        OUT.mkdir(exist_ok=True)
        (OUT / ("posts_%s.json" % today.isoformat())).write_text(
            json.dumps({"posts": posts, "problems": problems}, ensure_ascii=False, indent=1), encoding="utf-8")
        for pr in problems:
            log("проблема: " + pr)
        posts = posts[: int(cfg["posts_per_day"]) + 1]
        articles = get_articles(args, cfg, data, today, problems)
        if args.dry_run:
            for i, p in enumerate(posts, 1):
                log("--- пост %d (%s) ---\n%s" % (i, p["rubric"], plain(build_caption(p))))
                log("картинка: %s | подпись: %s" % (p["image_url"] or p["image_page_url"] or "нет", p["credit"] or "нет"))
            for i, a in enumerate(articles, 1):
                body = [a.get("title", ""), a.get("lead", "")]
                for sec in a.get("sections") or []:
                    body += [sec.get("heading", "")] + list(sec.get("paragraphs") or [])
                body += [a.get("outro", ""), a.get("cta", ""), "Источник: %s" % a.get("source_url", "")]
                text = "\n\n".join(x for x in body if x)
                log("--- статья для Дзена %d (%s, %d знаков) ---\n%s" % (i, a.get("rubric", ""), len(text), text))
            log("dry-run: в Telegram ничего не отправлено")
            return 0
        if cfg.get("delivery") == "server":
            try:
                res = push_batch(env, today, posts, problems, articles)
            except Exception as ex:
                log("сервер Aion не принял подборку: %s" % ex)
                tg_vpn = connect_tg(tg, cfg)
                try:
                    tg.message("❌ Aion Space: подборка за %s готова, но сервер её не принял.\n%s\nФайл: out\\posts_%s.json"
                               % (today.strftime("%d.%m"), scrub(str(ex))[:500], today.isoformat()), html_mode=False)
                except Exception as ex2:
                    log("и уведомление не ушло: %s" % ex2)
                return 1
            for pr in res.get("problems") or []:
                if pr not in problems:
                    log("сервер: " + pr)
            accepted = set(res.get("sources") or [])
            for p in posts:
                if p["source_url"] in accepted:
                    history.append({"date": today.isoformat(), "title": p["title"], "source_url": p["source_url"]})
            save_history(history)
            n = int(res.get("accepted") or 0)
            if n >= int(cfg["min_posts"]):
                STATE.mkdir(exist_ok=True)
                done.write_text(now_msk().isoformat(), encoding="utf-8")
                log("готово: сервер принял %d постов и %d статей для Дзена (подборка %s), они придут вам на утверждение"
                    % (n, int(res.get("articles") or 0), res.get("batch")))
                return 0
            log("сервер принял только %d из %s, день не помечен выполненным" % (n, cfg["min_posts"]))
            return 1
        tg_vpn = connect_tg(tg, cfg)
        if not posts:
            tg.message("❌ Aion Space: за %s нет ни одного подходящего поста.\n%s" % (today.strftime("%d.%m"), "\n".join(problems)[:600]), html_mode=False)
            return 1
        warns = list(problems)
        sent: list[dict] = []
        fails_in_row = 0
        for i, p in enumerate(posts, 1):
            try:
                warns += send_post(tg, p, cfg)
                sent.append(p)
                fails_in_row = 0
                log("отправлен пост %d: %s" % (i, p["title"]))
            except Exception as ex:
                fails_in_row += 1
                warns.append("пост %d («%s») не отправлен: %s" % (i, p["title"][:40], scrub(str(ex))))
                log(warns[-1])
                if fails_in_row >= 2:
                    log("два поста подряд не ушли: Telegram недоступен, останавливаюсь")
                    break
            time.sleep(float(cfg["pause_between_messages_sec"]))
        if cfg["send_digest"] and sent:
            try:
                tg.message(build_digest(today, sent, warns), html_mode=False)
            except Exception as ex:
                log("сводка не отправлена: %s" % ex)
        for p in sent:
            history.append({"date": today.isoformat(), "title": p["title"], "source_url": p["source_url"]})
        save_history(history)
        if len(sent) >= int(cfg["min_posts"]):
            STATE.mkdir(exist_ok=True)
            done.write_text(now_msk().isoformat(), encoding="utf-8")
            log("готово: отправлено %d постов" % len(sent))
            return 0
        log("отправлено только %d из %s, день не помечен выполненным" % (len(sent), cfg["min_posts"]))
        return 1
    finally:
        if tg_vpn:
            stop_vpn(cfg)
            log("VPN-прокси для Telegram выключен")
        keep_awake(False)
        LOCK.unlink(missing_ok=True)
        log("=== конец ===")


def cmd_test_telegram(cfg: dict, env: dict) -> int:
    tg = make_tg(env, cfg)
    if tg is None:
        print("В .env не заданы TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID")
        return 2
    vpn = connect_tg(tg, cfg)
    try:
        tg.message("<b>Aion Space</b>\n\nБот подключён. Утренняя подборка будет приходить сюда.")
    except Exception as ex:
        print("Не отправилось: %s" % scrub(str(ex)))
        return 1
    finally:
        if vpn:
            stop_vpn(cfg)
    print("Отправлено, проверьте бота.")
    return 0


def cmd_chat_id(cfg: dict, env: dict) -> int:
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    _SECRETS.append(token)
    if not token:
        print("Сначала впишите TELEGRAM_BOT_TOKEN в .env, напишите боту /start и запустите снова.")
        return 2
    tg = Telegram(token, [], cfg["telegram_api"], env.get("TELEGRAM_PROXY", ""))
    vpn = connect_tg(tg, cfg)
    try:
        updates = tg.call("getUpdates")
    except Exception as ex:
        print("Ошибка: %s" % scrub(str(ex)))
        return 1
    finally:
        if vpn:
            stop_vpn(cfg)
    found = {}
    for u in updates:
        m = u.get("message") or u.get("channel_post") or {}
        c = m.get("chat") or {}
        if c.get("id") is not None:
            found[c["id"]] = c
    if not found:
        print("Обновлений нет. Откройте бота в Telegram, нажмите Start (или напишите ему), и повторите.")
        return 1
    for cid, c in found.items():
        print("TELEGRAM_CHAT_ID=%s   (%s %s)" % (cid, c.get("type", ""), c.get("title") or c.get("username") or c.get("first_name") or ""))
    private = [cid for cid, c in found.items() if c.get("type") == "private"]
    if len(private) == 1 and not env.get("TELEGRAM_CHAT_ID", "").strip():
        try:
            envp = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
            with open(envp, encoding="utf-8-sig") as f:
                lines = f.read().splitlines()
            out, done = [], False
            for ln in lines:
                if ln.strip().startswith("TELEGRAM_CHAT_ID="):
                    out.append("TELEGRAM_CHAT_ID=%s" % private[0]); done = True
                else:
                    out.append(ln)
            if not done:
                out.append("TELEGRAM_CHAT_ID=%s" % private[0])
            with open(envp, "w", encoding="utf-8", newline="\r\n") as f:
                f.write("\n".join(out) + "\n")
            print("Сохранено в .env автоматически.")
        except Exception as ex:
            print("Не удалось записать .env: %s" % ex)
    return 0


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="Aion Space: утренняя подборка постов в бота Telegram")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--dry-run", action="store_true", help="сгенерировать и показать, в Telegram не отправлять")
    r.add_argument("--force", action="store_true", help="игнорировать отметку «уже отправлено сегодня»")
    r.add_argument("--from-json", help="взять готовый JSON вместо запроса к Claude (проверка отправки)")
    sub.add_parser("test-telegram")
    sub.add_parser("chat-id")
    args = ap.parse_args()
    cfg, env = load_settings(), load_env()
    _SECRETS.append(env.get("TELEGRAM_BOT_TOKEN", ""))
    if args.cmd == "run":
        return cmd_run(args, cfg, env)
    if args.cmd == "test-telegram":
        return cmd_test_telegram(cfg, env)
    return cmd_chat_id(cfg, env)


if __name__ == "__main__":
    sys.exit(main())
