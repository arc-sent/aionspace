"""Тесты сервера Aion без сети: поддельный Telegram и управляемое время."""
import datetime as dt
import json
import sys
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import api  # noqa: E402
import posts as P  # noqa: E402
from config import Config, parse_slots  # noqa: E402
from db import DB  # noqa: E402
from service import Service  # noqa: E402
from tg import TelegramError  # noqa: E402

MSK = P.MSK
ADMIN, CHANNEL = 111, "@aion_test"


class FakeTG:
    def __init__(self):
        self.sent, self.edits, self.answers, self.previews = [], [], [], []
        self.fail_channel = 0
        self._id = 100

    def _msg(self, chat, kind, payload, markup):
        if chat == CHANNEL and self.fail_channel:
            self.fail_channel -= 1
            raise TelegramError("Bad Request: not enough rights")
        self._id += 1
        self.sent.append({"chat": chat, "kind": kind, "payload": payload, "markup": markup, "id": self._id})
        res = {"message_id": self._id}
        if kind == "photo":
            res["photo"] = [{"file_id": "small"}, {"file_id": "FILE%d" % self._id}]
        return res

    def send_text(self, chat, text, markup=None, html_mode=True, preview_url=""):
        self.previews.append(preview_url)
        return self._msg(chat, "text", text, markup)

    def send_photo(self, chat, photo, caption, markup=None):
        return self._msg(chat, "photo", {"photo": photo if isinstance(photo, str) else "<bytes>",
                                         "caption": caption}, markup)

    def edit_text(self, chat, msg_id, text, markup=None):
        self.edits.append({"msg": msg_id, "text": text, "markup": markup})

    def answer(self, cid, text=""):
        self.answers.append(text)

    def to(self, chat):
        return [m for m in self.sent if m["chat"] == chat]


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def post(n, credit="NASA", **kw):
    p = {"rubric": "Рубрика %d" % n, "title": "Заголовок %d" % n, "paragraphs": ["Абзац один.", "Абзац два."],
         "outro": "Вопрос?", "hashtags": ["космос", "#nasa"], "image_url": "https://www.nasa.gov/img%d.jpg" % n,
         "credit": credit, "source_url": "https://www.nasa.gov/news/%d" % n, "source_label": "NASA"}
    p.update(kw)
    return p


@pytest.fixture
def env(tmp_path):
    cfg = Config(telegram_token="x", admin_chat_id=ADMIN, channel_id=CHANNEL, api_token="secret",
                 data_dir=tmp_path, slots=[(13, 0), (15, 0), (17, 0), (21, 0)])
    clock = Clock(dt.datetime(2026, 10, 19, 8, 5, tzinfo=MSK))
    tg = FakeTG()

    def fake_image(p, domains):
        return (b"\xff\xd8JPEGDATA", "") if p.get("credit") else (None, "нет подписи автора фото")

    svc = Service(cfg, DB(tmp_path / "aion.db"), tg, now=clock, fetch_image=fake_image)
    return svc, tg, clock


def press(svc, action, pid, user=ADMIN):
    svc.handle_update({"callback_query": {"id": "q", "from": {"id": user}, "data": "%s:%d" % (action, pid)}})


def control_ids(tg):
    """id постов из кнопок карточек управления, по порядку."""
    out = []
    for m in tg.to(ADMIN):
        for row in (m["markup"] or {}).get("inline_keyboard", []):
            for b in row:
                if b["callback_data"].startswith("ap:"):
                    out.append(int(b["callback_data"][3:]))
    return out


# ------------------------------------------------------------------ чистые функции
def test_parse_slots():
    assert parse_slots("21, 13,15:30") == [(13, 0), (15, 30), (21, 0)]
    with pytest.raises(ValueError):
        parse_slots("25")


def test_next_slot_skips_taken_and_rolls_to_tomorrow():
    slots = [(13, 0), (15, 0), (17, 0), (21, 0)]
    now = dt.datetime(2026, 10, 19, 14, 0, tzinfo=MSK)
    first = P.next_slot(now, slots, set())
    assert first.hour == 15
    taken = {first.isoformat(), first.replace(hour=17).isoformat(), first.replace(hour=21).isoformat()}
    nxt = P.next_slot(now, slots, taken)
    assert (nxt.day, nxt.hour) == (20, 13)


def test_render_long_text_splits_photo_and_text():
    p = post(1, paragraphs=["x" * 1100])
    parts = P.render(P.validate({"posts": [p]}, set())[0][0], True)
    assert [k for k, _ in parts] == ["photo", "text"]
    assert parts[0][1] == "<b>Заголовок 1</b>"


def test_render_without_image_drops_photo_credit():
    v = P.validate({"posts": [post(1)]}, set())[0][0]
    text = P.render(v, False)[0][1]
    assert "Фото:" not in text and "Источник" in text


def test_validate_rejects_missing_source_and_duplicates():
    good, bad = post(1), post(2, source_url="http://insecure")
    out, problems = P.validate({"posts": [good, bad, post(3, source_url=good["source_url"])]}, set())
    assert len(out) == 1 and len(problems) == 2


# ------------------------------------------------------------------ сценарии
def test_full_flow_approve_and_publish_in_slots(env):
    svc, tg, clock = env
    res = svc.ingest({"day": "2026-10-19", "posts": [post(1), post(2), post(3, credit="")]})
    assert res["accepted"] == 3

    svc.tick()
    admin = tg.to(ADMIN)
    assert admin[0]["kind"] == "text" and "Постов на утверждение: 3" in admin[0]["payload"]   # сводка первой
    assert len(control_ids(tg)) == 3
    assert not tg.to(CHANNEL)                                       # без утверждения ничего не публикуется
    p1, p2, p3 = control_ids(tg)
    assert "Без фото" in [m for m in admin if m["markup"] and "ap:%d" % p3 in json.dumps(m["markup"])][0]["payload"]

    press(svc, "ap", p1)
    press(svc, "ap", p2)
    press(svc, "rj", p3)
    assert svc.db.get(p1)["slot_at"].startswith("2026-10-19T13:00")
    assert svc.db.get(p2)["slot_at"].startswith("2026-10-19T15:00")
    assert svc.db.get(p3)["status"] == "rejected"

    clock.t = clock.t.replace(hour=12, minute=59)
    svc.tick()
    assert not tg.to(CHANNEL)

    clock.t = clock.t.replace(hour=13, minute=0)
    svc.tick()
    ch = tg.to(CHANNEL)
    assert len(ch) == 1 and ch[0]["kind"] == "photo"
    assert ch[0]["payload"]["photo"].startswith("FILE")             # фото не грузится второй раз: file_id из карточки
    r1 = svc.db.get(p1)
    assert r1["status"] == "published" and r1["channel_msg"] == ch[0]["id"]
    assert "https://t.me/aion_test/%d" % ch[0]["id"] in tg.edits[-1]["text"]

    clock.t = clock.t.replace(hour=16)
    svc.tick()
    assert len(tg.to(CHANNEL)) == 2 and svc.db.get(p2)["status"] == "published"


def test_approve_after_last_slot_goes_to_tomorrow(env):
    svc, tg, clock = env
    svc.ingest({"posts": [post(1)]})
    svc.tick()
    clock.t = clock.t.replace(hour=22)
    press(svc, "ap", control_ids(tg)[0])
    assert svc.db.get(control_ids(tg)[0])["slot_at"].startswith("2026-10-20T13:00")


def test_publish_now_and_double_press(env):
    svc, tg, clock = env
    svc.ingest({"posts": [post(1)]})
    svc.tick()
    pid = control_ids(tg)[0]
    press(svc, "now", pid)
    press(svc, "now", pid)
    assert len(tg.to(CHANNEL)) == 1 and svc.db.get(pid)["status"] == "published"
    assert tg.answers[-1] == "Уже обработан"


def test_unapprove_frees_slot(env):
    svc, tg, clock = env
    svc.ingest({"posts": [post(1), post(2)]})
    svc.tick()
    a, b = control_ids(tg)
    press(svc, "ap", a)
    press(svc, "un", a)
    press(svc, "ap", b)
    assert svc.db.get(a)["status"] == "review"
    assert svc.db.get(b)["slot_at"].startswith("2026-10-19T13:00")


def test_channel_failure_retries_then_fails(env):
    svc, tg, clock = env
    svc.ingest({"posts": [post(1)]})
    svc.tick()
    pid = control_ids(tg)[0]
    press(svc, "ap", pid)
    clock.t = clock.t.replace(hour=13)
    tg.fail_channel = 5
    for _ in range(3):
        svc.tick()
    r = svc.db.get(pid)
    assert r["status"] == "failed" and "rights" in r["error"]
    tg.fail_channel = 0
    press(svc, "now", pid)                                          # «🔁 Повторить»
    assert svc.db.get(pid)["status"] == "published"


def test_expire_unanswered(env):
    svc, tg, clock = env
    svc.ingest({"posts": [post(1)]})
    svc.tick()
    pid = control_ids(tg)[0]
    clock.t = clock.t + dt.timedelta(hours=25)
    svc.tick()
    assert svc.db.get(pid)["status"] == "expired"
    assert "снят" in tg.edits[-1]["text"]


def test_strangers_cannot_press(env):
    svc, tg, clock = env
    svc.ingest({"posts": [post(1)]})
    svc.tick()
    pid = control_ids(tg)[0]
    press(svc, "now", pid, user=999)
    assert svc.db.get(pid)["status"] == "review" and tg.answers[-1] == "Нет доступа"


def test_resend_of_same_batch_is_deduplicated(env):
    svc, tg, clock = env
    assert svc.ingest({"posts": [post(1), post(2)]})["accepted"] == 2
    again = svc.ingest({"posts": [post(1), post(2)]})              # ноутбук повторил после таймаута
    assert again["accepted"] == 0 and len(again["problems"]) == 2


def test_empty_batch_notifies(env):
    svc, tg, clock = env
    svc.ingest({"posts": [post(1, source_url="")], "problems": ["claude: мало тем"]})
    svc.tick()
    assert "нет" in tg.to(ADMIN)[0]["payload"] and "мало тем" in tg.to(ADMIN)[0]["payload"]


def test_restart_keeps_state(env, tmp_path):
    svc, tg, clock = env
    svc.ingest({"posts": [post(1)]})
    svc.tick()
    pid = control_ids(tg)[0]
    press(svc, "ap", pid)
    tg2 = FakeTG()
    svc2 = Service(svc.cfg, DB(tmp_path / "aion.db"), tg2, now=clock, fetch_image=svc.fetch_image)
    clock.t = clock.t.replace(hour=18)                             # сервер «лежал» во время слота 13:00
    svc2.tick()
    assert len(tg2.to(CHANNEL)) == 1                               # пропущенный слот догнан
    assert not tg2.to(ADMIN)                                       # карточки повторно не пришли
    assert "Опубликован" in tg2.edits[-1]["text"]


# ------------------------------------------------------------------ HTTP API
def test_api_auth_and_batch(env):
    svc, tg, clock = env
    server = api.start_in_thread("127.0.0.1", 0, "secret", svc)
    base = "http://127.0.0.1:%d" % server.server_address[1]
    try:
        assert json.load(urllib.request.urlopen(base + "/health")) == {"ok": True}
        body = json.dumps({"posts": [post(1)]}).encode()
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(urllib.request.Request(base + "/batch", data=body))
        assert e.value.code == 401
        req = urllib.request.Request(base + "/batch", data=body, headers={"Authorization": "Bearer secret"})
        assert json.load(urllib.request.urlopen(req))["accepted"] == 1
        req = urllib.request.Request(base + "/history?days=7", headers={"Authorization": "Bearer secret"})
        assert json.load(urllib.request.urlopen(req))["items"][0]["title"] == "Заголовок 1"
    finally:
        server.shutdown()
