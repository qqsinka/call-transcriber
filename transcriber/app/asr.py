"""Распознавание: silero-vad режет канал по паузам, GigaAM-v3 e2e распознаёт куски батчами.

GigaAMASR.transcribe() принимает только путь к файлу и падает на записях длиннее 25 с
(gigaam/model.py: LONGFORM_THRESHOLD = 25 * SAMPLE_RATE). Встроенный transcribe_longform
требует pyannote и HF-токен, поэтому используем тот же путь, что и он сам внутри:
model.forward(wav_pad, wav_lens) + model._decode(...) на уже нарезанных тензорах.
"""
import logging
import threading
import time

import numpy as np
import torch

from .audio import SAMPLE_RATE, load_channels
from .config import Settings
from .emotion import EmotionAnalyzer
from .replacements import Replacer

log = logging.getLogger(__name__)

GIGAAM_LIMIT_S = 25.0
ROLE_LABELS = {"client": "Клиент", "operator": "Оператор"}


def split_long(segments: list[tuple[int, int]], max_len: int) -> list[tuple[int, int]]:
    """Подстраховка: куски длиннее max_len режем на равные части."""
    out = []
    for start, end in segments:
        length = end - start
        if length <= max_len:
            out.append((start, end))
            continue
        parts = int(np.ceil(length / max_len))
        step = length / parts
        for i in range(parts):
            out.append((start + int(i * step), start + int((i + 1) * step) if i < parts - 1 else end))
    return out


def format_ts(ms: int) -> str:
    sec = ms // 1000
    return f"{sec // 60:02d}:{sec % 60:02d}"


def merge_phrases(per_channel: list[list[dict]]) -> list[dict]:
    """Сливаем фразы всех каналов в одну ленту по времени начала."""
    phrases = [p for channel in per_channel for p in channel]
    phrases.sort(key=lambda p: (p["start_ms"], p["end_ms"], p["channel"]))
    return phrases


def phrases_to_text(phrases: list[dict]) -> str:
    lines = []
    for p in phrases:
        label = "" if p["channel"] == "mono" else f"{ROLE_LABELS.get(p['channel'], p['channel'])}: "
        lines.append(f"[{format_ts(p['start_ms'])}] {label}{p['text']}")
    return "\n".join(lines)


class Transcriber:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.model = None
        self.vad = None
        self.ready = threading.Event()
        self.load_error: str | None = None
        self.replace = Replacer(settings.replacements_path)
        self.emotion = EmotionAnalyzer(settings) if settings.emotion_enabled else None
        self.emotion_error: str | None = None
        # Модель одна на процесс; распознавание сериализуем, LLM-часть задач может идти параллельно
        self._lock = threading.Lock()

    def load(self) -> None:
        import gigaam
        from silero_vad import load_silero_vad

        s = self.settings
        try:
            if s.device == "cpu" and s.cpu_threads > 0:
                torch.set_num_threads(s.cpu_threads)
            started = time.time()
            self.model = gigaam.load_model(
                s.asr_model, device=s.device, download_root=s.model_dir or None
            )
            self.vad = load_silero_vad()
            log.info("ASR %s загружена на %s за %.1f с", s.asr_model, s.device, time.time() - started)
            if self.emotion:
                try:
                    self.emotion.load()
                except Exception as exc:  # без эмоций транскрибация всё равно нужна
                    self.emotion_error = f"{type(exc).__name__}: {exc}"
                    self.emotion = None
                    log.exception("Не удалось загрузить Emo-модель, эмоции отключены")
            self.ready.set()
        except Exception as exc:  # сервис остаётся жив, /health покажет ошибку
            self.load_error = f"{type(exc).__name__}: {exc}"
            log.exception("Не удалось загрузить ASR-модель")
            raise

    def _segments(self, wav: np.ndarray) -> list[tuple[int, int]]:
        from silero_vad import get_speech_timestamps

        s = self.settings
        max_s = min(s.max_segment_s, GIGAAM_LIMIT_S - 1.0)
        stamps = get_speech_timestamps(
            torch.from_numpy(wav),
            self.vad,
            sampling_rate=SAMPLE_RATE,
            threshold=s.vad_threshold,
            min_silence_duration_ms=s.vad_min_silence_ms,
            speech_pad_ms=s.vad_speech_pad_ms,
            max_speech_duration_s=max_s,
        )
        return split_long([(t["start"], t["end"]) for t in stamps], int(max_s * SAMPLE_RATE))

    @torch.inference_mode()
    def _recognize(self, chunks: list[np.ndarray]) -> list[str]:
        model = self.model
        device = model._device
        dtype = model._dtype
        texts: list[str] = [""] * len(chunks)
        # батчи из кусков близкой длины -> меньше паддинга
        order = sorted(range(len(chunks)), key=lambda i: len(chunks[i]))
        batch_size = max(1, self.settings.asr_batch)
        for b in range(0, len(order), batch_size):
            idx = order[b : b + batch_size]
            lens = torch.tensor([len(chunks[i]) for i in idx], dtype=torch.long)
            batch = torch.zeros(len(idx), int(lens.max()), dtype=torch.float32)
            for row, i in enumerate(idx):
                batch[row, : len(chunks[i])] = torch.from_numpy(chunks[i])
            batch = batch.to(device).to(dtype)
            lens = lens.to(device)
            encoded, encoded_len = model.forward(batch, lens)
            for row, (text, _) in enumerate(model._decode(encoded, encoded_len, lens)):
                texts[idx[row]] = text.strip()
        return texts

    def transcribe_file(self, path: str) -> dict:
        if not self.ready.wait(timeout=3600):
            raise RuntimeError(self.load_error or "ASR-модель не загружена")
        channels, duration = load_channels(path, self.settings.stereo_map)
        per_channel = []
        with self._lock:
            for name, wav in channels:
                self.vad.reset_states()
                segments = self._segments(wav)
                if not segments:
                    continue
                min_len = int(0.1 * SAMPLE_RATE)  # совсем короткие щелчки не распознаём
                segments = [seg for seg in segments if seg[1] - seg[0] >= min_len]
                texts = [self.replace(text) for text in self._recognize([wav[a:b] for a, b in segments])]
                per_channel.append([
                    {
                        "channel": name,
                        "start_ms": int(a * 1000 / SAMPLE_RATE),
                        "end_ms": int(b * 1000 / SAMPLE_RATE),
                        "text": text,
                    }
                    for (a, b), text in zip(segments, texts)
                    if text
                ])
            phrases = merge_phrases(per_channel)
            emotion = self.emotion.analyze(dict(channels), phrases) if self.emotion else None
        return {
            "duration_s": round(duration, 2),
            "channels": [name for name, _ in channels],
            "phrases": phrases,
            "text": phrases_to_text(phrases),
            "emotion": emotion,
        }
