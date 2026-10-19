"""Настройки сервера Aion из окружения (и из .env рядом, если он есть)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent

DEFAULT_IMAGE_DOMAINS = ["nasa.gov", "esawebb.org", "esahubble.org", "eso.org", "esa.int",
                         "webbtelescope.org", "hubblesite.org", "stsci.edu",
                         "chandra.si.edu", "chandra.harvard.edu"]


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def parse_slots(text: str) -> list[tuple[int, int]]:
    """«13, 15:30,21» -> [(13, 0), (15, 30), (21, 0)], по возрастанию."""
    out = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        hh, _, mm = part.partition(":")
        h, m = int(hh), int(mm or 0)
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError("неверное время слота: %s" % part)
        out.append((h, m))
    if not out:
        raise ValueError("SLOTS пуст")
    return sorted(set(out))


@dataclass
class Config:
    telegram_token: str = ""
    admin_chat_id: int = 0
    channel_id: str = ""
    slots: list[tuple[int, int]] = field(default_factory=lambda: [(13, 0), (15, 0), (17, 0), (21, 0)])
    api_token: str = ""
    api_host: str = "127.0.0.1"
    api_port: int = 8788
    data_dir: Path = HERE / "data"
    telegram_api: str = "https://api.telegram.org"
    telegram_proxy: str = ""
    expire_hours: int = 24
    history_days: int = 30
    image_domains: list[str] = field(default_factory=lambda: list(DEFAULT_IMAGE_DOMAINS))

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv(HERE / ".env")
        e = os.environ.get
        cfg = cls(
            telegram_token=e("TELEGRAM_TOKEN", ""),
            admin_chat_id=int(e("ADMIN_CHAT_ID") or 0),
            channel_id=e("CHANNEL_ID", ""),
            slots=parse_slots(e("SLOTS") or "13,15,17,21"),
            api_token=e("AION_API_TOKEN", ""),
            api_host=e("AION_API_HOST", "127.0.0.1"),
            api_port=int(e("AION_API_PORT") or 8788),
            data_dir=Path(e("DATA_DIR") or (HERE / "data")),
            telegram_api=e("TELEGRAM_API") or "https://api.telegram.org",
            telegram_proxy=e("TELEGRAM_PROXY", ""),
            expire_hours=int(e("EXPIRE_HOURS") or 24),
            history_days=int(e("HISTORY_DAYS") or 30),
        )
        if e("IMAGE_DOMAINS"):
            cfg.image_domains = [d.strip().lower() for d in e("IMAGE_DOMAINS").split(",") if d.strip()]
        return cfg

    def check(self) -> list[str]:
        miss = []
        if not self.telegram_token:
            miss.append("TELEGRAM_TOKEN")
        if not self.admin_chat_id:
            miss.append("ADMIN_CHAT_ID")
        if not self.channel_id:
            miss.append("CHANNEL_ID")
        if not self.api_token:
            miss.append("AION_API_TOKEN")
        return miss
