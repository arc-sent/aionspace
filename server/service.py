"""Логика сервера Aion: приём подборки, карточки на утверждение, кнопки, публикация по слотам.

Всё, что меняет состояние, сначала пишется в БД, поэтому рестарт ничего не теряет: неотправленные карточки
уйдут при следующем тике, а пропущенные слоты будут опубликованы, как только сервер снова заработает."""
from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Callable

import posts as P
from config import Config
from db import DB
from tg import Telegram, TelegramError, kb, scrub

log = logging.getLogger("aion")
MAX_PUBLISH_ATTEMPTS = 3


class Service:
    def __init__(self, cfg: Config, db: DB, tg: Telegram,
                 now: Callable[[], dt.datetime] = P.now_msk,
                 fetch_image: Callable[[dict, list[str]], tuple[bytes | None, str]] = P.fetch_image):
        self.cfg, self.db, self.tg, self.now, self.fetch_image = cfg, db, tg, now, fetch_image
        self.images = cfg.data_dir / "images"
        self.images.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ приём с ноутбука
    def ingest(self, body: object) -> dict:
        if not isinstance(body, dict):
            raise ValueError("ожидается JSON-объект с полем posts")
        now = self.now()
        day = str(body.get("day") or now.date().isoformat())[:10]
        accepted, problems = P.validate(body, self.db.known_sources())
        problems = [str(x) for x in (body.get("problems") or []) if str(x).strip()] + problems
        bid, ids = self.db.add_batch(day, now.isoformat(), accepted, problems)
        log.info("подборка %s за %s: принято %d, проблем %d", bid, day, len(ids), len(problems))
        return {"ok": True, "batch": bid, "accepted": len(ids), "problems": problems,
                "sources": [p["source_url"] for p in accepted]}

    def history(self, days: int) -> list[dict]:
        since = (self.now() - dt.timedelta(days=days)).isoformat()
        return self.db.history(since)

    # ------------------------------------------------------------------ периодическая работа
    def tick(self) -> None:
        for step in (self._send_digests, self._send_reviews, self._expire, self._publish_due):
            try:
                step()
            except Exception:
                log.exception("ошибка шага %s", step.__name__)

    def _send_digests(self) -> None:
        for b in self.db.batches_without_digest():
            rows = self.db.batch_posts(b["id"])
            problems = json.loads(b["problems"])
            if rows:
                text = P.build_digest(b["day"], [json.loads(r["data"]) for r in rows], problems, self.cfg.slots)
            else:
                text = "❌ Aion Space: подборка за %s пришла, но подходящих постов нет.\n%s" % (
                    b["day"], "\n".join("- " + x for x in problems))[:4000]
            self.tg.send_text(self.cfg.admin_chat_id, text, html_mode=False)
            self.db.mark_digest(b["id"])

    def _send_reviews(self) -> None:
        for r in self.db.by_status("new"):
            self._prepare_image(r)
            r = self.db.get(r["id"])
            self._send_review(r)

    def _prepare_image(self, r) -> None:
        if r["image_path"] or r["image_note"]:
            return
        data = json.loads(r["data"])
        try:
            img, why = self.fetch_image(data, self.cfg.image_domains)
        except Exception as ex:
            img, why = None, "картинка не скачалась: %s" % ex
        if img:
            path = self.images / ("%d.jpg" % r["id"])
            path.write_bytes(img)
            self.db.update(r["id"], image_path=str(path))
        else:
            self.db.update(r["id"], image_note=why or "картинки нет")

    def _image_ref(self, r):
        if r["image_file_id"]:
            return r["image_file_id"]
        if r["image_path"]:
            try:
                with open(r["image_path"], "rb") as f:
                    return f.read()
            except OSError:
                return None
        return None

    def _send_parts(self, chat, r) -> tuple[int, str | None]:
        """Отправить пост как он выглядит в канале. (id первого сообщения, file_id фото)."""
        data = json.loads(r["data"])
        photo = self._image_ref(r)
        first, file_id = None, r["image_file_id"]
        for kind, text in P.render(data, photo is not None):
            if kind == "photo":
                res = self.tg.send_photo(chat, photo, text)
                if res.get("photo"):
                    file_id = res["photo"][-1]["file_id"]
            else:
                res = self.tg.send_text(chat, text)
            first = first or res["message_id"]
        return first, file_id

    def _send_review(self, r) -> None:
        _, file_id = self._send_parts(self.cfg.admin_chat_id, r)
        if file_id and not r["image_file_id"]:
            self.db.update(r["id"], image_file_id=file_id)
        r = self.db.get(r["id"])
        msg = self.tg.send_text(self.cfg.admin_chat_id, self._control_text(r), self._control_kb(r), html_mode=False)
        self.db.transition(r["id"], ("new",), "review", review_msg=msg["message_id"])

    def _expire(self) -> None:
        limit = (self.now() - dt.timedelta(hours=self.cfg.expire_hours)).isoformat()
        for r in self.db.by_status("review"):
            if r["created_at"] < limit and self.db.transition(r["id"], ("review",), "expired"):
                self._refresh_control(r["id"])

    def _publish_due(self) -> None:
        for r in self.db.due(self.now().isoformat()):
            if self.db.transition(r["id"], ("approved",), "publishing"):
                self._publish(r["id"])

    def _publish(self, pid: int) -> None:
        r = self.db.get(pid)
        try:
            msg_id, file_id = self._send_parts(self.cfg.channel_id, r)
        except Exception as ex:
            err = scrub(str(ex))[:300]
            attempts = r["attempts"] + 1
            new = "failed" if attempts >= MAX_PUBLISH_ATTEMPTS else "approved"
            self.db.transition(pid, ("publishing",), new, attempts=attempts, error=err)
            log.warning("пост %s не ушёл в канал (попытка %d): %s", pid, attempts, err)
            if new == "failed":
                self._refresh_control(pid)
            return
        self.db.transition(pid, ("publishing",), "published", published_at=self.now().isoformat(),
                           channel_msg=msg_id, image_file_id=file_id or r["image_file_id"], error=None)
        log.info("пост %s опубликован, сообщение %s", pid, msg_id)
        self._refresh_control(pid)

    # ------------------------------------------------------------------ карточка управления
    def channel_link(self, msg_id: int | None) -> str:
        ch = str(self.cfg.channel_id)
        if not msg_id:
            return ""
        if ch.startswith("@"):
            return "https://t.me/%s/%s" % (ch[1:], msg_id)
        if ch.startswith("-100"):
            return "https://t.me/c/%s/%s" % (ch[4:], msg_id)
        return ""

    def _control_text(self, r) -> str:
        data = json.loads(r["data"])
        total = len(self.db.batch_posts(r["batch_id"]))
        head = "Пост %d из %d · %s\n«%s»" % (r["n"], total, data.get("rubric", ""), r["title"])
        st = r["status"]
        if st in ("new", "review"):
            note = "\n⚠️ Без фото: %s" % r["image_note"] if r["image_note"] and not r["image_path"] else ""
            return "%s%s\n\nОпубликовать в канале?" % (head, note)
        if st == "approved":
            return "%s\n\n✅ Утверждён, выйдет %s" % (head, P.human_time(r["slot_at"], self.now()))
        if st == "publishing":
            return "%s\n\n⏳ Публикуется…" % head
        if st == "published":
            when = P.human_time(r["published_at"], self.now())
            return "%s\n\n📢 Опубликован %s\n%s" % (head, when, self.channel_link(r["channel_msg"]))
        if st == "rejected":
            return "%s\n\n❌ Отклонён" % head
        if st == "expired":
            return "%s\n\n⌛ Не утверждён за %d ч, снят" % (head, self.cfg.expire_hours)
        if st == "failed":
            return "%s\n\n⚠️ Не ушёл в канал: %s" % (head, r["error"] or "ошибка")
        return head

    def _control_kb(self, r) -> dict:
        i, st = r["id"], r["status"]
        if st in ("new", "review"):
            return kb([("✅ Опубликовать", "ap:%d" % i), ("❌ Отклонить", "rj:%d" % i)],
                      [("🚀 Сейчас", "now:%d" % i)])
        if st == "approved":
            return kb([("🚀 Сейчас", "now:%d" % i), ("↩️ Отменить", "un:%d" % i)])
        if st in ("rejected", "expired"):
            return kb([("↩️ Вернуть", "un:%d" % i)])
        if st == "failed":
            return kb([("🔁 Повторить", "now:%d" % i)])
        return {"inline_keyboard": []}

    def _refresh_control(self, pid: int) -> None:
        r = self.db.get(pid)
        if not r or not r["review_msg"]:
            return
        try:
            self.tg.edit_text(self.cfg.admin_chat_id, r["review_msg"], self._control_text(r), self._control_kb(r))
        except TelegramError as ex:
            log.warning("не обновил карточку %s: %s", pid, ex)

    # ------------------------------------------------------------------ кнопки и команды
    def handle_update(self, u: dict) -> None:
        if "callback_query" in u:
            return self._on_button(u["callback_query"])
        m = u.get("message") or {}
        if (m.get("from") or {}).get("id") != self.cfg.admin_chat_id:
            return
        text = (m.get("text") or "").strip()
        if text.startswith("/start") or text.startswith("/status"):
            self.tg.send_text(self.cfg.admin_chat_id, self.status_text(), html_mode=False)

    def _on_button(self, q: dict) -> None:
        if (q.get("from") or {}).get("id") != self.cfg.admin_chat_id:
            return self.tg.answer(q["id"], "Нет доступа")
        action, _, sid = str(q.get("data", "")).partition(":")
        if not sid.isdigit() or not self.db.get(int(sid)):
            return self.tg.answer(q["id"], "Пост не найден")
        pid = int(sid)
        reply = self.act(action, pid)
        self.tg.answer(q["id"], reply)
        self._refresh_control(pid)

    def act(self, action: str, pid: int) -> str:
        """Действие кнопки. Возвращает короткий ответ для всплывашки."""
        now = self.now()
        if action == "ap":
            slot = P.next_slot(now, self.cfg.slots, self.db.taken_slots()).isoformat()
            if self.db.transition(pid, ("review", "new"), "approved", slot_at=slot):
                return "Выйдет %s" % P.human_time(slot, now)
            return "Уже обработан"
        if action == "rj":
            return "Отклонён" if self.db.transition(pid, ("review", "new"), "rejected") else "Уже обработан"
        if action == "un":
            if self.db.transition(pid, ("approved", "rejected", "expired"), "review", slot_at=None):
                return "Снова ждёт решения"
            return "Уже обработан"
        if action == "now":
            if self.db.transition(pid, ("review", "new", "approved", "failed"), "publishing",
                                  slot_at=now.isoformat(), attempts=0):
                self._publish(pid)
                r = self.db.get(pid)
                return "Опубликован" if r["status"] == "published" else "Не вышло, повторю"
            return "Уже обработан"
        return "Неизвестная кнопка"

    def status_text(self) -> str:
        c = self.db.counts()
        lines = ["Aion Space · сервер работает",
                 "Ждут решения: %d" % (c.get("review", 0) + c.get("new", 0)),
                 "В очереди на публикацию: %d" % c.get("approved", 0),
                 "Опубликовано всего: %d" % c.get("published", 0)]
        queue = self.db.by_status("approved")
        if queue:
            lines.append("")
            for r in sorted(queue, key=lambda r: r["slot_at"]):
                lines.append("• %s — «%s»" % (P.human_time(r["slot_at"], self.now()), r["title"][:60]))
        lines.append("")
        lines.append("Слоты: " + ", ".join("%d:%02d" % s for s in self.cfg.slots))
        return "\n".join(lines)
