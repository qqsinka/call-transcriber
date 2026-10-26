"""Прогон папки с реальными записями через Transcriber (без API и LLM) + диагностика VAD.

Для каждого файла пишет <папка>/out/<имя>.txt и <имя>.json и печатает сводку.
Запуск: python transcriber/scripts/run_real.py samples/real [папка_вывода] [--txt-only] [--skip-existing]
  --txt-only       только .txt, без .json и без диагностики VAD (быстрее)
  --skip-existing  пропускать файлы, для которых .txt уже есть (дозапуск по докинутым записям)
Параметры VAD/ролей берутся из окружения (.env), как у сервиса: STEREO_MAP, VAD_*, MAX_SEGMENT_S.
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.asr import ROLE_LABELS, Transcriber, format_ts  # noqa: E402
from app.audio import SAMPLE_RATE, is_fake_stereo, load_channels, decode  # noqa: E402
from app.config import AUDIO_EXTENSIONS, Settings  # noqa: E402
from app.emotion import LABELS_RU, describe  # noqa: E402


def source_info(path: Path) -> dict:
    """Параметры исходника как есть (до перекодирования в 16 кГц)."""
    cmd = ["ffprobe", "-v", "error", "-select_streams", "a:0",
           "-show_entries", "stream=codec_name,channels,sample_rate,bit_rate:format=duration,bit_rate,format_name",
           "-of", "json", str(path)]
    info = json.loads(subprocess.run(cmd, capture_output=True, text=True, check=True).stdout)
    stream = info["streams"][0]
    fmt = info.get("format", {})
    return {
        "format": fmt.get("format_name"),
        "codec": stream.get("codec_name"),
        "channels": int(stream.get("channels") or 1),
        "sample_rate": int(stream.get("sample_rate") or 0),
        "bit_rate": int(stream.get("bit_rate") or fmt.get("bit_rate") or 0) or None,
        "duration_s": round(float(fmt.get("duration") or 0), 2),
    }


def rms_db(x: np.ndarray) -> float:
    return round(float(20 * np.log10(np.sqrt(np.mean(x ** 2)) + 1e-9)), 1)


def vad_diagnostics(t: Transcriber, path: Path, settings: Settings) -> list[dict]:
    """Сегменты VAD по каналам: где резало принудительно (по длине), короткие куски, уровень сигнала."""
    channels, _ = load_channels(str(path), settings.stereo_map)
    max_len = min(settings.max_segment_s, 24.0)
    out = []
    for name, wav in channels:
        t.vad.reset_states()
        segs = t._segments(wav)
        gaps = [(segs[i + 1][0] - segs[i][1]) / SAMPLE_RATE for i in range(len(segs) - 1)]
        speech_s = sum(b - a for a, b in segs) / SAMPLE_RATE
        out.append({
            "channel": name,
            "rms_db": rms_db(wav),
            "segments": len(segs),
            "speech_s": round(speech_s, 1),
            "speech_share": round(speech_s / (len(wav) / SAMPLE_RATE + 1e-9), 2),
            # кусок упёрся в MAX_SEGMENT_S, а следующий начался почти сразу -> вероятный разрез посреди фразы
            "forced_cuts": [format_ts(int(segs[i][1] * 1000 / SAMPLE_RATE)) for i, g in enumerate(gaps)
                            if g < 0.15 and (segs[i][1] - segs[i][0]) / SAMPLE_RATE >= max_len - 1.5],
            "short_segments": sum(1 for a, b in segs if (b - a) / SAMPLE_RATE < 0.5),
        })
    return out


def crosstalk(path: Path) -> float | None:
    """Корреляция огибающих каналов: высокая -> голос одного канала слышен в другом (эхо/смешанное стерео)."""
    wav = decode(str(path), 2)
    if is_fake_stereo(wav):
        return 1.0
    hop = SAMPLE_RATE // 50
    env = [np.abs(ch[: len(ch) // hop * hop]).reshape(-1, hop).mean(axis=1) for ch in wav]
    return round(float(np.corrcoef(env[0], env[1])[0, 1]), 2)


def render_txt(name: str, src: dict, result: dict, took: float) -> str:
    lines = [
        f"# {name}",
        f"# исходник: {src['codec']} {src['sample_rate']} Гц, каналов {src['channels']}, {src['duration_s']} с",
        f"# обработано как: {', '.join(result['channels'])}; обработка {took:.1f} с "
        f"({took / max(result['duration_s'], 1e-9) * 60:.1f} с на 1 мин)",
        "",
    ]
    for p in result["phrases"]:
        label = "" if p["channel"] == "mono" else f"{ROLE_LABELS.get(p['channel'], p['channel'])}: "
        lines.append(f"[{format_ts(p['start_ms'])}-{format_ts(p['end_ms'])}] {label}{p['text']}")
    return "\n".join(lines) + "\n"


def render_emotion_txt(name: str, result: dict) -> str:
    # сводку в шапке summarize_dir.py переносит в файл саммари и передаёт LLM на шаг оценки
    lines = [f"# {name}", *describe(result["emotion"]), ""]
    for p in result["phrases"]:
        label = "" if p["channel"] == "mono" else f"{ROLE_LABELS.get(p['channel'], p['channel'])}: "
        tag = f"{LABELS_RU[p['emotion']]} {p['emotion_p']:.2f}" if "emotion" in p else "—"
        lines.append(f"[{format_ts(p['start_ms'])}] {tag:>16} | {label}{p['text']}")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", nargs="?", default="samples/real")
    parser.add_argument("out_dir", nargs="?")
    parser.add_argument("--txt-only", action="store_true")
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--emotion-dir", help="папка для эмоций по голосу (по фразам + сводка)")
    args = parser.parse_args()

    folder = Path(args.folder)
    out_dir = Path(args.out_dir) if args.out_dir else folder / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    emo_dir = Path(args.emotion_dir) if args.emotion_dir else None
    if emo_dir:
        emo_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS)
    if not files:
        sys.exit(f"нет аудиофайлов в {folder}")
    if args.skip_existing:
        done = [f for f in files if (out_dir / f"{f.stem}.txt").exists()
                and (not emo_dir or (emo_dir / f"{f.stem}.txt").exists())]
        files = [f for f in files if f not in done]
        print(f"уже обработано, пропускаю: {len(done)}")
    print(f"к обработке: {len(files)} файлов -> {out_dir}")

    settings = Settings()
    t = Transcriber(settings)
    t.load()
    print(f"ASR={settings.asr_model} STEREO_MAP={','.join(settings.stereo_map)} "
          f"VAD: threshold={settings.vad_threshold} min_silence={settings.vad_min_silence_ms}ms "
          f"pad={settings.vad_speech_pad_ms}ms max_segment={settings.max_segment_s}s\n")

    summary, failed = [], []
    for num, f in enumerate(files, 1):
        try:
            src = source_info(f)
            started = time.time()
            result = t.transcribe_file(str(f))
        except Exception as exc:  # битый файл не должен останавливать пачку
            failed.append(f.name)
            print(f"!! [{num}/{len(files)}] {f.name}: ОШИБКА {type(exc).__name__}: {exc}", flush=True)
            continue
        took = time.time() - started
        per_min = took / max(result["duration_s"], 1e-9) * 60
        (out_dir / f"{f.stem}.txt").write_text(render_txt(f.name, src, result, took), encoding="utf-8")
        if emo_dir and result.get("emotion"):
            (emo_dir / f"{f.stem}.txt").write_text(render_emotion_txt(f.name, result), encoding="utf-8")
        summary.append((f.name, src, result, took, per_min))

        if args.txt_only:
            emo = result.get("emotion")
            angry = max((v["angry"] for v in emo["by_channel"].values()), default=0) if emo else None
            print(f"[{num}/{len(files)}] {f.name}: {result['duration_s']:.0f} с аудио, обработка {took:.1f} с, "
                  f"фраз {len(result['phrases'])}" + (f", злость по голосу {angry:.0%}" if emo else ""), flush=True)
            continue

        diag = vad_diagnostics(t, f, settings)
        xtalk = crosstalk(f) if src["channels"] == 2 else None
        (out_dir / f"{f.stem}.json").write_text(json.dumps({
            "file": f.name, "source": src, "processed_as": result["channels"],
            "duration_s": result["duration_s"], "processing_s": round(took, 2), "per_min_s": round(per_min, 1),
            "settings": {"asr_model": settings.asr_model, "stereo_map": list(settings.stereo_map),
                         "vad_threshold": settings.vad_threshold, "vad_min_silence_ms": settings.vad_min_silence_ms,
                         "vad_speech_pad_ms": settings.vad_speech_pad_ms, "max_segment_s": settings.max_segment_s},
            "vad": diag, "crosstalk": xtalk,
            "phrases": result["phrases"], "text": result["text"],
        }, ensure_ascii=False, indent=1), encoding="utf-8")

        print(f"== {f.name}: {src['codec']} {src['sample_rate']} Гц, каналов {src['channels']} -> "
              f"{'/'.join(result['channels'])}, {result['duration_s']} с, обработка {took:.1f} с, "
              f"{per_min:.1f} с/мин, фраз {len(result['phrases'])}, crosstalk={xtalk}")
        for d in diag:
            print(f"   VAD {d['channel']}: {d}")
        print(render_txt(f.name, src, result, took))

    total_audio = sum(r["duration_s"] for _, _, r, _, _ in summary)
    total_took = sum(tk for *_, tk, _ in summary)
    print(f"ИТОГО: готово {len(summary)}, ошибок {len(failed)}; аудио {total_audio:.0f} с, обработка {total_took:.0f} с, "
          f"{total_took / max(total_audio, 1e-9) * 60:.1f} с на 1 мин аудио")
    for name in failed:
        print(f"  ошибка: {name}")


if __name__ == "__main__":
    main()
