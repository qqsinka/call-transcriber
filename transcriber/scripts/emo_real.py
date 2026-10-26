"""Эксперимент: эмоция по голосу (GigaAM-Emo) для каждой фразы звонка.

GigaAM-Emo — классификатор из семейства GigaAM (4 класса: angry / sad / neutral / positive),
обучен на русском датасете Dusha. Оценивает КАК сказано (интонация), а не ЧТО сказано.
Фразы берём из нашей транскрибации (те же VAD-куски), эмоцию считаем по аудио каждой фразы.

Запуск: python transcriber/scripts/emo_real.py samples/real [папка_вывода]
Пишет <папка_вывода>/<имя>.txt: по фразам метка + вероятности, в начале — сводка по звонку.
"""
import sys
import time
from pathlib import Path

import numpy as np
import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.asr import ROLE_LABELS, Transcriber, format_ts  # noqa: E402
from app.audio import SAMPLE_RATE, load_channels  # noqa: E402
from app.config import AUDIO_EXTENSIONS, Settings  # noqa: E402

LABELS_RU = {"angry": "злость", "sad": "грусть", "neutral": "нейтр", "positive": "позитив"}
MIN_PHRASE_S = 0.8  # на совсем коротких «угу/да» эмоция по голосу неинформативна


@torch.inference_mode()
def emo_probs(model, wav: np.ndarray) -> dict[str, float]:
    """То же, что GigaAMEmo.get_probs(), но на массиве, а не на пути к файлу."""
    x = torch.from_numpy(wav).to(model._device).to(model._dtype).unsqueeze(0)
    length = torch.full([1], x.shape[-1], device=model._device)
    encoded, _ = model.forward(x, length)
    pooled = torch.nn.functional.avg_pool1d(encoded, kernel_size=encoded.shape[-1]).squeeze(-1)
    probs = torch.nn.functional.softmax(model.head(pooled)[0], dim=-1).tolist()
    return {model.id2name[i]: probs[i] for i in range(len(model.id2name))}


def main() -> None:
    import gigaam

    folder = Path(sys.argv[1] if len(sys.argv) > 1 else "samples/real")
    out_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else folder / "out_emo"
    out_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS)

    settings = Settings()
    asr = Transcriber(settings)
    asr.load()
    emo = gigaam.load_model("emo", device=settings.device)
    print("классы:", list(emo.id2name))

    for f in files:
        result = asr.transcribe_file(str(f))
        channels = dict(load_channels(str(f), settings.stereo_map)[0])
        started = time.time()
        rows, weights = [], {k: 0.0 for k in LABELS_RU}
        for p in result["phrases"]:
            a = p["start_ms"] * SAMPLE_RATE // 1000
            b = p["end_ms"] * SAMPLE_RATE // 1000
            dur = (b - a) / SAMPLE_RATE
            if dur < MIN_PHRASE_S:
                rows.append((p, None))
                continue
            probs = emo_probs(emo, channels[p["channel"]][a:b])
            rows.append((p, probs))
            for k, v in probs.items():
                weights[k] += v * dur
        took = time.time() - started

        total = sum(weights.values()) or 1.0
        share = {k: v / total for k, v in weights.items()}
        scored = [(p, pr) for p, pr in rows if pr]
        top_angry = sorted(scored, key=lambda r: -r[1]["angry"])[:3]
        top_pos = sorted(scored, key=lambda r: -r[1]["positive"])[:3]

        lines = [
            f"# {f.name}",
            f"# эмоция по голосу, доли по длительности речи: "
            + ", ".join(f"{LABELS_RU[k]} {share[k]:.0%}" for k in LABELS_RU),
            f"# фраз оценено {len(scored)} из {len(rows)} (короче {MIN_PHRASE_S} с — пропуск), "
            f"время Emo {took:.1f} с",
            "# самые «злые» фразы: " + "; ".join(f"{format_ts(p['start_ms'])} ({pr['angry']:.2f})" for p, pr in top_angry),
            "# самые «позитивные»: " + "; ".join(f"{format_ts(p['start_ms'])} ({pr['positive']:.2f})" for p, pr in top_pos),
            "",
        ]
        for p, probs in rows:
            role = "" if p["channel"] == "mono" else f"{ROLE_LABELS.get(p['channel'], p['channel'])}: "
            if probs:
                best = max(probs, key=probs.get)
                tag = f"{LABELS_RU[best]:>7} {probs[best]:.2f} | з{probs['angry']:.2f} г{probs['sad']:.2f} п{probs['positive']:.2f}"
            else:
                tag = f"{'—':>7}      |"
            lines.append(f"[{format_ts(p['start_ms'])}] {tag} | {role}{p['text']}")
        text = "\n".join(lines) + "\n"
        (out_dir / f"{f.stem}.txt").write_text(text, encoding="utf-8")
        print(text)


if __name__ == "__main__":
    main()
