"""Эмоция по голосу для каждой фразы: GigaAM-Emo (angry / sad / neutral / positive, датасет Dusha).

Оценивает КАК сказано (интонацию), а не ЧТО сказано — тон по смыслу даёт LLM в саммари.
На коротких репликах («да», «угу») модель выдаёт шум, поэтому их не оцениваем.
"""
import logging

import numpy as np
import torch

from .audio import SAMPLE_RATE
from .config import Settings

log = logging.getLogger(__name__)

LABELS = ("angry", "sad", "neutral", "positive")
LABELS_RU = {"angry": "злость", "sad": "грусть", "neutral": "нейтрально", "positive": "позитив"}
TOP_N = 3
CHANNEL_RU = {"client": "Клиент", "operator": "Оператор"}


def format_ts(ms: int) -> str:
    sec = ms // 1000
    return f"{sec // 60:02d}:{sec % 60:02d}"


def describe(emotion: dict) -> list[str]:
    """Сводка эмоций по голосу текстом: для файлов результатов и как подсказка LLM при оценке звонка."""
    lines = []
    for channel, shares in emotion["by_channel"].items():
        who = "" if channel == "mono" else f" ({CHANNEL_RU.get(channel, channel)})"
        lines.append(f"Эмоции по голосу{who}: " + ", ".join(f"{LABELS_RU[k]} {v:.0%}" for k, v in shares.items()))
    for key, title in (("top_angry", "Злость"), ("top_positive", "Позитив")):
        items = emotion[key]
        lines.append(f"{title} (p>=0.5): " + ("; ".join(f"[{format_ts(x['start_ms'])}] {x['p']:.2f} «{x['text'][:60]}»"
                                                         for x in items) if items else "нет"))
    lines.append(f"Оценено фраз: {emotion['phrases_scored']} из {emotion['phrases_total']} (короткие реплики не оцениваются)")
    return lines


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
