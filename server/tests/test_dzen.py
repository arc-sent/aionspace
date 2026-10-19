"""Статьи для Дзена: свой канал, свои слоты, одно сообщение с превью-картинкой, крючок на основной канал."""
import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import posts as P  # noqa: E402
from config import Config  # noqa: E402
from db import DB  # noqa: E402
from service import Service  # noqa: E402
from test_server import ADMIN, CHANNEL, MSK, Clock, FakeTG, control_ids, post, press  # noqa: E402
from test_vk import FakeVK  # noqa: E402

DZEN = "@interesOKosmose"


def article(n=1, **kw):
    a = {"rubric": "Почему", "title": "Почему на Венере день длиннее года %d" % n,
         "lead": "Звучит как ошибка, но это правда.",
         "sections": [{"heading": "Проверьте себя", "paragraphs": ["Сколько длятся сутки на Венере?", "А) 1 день Б) 243 дня"]},
                      {"heading": "Ответ", "paragraphs": ["243 земных дня."]},
                      {"heading": "Почему так", "paragraphs": ["Облака и приливы тормозят планету."]}],
         "outro": "А вы бы смогли жить на Венере?", "cta": "Свежие новости — в t.me/aion_space",
         "hashtags": ["#космос", "#венера"], "image_url": "https://science.nasa.gov/venus%d.jpg" % n,
         "credit": "NASA/JPL", "source_url": "https://science.nasa.gov/venus/%d" % n, "source_label": "NASA",
         "interactive": "Проверьте себя, раздел 1"}
    a.update(kw)
    return a


@pytest.fixture
def env(tmp_path):
    cfg = Config(telegram_token="x", admin_chat_id=ADMIN, channel_id=CHANNEL, api_token="s", data_dir=tmp_path,
                 dzen_channel_id=DZEN, dzen_slots=[(12, 0)], vk_token="t", vk_group="g")
    clock = Clock(dt.datetime(2026, 10, 19, 8, 5, tzinfo=MSK))
    tg, fvk = FakeTG(), FakeVK()

    def fake_image(p, domains):
        return b"\xff\xd8IMG", "", p.get("image_url") or "https://www.nasa.gov/og.jpg"

    svc = Service(cfg, DB(tmp_path / "aion.db"), tg, now=clock, fetch_image=fake_image, vk=fvk)
    return svc, tg, fvk, clock


def test_article_render_has_bold_headings_and_cta():
    a = P.validate_articles({"articles": [article()]}, set())[0][0]
    text, warns = P.build_article(a, "DEFAULT CTA t.me/aion_space", "aion_space")
    assert text.startswith("<b>Почему на Венере день длиннее года 1</b>\n\n<i>Звучит как ошибка")
    assert "<b>Проверьте себя</b>\nСколько длятся" in text
    assert "Свежие новости — в t.me/aion_space" in text and "DEFAULT CTA" not in text
    assert "Фото: NASA/JPL" in text and not warns


def test_missing_cta_gets_default_hook():
    a = P.validate_articles({"articles": [article(cta="Подписывайтесь!")]}, set())[0][0]
    text, _ = P.build_article(a, "Каждый день — t.me/aion_space", "aion_space")
    assert "Подписывайтесь!" in text and "Каждый день — t.me/aion_space" in text


def test_too_long_article_drops_sections_from_the_end():
    long = [{"heading": "Раздел %d" % i, "paragraphs": ["x" * 900]} for i in range(6)]
    a = P.validate_articles({"articles": [article(sections=long)]}, set())[0][0]
    text, warns = P.build_article(a, "t.me/aion_space", "aion_space")
    assert P.utf16_len(P.plain(text)) <= 4096 and warns and "Раздел 0" in text and "Раздел 5" not in text


def test_article_flow_own_channel_slot_and_no_vk(env):
    svc, tg, fvk, clock = env
    res = svc.ingest({"posts": [post(1)], "articles": [article()]})
    assert res["accepted"] == 1 and res["articles"] == 1
    svc.tick()
    digest = tg.to(ADMIN)[0]["payload"]
    assert "Статей для Дзена: 1 (@interesOKosmose, в 12:00)" in digest and "Интерактив" in digest
    ids = control_ids(tg)
    assert len(ids) == 2
    art = [i for i in ids if svc.db.get(i)["target"] == "dzen"][0]
    card = [m for m in tg.to(ADMIN) if m["markup"] and "ap:%d" % art in str(m["markup"])][0]["payload"]
    assert card.startswith("Большая статья для Дзена 1 из 1") and "в @interesOKosmose (оттуда — в Дзен)" in card
    assert "https://science.nasa.gov/venus1.jpg" in tg.previews        # карточка с превью картинки

    press(svc, "ap", art)
    press(svc, "ap", [i for i in ids if i != art][0])
    assert svc.db.get(art)["slot_at"].startswith("2026-10-19T12:00")   # свой слот, основной — 13:00
    clock.t = clock.t.replace(hour=12)
    svc.tick()
    out = tg.to(DZEN)
    assert len(out) == 1 and out[0]["kind"] == "text" and "t.me/aion_space" in out[0]["payload"]
    assert not tg.to(CHANNEL) and not fvk.posts                         # основной пост ещё ждёт 13:00, ВК не тронут
    assert "https://t.me/interesOKosmose/%d" % out[0]["id"] in tg.edits[-1]["text"]
    clock.t = clock.t.replace(hour=13)
    svc.tick()
    assert len(tg.to(CHANNEL)) == 1 and len(fvk.posts) == 1 and len(tg.to(DZEN)) == 1


def test_article_and_post_may_share_a_source(env):
    svc, tg, fvk, clock = env
    src = "https://science.nasa.gov/venus/1"
    res = svc.ingest({"posts": [post(1, source_url=src)], "articles": [article(source_url=src)]})
    assert res["accepted"] == 1 and res["articles"] == 1
    again = svc.ingest({"articles": [article(source_url=src)]})
    assert again["articles"] == 0


def test_articles_rejected_without_dzen_channel(tmp_path):
    cfg = Config(telegram_token="x", admin_chat_id=ADMIN, channel_id=CHANNEL, api_token="s", data_dir=tmp_path)
    svc = Service(cfg, DB(tmp_path / "a.db"), FakeTG(), now=Clock(dt.datetime(2026, 10, 19, 8, tzinfo=MSK)))
    res = svc.ingest({"articles": [article()]})
    assert res["articles"] == 0 and "DZEN_CHANNEL_ID" in res["problems"][-1]


def test_short_article_goes_as_photo_with_caption(env):
    svc, tg, fvk, clock = env
    short = article(format="short", sections=[{"heading": "Факт", "paragraphs": ["Один удивительный факт."]}])
    svc.ingest({"articles": [short, article(2)]})
    svc.tick()
    s_id, l_id = control_ids(tg)
    cards = [m["payload"] for m in tg.to(ADMIN) if m["markup"]]
    assert cards[0].startswith("Короткая статья для Дзена (фото) 1 из 2")
    assert cards[1].startswith("Большая статья для Дзена 2 из 2")
    press(svc, "now", s_id)
    press(svc, "now", l_id)
    out = tg.to(DZEN)
    assert [m["kind"] for m in out] == ["photo", "text"]
    assert out[0]["payload"]["photo"].startswith("FILE")                # то же фото, что в карточке
    cap = out[0]["payload"]["caption"]
    assert P.utf16_len(P.plain(cap)) <= 1024 and "t.me/aion_space" in cap and "Фото: NASA/JPL" in cap


def test_short_article_is_trimmed_to_caption_limit():
    long_secs = [{"heading": "Раздел %d" % i, "paragraphs": ["Предложение номер %d. " % i * 20]} for i in range(3)]
    a = P.validate_articles({"articles": [article(format="short", sections=long_secs)]}, set())[0][0]
    text, warns = P.build_article(a, "t.me/aion_space", "aion_space", limit=P.CAPTION_LIMIT)
    assert P.utf16_len(P.plain(text)) <= 1024 and warns
    assert "Раздел 0" in text and "Раздел 2" not in text and "t.me/aion_space" in text
