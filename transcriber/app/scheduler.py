"""Ночной режим, режим реального времени и уборка.

- Реальное время: линии ATS_LIVE_LINES проверяются каждые ATS_LIVE_EVERY_MIN минут в часы ATS_LIVE_HOURS;
  новая запись (АТС кладёт файл после окончания разговора) сразу скачивается и уходит в обработку.
- В заданное время (ATS_FETCH_AT, например «20:30» или «13:00,20:30») скачивает с АТС записи за сегодня
  и за предыдущие ATS_FETCH_DAYS-1 дней в папку автоприёма — дальше их обрабатывает очередь.
  Уже обработанные звонки (есть в базе) и уже скачанные файлы повторно не качаются.
- Раз в час убирает старое: аудио, оставшееся после ошибок, старше AUDIO_RETENTION_DAYS;
  результаты старше RESULTS_RETENTION_DAYS (если задано).
"""
import logging
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from .ats import AtsFetcher, FetchReport
from .config import AUDIO_EXTENSIONS, Settings
from .db import JobStore

log = logging.getLogger(__name__)

CLEANUP_EVERY_S = 3600


def next_run(now: datetime, times: tuple[str, ...]) -> datetime | None:
    """Ближайший момент из списка «ЧЧ:ММ» после now (в том же часовом поясе)."""
    candidates = []
    for value in times:
        hh, mm = (int(x) for x in value.split(":"))
        moment = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        candidates.append(moment if moment > now else moment + timedelta(days=1))
    return min(candidates) if candidates else None


def in_hours(now: datetime, hours: str) -> bool:
    """now попадает в интервал «ЧЧ:ММ-ЧЧ:ММ» (интервал через полночь, например «22:00-06:00», тоже работает)."""
    start, end = (datetime.strptime(x.strip(), "%H:%M").time() for x in hours.split("-"))
    t = now.time()
    return start <= t < end if start <= end else (t >= start or t < end)


def remove_old_files(folder: Path, days: int, now: float | None = None) -> int:
    """Удалить аудиофайлы старше days дней (по времени изменения). Возвращает количество."""
    if days <= 0 or not folder.exists():
        return 0
    border = (now or time.time()) - days * 86400
    removed = 0
    for f in folder.iterdir():
        if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS | {".part"} and f.stat().st_mtime < border:
            f.unlink(missing_ok=True)
            removed += 1
    return removed


class NightScheduler:
    def __init__(self, settings: Settings, store: JobStore):
        self.settings = settings
        self.store = store
        self.last_fetch: list[str] = []
        self.last_fetch_at: str | None = None
        self.next_fetch_at: datetime | None = None
        self.last_cleanup: dict = {}
        self.live: dict = {"last_poll_at": None, "last_new_at": None, "last_new": [], "error": None}
        self._fetch_lock = threading.Lock()

    @property
    def inbox(self) -> Path:
        return Path(self.settings.inbox_dir or "inbox")

    def is_known(self, name: str) -> bool:
        """Звонок уже скачивали: задача с таким call_id есть в базе (сам файл к этому времени мог быть удалён)."""
        return self.store.count_call(Path(name).stem) > 0

    def fetch(self, days: list[date], limit: int | None = None, dry_run: bool = False) -> list[FetchReport]:
        """Скачать записи за указанные дни в папку автоприёма. Используется по расписанию и из API."""
        if not self.settings.ats_url:
            raise ValueError("загрузка с АТС не настроена: ATS_URL пуст")
        if not self.settings.inbox_dir:
            raise ValueError("INBOX_DIR не задан: скачанные записи некому обрабатывать")
        with self._fetch_lock:  # расписание и ручной запуск не качают одновременно
            fetcher = AtsFetcher(self.settings)
            reports = []
            for day in days:
                left = None if limit is None else limit - sum(len(r.downloaded) for r in reports)
                if left is not None and left <= 0:
                    break
                reports.append(fetcher.fetch_day(day, self.inbox, dry_run=dry_run, is_known=self.is_known, limit=left))
        if not dry_run:
            self.last_fetch = [str(r) for r in reports]
            self.last_fetch_at = datetime.now().isoformat(timespec="seconds")
        return reports

    def live_poll(self, now: datetime | None = None) -> FetchReport | None:
        """Одна проверка линий реального времени: новые записи за сегодня -> папка автоприёма.
        Если в этот момент идёт ночная или ручная загрузка — пропускаем, она и так всё заберёт."""
        if not self._fetch_lock.acquire(blocking=False):
            return None
        now = now or datetime.now()
        try:
            report = AtsFetcher(self.settings).fetch_day(now.date(), self.inbox, is_known=self.is_known,
                                                         lines=self.settings.ats_live_lines, quiet=True,
                                                         outgoing=False)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if error != self.live["error"]:  # одну и ту же ошибку каждые 2 минуты в лог не пишем
                log.warning("Реальное время: АТС недоступна: %s", error)
            self.live["error"] = error
            return None
        finally:
            self._fetch_lock.release()
        if self.live["error"]:
            log.info("Реальное время: связь с АТС восстановлена")
        self.live.update(error=None, last_poll_at=now.isoformat(timespec="seconds"))
        if report.downloaded:
            self.live.update(last_new_at=now.isoformat(timespec="seconds"), last_new=report.downloaded)
        return report

    def cleanup(self) -> dict:
        s = self.settings
        result = {
            "audio_removed": sum(remove_old_files(folder, s.audio_retention_days)
                                 for folder in (self.inbox / "done", s.upload_dir)),
            "results_removed": self.store.delete_finished_before(s.results_retention_days)
            if s.results_retention_days > 0 else 0,
        }
        if any(result.values()):
            log.info("Уборка: удалено аудио %d, результатов %d", result["audio_removed"], result["results_removed"])
        self.last_cleanup = {**result, "at": datetime.now().isoformat(timespec="seconds")}
        return result

    def run(self, stop: threading.Event) -> None:
        s = self.settings
        times = s.ats_fetch_at if s.ats_url and s.inbox_dir else ()
        live = bool(s.ats_url and s.inbox_dir and s.ats_live_lines)
        if times:
            log.info("Ночной режим: загрузка с АТС в %s, дней назад: %d", ", ".join(times), s.ats_fetch_days)
        if live:
            log.info("Реальное время: линии %s, каждые %g мин, в часы %s",
                     ", ".join(s.ats_live_lines), s.ats_live_every_min, s.ats_live_hours)
        last_cleanup = last_live = 0.0
        self.next_fetch_at = next_run(datetime.now(), times)
        while not stop.is_set():
            if live and time.time() - last_live >= s.ats_live_every_min * 60 and in_hours(datetime.now(), s.ats_live_hours):
                self.live_poll()
                last_live = time.time()
            if self.next_fetch_at and datetime.now() >= self.next_fetch_at:
                today = date.today()
                try:
                    self.fetch([today - timedelta(days=i) for i in range(max(1, self.settings.ats_fetch_days))])
                except Exception:
                    log.exception("Ночной режим: загрузка с АТС не удалась")
                self.next_fetch_at = next_run(datetime.now(), times)
            if time.time() - last_cleanup >= CLEANUP_EVERY_S:
                try:
                    self.cleanup()
                except Exception:
                    log.exception("Уборка не удалась")
                last_cleanup = time.time()
            stop.wait(10)
