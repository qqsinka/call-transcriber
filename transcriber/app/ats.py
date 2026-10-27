"""Загрузка записей звонков с веб-сервера АТС (список файлов Apache, basic-auth).

Структура на АТС: <ATS_URL>/ГГ/ММ/ДД/<линия или внутренний номер>/<запись>.mp3
Записи скачиваются в папку автоприёма (INBOX_DIR): дальше их подхватывает сервис.
Уже скачанные (лежат в папке или в её done/) и пустышки (несостоявшиеся звонки) пропускаются.
"""
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import unquote, urljoin

import httpx

from .config import AUDIO_EXTENSIONS, Settings

log = logging.getLogger(__name__)

_HREF = re.compile(r'<a\s+href="([^"?#]+)"', re.IGNORECASE)
# размер в колонке после ссылки: «288», «77K», «1.0M»
_SIZE = re.compile(r"</a>.*?>\s*(\d+(?:\.\d+)?)([KMG]?)\s*<", re.IGNORECASE | re.DOTALL)
_UNITS = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}


@dataclass
class Entry:
    name: str
    url: str
    is_dir: bool
    size: int | None = None


@dataclass
class FetchReport:
    day: date
    dry_run: bool = False
    downloaded: list[str] = field(default_factory=list)
    already: int = 0
    too_small: int = 0
    failed: list[str] = field(default_factory=list)
    folders: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        verb = "к скачиванию" if self.dry_run else "скачано"
        return (f"{self.day}: папки {', '.join(self.folders) or '—'}; {verb} {len(self.downloaded)}, "
                f"уже было {self.already}, пустышек {self.too_small}, ошибок {len(self.failed)}")


def parse_listing(html: str, base_url: str) -> list[Entry]:
    """Ссылки из страницы-списка Apache: папки и аудиофайлы (родительская папка и сортировка — мимо)."""
    entries = []
    for match in _HREF.finditer(html):
        href = match.group(1)
        if href.startswith(("/", "..", "http:", "https:")):
            continue
        name = unquote(href.rstrip("/"))
        is_dir = href.endswith("/")
        if not is_dir and Path(name).suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        size = None
        if not is_dir:
            m = _SIZE.search(html, match.end(), match.end() + 400)
            if m:
                size = int(float(m.group(1)) * _UNITS[m.group(2).upper()])
        entries.append(Entry(name, urljoin(base_url, href), is_dir, size))
    return entries


class AtsFetcher:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        if not settings.ats_url:
            raise ValueError("ATS_URL не задан")
        self.settings = settings
        auth = (settings.ats_user, settings.ats_password) if settings.ats_user else None
        self.client = client or httpx.Client(auth=auth, timeout=settings.ats_timeout_s, follow_redirects=True)

    def day_url(self, day: date) -> str:
        return f"{self.settings.ats_url.rstrip('/')}/{day:%y}/{day:%m}/{day:%d}/"

    def _list(self, url: str) -> list[Entry]:
        resp = self.client.get(url)
        if resp.status_code == 404:
            return []
        if resp.status_code == 401:
            raise PermissionError("АТС: неверный логин или пароль (ATS_USER / ATS_PASSWORD)")
        resp.raise_for_status()
        return parse_listing(resp.text, url)

    def _download(self, entry: Entry, target: Path) -> int:
        part = target.with_name(target.name + ".part")  # .part не похож на аудио — сервис его не тронет
        try:
            with self.client.stream("GET", entry.url) as resp:
                resp.raise_for_status()
                with part.open("wb") as out:
                    for chunk in resp.iter_bytes(64 * 1024):
                        out.write(chunk)
            size = part.stat().st_size
            if size < self.settings.ats_min_bytes:
                part.unlink()
                return size
            part.replace(target)
            return size
        except BaseException:
            part.unlink(missing_ok=True)
            raise

    def fetch_day(self, day: date, dest: Path, dry_run: bool = False) -> FetchReport:
        """Скачать записи за день в dest. Пропускает файлы, которые уже есть в dest или dest/done."""
        report = FetchReport(day, dry_run)
        dest.mkdir(parents=True, exist_ok=True)
        known = {p.name for folder in (dest, dest / "done") if folder.exists() for p in folder.iterdir()}
        wanted = set(self.settings.ats_lines)
        for folder in self._list(self.day_url(day)):
            if not folder.is_dir or (wanted and folder.name not in wanted):
                continue
            report.folders.append(folder.name)
            for entry in self._list(folder.url + ("" if folder.url.endswith("/") else "/")):
                if entry.is_dir:
                    continue
                if entry.name in known:
                    report.already += 1
                    continue
                if entry.size is not None and entry.size < self.settings.ats_min_bytes:
                    report.too_small += 1
                    continue
                if dry_run:
                    report.downloaded.append(entry.name)
                    continue
                try:
                    size = self._download(entry, dest / entry.name)
                except Exception as exc:
                    log.warning("АТС: не скачался %s: %s", entry.name, exc)
                    report.failed.append(entry.name)
                    continue
                if size < self.settings.ats_min_bytes:
                    report.too_small += 1
                else:
                    report.downloaded.append(entry.name)
        log.info("Загрузка с АТС %s", report)
        return report
