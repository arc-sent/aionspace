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
from vk import scrub as vk_scrub

log = logging.getLogger("aion")
MAX_PUBLISH_ATTEMPTS = 3


class Service:
    def __init__(self, cfg: Config, db: DB, tg: Telegram,
                 now: Callable[[], dt.datetime] = P.now_msk,
                 fetch_image: Callable[[dict, list[str]], tuple[bytes | None, str]] = P.fetch_image,
                 vk=None):
        self.cfg, self.db, self.tg, self.now, self.fetch_image = cfg, db, tg, now, fetch_image
        self.vk = vk                    # vk.VK или None — тогда посты выходят только в Telegram
        self.images = cfg.data_dir / "images"
        self.images.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ приём с ноутбука
    def ingest(self, body: object) -> dict:
        if not isinstance(body, dict):
            raise ValueError("ожидается JSON-объект с полем posts")
        now = self.now()
        day = str(body.get("day") or now.date().isoformat())[:10]
        accepted, problems = P.validate(body, self.db.known_sources("main")) if body.get("posts") else ([], [])
        articles, a_problems = P.validate_articles(body, self.db.known_sources("dzen"))
        if articles and not self.cfg.dzen_channel_id:
            a_problems.append("Дзен: пришло статей %d, но DZEN_CHANNEL_ID не задан — статьи не приняты" % len(articles))
            articles = []
        problems = [str(x) for x in (body.get("problems") or []) if str(x).strip()] + problems + a_problems
        bid, ids = self.db.add_batch(day, now.isoformat(), accepted + articles, problems)
        log.info("подборка %s за %s: постов %d, статей %d, проблем %d", bid, day, len(accepted), len(articles),
                 len(problems))
        return {"ok": True, "batch": bid, "accepted": len(accepted), "articles": len(articles), "problems": problems,
                "sources": [p["source_url"] for p in accepted],
                "article_sources": [a["source_url"] for a in articles]}

    # ------------------------------------------------------------------ площадки
    def _is_dzen(self, r) -> bool:
        return r["target"] == "dzen"

    def _channel(self, r):
        return self.cfg.dzen_channel_id if self._is_dzen(r) else self.cfg.channel_id

    def _slots(self, r):
        return self.cfg.dzen_slots if self._is_dzen(r) else self.cfg.slots

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
                main = [json.loads(r["data"]) for r in rows if not self._is_dzen(r)]
                dzen = [json.loads(r["data"]) for r in rows if self._is_dzen(r)]
                text = P.build_digest(b["day"], main, problems, self.cfg.slots, dzen, self.cfg.dzen_slots,
                                      self.cfg.dzen_channel_id)
                if self.vk:
                    text = text.replace(" по одному.", " по одному, и в канал, и в ВК.", 1)
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
            res = self.fetch_image(data, self.cfg.image_domains)
            img, why, src = res[0], res[1], (res[2] if len(res) > 2 else "")
        except Exception as ex:
            img, why, src = None, "картинка не скачалась: %s" % ex, ""
        if img:
            path = self.images / ("%d.jpg" % r["id"])
            path.write_bytes(img)
            self.db.update(r["id"], image_path=str(path), image_src=src or None)
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
        if self._is_dzen(r):
            # статья — одно сообщение до 4096 знаков; фото — крупное превью ссылки на картинку над текстом
            text, _ = P.build_article(data, self.cfg.dzen_cta, self.cfg.dzen_cta_marker,
                                      with_credit=bool(r["image_src"]))
            res = self.tg.send_text(chat, text, preview_url=r["image_src"] or "")
            return res["message_id"], None
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
        """Telegram и ВК независимы: каждая площадка публикуется один раз, сбой одной не мешает другой.
        Статус поста (published/failed) — по Telegram; у ВК свой vk_status."""
        r = self.db.get(pid)
        err = None
        if not r["channel_msg"]:
            try:
                msg_id, file_id = self._send_parts(self._channel(r), r)
                self.db.update(pid, channel_msg=msg_id, image_file_id=file_id or r["image_file_id"])
                log.info("пост %s опубликован в канал, сообщение %s", pid, msg_id)
            except Exception as ex:
                err = scrub(str(ex))[:300]
        self._publish_vk(pid)
        if err is None:
            self.db.transition(pid, ("publishing",), "published", published_at=self.now().isoformat(), error=None)
            self._refresh_control(pid)
            return
        attempts = r["attempts"] + 1
        new = "failed" if attempts >= MAX_PUBLISH_ATTEMPTS else "approved"
        self.db.transition(pid, ("publishing",), new, attempts=attempts, error=err)
        log.warning("пост %s не ушёл в канал (попытка %d): %s", pid, attempts, err)
        if new == "failed":
            self._refresh_control(pid)

    def _publish_vk(self, pid: int, force: bool = False) -> None:
        if not self.vk:
            return
        r = self.db.get(pid)
        if self._is_dzen(r):            # статьи Дзена в ВК не дублируем
            return
        if r["vk_status"] == "published" or (r["vk_status"] == "unknown" and not force):
            return
        image = None
        if r["image_path"]:
            try:
                with open(r["image_path"], "rb") as f:
                    image = f.read()
            except OSError:
                pass
        try:
            post_id = self.vk.post(P.vk_text(json.loads(r["data"]), image is not None), image)
        except Exception as ex:
            status = "unknown" if getattr(ex, "no_retry", False) else "failed"
            self.db.update(pid, vk_status=status, vk_error=vk_scrub(str(ex))[:300])
            log.warning("пост %s не вышел в ВК (%s): %s", pid, status, vk_scrub(str(ex)))
            return
        self.db.update(pid, vk_status="published", vk_post_id=post_id, vk_error=None)
        log.info("пост %s опубликован в ВК, запись %s", pid, post_id)

    # ------------------------------------------------------------------ карточка управления
    def channel_link(self, msg_id: int | None, channel=None) -> str:
        ch = str(channel or self.cfg.channel_id)
        if not msg_id:
            return ""
        if ch.startswith("@"):
            return "https://t.me/%s/%s" % (ch[1:], msg_id)
        if ch.startswith("-100"):
            return "https://t.me/c/%s/%s" % (ch[4:], msg_id)
        return ""

    def _vk_line(self, r) -> str:
        if not self.vk or self._is_dzen(r) or r["status"] not in ("published", "failed"):
            return ""
        st = r["vk_status"]
        if st == "published":
            return "\nВК: ✅ %s" % self.vk.wall_link(r["vk_post_id"])
        if st == "failed":
            return "\nВК: ⚠️ не вышло: %s" % (r["vk_error"] or "ошибка")
        if st == "unknown":
            return "\nВК: ❓ ответ ВК потерян, запись могла выйти. Проверьте стену группы, прежде чем повторять"
        return ""

    def _control_text(self, r) -> str:
        return self._control_body(r) + self._vk_line(r)

    def _control_body(self, r) -> str:
        data = json.loads(r["data"])
        same = [x for x in self.db.batch_posts(r["batch_id"]) if x["target"] == r["target"]]
        n = next((k for k, x in enumerate(same, 1) if x["id"] == r["id"]), r["n"])
        kind = "Статья для Дзена" if self._is_dzen(r) else "Пост"
        head = "%s %d из %d · %s\n«%s»" % (kind, n, len(same), data.get("rubric", ""), r["title"])
        st = r["status"]
        if st in ("new", "review"):
            note = "\n⚠️ Без фото: %s" % r["image_note"] if r["image_note"] and not r["image_path"] else ""
            if self._is_dzen(r):
                where = "в %s (оттуда — в Дзен)" % self.cfg.dzen_channel_id
            else:
                where = "в канале и в ВК" if self.vk else "в канале"
            return "%s%s\n\nОпубликовать %s?" % (head, note, where)
        if st == "approved":
            return "%s\n\n✅ Утверждён, выйдет %s" % (head, P.human_time(r["slot_at"], self.now()))
        if st == "publishing":
            return "%s\n\n⏳ Публикуется…" % head
        if st == "published":
            when = P.human_time(r["published_at"], self.now())
            return "%s\n\n📢 Опубликован %s\n%s" % (head, when, self.channel_link(r["channel_msg"], self._channel(r)))
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
        vk_retry = [("🔁 ВК ещё раз", "vk:%d" % i)] if self.vk and r["vk_status"] in ("failed", "unknown") else []
        if st == "failed":
            return kb([("🔁 Повторить", "now:%d" % i)] + vk_retry)
        if vk_retry:
            return kb(vk_retry)
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
            r = self.db.get(pid)
            slot = P.next_slot(now, self._slots(r), self.db.taken_slots(r["target"])).isoformat()
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
        if action == "vk":
            r = self.db.get(pid)
            if not self.vk or r["status"] not in ("published", "failed") or not self.db.vk_claim(pid):
                return "Уже обработан"
            self._publish_vk(pid, force=True)
            return "Вышло в ВК" if self.db.get(pid)["vk_status"] == "published" else "ВК снова не принял"
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
                tag = "Дзен · " if self._is_dzen(r) else ""
                lines.append("• %s — %s«%s»" % (P.human_time(r["slot_at"], self.now()), tag, r["title"][:60]))
        lines.append("")
        lines.append("Слоты: " + ", ".join("%d:%02d" % s for s in self.cfg.slots))
        if self.cfg.dzen_channel_id:
            lines.append("Дзен (%s): %s" % (self.cfg.dzen_channel_id,
                                            ", ".join("%d:%02d" % s for s in self.cfg.dzen_slots)))
        return "\n".join(lines)
