"""Оценка качества саммари по эталону (data/eval/reference.json).

Читает папку с файлами саммари (формат summarize_dir.py) и для каждого звонка считает:
  факты    — сколько ключевых фактов эталона нашлось в тексте (тема+суть+итог+шаг+саммари+риски)
  тон      — совпал ли с допустимым
  приоритет / негатив — если поля есть в файле
  длина    — 3-5 предложений в саммари, тема не длиннее 7 слов
  утечки   — куски промпта в ответе («Кто звонил:», «массив» и т. п.) или китайские символы
Запуск: python transcriber/scripts/eval_summary.py <папка_саммари> [эталон.json]
"""
import json
import re
import sys
from pathlib import Path

LEAKS = re.compile(r"кто звонил\s*:|с чем\s*:|чем законч\w*\s*:|массив|тон клиента|tone|summary|"
                   r"[぀-ヿ㐀-䶿一-鿿]", re.IGNORECASE)
FIELDS = {
    "topic": "Тема:", "problem": "Суть обращения:", "result": "Итог:", "next_step": "Следующий шаг:",
    "tone": "Тон клиента (по смыслу):", "priority": "Приоритет:", "neg": "Оценка негатива:", "risks": "Риски:",
}


def parse(path: Path) -> dict:
    lines = path.read_text(encoding="utf-8").splitlines()
    data = {"seconds": None}
    m = re.search(r"время:\s*([\d.]+)", lines[1] if len(lines) > 1 else "")
    if m:
        data["seconds"] = float(m.group(1))
    for i, line in enumerate(lines):
        for key, label in FIELDS.items():
            if line.startswith(label):
                data[key] = line[len(label):].strip()
        if line.startswith("Саммари:") and i + 1 < len(lines):
            data["summary"] = lines[i + 1].strip()
    if "tone" in data:
        data["tone_label"] = data["tone"].split("—")[0].strip().lower()
    if "neg" in data:
        m = re.match(r"(\d+)", data["neg"])
        data["neg_value"] = int(m.group(1)) if m else None
    return data


def sentences(text: str) -> int:
    return len([s for s in re.split(r"(?<=[.!?…])\s+", text.strip()) if s.strip()])


def main() -> None:
    folder = Path(sys.argv[1])
    ref = json.loads(Path(sys.argv[2] if len(sys.argv) > 2 else "data/eval/reference.json").read_text(encoding="utf-8"))["calls"]

    totals = {"facts": 0, "facts_hit": 0, "tone": 0, "prio": 0, "prio_n": 0, "neg": 0, "neg_n": 0,
              "len": 0, "topic": 0, "leaks": 0, "n": 0, "seconds": 0.0}
    print(f"{'звонок':18} {'факты':>6} {'тон':>4} {'прио':>4} {'негат':>5} {'предл':>5} {'тема':>4} {'утеч':>4}  пропущенные факты")
    for key, exp in ref.items():
        files = sorted(folder.glob(f"{key}*.txt"))
        if not files:
            print(f"{key:18} — нет файла")
            continue
        d = parse(files[0])
        text = " ".join(d.get(k, "") for k in ("topic", "problem", "result", "next_step", "summary", "risks"))
        missing = [f for f in exp["facts"] if not re.search(f, text, re.IGNORECASE)]
        hit = len(exp["facts"]) - len(missing)
        tone_ok = d.get("tone_label") in exp["tone"]
        prio = d.get("priority", "").split("—")[0].strip().lower() or None
        prio_ok = prio in exp["priority"] if prio else None
        neg = d.get("neg_value")
        neg_ok = exp["neg"][0] <= neg <= exp["neg"][1] if neg is not None else None
        n_sent = sentences(d.get("summary", ""))
        len_ok = 3 <= n_sent <= 5
        topic_ok = len(d.get("topic", "").split()) <= 7
        leaks = len(LEAKS.findall(" ".join(d.get(k, "") for k in ("topic", "problem", "result", "next_step", "summary"))))

        totals["n"] += 1
        totals["facts"] += len(exp["facts"])
        totals["facts_hit"] += hit
        totals["tone"] += tone_ok
        totals["len"] += len_ok
        totals["topic"] += topic_ok
        totals["leaks"] += leaks
        totals["seconds"] += d["seconds"] or 0
        if prio_ok is not None:
            totals["prio"] += prio_ok
            totals["prio_n"] += 1
        if neg_ok is not None:
            totals["neg"] += neg_ok
            totals["neg_n"] += 1
        mark = lambda ok: "—" if ok is None else ("ok" if ok else "НЕТ")  # noqa: E731
        print(f"{key:18} {hit:>3}/{len(exp['facts']):<2} {mark(tone_ok):>4} {mark(prio_ok):>4} "
              f"{(str(neg) if neg is not None else '—'):>5} {n_sent:>5} {mark(topic_ok):>4} {leaks:>4}  "
              + ", ".join(missing))

    n = totals["n"] or 1
    print("\nИТОГО:")
    print(f"  факты:      {totals['facts_hit']}/{totals['facts']} = {totals['facts_hit'] / max(totals['facts'], 1):.0%}")
    print(f"  тон:        {totals['tone']}/{n}")
    print(f"  приоритет:  " + (f"{totals['prio']}/{totals['prio_n']}" if totals["prio_n"] else "нет поля"))
    print(f"  негатив:    " + (f"{totals['neg']}/{totals['neg_n']} в диапазоне" if totals["neg_n"] else "нет поля"))
    print(f"  длина 3-5:  {totals['len']}/{n}; тема <=7 слов: {totals['topic']}/{n}; утечек промпта: {totals['leaks']}")
    print(f"  время LLM:  {totals['seconds']:.0f} с всего, {totals['seconds'] / n:.0f} с на звонок")


if __name__ == "__main__":
    main()
