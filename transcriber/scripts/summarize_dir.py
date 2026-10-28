"""Саммари по готовым транскриптам (.txt из run_real.py) — второй этап пакетной обработки.

Транскрибация и LLM на одной машине не держатся в памяти одновременно (GigaAM в WSL + GigaChat 6.5 ГБ),
поэтому пачку обрабатываем в два этапа: run_real.py --txt-only, затем этот скрипт.
Саммари строится тем же кодом, что в сервисе (app.summary.Summarizer).

Запуск: python transcriber/scripts/summarize_dir.py <папка_транскриптов> <папка_саммари> [--skip-existing]
        [--emotion-dir <папка_эмоций>]  — добавить в саммари сводку эмоций по голосу
"""
import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import Settings  # noqa: E402
from app.render import summary_lines  # noqa: E402
from app.replacements import Replacer  # noqa: E402
from app.summary import LLMUnavailable, Summarizer  # noqa: E402

LINE = re.compile(r"^\[(\d\d:\d\d)-\d\d:\d\d\] ")


def transcript_text(path: Path) -> str:
    """txt из run_real.py -> текст в формате сервиса: без шапки, таймкод только начала фразы."""
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        lines.append(LINE.sub(r"[\1] ", line))
    return "\n".join(lines)


def emotion_lines(path: Path) -> list[str]:
    """Сводка из файла эмоций (run_real.py --emotion-dir): строки между заголовком и первой пустой строкой."""
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()[1:]
    return lines[: lines.index("")] if "" in lines else lines


def render(name: str, summary: dict | None, warning: str | None, model: str, took: float,
           emotion: list[str]) -> str:
    steps = ", ".join(f"{k} {v} с" for k, v in (summary or {}).get("llm_seconds", {}).items())
    head = [f"# {name}", f"# модель: {model}; время: {took:.1f} с" + (f" ({steps})" if steps else ""), ""]
    tail = ["", *emotion] if emotion else []
    if warning:
        tail += ["", f"Предупреждение: {warning}"]
    if not summary:
        return "\n".join(head + [f"САММАРИ НЕ ПОСТРОЕНО: {warning}"] + tail) + "\n"
    return "\n".join(head + summary_lines(summary) + tail) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("src")
    parser.add_argument("out")
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--emotion-dir", help="папка с эмоциями по голосу: их сводка добавится в саммари")
    args = parser.parse_args()
    emo_dir = Path(args.emotion_dir) if args.emotion_dir else None

    src, out = Path(args.src), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    files = sorted(src.glob("*.txt"))
    if args.skip_existing:
        files = [f for f in files if not (out / f.name).exists()]
    settings = Settings()
    summarizer = Summarizer(settings)
    # автозамены сервис применяет при распознавании; транскрипты могли быть сделаны до появления новых правил
    replace = Replacer(settings.replacements_path)
    print(f"к обработке: {len(files)} -> {out}; модель {settings.llm_model} ({settings.llm_url})", flush=True)

    started = time.time()
    try:
        summarizer.warmup()
    except LLMUnavailable as exc:
        sys.exit(f"LLM недоступна: {exc}")
    print(f"модель загружена за {time.time() - started:.1f} с", flush=True)

    done, failed = 0, []
    for num, f in enumerate(files, 1):
        emotion = emotion_lines(emo_dir / f.name) if emo_dir else []
        started = time.time()
        summary, warning = summarizer.summarize(replace(transcript_text(f)), "\n".join(emotion) or None)
        took = time.time() - started
        (out / f.name).write_text(render(f.name, summary, warning, settings.llm_model, took, emotion), encoding="utf-8")
        if summary:
            done += 1
            print(f"[{num}/{len(files)}] {f.stem[:17]}: {took:.1f} с, приоритет {summary['priority']}, "
                  f"негатив {summary['negative_score']}, тема: {summary['topic']}", flush=True)
        else:
            failed.append(f.name)
            print(f"!! [{num}/{len(files)}] {f.stem}: {took:.1f} с, НЕТ саммари: {warning}", flush=True)
    print(f"ИТОГО: саммари {done}, без саммари {len(failed)}")
    for name in failed:
        print(f"  без саммари: {name}")


if __name__ == "__main__":
    main()
