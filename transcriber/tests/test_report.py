from app.db import JobStore
from app.render import job_report, phrase_lines
from scripts.report import load_jobs, table

SUMMARY = {
    "topic": "Не печатает чек", "problem": "Касса не печатает чек после обновления.", "result": "Переустановили драйвер.",
    "next_step": "", "summary": "Клиент сообщил, что касса не печатает чек. Оператор переустановил драйвер.",
    "priority": "важный", "priority_reason": "работа стоит", "negative_score": 5, "tone": "негативный",
    "tone_reason": "недоволен простоем", "risks": ["простой кассы"],
    "facts": {"caller": {"org": "ИП Иванова"}, "details": ["АТОЛ 30Ф"]},
}
EMOTION = {"by_channel": {"mono": {"angry": 0.3, "sad": 0.0, "neutral": 0.7, "positive": 0.0}},
           "top_angry": [{"start_ms": 5000, "p": 0.8, "text": "Сколько можно ждать"}], "top_positive": [],
           "phrases_scored": 2, "phrases_total": 3}
PHRASES = [
    {"channel": "mono", "start_ms": 0, "end_ms": 2000, "text": "Касса не печатает.", "emotion": "neutral", "emotion_p": 0.9},
    {"channel": "mono", "start_ms": 5000, "end_ms": 8000, "text": "Сколько можно ждать!", "emotion": "angry", "emotion_p": 0.8},
    {"channel": "mono", "start_ms": 9000, "end_ms": 9500, "text": "Да."},
]


def make_store(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    store.submit("26-10-28_09-10-04_79000000001_306090_1.1", "a.mp3", "a.mp3", source="inbox",
                 phone="79000000001", ext="306090", call_date="2026-10-28T09:10:04")
    store.submit("26-10-28_10-00-00_79000000002_306090_2.2", "b.mp3", "b.mp3", source="inbox",
                 phone="79000000002", ext="306090", call_date="2026-10-28T10:00:00")
    store.submit("26-10-27_17-00-00_79000000003_306090_3.3", "c.mp3", "c.mp3", source="inbox",
                 phone="79000000003", ext="306090", call_date="2026-10-27T17:00:00")
    job = store.claim_next()
    store.finish(job["call_id"], job["version"], "done", duration_s=125.0, phrases=PHRASES, text="…",
                 summary=SUMMARY, emotion=EMOTION, processing_s=150.2)
    job = store.claim_next()
    store.finish(job["call_id"], job["version"], "error", error="AudioError: битый файл")
    store.close()
    return tmp_path / "jobs.db"


def test_load_jobs_by_call_date(tmp_path):
    jobs = load_jobs(make_store(tmp_path), "2026-10-28")
    assert [j["phone"] for j in jobs] == ["79000000001", "79000000002"]  # 27.10 не попал, порядок по времени
    assert jobs[0]["summary"]["topic"] == "Не печатает чек" and jobs[0]["phrases"][1]["emotion"] == "angry"


def test_table_and_totals(tmp_path):
    lines = table(load_jobs(make_store(tmp_path), "2026-10-28"))
    assert lines[1].startswith("09:10") and "2:05" in lines[1] and "важный" in lines[1] and "Не печатает чек" in lines[1]
    assert "AudioError" in lines[2]
    assert lines[-1] == "Всего 2: done 1, error 1; критических 0, важных 1"


def test_job_report(tmp_path):
    done, failed = load_jobs(make_store(tmp_path), "2026-10-28")
    text = job_report(done)
    assert text.startswith("Звонок: 2026-10-28 09:10:04, номер 79000000001, линия 306090, длительность 2:05")
    assert "Приоритет: важный — работа стоит" in text and "Клиент: ИП Иванова" in text
    assert "Злость (p>=0.5): [00:05] 0.80" in text
    assert "[00:05] Сколько можно ждать!  ⟨злость 0.80⟩" in text
    assert "Статус: error — AudioError: битый файл" in job_report(failed)


def test_phrase_lines_hide_neutral_and_unscored():
    assert phrase_lines(PHRASES) == ["[00:00] Касса не печатает.", "[00:05] Сколько можно ждать!  ⟨злость 0.80⟩", "[00:09] Да."]
