"""FastAPI: приём записей звонков, статус и результат транскрибации."""
import logging
import secrets
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .asr import Transcriber
from .config import Settings
from .db import JobStore
from .summary import Summarizer
from .worker import Worker, safe_name

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("transcriber")

MAX_CALL_ID = 64


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.upload_dir.mkdir(parents=True, exist_ok=True)
        store = JobStore(settings.db_path)
        worker = Worker(settings, store, Transcriber(settings), Summarizer(settings))
        app.state.store, app.state.worker = store, worker
        if not settings.auth_user:
            log.warning("AUTH_USER не задан — basic-auth выключена")
        worker.start()
        yield
        worker.stop()
        store.close()

    app = FastAPI(title="Транскрибация звонков", version="1.0.0", lifespan=lifespan)
    basic = HTTPBasic(auto_error=False)

    def check_auth(credentials: HTTPBasicCredentials | None = Depends(basic)) -> None:
        if not settings.auth_user:
            return
        ok = credentials is not None and (
            secrets.compare_digest(credentials.username.encode(), settings.auth_user.encode())
            & secrets.compare_digest(credentials.password.encode(), settings.auth_password.encode())
        )
        if not ok:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверный логин или пароль",
                                headers={"WWW-Authenticate": "Basic"})

    @app.get("/health")
    def health():
        worker: Worker = app.state.worker
        t = worker.transcriber
        asr = {"model": settings.asr_model, "device": settings.device, "ready": t.ready.is_set()}
        if t.load_error:
            asr["error"] = t.load_error
        asr["emotion"] = bool(t.emotion and t.emotion.model) if settings.emotion_enabled else False
        if t.emotion_error:
            asr["emotion_error"] = t.emotion_error
        ffmpeg_ok = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
        if not ffmpeg_ok:
            state = "error"
        elif t.ready.is_set():
            state = "ok"
        else:
            state = "error" if t.load_error else "starting"
        return {
            "status": state,
            "ffmpeg": ffmpeg_ok,
            "asr": asr,
            "llm": worker.summarizer.health(),
            "queue": app.state.store.counts(),
            "active": worker.active,
            "inbox": settings.inbox_dir or None,
        }

    @app.post("/jobs", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(check_auth)])
    def create_job(
        file: UploadFile = File(...),
        call_id: str = Form(...),
        phone: str | None = Form(None),
        ext: str | None = Form(None),
        call_date: str | None = Form(None),
    ):
        call_id = call_id.strip()
        if not call_id or len(call_id) > MAX_CALL_ID:
            raise HTTPException(422, f"call_id обязателен, не длиннее {MAX_CALL_ID} символов")
        suffix = Path(file.filename or "").suffix.lower()[:10] or ".bin"
        # уникальное имя на каждую отправку: старый файл может ещё читать воркер
        target = settings.upload_dir / f"{safe_name(call_id)}-{uuid.uuid4().hex[:8]}{suffix}"
        with target.open("wb") as out:
            shutil.copyfileobj(file.file, out)
        if target.stat().st_size == 0:
            target.unlink()
            raise HTTPException(422, "пустой файл")
        app.state.worker.submit_file(call_id, target, file.filename or target.name,
                                     phone=phone, ext=ext, call_date=call_date)
        return {"call_id": call_id, "status": "queued"}

    @app.get("/jobs/{call_id}", dependencies=[Depends(check_auth)])
    def get_job(call_id: str):
        job = app.state.store.get(call_id)
        if not job:
            raise HTTPException(404, "задача не найдена")
        return {
            "call_id": job["call_id"],
            "status": job["status"],
            "duration_s": job["duration_s"],
            "phrases": job["phrases"] or [],
            "text": job["text"],
            "summary": job["summary"],
            "emotion": job["emotion"],  # эмоции по голосу: доли по каналам + самые «злые»/«позитивные» фразы
            "error": job["error"],
            # дополнительно
            "warning": job["warning"],
            "channels": job["channels"],
            "phone": job["phone"],
            "ext": job["ext"],
            "call_date": job["call_date"],
            "processing_s": job["processing_s"],
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
        }

    return app


app = create_app()
