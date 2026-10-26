import sqlite3
from dataclasses import replace

import numpy as np

from app.audio import SAMPLE_RATE
from app.config import Settings
from app.db import JobStore
from app.emotion import EmotionAnalyzer


class FakeEmotion(EmotionAnalyzer):
    """Вместо модели: «злость» для фраз со словом «безобразие», иначе нейтрально."""

    def __init__(self):
        super().__init__(replace(Settings(), emo_min_phrase_s=1.5))
        self.texts = {}

    def probs(self, wav):
        angry = self.texts.get(len(wav), False)
        return {"angry": 0.9, "sad": 0.0, "neutral": 0.1, "positive": 0.0} if angry else \
               {"angry": 0.0, "sad": 0.0, "neutral": 0.9, "positive": 0.1}


def phrase(start_s, end_s, text, channel="client"):
    return {"channel": channel, "start_ms": int(start_s * 1000), "end_ms": int(end_s * 1000), "text": text}


def test_analyze_scores_long_phrases_and_aggregates():
    emo = FakeEmotion()
    wav = np.zeros(SAMPLE_RATE * 20, dtype=np.float32)
    phrases = [phrase(0, 2, "Здравствуйте, касса не работает."), phrase(3, 3.5, "Да."),
               phrase(5, 9, "Это безобразие, третий раз звоню!"), phrase(10, 12, "Подключаюсь.", "operator")]
    emo.texts[4 * SAMPLE_RATE] = True  # фраза 5-9 с
    result = emo.analyze({"client": wav, "operator": wav}, phrases)

    assert "emotion" not in phrases[1]  # короче 1.5 с — не оцениваем
    assert phrases[2]["emotion"] == "angry" and phrases[2]["emotion_p"] == 0.9
    assert result["phrases_scored"] == 3 and result["phrases_total"] == 4
    assert result["by_channel"]["client"]["angry"] > 0.5  # 4 с злости против 2 с нейтрального
    assert result["by_channel"]["operator"]["neutral"] == 0.9
    assert [x["start_ms"] for x in result["top_angry"]] == [5000]


def test_old_db_gets_emotion_column(tmp_path):
    path = tmp_path / "jobs.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE jobs (call_id TEXT PRIMARY KEY, status TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,"
                 " file_path TEXT NOT NULL, file_name TEXT, source TEXT NOT NULL DEFAULT 'api', phone TEXT, ext TEXT,"
                 " call_date TEXT, duration_s REAL, channels TEXT, phrases TEXT, text TEXT, summary TEXT, error TEXT,"
                 " warning TEXT, processing_s REAL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,"
                 " started_at TEXT, finished_at TEXT)")
    conn.commit()
    conn.close()

    store = JobStore(path)
    store.submit("c1", "/tmp/a.mp3", "a.mp3")
    job = store.claim_next()
    assert store.finish("c1", job["version"], "done", emotion={"by_channel": {"mono": {"angry": 0.2}}})
    assert store.get("c1")["emotion"]["by_channel"]["mono"]["angry"] == 0.2
    # повторная отправка сбрасывает эмоции вместе с остальным результатом
    store.submit("c1", "/tmp/b.mp3", "b.mp3")
    assert store.get("c1")["emotion"] is None
    store.close()
