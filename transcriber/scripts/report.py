"""Просмотр результатов из базы сервиса: список звонков за день и выгрузка в txt-файлы.

Только читает jobs.db (сервису не мешает), без моделей и torch — запускается за секунду.

    python transcriber/scripts/report.py list   [--date 2026-10-28 | today | yesterday]
    python transcriber/scripts/report.py export [--date …] [--out <папка>]

export: по файлу на звонок (карточка, эмоции, расшифровка) и _список.txt; по умолчанию в <DATA_DIR>/../export/<дата>.
В файлах персональные данные клиентов — после просмотра удалить.
"""
import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import Settings  # noqa: E402
from app.db import JSON_FIELDS  # noqa: E402
from app.render import job_report  # noqa: E402


def parse_day(value: str) -> str:
    if value == "today":
        return date.today().isoformat()
    if value == "yesterday":
        return (date.today() - timedelta(days=1)).isoformat()
    return date.fromisoformat(value).isoformat()


def load_jobs(db_path: Path, day: str) -> list[dict]:
    """Звонки за день: по дате звонка из имени файла, для загруженных через API без даты — по дате приёма."""
    if not db_path.exists():
        raise SystemExit(f"Нет базы {db_path}")
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM jobs WHERE COALESCE(call_date, created_at) LIKE ? "
                            "ORDER BY COALESCE(call_date, created_at)", (f"{day}%",)).fetchall()
    finally:
        conn.close()
    jobs = []
    for row in rows:
        job = dict(row)
        for key in JSON_FIELDS:
            job[key] = json.loads(job[key]) if job.get(key) else None
        jobs.append(job)
    return jobs


def table(jobs: list[dict]) -> list[str]:
    lines = [f"{'Время':<8} {'Номер':<12} {'Длит.':>5}  {'Статус':<10} {'Приоритет':<11} {'Тон':<11} {'Нег.':>4}  Тема"]
    for job in jobs:
        s = job["summary"] or {}
        duration = int(job["duration_s"] or 0)
        when = (job["call_date"] or "")[11:16] or "—"
        lines.append(f"{when:<8} {job['phone'] or '—':<12} {duration // 60:>2}:{duration % 60:02d}  {job['status']:<10} "
                     f"{s.get('priority', ''):<11} {s.get('tone', ''):<11} "
                     f"{s.get('negative_score', ''):>4}  {s.get('topic') or job['error'] or job['warning'] or ''}")
    status = Counter(job["status"] for job in jobs)
    priority = Counter((job["summary"] or {}).get("priority") for job in jobs)
    lines += ["", f"Всего {len(jobs)}: " + ", ".join(f"{k} {v}" for k, v in sorted(status.items()))
              + f"; критических {priority['критический']}, важных {priority['важный']}"]
    return lines


def main() -> None:
    p = argparse.ArgumentParser(description="Список звонков за день и выгрузка в txt")
    p.add_argument("command", choices=["list", "export"])
    p.add_argument("--date", default="today", help="ГГГГ-ММ-ДД, today или yesterday")
    p.add_argument("--out", type=Path, help="папка выгрузки (по умолчанию <DATA_DIR>/../export/<дата>)")
    args = p.parse_args()

    settings = Settings()
    day = parse_day(args.date)
    jobs = load_jobs(settings.db_path, day)
    lines = table(jobs)
    if args.command == "list":
        print(f"Звонки за {day}\n")
        print("\n".join(lines))
        return

    out = args.out or settings.data_dir.resolve().parent / "export" / day
    out.mkdir(parents=True, exist_ok=True)
    # utf-8 с BOM — чтобы старый Блокнот Windows не показал кракозябры
    (out / "_список.txt").write_text(f"Звонки за {day}\n\n" + "\n".join(lines) + "\n", encoding="utf-8-sig")
    for job in jobs:
        (out / f"{job['call_id']}.txt").write_text(job_report(job), encoding="utf-8-sig")
    print(f"Выгружено звонков: {len(jobs)} -> {out}")
    print("В файлах персональные данные клиентов — после просмотра папку удалить.")


if __name__ == "__main__":
    main()
