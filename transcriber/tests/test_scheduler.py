import os
import time
from dataclasses import replace
from datetime import date, datetime

import pytest

from app.config import Settings
from app.db import JobStore
from app.scheduler import NightScheduler, in_hours, next_run, remove_old_files
from app.worker import Worker
from scripts.checks.fake_ats import serve

NAME = "26-10-26_09-02-47_70000000002_306090_2.2"


def test_next_run_today_tomorrow_and_several_times():
    now = datetime(2026, 10, 26, 19, 0)
    assert next_run(now, ("20:30",)) == datetime(2026, 10, 26, 20, 30)
    assert next_run(datetime(2026, 10, 26, 21, 0), ("20:30",)) == datetime(2026, 10, 27, 20, 30)
    assert next_run(datetime(2026, 10, 26, 14, 0), ("13:00", "20:30")) == datetime(2026, 10, 26, 20, 30)
    assert next_run(now, ()) is None


def test_in_hours_including_overnight():
    assert in_hours(datetime(2026, 10, 27, 8, 0), "08:00-20:00")
    assert not in_hours(datetime(2026, 10, 27, 20, 0), "08:00-20:00")
    assert not in_hours(datetime(2026, 10, 27, 7, 59), "08:00-20:00")
    assert in_hours(datetime(2026, 10, 27, 23, 0), "22:00-06:00") and in_hours(datetime(2026, 10, 27, 5, 0), "22:00-06:00")
    assert not in_hours(datetime(2026, 10, 27, 12, 0), "22:00-06:00")


def test_remove_old_files_only_old_audio(tmp_path):
    old, fresh, other = tmp_path / "old.mp3", tmp_path / "fresh.mp3", tmp_path / "notes.txt"
    for f in (old, fresh, other):
        f.write_bytes(b"x")
    month_ago = time.time() - 30 * 86400
    for f in (old, other):
        os.utime(f, (month_ago, month_ago))
    assert remove_old_files(tmp_path, 7) == 1
    assert not old.exists() and fresh.exists() and other.exists()
    assert remove_old_files(tmp_path, 0) == 0  # 0 — не удалять


@pytest.fixture
def ats(tmp_path):
    folder = tmp_path / "ats" / "26" / "10" / "26" / "306090"
    folder.mkdir(parents=True)
    (folder / f"{NAME}.mp3").write_bytes(b"\0" * 50_000)
    server, url = serve(tmp_path / "ats")
    yield url
    server.shutdown()


def make_settings(tmp_path, **kw):
    return replace(Settings(), **{"data_dir": tmp_path / "data", "inbox_dir": str(tmp_path / "inbox"),
                                  "ats_user": "user", "ats_password": "secret", **kw})


def test_fetch_puts_files_to_inbox_once(tmp_path, ats):
    s = make_settings(tmp_path, ats_url=ats)
    store = JobStore(s.db_path)
    sched = NightScheduler(s, store)
    reports = sched.fetch([date(2026, 10, 26)])
    assert reports[0].downloaded == [f"{NAME}.mp3"] and (tmp_path / "inbox" / f"{NAME}.mp3").exists()
    assert sched.last_fetch and sched.last_fetch_at

    # звонок обработан: аудио удалено, задача в базе -> повторно не скачивается
    (tmp_path / "inbox" / f"{NAME}.mp3").unlink()
    store.submit(NAME, "/nowhere.mp3", f"{NAME}.mp3", source="inbox")
    assert sched.fetch([date(2026, 10, 26)])[0].downloaded == []
    store.close()


def test_live_poll_takes_only_live_lines_and_new_calls(tmp_path):
    root = tmp_path / "ats" / "26" / "10" / "27"
    for line, name in (("306090", NAME), ("687755", "26-10-27_11-00-00_70000000005_687755_5.5"),
                       ("555", "26-10-27_12-00-00_70000000006_555_6.6")):
        (root / line).mkdir(parents=True)
        (root / line / f"{name}.mp3").write_bytes(b"\0" * 50_000)
    server, url = serve(tmp_path / "ats")
    s = make_settings(tmp_path, ats_url=url, ats_live_lines=("306090", "687755", "111"))
    store = JobStore(s.db_path)
    sched = NightScheduler(s, store)
    now = datetime(2026, 10, 27, 12, 30)

    report = sched.live_poll(now)
    assert sorted(report.folders) == ["306090", "687755"]  # 111 — папки за день нет, 555 — не наша линия
    assert len(report.downloaded) == 2 and sched.live["last_new_at"] == "2026-10-27T12:30:00"
    assert sched.live_poll(now).downloaded == []  # уже скачаны -> ничего нового

    server.shutdown()
    server.server_close()
    assert sched.live_poll(now) is None and sched.live["error"]  # АТС пропала — ошибка записана, сервис жив
    store.close()


def test_live_poll_skipped_while_other_fetch_runs(tmp_path):
    sched = NightScheduler(make_settings(tmp_path, ats_url="http://ats/record", ats_live_lines=("306090",)),
                           JobStore(tmp_path / "jobs.db"))
    with sched._fetch_lock:
        assert sched.live_poll() is None


def test_fetch_requires_ats_and_inbox(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    with pytest.raises(ValueError):
        NightScheduler(replace(Settings(), ats_url=""), store).fetch([date.today()])
    with pytest.raises(ValueError):
        NightScheduler(replace(Settings(), ats_url="http://ats/record", inbox_dir=""), store).fetch([date.today()])
    store.close()


def test_cleanup_removes_old_results_only_when_enabled(tmp_path):
    s = make_settings(tmp_path, results_retention_days=30)
    store = JobStore(s.db_path)
    for call_id in ("old", "new"):
        store.submit(call_id, "/x.mp3", "x.mp3")
        job = store.claim_next()
        store.finish(call_id, job["version"], "done", text="текст")
    store._exec("UPDATE jobs SET finished_at='2026-01-01T00:00:00+00:00' WHERE call_id='old'")
    assert NightScheduler(s, store).cleanup()["results_removed"] == 1
    assert store.get("old") is None and store.get("new") is not None
    assert NightScheduler(replace(s, results_retention_days=0), store).cleanup()["results_removed"] == 0
    store.close()


# ---- удаление аудио после обработки

class StubTranscriber:
    def __init__(self, fail=False):
        self.fail = fail

    def transcribe_file(self, path):
        if self.fail:
            raise RuntimeError("битый файл")
        return {"duration_s": 1.0, "channels": ["mono"], "text": "[00:00] Алло",
                "phrases": [{"channel": "mono", "start_ms": 0, "end_ms": 1000, "text": "Алло"}], "emotion": None}


class StubSummarizer:
    def summarize(self, text, voice=None):
        return {"topic": "Тест"}, None


@pytest.mark.parametrize("fail, audio_left", [(False, False), (True, True)])
def test_audio_deleted_after_success_kept_after_error(tmp_path, fail, audio_left):
    s = make_settings(tmp_path)
    store = JobStore(s.db_path)
    audio = tmp_path / "inbox" / "done" / f"{NAME}.mp3"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"x")
    worker = Worker(s, store, StubTranscriber(fail), StubSummarizer())
    worker.submit_file(NAME, audio, audio.name, source="inbox")
    worker.process(store.claim_next())
    assert store.get(NAME)["status"] == ("error" if fail else "done")
    assert audio.exists() is audio_left  # удачно обработанное — удаляется сразу, ошибочное — ждёт разбора
    store.close()
