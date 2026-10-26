"""Эксперимент: сравнение LLM для саммари и тона на готовых расшифровках.

Берёт тексты из <папка>/*.json (результат run_real.py), для каждой модели строит саммари
тем же кодом, что сервис (app.summary.Summarizer), и пишет:
  <вывод>/<модель>/<имя>.json — ответ модели, время, число попыток
  <вывод>/report.md           — сводная таблица и все саммари рядом
Запуск: LLM_URL=http://host:11434 python transcriber/scripts/compare_llm.py samples/real/out samples/real/out_llm model1 model2 ...
"""
import json
import re
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import Settings  # noqa: E402
from app.summary import Summarizer  # noqa: E402


def safe(name: str) -> str:
    return re.sub(r"[^\w.-]+", "_", name)[-60:]


def main() -> None:
    src, out = Path(sys.argv[1]), Path(sys.argv[2])
    models = sys.argv[3:]
    transcripts = sorted(src.glob("*.json"))
    base = Settings()
    rows, details = [], []

    for model in models:
        summarizer = Summarizer(replace(base, llm_model=model, llm_enabled=True))
        attempts = {"n": 0}
        original_chat = summarizer._chat

        def counting_chat(messages, _orig=original_chat):
            attempts["n"] += 1
            return _orig(messages)

        summarizer._chat = counting_chat
        model_dir = out / safe(model)
        model_dir.mkdir(parents=True, exist_ok=True)

        # первый запрос модели включает её загрузку в память — прогреваем отдельно, чтобы не портить замер
        started = time.time()
        summarizer.warmup()
        print(f"== {model}: загрузка/прогрев {time.time() - started:.1f} с", flush=True)

        for path in transcripts:
            data = json.loads(path.read_text(encoding="utf-8"))
            attempts["n"] = 0
            started = time.time()
            summary, warning = summarizer.summarize(data["text"])
            took = time.time() - started
            record = {"model": model, "file": data["file"], "seconds": round(took, 1),
                      "attempts": attempts["n"], "warning": warning, "summary": summary}
            if summary:
                sentences = len([x for x in re.split(r"(?<=[.!?…])\s+", summary["summary"]) if x.strip()])
                record["summary_sentences"] = sentences
                record["topic_words"] = len(summary["topic"].split())
                verdict = f"OK тон={summary['tone']}, предложений {sentences}, слов в теме {record['topic_words']}"
            else:
                verdict = "НЕТ: " + str(warning)
            print(f"   {path.stem[:20]}: {took:.1f} с, попыток {attempts['n']}, {verdict}", flush=True)
            (model_dir / f"{path.stem}.json").write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
            rows.append(record)

    lines = ["# Сравнение LLM: саммари и тон", "",
             "| Модель | Звонок | Время, с | Попыток | Тон | Итог |", "|---|---|---|---|---|---|"]
    for r in rows:
        s = r["summary"]
        lines.append(f"| {r['model']} | {r['file'][:17]} | {r['seconds']} | {r['attempts']} | "
                     f"{s['tone'] if s else '—'} | {'ok' if s else r['warning']} |")
    for path in transcripts:
        name = json.loads(path.read_text(encoding="utf-8"))["file"]
        lines += ["", f"## {name[:17]}"]
        for r in rows:
            if r["file"] != name:
                continue
            s = r["summary"] or {}
            lines += ["", f"**{r['model']}** ({r['seconds']} с)", ""]
            lines += [f"- {k}: {s.get(k, '')}" for k in ("topic", "problem", "result", "next_step", "summary", "tone", "tone_reason")]
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("отчёт:", out / "report.md")


if __name__ == "__main__":
    main()
