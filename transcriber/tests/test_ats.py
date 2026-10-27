from dataclasses import replace
from datetime import date

import pytest

from app.ats import AtsFetcher, parse_listing
from app.callmeta import parse_call_name
from app.config import Settings
from scripts.checks.fake_ats import KB, human, listing, serve

# поддельная АТС: тот же формат, что у Apache на настоящей, номера вымышленные
FILES = {
    "306090": {
        "26-10-26_08-56-56_70000000001_306090_1.1.mp3": 288,          # пустышка
        "26-10-26_09-02-47_70000000002_306090_2.2.mp3": 77 * KB,
        "26-10-26_09-23-52_70000000003_306090_3.3.mp3": int(1.0 * KB * KB),
    },
    "103": {"26-10-26_10-00-00_103_80000000004_4.4.mp3": 50 * KB},
}


@pytest.fixture(scope="module")
def ats_url(tmp_path_factory):
    root = tmp_path_factory.mktemp("ats")
    for line, files in FILES.items():
        folder = root / "26" / "10" / "26" / line
        folder.mkdir(parents=True)
        for name, size in files.items():
            (folder / name).write_bytes(b"\0" * size)
    server, url = serve(root)
    yield url
    server.shutdown()


def fetcher(url, **kw):
    return AtsFetcher(replace(Settings(), **{"ats_url": url, "ats_user": "user", "ats_password": "secret",
                                             "ats_lines": (), **kw}))


# ---- разбор страницы и имени файла

def test_parse_listing_sizes_and_skips_service_links():
    html = listing("/record/26/10/26/306090", [(n, human(s)) for n, s in FILES["306090"].items()])
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


def test_fetch_skips_processed_calls_and_respects_limit(ats_url, tmp_path):
    # аудио обработанного звонка уже удалено, но в базе он есть -> не качаем снова
    processed = {"26-10-26_09-02-47_70000000002_306090_2.2"}
    report = fetcher(ats_url).fetch_day(date(2026, 10, 26), tmp_path, limit=1,
                                        is_known=lambda name: name.rsplit(".", 1)[0] in processed)
    assert (report.already, len(report.downloaded), report.too_small, report.skipped_by_limit) == (1, 1, 1, 1)


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
