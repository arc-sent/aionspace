"""ВК: публикация вместе с Telegram, независимые сбои, повтор по кнопке, сам клиент VK (без сети)."""
import datetime as dt
import sqlite3
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import vk as vkmod  # noqa: E402
from config import Config  # noqa: E402
from db import DB  # noqa: E402
from service import Service  # noqa: E402
from test_server import ADMIN, CHANNEL, MSK, Clock, FakeTG, control_ids, post, press  # noqa: E402
from vk import VK, VKError, group_from_link  # noqa: E402


class FakeVK:
    def __init__(self):
        self.posts, self.fail = [], []          # fail: очередь исключений на ближайшие вызовы

    def post(self, text, image):
        if self.fail:
            raise self.fail.pop(0)
        self.posts.append((text, image))
        return 500 + len(self.posts)

    def wall_link(self, post_id):
        return "https://vk.com/wall-42_%d" % post_id


@pytest.fixture
def env(tmp_path):
    cfg = Config(telegram_token="x", admin_chat_id=ADMIN, channel_id=CHANNEL, api_token="secret",
                 data_dir=tmp_path, vk_token="t", vk_group="aion")
    clock = Clock(dt.datetime(2026, 10, 19, 8, 5, tzinfo=MSK))
    tg, fvk = FakeTG(), FakeVK()

    def fake_image(p, domains):
        return (b"\xff\xd8JPEGDATA", "") if p.get("credit") else (None, "нет подписи автора фото")

    svc = Service(cfg, DB(tmp_path / "aion.db"), tg, now=clock, fetch_image=fake_image, vk=fvk)
    return svc, tg, fvk, clock


def approve_one(svc, tg, now=True):
    svc.ingest({"posts": [post(1)]})
    svc.tick()
    pid = control_ids(tg)[0]
    press(svc, "now" if now else "ap", pid)
    return pid


def test_publishes_to_both(env):
    svc, tg, fvk, clock = env
    pid = approve_one(svc, tg)
    assert len(tg.to(CHANNEL)) == 1 and len(fvk.posts) == 1
    text, image = fvk.posts[0]
    assert image == b"\xff\xd8JPEGDATA"
    assert text.startswith("Заголовок 1\n\nАбзац один.") and "<b>" not in text
    assert "Фото: NASA" in text and "Источник: https://www.nasa.gov/news/1" in text and "#космос #nasa" in text
    r = svc.db.get(pid)
    assert r["status"] == "published" and r["vk_status"] == "published" and r["vk_post_id"] == 501
    assert "ВК: ✅ https://vk.com/wall-42_501" in tg.edits[-1]["text"]
    assert "в канале и в ВК" in [m for m in tg.to(ADMIN) if m["markup"]][0]["payload"]


def test_vk_failure_does_not_block_telegram_and_retry_button(env):
    svc, tg, fvk, clock = env
    fvk.fail = [VKError(15, "VK 15: Access denied", stage="VK wall.post")]
    pid = approve_one(svc, tg)
    r = svc.db.get(pid)
    assert r["status"] == "published" and r["vk_status"] == "failed"
    assert "ВК: ⚠️ не вышло: VK 15" in tg.edits[-1]["text"]
    assert "vk:%d" % pid in str(tg.edits[-1]["markup"])
    press(svc, "vk", pid)
    assert svc.db.get(pid)["vk_status"] == "published" and len(fvk.posts) == 1
    assert len(tg.to(CHANNEL)) == 1                                   # в Telegram повтора нет
    press(svc, "vk", pid)                                             # второе нажатие — без дубля
    assert len(fvk.posts) == 1 and tg.answers[-1] == "Уже обработан"


def test_lost_wall_post_answer_is_not_retried_automatically(env):
    svc, tg, fvk, clock = env
    fvk.fail = [VKError(None, "ответ VK потерян", stage="VK wall.post", network=True, no_retry=True)]
    pid = approve_one(svc, tg, now=False)
    clock.t = clock.t.replace(hour=13)
    svc.tick()
    svc.tick()
    r = svc.db.get(pid)
    assert r["vk_status"] == "unknown" and not fvk.posts
    assert "Проверьте стену" in tg.edits[-1]["text"]
    press(svc, "vk", pid)
    assert svc.db.get(pid)["vk_status"] == "published"


def test_telegram_failure_retries_without_vk_duplicate(env):
    svc, tg, fvk, clock = env
    pid = approve_one(svc, tg, now=False)
    clock.t = clock.t.replace(hour=13)
    tg.fail_channel = 1
    svc.tick()
    assert svc.db.get(pid)["status"] == "approved" and len(fvk.posts) == 1
    svc.tick()
    assert svc.db.get(pid)["status"] == "published"
    assert len(tg.to(CHANNEL)) == 1 and len(fvk.posts) == 1


def test_without_vk_config_nothing_changes(tmp_path):
    cfg = Config(telegram_token="x", admin_chat_id=ADMIN, channel_id=CHANNEL, api_token="s", data_dir=tmp_path)
    assert not cfg.vk_enabled
    tg = FakeTG()
    svc = Service(cfg, DB(tmp_path / "a.db"), tg, now=Clock(dt.datetime(2026, 10, 19, 8, tzinfo=MSK)),
                  fetch_image=lambda p, d: (b"\xff\xd8x" * 1000, ""))
    pid = approve_one(svc, tg)
    assert svc.db.get(pid)["status"] == "published" and "ВК" not in tg.edits[-1]["text"]


def test_old_database_gets_vk_columns(tmp_path):
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY, batch_id INT, n INT, data TEXT, source_url TEXT, "
              "title TEXT, status TEXT, created_at TEXT)")
    c.commit()
    c.close()
    DB(path)
    cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(posts)")}
    assert {"vk_status", "vk_post_id", "vk_error"} <= cols


# ------------------------------------------------------------------ клиент VK
def test_group_from_link():
    assert group_from_link("https://vk.com/aion_space/") == "aion_space"
    assert group_from_link("vk.com/club12345") == "12345"
    assert group_from_link("https://m.vk.com/public777") == "777"
    assert group_from_link("-42") == "42"


class Resp:
    def __init__(self, j):
        self.j = j

    def json(self):
        return self.j

    def raise_for_status(self):
        pass


def fake_vk_api(monkeypatch, script):
    """script: список ответов (dict или исключение) по порядку вызовов requests.post."""
    calls = []

    def fake_post(url, data=None, files=None, timeout=None, proxies=None):
        calls.append((url.rsplit("/", 1)[-1], data))
        item = script.pop(0)
        if isinstance(item, Exception):
            raise item
        return Resp(item)

    monkeypatch.setattr(vkmod.requests, "post", fake_post)
    monkeypatch.setattr(vkmod.time, "sleep", lambda s: None)
    return calls


UPLOAD = {"response": {"upload_url": "https://pu.vk.com/up"}}
UPLOADED = {"server": 1, "photo": '[{"p":1}]', "hash": "h"}
SAVED = {"response": [{"owner_id": -42, "id": 7}]}


def test_vk_post_full_chain_and_flood_retry(monkeypatch):
    calls = fake_vk_api(monkeypatch, [
        UPLOAD, UPLOADED, SAVED, {"error": {"error_code": 9, "error_msg": "Flood control"}},
        UPLOAD, UPLOADED, SAVED, {"response": {"post_id": 33}},
    ])
    v = VK("vk1.a.secret", "42")
    v.group_name = "Aion"
    assert v.post("текст", b"img") == 33
    wall = [d for m, d in calls if m == "wall.post"][-1]
    assert wall["owner_id"] == -42 and wall["from_group"] == 1 and wall["attachments"] == "photo-42_7"
    assert v.wall_link(33) == "https://vk.com/wall-42_33"


def test_vk_lost_answer_on_wall_post_is_not_retried(monkeypatch):
    calls = fake_vk_api(monkeypatch, [UPLOAD, UPLOADED, SAVED, requests.ConnectionError("reset")])
    v = VK("vk1.a.secret", "42")
    v.group_name = "Aion"
    with pytest.raises(VKError) as e:
        v.post("текст", b"img")
    assert e.value.no_retry and sum(1 for m, _ in calls if m == "wall.post") == 1


def test_vk_fatal_error_not_retried_and_token_hidden(monkeypatch):
    fake_vk_api(monkeypatch, [{"error": {"error_code": 27, "error_msg": "Group authorization failed"}}])
    v = VK("vk1.a.secret", "42")
    v.group_name = "Aion"
    with pytest.raises(VKError) as e:
        v.post("текст", b"img")
    assert e.value.code == 27
    assert vkmod.scrub("x access_token=vk1.a.abc&v=1") == "x access_token=***&v=1"
