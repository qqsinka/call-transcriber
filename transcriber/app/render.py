"""Текстовое оформление результатов: таймкоды, сводка эмоций, карточка звонка, отчёт по звонку.

Без torch и моделей — используется и сервисом, и командами просмотра (scripts/report.py).
"""
ROLE_LABELS = {"client": "Клиент", "operator": "Оператор"}
LABELS_RU = {"angry": "злость", "sad": "грусть", "neutral": "нейтрально", "positive": "позитив"}


def format_ts(ms: int) -> str:
    sec = ms // 1000
    return f"{sec // 60:02d}:{sec % 60:02d}"


def describe(emotion: dict) -> list[str]:
    """Сводка эмоций по голосу текстом: для файлов результатов и как подсказка LLM при оценке звонка."""
    lines = []
    for channel, shares in emotion["by_channel"].items():
        who = "" if channel == "mono" else f" ({ROLE_LABELS.get(channel, channel)})"
        lines.append(f"Эмоции по голосу{who}: " + ", ".join(f"{LABELS_RU[k]} {v:.0%}" for k, v in shares.items()))
    for key, title in (("top_angry", "Злость"), ("top_positive", "Позитив")):
        items = emotion[key]
        lines.append(f"{title} (p>=0.5): " + ("; ".join(f"[{format_ts(x['start_ms'])}] {x['p']:.2f} «{x['text'][:60]}»"
                                                         for x in items) if items else "нет"))
    lines.append(f"Оценено фраз: {emotion['phrases_scored']} из {emotion['phrases_total']} (короткие реплики не оцениваются)")
    return lines


def summary_lines(summary: dict) -> list[str]:
    """Карточка звонка: тема, клиент, суть, итог, следующий шаг, оценка, пересказ, детали."""
    facts = summary.get("facts") or {}
    caller = ", ".join(x for x in (facts.get("caller") or {}).values() if x)
    lines = [
        f"Тема: {summary['topic']}",
        f"Клиент: {caller or '—'}",
        f"Суть обращения: {summary['problem']}",
        f"Итог: {summary['result']}",
        f"Следующий шаг: {summary['next_step'] or '—'}",
    ]
    if summary.get("priority"):
        lines += [
            f"Приоритет: {summary['priority']} — {summary['priority_reason']}",
            f"Оценка негатива: {summary['negative_score']}/10",
            f"Тон клиента (по смыслу): {summary['tone']} — {summary['tone_reason']}",
            f"Риски: {'; '.join(summary['risks']) or 'нет'}",
        ]
    lines += ["", "Саммари:", summary["summary"]]
    if facts.get("details"):
        lines += ["", "Детали: " + "; ".join(facts["details"])]
    return lines


def phrase_lines(phrases: list[dict]) -> list[str]:
    """Расшифровка по фразам; заметная эмоция голоса (не «нейтрально») — в конце строки."""
    lines = []
    for p in phrases:
        label = "" if p["channel"] == "mono" else f"{ROLE_LABELS.get(p['channel'], p['channel'])}: "
        mood = p.get("emotion")
        tag = f"  ⟨{LABELS_RU[mood]} {p['emotion_p']:.2f}⟩" if mood and mood != "neutral" else ""
        lines.append(f"[{format_ts(p['start_ms'])}] {label}{p['text']}{tag}")
    return lines


def job_report(job: dict) -> str:
    """Отчёт по звонку из записи базы (JobStore.get): шапка, карточка, эмоции, расшифровка."""
    when = (job.get("call_date") or "").replace("T", " ")
    duration = int(job.get("duration_s") or 0)
    head = [
        f"Звонок: {when or '—'}, номер {job.get('phone') or '—'}, линия {job.get('ext') or '—'}, "
        f"длительность {duration // 60}:{duration % 60:02d}",
        f"Файл: {job['call_id']}; обработка {round(job.get('processing_s') or 0)} с",
        "",
    ]
    if job["status"] != "done":
        return "\n".join(head + [f"Статус: {job['status']}" + (f" — {job['error']}" if job.get("error") else "")]) + "\n"
    body = summary_lines(job["summary"]) if job.get("summary") else [f"САММАРИ НЕТ: {job.get('warning') or '—'}"]
    if job.get("emotion"):
        body += ["", *describe(job["emotion"])]
    body += ["", "Расшифровка:", *phrase_lines(job.get("phrases") or [])]
    return "\n".join(head + body) + "\n"
