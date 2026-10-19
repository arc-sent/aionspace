"""SQLite: подборки, посты и их статусы. Каждый вызов открывает своё соединение (API, планировщик и
опрос Telegram работают в разных потоках).

Статус поста:
    new        — пришёл с ноутбука, карточка на утверждение ещё не отправлена
    review     — карточка у вас в личке, ждёт кнопки
    approved   — утверждён, slot_at = когда выйдет
    publishing — прямо сейчас уходит в канал (захват, чтобы не опубликовать дважды)
    published  — вышел в канал
    rejected   — отклонён
    expired    — не утверждён за EXPIRE_HOURS
    failed     — не ушёл в канал за несколько попыток
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    received_at TEXT NOT NULL,
    problems TEXT NOT NULL DEFAULT '[]',
    digest_sent INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL,
    n INTEGER NOT NULL,
    data TEXT NOT NULL,
    source_url TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    created_at TEXT NOT NULL,
    image_path TEXT,
    image_note TEXT,
    image_file_id TEXT,
    review_msg INTEGER,
    slot_at TEXT,
    published_at TEXT,
    channel_msg INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
CREATE INDEX IF NOT EXISTS posts_status ON posts(status);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


class DB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            c.execute("PRAGMA journal_mode=WAL")
            yield c
            c.commit()
        finally:
            c.close()

    # ---- подборки
    def add_batch(self, day: str, received_at: str, posts: list[dict], problems: list[str]) -> tuple[int, list[int]]:
        with self.conn() as c:
            cur = c.execute("INSERT INTO batches(day, received_at, problems) VALUES (?,?,?)",
                            (day, received_at, json.dumps(problems, ensure_ascii=False)))
            bid = cur.lastrowid
            ids = []
            for n, p in enumerate(posts, 1):
                cur = c.execute(
                    "INSERT INTO posts(batch_id, n, data, source_url, title, created_at) VALUES (?,?,?,?,?,?)",
                    (bid, n, json.dumps(p, ensure_ascii=False), p["source_url"], p["title"], received_at))
                ids.append(cur.lastrowid)
            return bid, ids

    def batches_without_digest(self) -> list[sqlite3.Row]:
        with self.conn() as c:
            return c.execute("SELECT * FROM batches WHERE digest_sent=0 ORDER BY id").fetchall()

    def mark_digest(self, bid: int) -> None:
        with self.conn() as c:
            c.execute("UPDATE batches SET digest_sent=1 WHERE id=?", (bid,))

    def batch_posts(self, bid: int) -> list[sqlite3.Row]:
        with self.conn() as c:
            return c.execute("SELECT * FROM posts WHERE batch_id=? ORDER BY n", (bid,)).fetchall()

    # ---- посты
    def get(self, pid: int) -> sqlite3.Row | None:
        with self.conn() as c:
            return c.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()

    def by_status(self, *statuses: str) -> list[sqlite3.Row]:
        q = "SELECT * FROM posts WHERE status IN (%s) ORDER BY id" % ",".join("?" * len(statuses))
        with self.conn() as c:
            return c.execute(q, statuses).fetchall()

    def update(self, pid: int, **fields) -> None:
        if not fields:
            return
        sets = ", ".join("%s=?" % k for k in fields)
        with self.conn() as c:
            c.execute("UPDATE posts SET %s WHERE id=?" % sets, (*fields.values(), pid))

    def transition(self, pid: int, old: tuple[str, ...], new: str, **fields) -> bool:
        """Сменить статус, только если он сейчас один из old. True — получилось (защита от двойного нажатия
        и от гонки планировщика с кнопкой «Сейчас»)."""
        sets = ", ".join(["status=?"] + ["%s=?" % k for k in fields])
        q = "UPDATE posts SET %s WHERE id=? AND status IN (%s)" % (sets, ",".join("?" * len(old)))
        with self.conn() as c:
            return c.execute(q, (new, *fields.values(), pid, *old)).rowcount == 1

    def taken_slots(self) -> set[str]:
        with self.conn() as c:
            rows = c.execute("SELECT slot_at FROM posts WHERE status IN ('approved','publishing','published') "
                             "AND slot_at IS NOT NULL").fetchall()
        return {r["slot_at"] for r in rows}

    def due(self, now_iso: str) -> list[sqlite3.Row]:
        with self.conn() as c:
            return c.execute("SELECT * FROM posts WHERE status='approved' AND slot_at<=? ORDER BY slot_at, id",
                             (now_iso,)).fetchall()

    def known_sources(self) -> set[str]:
        """Источники, которые уже были (кроме отклонённых и снятых: их тему можно предложить снова)."""
        with self.conn() as c:
            rows = c.execute("SELECT source_url FROM posts WHERE status NOT IN ('rejected','expired')").fetchall()
        return {r["source_url"] for r in rows}

    def history(self, since_iso: str) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT created_at, title, source_url, status FROM posts WHERE created_at>=? "
                             "ORDER BY id", (since_iso,)).fetchall()
        return [{"date": r["created_at"][:10], "title": r["title"], "source_url": r["source_url"],
                 "status": r["status"]} for r in rows]

    def counts(self) -> dict[str, int]:
        with self.conn() as c:
            rows = c.execute("SELECT status, COUNT(*) n FROM posts GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    # ---- разное
    def kv_get(self, k: str, default: str = "") -> str:
        with self.conn() as c:
            r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r["v"] if r else default

    def kv_set(self, k: str, v: str) -> None:
        with self.conn() as c:
            c.execute("INSERT INTO kv(k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
