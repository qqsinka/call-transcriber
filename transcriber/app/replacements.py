"""Автозамены в распознанном тексте (термины, названия, частые ошибки ASR).

Формат файла: по строке на правило `регулярное выражение => замена`, регистр не учитывается,
в замене доступны группы (\\1). Строки с `#` в начале — комментарии.
Файл перечитывается при изменении — рестарт сервиса не нужен.
"""
import logging
import re
import shutil
import threading
from pathlib import Path

log = logging.getLogger(__name__)

SEPARATOR = "=>"
# шаблон из репозитория: копируется в DATA_DIR, если там файла ещё нет (data/ не коммитится)
EXAMPLE = Path(__file__).resolve().parents[1] / "replacements.example.txt"


def parse_rules(lines: list[str], source: str = "") -> list[tuple[re.Pattern, str]]:
    rules = []
    for num, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if SEPARATOR not in line:
            log.warning("%s:%d: нет '%s', правило пропущено", source, num, SEPARATOR)
            continue
        pattern, replacement = (part.strip() for part in line.rsplit(SEPARATOR, 1))
        try:
            rules.append((re.compile(pattern, re.IGNORECASE), replacement))
        except re.error as exc:
            log.warning("%s:%d: ошибка в выражении (%s), правило пропущено", source, num, exc)
    return rules


def apply_rules(text: str, rules: list[tuple[re.Pattern, str]]) -> str:
    for pattern, replacement in rules:
        text = pattern.sub(replacement, text)
    return text


class Replacer:
    def __init__(self, path: Path):
        self.path = path
        self._rules: list[tuple[re.Pattern, str]] = []
        self._mtime: float | None = None
        self._lock = threading.Lock()
        if not path.exists() and EXAMPLE.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(EXAMPLE, path)
            log.info("Создан %s из шаблона", path)

    def _reload(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            self._rules, self._mtime = [], None
            return
        if mtime == self._mtime:
            return
        lines = self.path.read_text(encoding="utf-8-sig").splitlines()
        self._rules, self._mtime = parse_rules(lines, str(self.path)), mtime
        log.info("Автозамены: загружено правил %d из %s", len(self._rules), self.path)

    def __call__(self, text: str) -> str:
        with self._lock:
            self._reload()
            rules = self._rules
        return apply_rules(text, rules)
