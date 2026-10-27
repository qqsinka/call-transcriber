import base64
import threading
from dataclasses import replace
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.ats import AtsFetcher, parse_listing
from app.callmeta import parse_call_name
from app.config import Settings

KB = 1024
# поддельная АТС: тот же формат, что у Apache на настоящей, номера вымышленные
FILES = {
    "306090": {
        "26-10-26_08-56-56_70000000001_306090_1.1.mp3": 288,          # пустышка
        "26-10-26_09-02-47_70000000002_306090_2.2.mp3": 77 * KB,
        "26-10-26_09-23-52_70000000003_306090_3.3.mp3": int(1.0 * KB * KB),
    },
    "103": {"26-10-26_10-00-00_103_80000000004_4.4.mp3": 50 * KB},
}


def human(size: int) -> str:
    if size >= KB * KB:
        return f"{size / KB / KB:.1f}M"
    return f"{size // KB}K" if size >= KB else str(size)


def listing(path: str, rows: list[tuple[str, str, str]]) -> str:
    head = ('<html><head><title>Index of {p}</title></head><body><h1>Index of {p}</h1><table>'
            '<tr><th><img src="/icons/blank.gif" alt="[ICO]"></th><th><a href="?C=N;O=D">Name</a></th>'
            '<th><a href="?C=M;O=A">Last modified</a></th><th><a href="?C=S;O=A">Size</a></th></tr>'
            '<tr><td><img src="/icons/back.gif" alt="[PARENTDIR]"></td><td><a href="/record/26/10/">'
            'Parent Directory</a></td><td>&nbsp;</td><td align="right">  - </td><td>&nbsp;</td></tr>').format(p=path)
    body = "".join(f'<tr><td><img src="/icons/{icon}.gif" alt="[X]"></td><td><a href="{href}">{href}</a></td>'
                   f'<td align="right">2026-10-26 09:00  </td><td align="right">{size} </td><td>&nbsp;</td></tr>'
                   for href, size, icon in rows)
    return head + body + "</table></body></html>"


class FakeAts(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.headers.get("Authorization") != "Basic " + base64.b64encode(b"user:secret").decode():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Enter your name and password"')
            self.end_headers()
            return
        parts = [p for p in self.path.split("/") if p]
        if parts == ["record", "26", "10", "26"]:
            return self.reply(listing(self.path, [(f"{d}/", "-", "folder") for d in FILES]).encode())
        if len(parts) == 5 and parts[4] in FILES:
            rows = [(name, human(size), "sound2") for name, size in FILES[parts[4]].items()]
            return self.reply(listing(self.path, rows).encode())
        if len(parts) == 6 and parts[5] in FILES.get(parts[4], {}):
            return self.reply(b"\0" * FILES[parts[4]][parts[5]], "audio/mpeg")
        self.send_response(404)
        self.end_headers()

    def reply(self, data: bytes, ctype: str = "text/html") -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture(scope="module")
def ats_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAts)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/record"
    server.shutdown()


def fetcher(url, **kw):
    return AtsFetcher(replace(Settings(), **{"ats_url": url, "ats_user": "user", "ats_password": "secret",
                                             "ats_lines": (), **kw}))


# ---- разбор страницы и имени файла

def test_parse_listing_sizes_and_skips_service_links():
    html = listing("/record/26/10/26/306090", [(n, human(s), "sound2") for n, s in FILES["306090"].items()])
    entries = parse_listing(html, "http://ats/record/26/10/26/306090/")
    assert [(e.name, e.size) for e in entries] == [
        ("26-10-26_08-56-56_70000000001_306090_1.1.mp3", 288),
        ("26-10-26_09-02-47_70000000002_306090_2.2.mp3", 77 * KB),
        ("26-10-26_09-23-52_70000000003_306090_3.3.mp3", KB * KB),
    ]
    assert entries[1].url == "http://ats/record/26/10/26/306090/26-10-26_09-02-47_70000000002_306090_2.2.mp3"


def test_parse_call_name():
    assert parse_call_name("26-10-26_09-02-47_70000000002_306090_1792994567.120720") == {
        "call_date": "2026-10-26T09:02:47", "phone": "70000000002", "ext": "306090", "direction": "incoming"}
    out = parse_call_name("26-10-20_17-46-44_103_80000000004_1792507604.110735")
    assert (out["direction"], out["phone"], out["ext"]) == ("outgoing", "80000000004", "103")
    assert parse_call_name("какой-то файл") == {}


# ---- загрузка

def test_fetch_day_downloads_skips_empty_and_known(ats_url, tmp_path):
    (tmp_path / "done").mkdir()
    (tmp_path / "done" / "26-10-26_09-23-52_70000000003_306090_3.3.mp3").write_bytes(b"x")  # уже обработан
    report = fetcher(ats_url).fetch_day(date(2026, 10, 26), tmp_path)

    assert sorted(report.folders) == ["103", "306090"]
    assert sorted(report.downloaded) == ["26-10-26_09-02-47_70000000002_306090_2.2.mp3",
                                         "26-10-26_10-00-00_103_80000000004_4.4.mp3"]
    assert (report.already, report.too_small, report.failed) == (1, 1, [])
    assert (tmp_path / "26-10-26_09-02-47_70000000002_306090_2.2.mp3").stat().st_size == 77 * KB
    assert not list(tmp_path.glob("*.part"))

    again = fetcher(ats_url).fetch_day(date(2026, 10, 26), tmp_path)  # повторный запуск ничего не качает
    assert again.downloaded == [] and again.already == 3


def test_fetch_only_selected_lines_and_dry_run(ats_url, tmp_path):
    report = fetcher(ats_url, ats_lines=("306090",)).fetch_day(date(2026, 10, 26), tmp_path, dry_run=True)
    assert report.folders == ["306090"] and len(report.downloaded) == 2
    assert not any(tmp_path.iterdir())  # dry-run ничего не пишет
    assert "к скачиванию 2" in str(report)


def test_wrong_password_and_missing_day(ats_url, tmp_path):
    bad = AtsFetcher(replace(Settings(), ats_url=ats_url, ats_user="user", ats_password="wrong"))
    with pytest.raises(PermissionError):
        bad.fetch_day(date(2026, 10, 26), tmp_path)
    empty = fetcher(ats_url).fetch_day(date(2026, 10, 27), tmp_path)  # папки за день ещё нет
    assert empty.folders == [] and empty.downloaded == []
