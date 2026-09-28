"""Фейковый контроллер шлагбаума для E2E-стенда: HTTP_REST-протокол драйвера (POST /open, /close → 200),
журнал команд GET /commands (для проверок «шлагбаум открыт»), режим отказа POST /mode {"fail": true|false}.
Слушает на не-loopback адресе хоста: SSRF-политика всегда блокирует 127.0.0.0/8, а приватный адрес хоста
добавляется в barrier_cidr_allowlist площадки стенда.

    python scripts/fake_barrier.py <host> <port>
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

COMMANDS: list[dict[str, object]] = []
STATE = {"fail": False}
LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_: object) -> None:  # тихо
        return

    def _json(self, status: int, body: object) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path.startswith("/commands"):
            with LOCK:
                self._json(200, COMMANDS)
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/mode":
            with LOCK:
                STATE["fail"] = bool(payload.get("fail"))
            self._json(200, STATE)
            return
        if self.path in {"/open", "/close"}:
            with LOCK:
                COMMANDS.append(
                    {
                        "action": self.path[1:],
                        "command_id": payload.get("command_id"),
                        "auth": bool(self.headers.get("Authorization")),
                    }
                )
                fail = STATE["fail"]
            if fail:
                self._json(500, {"error": "controller failure"})
            else:
                self._json(200, {"ok": True})
            return
        self._json(404, {"error": "not found"})


def main() -> None:
    host, port = sys.argv[1], int(sys.argv[2])
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
