"""Эмоция по голосу для каждой фразы: GigaAM-Emo (angry / sad / neutral / positive, датасет Dusha).

Оценивает КАК сказано (интонацию), а не ЧТО сказано — тон по смыслу даёт LLM в саммари.
На коротких репликах («да», «угу») модель выдаёт шум, поэтому их не оцениваем.
"""
import logging

import numpy as np
import torch

from .audio import SAMPLE_RATE
from .config import Settings
from .render import LABELS_RU, describe  # noqa: F401 — describe отсюда импортируют worker и скрипты

log = logging.getLogger(__name__)

LABELS = ("angry", "sad", "neutral", "positive")
TOP_N = 3


class EmotionAnalyzer:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.model = None

    def load(self) -> None:
        import gigaam

        s = self.settings
        self.model = gigaam.load_model("emo", device=s.device, download_root=s.model_dir or None)
        log.info("Emo-модель загружена (%s)", ", ".join(self.model.id2name))

    @torch.inference_mode()
    def probs(self, wav: np.ndarray) -> dict[str, float]:
        """То же, что GigaAMEmo.get_probs(), но на массиве, а не на пути к файлу."""
        m = self.model
        x = torch.from_numpy(wav).to(m._device).to(m._dtype).unsqueeze(0)
        length = torch.full([1], x.shape[-1], device=m._device)
        encoded, _ = m.forward(x, length)
        pooled = torch.nn.functional.avg_pool1d(encoded, kernel_size=encoded.shape[-1]).squeeze(-1)
        values = torch.nn.functional.softmax(m.head(pooled)[0], dim=-1).tolist()
        return {m.id2name[i]: values[i] for i in range(len(m.id2name))}

    def analyze(self, channels: dict[str, np.ndarray], phrases: list[dict]) -> dict:
        """Дописывает в фразы emotion / emotion_p и возвращает сводку по каждому каналу."""
        min_len = self.settings.emo_min_phrase_s
        weights: dict[str, dict[str, float]] = {}
        scored: list[tuple[dict, dict]] = []
        for p in phrases:
            a = p["start_ms"] * SAMPLE_RATE // 1000
            b = p["end_ms"] * SAMPLE_RATE // 1000
            duration = (b - a) / SAMPLE_RATE
            if duration < min_len:
                continue
            pr = self.probs(channels[p["channel"]][a:b])
            best = max(pr, key=pr.get)
            p["emotion"], p["emotion_p"] = best, round(pr[best], 2)
            scored.append((p, pr))
            acc = weights.setdefault(p["channel"], dict.fromkeys(LABELS, 0.0))
            for label in LABELS:
                acc[label] += pr.get(label, 0.0) * duration

        by_channel = {}
        for channel, acc in weights.items():
            total = sum(acc.values()) or 1.0
            by_channel[channel] = {label: round(acc[label] / total, 2) for label in LABELS}

        def top(label: str) -> list[dict]:
            items = sorted(scored, key=lambda r: -r[1][label])[:TOP_N]
            return [{"channel": p["channel"], "start_ms": p["start_ms"], "p": round(pr[label], 2), "text": p["text"]}
                    for p, pr in items if pr[label] >= 0.5]

        return {
            "by_channel": by_channel,  # доли эмоций по длительности речи
            "top_angry": top("angry"),
            "top_positive": top("positive"),
            "phrases_scored": len(scored),
            "phrases_total": len(phrases),
        }
