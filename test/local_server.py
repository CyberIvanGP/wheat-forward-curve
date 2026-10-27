"""Loopback-only test UI; no uploads, filesystem writes or request-body logging."""

from functools import partial
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
import json
import secrets
from urllib.parse import urlsplit

from calculation_run.files import encode, parse_json


def make_server(run, port=0):
    token = secrets.token_urlsafe(32)

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def valid_host(self):
            return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

        def send_json(self, status, data):
            body = json.dumps(data, ensure_ascii=False, default=encode, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.valid_host():
                self.send_error(403)
                return
            path = urlsplit(self.path).path
            if path == "/api/session":
                self.send_json(200, {"token": token, "npv": run.node_curves is not None,
                                     "margin": run.prepared_margin is not None})
                return
            if path == "/":
                self.path = "/report.html"
            elif path not in ("/report.html", "/excel/today_curve.xlsx"):
                # Do not expose input archives, positions or directory listings.
                self.send_error(404)
                return
            super().do_GET()

        def do_HEAD(self):
            self.send_error(405)

        def do_POST(self):
            origin = f"http://127.0.0.1:{self.server.server_port}"
            if (not self.valid_host() or self.headers.get("Origin") != origin
                    or not secrets.compare_digest(self.headers.get("X-Run-Token", ""), token)):
                self.send_json(403, {"error": "Запрос разрешён только из страницы этого локального запуска"})
                return
            if self.path not in ("/api/npv", "/api/margin"):
                self.send_json(404, {"error": "Неизвестная операция"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 1_000_000:
                    raise ValueError("Размер запроса должен быть от 1 до 1 000 000 байт")
                if self.headers.get_content_type() != "application/json":
                    raise ValueError("Нужен Content-Type: application/json")
                data = parse_json(self.rfile.read(length).decode("utf-8"))
                if not isinstance(data, dict) or set(data) != {"trades"}:
                    raise ValueError("Нужен объект с массивом trades")
                value = (run.calculate_npv if self.path == "/api/npv" else run.calculate_margin)(data["trades"])
                self.send_json(200, value)
            except (ValueError, TypeError, KeyError, RuntimeError, ArithmeticError) as exc:
                self.send_json(400, {"error": str(exc)})

    return ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, directory=str(run.folder)))


def serve(run, port=0):
    with make_server(run, port) as server:
        print(f"Страница: http://127.0.0.1:{server.server_port}/", flush=True)
        print("Ctrl+C завершает сервер. HTML-отчёт останется в папке запуска.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
