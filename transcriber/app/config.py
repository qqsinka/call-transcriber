"""Настройки сервиса из окружения / .env."""
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
if os.name == "nt":
    # TZ=Europe/Moscow — для Docker/Linux; Windows такой формат не понимает и молча считает время по UTC
    # (расписание съезжает на часы). Часовой пояс на Windows берётся из системы.
    os.environ.pop("TZ", None)

DEFAULT_LLM_MODEL = "hf.co/ai-sage/GigaChat3.1-10B-A1.8B-GGUF:Q4_K_M"


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int) -> int:
    value = _env(name)
    return int(value) if value else default


def _env_float(name: str, default: float) -> float:
    value = _env(name)
    return float(value) if value else default


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name).lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # ASR
    device: str = field(default_factory=lambda: _env("DEVICE", "cpu"))
    asr_model: str = field(default_factory=lambda: _env("ASR_MODEL", "v3_e2e_rnnt"))
    model_dir: str = field(default_factory=lambda: _env("MODEL_DIR"))  # пусто -> ~/.cache/gigaam
    cpu_threads: int = field(default_factory=lambda: _env_int("CPU_THREADS", 0))  # 0 -> все ядра
    asr_batch: int = field(default_factory=lambda: _env_int("ASR_BATCH", 8))
    # GigaAM.transcribe принимает максимум 25 с (LONGFORM_THRESHOLD), берём с запасом
    max_segment_s: float = field(default_factory=lambda: _env_float("MAX_SEGMENT_S", 20.0))
    # подобрано на реальных записях АТС (моно, 8 кГц): 0.5 терял тихую речь второй стороны,
    # 0.3 начинал цеплять фоновые разговоры в офисе
    vad_min_silence_ms: int = field(default_factory=lambda: _env_int("VAD_MIN_SILENCE_MS", 700))
    vad_speech_pad_ms: int = field(default_factory=lambda: _env_int("VAD_SPEECH_PAD_MS", 200))
    vad_threshold: float = field(default_factory=lambda: _env_float("VAD_THRESHOLD", 0.4))
    # эмоция по голосу (GigaAM-Emo): +~20 с на звонок на CPU
    emotion_enabled: bool = field(default_factory=lambda: _env_bool("EMOTION_ENABLED", True))
    emo_min_phrase_s: float = field(default_factory=lambda: _env_float("EMO_MIN_PHRASE_S", 1.5))
    stereo_map: tuple[str, str] = field(
        default_factory=lambda: tuple(x.strip() for x in _env("STEREO_MAP", "client,operator").split(","))[:2]  # type: ignore[return-value]
    )

    # LLM (Ollama)
    llm_url: str = field(default_factory=lambda: _env("LLM_URL", "http://localhost:11434").rstrip("/"))
    # GigaChat 3.1 Lightning (Сбер, MIT): на реальных звонках точнее и быстрее qwen2.5:7b, без «китайских» ответов
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", DEFAULT_LLM_MODEL))
    llm_timeout_s: float = field(default_factory=lambda: _env_float("LLM_TIMEOUT_S", 300.0))
    llm_max_chars: int = field(default_factory=lambda: _env_int("LLM_MAX_CHARS", 12000))
    llm_max_tokens: int = field(default_factory=lambda: _env_int("LLM_MAX_TOKENS", 1024))  # предел длины ответа
    llm_keep_alive: str = field(default_factory=lambda: _env("LLM_KEEP_ALIVE", "30m"))  # сколько держать модель в памяти
    llm_enabled: bool = field(default_factory=lambda: _env_bool("LLM_ENABLED", True))

    # Очередь и хранилище
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", "data")))
    workers: int = field(default_factory=lambda: _env_int("WORKERS", 1))
    keep_audio: bool = field(default_factory=lambda: _env_bool("KEEP_AUDIO", False))
    inbox_dir: str = field(default_factory=lambda: _env("INBOX_DIR"))
    inbox_poll_s: float = field(default_factory=lambda: _env_float("INBOX_POLL_S", 3.0))

    # Записи с АТС (веб-список файлов Apache): <ATS_URL>/ГГ/ММ/ДД/<линия>/<запись>.mp3
    ats_url: str = field(default_factory=lambda: _env("ATS_URL"))
    ats_user: str = field(default_factory=lambda: _env("ATS_USER"))
    ats_password: str = field(default_factory=lambda: _env("ATS_PASSWORD"))
    # папки линий/внутренних номеров через запятую; пусто -> все папки за день
    ats_lines: tuple[str, ...] = field(
        default_factory=lambda: tuple(x.strip() for x in _env("ATS_LINES").split(",") if x.strip())
    )
    # исходящие: кроме ATS_LINES забирать папки внешних номеров (5+ цифр — клиенты, на которые звонили сотрудники);
    # только при загрузке по расписанию и вручную, реальное время берёт лишь ATS_LIVE_LINES
    ats_outgoing: bool = field(default_factory=lambda: _env_bool("ATS_OUTGOING", False))
    # записи меньше — несостоявшиеся звонки (на АТС встречаются файлы по 288 байт)
    ats_min_bytes: int = field(default_factory=lambda: _env_int("ATS_MIN_BYTES", 4096))
    ats_timeout_s: float = field(default_factory=lambda: _env_float("ATS_TIMEOUT_S", 60.0))
    # ночной режим: когда забирать записи с АТС («20:30» или «13:00,20:30»); пусто -> только вручную
    ats_fetch_at: tuple[str, ...] = field(
        default_factory=lambda: tuple(x.strip() for x in _env("ATS_FETCH_AT").split(",") if x.strip())
    )
    # за сколько дней забирать: 1 — только сегодня, 2 — сегодня и вчера (добрать поздние звонки)
    ats_fetch_days: int = field(default_factory=lambda: _env_int("ATS_FETCH_DAYS", 2))
    # реальное время: эти линии проверяются каждые ATS_LIVE_EVERY_MIN минут в часы ATS_LIVE_HOURS,
    # новая запись сразу скачивается и обрабатывается; пусто -> выключено (работает только ночной режим)
    ats_live_lines: tuple[str, ...] = field(
        default_factory=lambda: tuple(x.strip() for x in _env("ATS_LIVE_LINES").split(",") if x.strip())
    )
    ats_live_every_min: float = field(default_factory=lambda: _env_float("ATS_LIVE_EVERY_MIN", 2.0))
    ats_live_hours: str = field(default_factory=lambda: _env("ATS_LIVE_HOURS", "08:00-20:00"))

    # Хранение. Аудио удачно обработанных звонков удаляется сразу (KEEP_AUDIO=false);
    # оставшееся (ошибки, KEEP_AUDIO=true) — через AUDIO_RETENTION_DAYS дней (0 — не удалять).
    audio_retention_days: int = field(default_factory=lambda: _env_int("AUDIO_RETENTION_DAYS", 7))
    # копия отчёта по каждому готовому звонку в txt: <SUMMARY_DIR>/вход|исход/<запись>.txt; пусто -> выключено
    summary_dir: str = field(default_factory=lambda: _env("SUMMARY_DIR"))
    # результаты (текст, саммари) в базе; 0 — хранить всегда
    results_retention_days: int = field(default_factory=lambda: _env_int("RESULTS_RETENTION_DAYS", 0))

    # Basic-auth (пусто -> выключена)
    auth_user: str = field(default_factory=lambda: _env("AUTH_USER"))
    auth_password: str = field(default_factory=lambda: _env("AUTH_PASSWORD"))

    # Автозамены в распознанном тексте; пусто -> DATA_DIR/replacements.txt
    replacements_file: str = field(default_factory=lambda: _env("REPLACEMENTS_FILE"))

    @property
    def replacements_path(self) -> Path:
        return Path(self.replacements_file) if self.replacements_file else self.data_dir / "replacements.txt"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jobs.db"

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"


AUDIO_EXTENSIONS = {".wav", ".mp3", ".ogg", ".oga", ".opus", ".m4a", ".aac", ".flac", ".wma", ".webm", ".amr"}
