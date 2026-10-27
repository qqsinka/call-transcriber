"""In-process очередь: N потоков-воркеров берут задачи из SQLite; watcher папки INBOX_DIR;
планировщик загрузки с АТС и уборки (scheduler.py)."""
import hashlib
import logging
import re
import shutil
import threading
import time
from pathlib import Path

from .asr import Transcriber
from .callmeta import parse_call_name
from .config import AUDIO_EXTENSIONS, Settings
from .db import JobStore
from .emotion import describe
from .scheduler import NightScheduler
from .summary import Summarizer

log = logging.getLogger(__name__)


def safe_name(call_id: str) -> str:
    """Имя файла из call_id: только безопасные символы + хвост, чтобы не было коллизий."""
    base = re.sub(r"[^\w.-]+", "_", call_id, flags=re.UNICODE)[:80] or "call"
    return f"{base}-{hashlib.sha1(call_id.encode()).hexdigest()[:8]}"


class Worker:
    def __init__(self, settings: Settings, store: JobStore, transcriber: Transcriber, summarizer: Summarizer):
        self.settings = settings
        self.store = store
        self.transcriber = transcriber
        self.summarizer = summarizer
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.active = 0
        self._active_lock = threading.Lock()
        self.scheduler = NightScheduler(settings, store)

    # ---- жизненный цикл ----
    def start(self) -> None:
        recovered = self.store.recover()
        if recovered:
            log.info("Возвращено в очередь после рестарта: %d", recovered)
        threading.Thread(target=self._load_model, name="asr-load", daemon=True).start()
        for i in range(max(1, self.settings.workers)):
            t = threading.Thread(target=self._loop, name=f"worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)
        if self.settings.inbox_dir:
            t = threading.Thread(target=self._watch_inbox, name="inbox", daemon=True)
            t.start()
            self._threads.append(t)
        # расписание загрузки с АТС и уборка старых файлов
        t = threading.Thread(target=self.scheduler.run, args=(self._stop,), name="scheduler", daemon=True)
        t.start()
        self._threads.append(t)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        for t in self._threads:
            t.join(timeout)

    def notify(self) -> None:
        self._wake.set()

    def _load_model(self) -> None:
        try:
            self.transcriber.load()
        except Exception:
            pass  # ошибка уже в логе и в /health

    # ---- обработка ----
    def _loop(self) -> None:
        while not self._stop.is_set():
            if not self.transcriber.ready.is_set():
                self.transcriber.ready.wait(timeout=1)
                continue
            job = self.store.claim_next()
            if not job:
                self._wake.wait(timeout=2)
                self._wake.clear()
                continue
            with self._active_lock:
                self.active += 1
            try:
                self.process(job)
            finally:
                with self._active_lock:
                    self.active -= 1

    def process(self, job: dict) -> None:
        call_id, version, path = job["call_id"], job["version"], job["file_path"]
        log.info("Задача %s: старт (%s)", call_id, path)
        started = time.time()
        try:
            result = self.transcriber.transcribe_file(path)
        except Exception as exc:
            if self.store.finish(call_id, version, "error", error=f"{type(exc).__name__}: {exc}",
                                 processing_s=round(time.time() - started, 2)):
                log.exception("Задача %s: ошибка распознавания", call_id)
            else:
                log.info("Задача %s переотправлена во время обработки — прерванный запуск отброшен", call_id)
            return
        asr_s = time.time() - started
        voice = "\n".join(describe(result["emotion"])) if result.get("emotion") else None
        summary, warning = self.summarizer.summarize(result["text"], voice)
        took = round(time.time() - started, 2)
        saved = self.store.finish(call_id, version, "done", summary=summary, warning=warning,
                                  processing_s=took, **result)
        if not saved:
            log.info("Задача %s переотправлена во время обработки — результат отброшен", call_id)
            return
        log.info("Задача %s: готово за %.1f с (ASR %.1f с, аудио %.1f с, фраз %d, саммари %s)",
                 call_id, took, asr_s, result["duration_s"], len(result["phrases"]), "да" if summary else "нет")
        # запись больше не нужна: текст, эмоции и саммари уже в базе (152-ФЗ, место на диске)
        if not self.settings.keep_audio:
            Path(path).unlink(missing_ok=True)

    # ---- приём файлов ----
    def submit_file(self, call_id: str, src: Path, file_name: str, source: str = "api", **meta) -> None:
        old = self.store.submit(call_id, str(src), file_name, source=source, **meta)
        if old and Path(old).parent == self.settings.upload_dir:
            Path(old).unlink(missing_ok=True)
        self.notify()

    def _watch_inbox(self) -> None:
        """Новый файл в INBOX_DIR -> задача с call_id = имя без расширения, сам файл -> INBOX_DIR/done."""
        inbox = Path(self.settings.inbox_dir)
        done = inbox / "done"
        done.mkdir(parents=True, exist_ok=True)
        sizes: dict[Path, int] = {}
        log.info("Слежу за папкой %s", inbox)
        while not self._stop.is_set():
            try:
                for f in sorted(inbox.iterdir()):
                    if not f.is_file() or f.suffix.lower() not in AUDIO_EXTENSIONS:
                        continue
                    size = f.stat().st_size
                    if sizes.get(f) != size:  # файл ещё копируется — ждём, пока размер перестанет меняться
                        sizes[f] = size
                        continue
                    sizes.pop(f, None)
                    target = done / f.name
                    if target.exists():
                        target = done / f"{f.stem}-{int(time.time())}{f.suffix}"
                    shutil.move(str(f), target)
                    # дата, номер абонента и линия/внутренний номер — из имени файла АТС, если оно в её формате
                    meta = parse_call_name(f.stem)
                    self.submit_file(f.stem, target, f.name, source="inbox", phone=meta.get("phone"),
                                     ext=meta.get("ext"), call_date=meta.get("call_date"))
                    log.info("Из папки: %s -> задача %s", f.name, f.stem)
            except Exception:
                log.exception("Ошибка обхода INBOX_DIR")
            self._stop.wait(self.settings.inbox_poll_s)
