"""Скачать записи звонков за день с веб-сервера АТС в папку автоприёма.

Настройки — в .env: ATS_URL, ATS_USER, ATS_PASSWORD, ATS_LINES (необязательно), INBOX_DIR.
Запуск:
  python transcriber/scripts/fetch_ats.py                     # вчерашний день
  python transcriber/scripts/fetch_ats.py --date 2026-10-26 --dry-run   # только показать, что скачается
  python transcriber/scripts/fetch_ats.py --date today --dest inbox
"""
import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.ats import AtsFetcher  # noqa: E402
from app.config import Settings  # noqa: E402


def parse_day(value: str) -> date:
    if value == "today":
        return date.today()
    if value == "yesterday":
        return date.today() - timedelta(days=1)
    return date.fromisoformat(value)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--date", default="yesterday", help="ГГГГ-ММ-ДД | today | yesterday (по умолчанию)")
    p.add_argument("--dest", help="куда скачивать; по умолчанию INBOX_DIR из настроек или ./inbox")
    p.add_argument("--dry-run", action="store_true", help="только показать список, ничего не скачивать")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    settings = Settings()
    dest = Path(a.dest or settings.inbox_dir or "inbox")
    report = AtsFetcher(settings).fetch_day(parse_day(a.date), dest, dry_run=a.dry_run)
    for name in report.downloaded:
        print(("будет скачано: " if a.dry_run else "скачано: ") + name)
    for name in report.failed:
        print("ОШИБКА: " + name)
    print(report)
    sys.exit(1 if report.failed else 0)


if __name__ == "__main__":
    main()
