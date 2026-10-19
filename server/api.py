"""HTTP-API для ноутбука. Только стандартная библиотека (как api.py в mother).

    GET  /health            — проверка (без токена)
    POST /batch             — подборка с ноутбука: {"day": "2026-10-19", "posts": [...], "problems": [...]}
                              -> {"ok": true, "batch": 5, "accepted": 4, "problems": [...]}
    GET  /history?days=30   — темы последних дней (чтобы Claude не повторялся):
                              {"items": [{"date", "title", "source_url", "status"}]}

Всё, кроме /health, требует ``Authorization: Bearer <AION_API_TOKEN>``. Наружу — только за HTTPS (Caddy)."""
from __future__ import annotations

import hmac
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("aion.api")
MAX_BODY = 2 * 1024 * 1024


def make_handler(token: str, service):
    class Handler(BaseHTTPRequestHandler):
        server_version = "aion"

        def log_message(self, fmt, *args):
            log.info("api %s - %s", self.address_string(), fmt % args)

        def _json(self, status: int, body: dict) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            sent = self.headers.get("Authorization", "")
            return bool(token) and hmac.compare_digest(sent.encode(), ("Bearer %s" % token).encode())

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/health":
                return self._json(200, {"ok": True})
            if not self._authorized():
                return self._json(401, {"error": "unauthorized"})
            if u.path == "/history":
                try:
                    days = max(1, min(365, int(parse_qs(u.query).get("days", ["30"])[0])))
                except ValueError:
                    days = 30
                return self._json(200, {"items": service.history(days)})
            self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._authorized():
                return self._json(401, {"error": "unauthorized"})
            if urlparse(self.path).path != "/batch":
                return self._json(404, {"error": "not found"})
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY:
                return self._json(400, {"error": "нужно тело JSON до 2 МБ"})
            try:
                body = json.loads(self.rfile.read(length))
                result = service.ingest(body)
            except ValueError as ex:
                return self._json(400, {"error": str(ex)})
            except Exception:
                log.exception("ошибка приёма подборки")
                return self._json(500, {"error": "ошибка сервера"})
            self._json(200, result)

    return Handler


def start_in_thread(host: str, port: int, token: str, service) -> ThreadingHTTPServer:
    if not token:
        raise ValueError("AION_API_TOKEN не задан — API без токена не запускаем")
    server = ThreadingHTTPServer((host, port), make_handler(token, service))
    threading.Thread(target=server.serve_forever, name="aion-api", daemon=True).start()
    log.info("API слушает %s:%s", host, server.server_address[1])
    return server
