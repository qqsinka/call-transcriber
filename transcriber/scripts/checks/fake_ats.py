"""Поддельная АТС для проверок: раздаёт папку как веб-список Apache с basic-auth — в том же формате,
что настоящий сервер записей. Структура папки: <root>/ГГ/ММ/ДД/<линия>/<запись>.mp3

Запуск: python fake_ats.py <папка> [--port 8099] [--user user] [--password secret]
Адрес для ATS_URL: http://localhost:8099/record
"""
import argparse
import base64
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote

PREFIX = "record"
KB = 1024


def human(size: int) -> str:
    """Размер как в списке Apache: 288, 77K, 1.0M."""
    if size >= KB * KB:
        return f"{size / KB / KB:.1f}M"
    return f"{size // KB}K" if size >= KB else str(size)


def listing(path: str, rows: list[tuple[str, str]]) -> str:
    """Страница списка файлов в формате Apache mod_autoindex (таблица): rows = [(href, размер)]."""
    head = ('<html><head><title>Index of {p}</title></head><body><h1>Index of {p}</h1><table>'
            '<tr><th><img src="/icons/blank.gif" alt="[ICO]"></th><th><a href="?C=N;O=D">Name</a></th>'
            '<th><a href="?C=M;O=A">Last modified</a></th><th><a href="?C=S;O=A">Size</a></th></tr>'
            '<tr><td><img src="/icons/back.gif" alt="[PARENTDIR]"></td><td><a href="/{prefix}/">'
            'Parent Directory</a></td><td>&nbsp;</td><td align="right">  - </td><td>&nbsp;</td></tr>'
            ).format(p=path, prefix=PREFIX)
    body = "".join(f'<tr><td><img src="/icons/sound2.gif" alt="[SND]"></td><td><a href="{quote(href)}">{href}</a></td>'
                   f'<td align="right">2026-10-26 09:00  </td><td align="right">{size} </td><td>&nbsp;</td></tr>'
                   for href, size in rows)
    return head + body + "</table></body></html>"


class Handler(BaseHTTPRequestHandler):
    root: Path
    token: str

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.headers.get("Authorization") != "Basic " + self.token:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Enter your name and password"')
            self.end_headers()
            return
        parts = [unquote(p) for p in self.path.split("?")[0].split("/") if p]
        target = self.root.joinpath(*parts[1:]) if parts[:1] == [PREFIX] else None
        if target is None or ".." in parts or not target.exists():
            self.send_response(404)
            self.end_headers()
            return
        if target.is_dir():
            items = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name))
            rows = [(p.name + "/", "-") if p.is_dir() else (p.name, human(p.stat().st_size)) for p in items]
            return self.reply(listing(self.path, rows).encode(), "text/html")
        return self.reply(target.read_bytes(), "audio/mpeg")

    def reply(self, data: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(root: Path, user: str = "user", password: str = "secret", port: int = 0,
          host: str = "127.0.0.1") -> tuple[ThreadingHTTPServer, str]:
    """Запустить в фоновом потоке. Возвращает (сервер, адрес для ATS_URL)."""
    handler = type("FakeAts", (Handler,), {"root": Path(root),
                                           "token": base64.b64encode(f"{user}:{password}".encode()).decode()})
    server = ThreadingHTTPServer((host, port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://{'localhost' if host in ('0.0.0.0', '127.0.0.1') else host}:{server.server_port}/{PREFIX}"


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("root")
    p.add_argument("--port", type=int, default=8099)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--user", default="user")
    p.add_argument("--password", default="secret")
    a = p.parse_args()
    srv, url = serve(Path(a.root), a.user, a.password, a.port, a.host)
    print(f"поддельная АТС: {url} (логин {a.user})", flush=True)
    threading.Event().wait()
