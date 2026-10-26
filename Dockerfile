# Сервис транскрибации: GigaAM + silero-vad + GigaAM-Emo, LLM — в отдельном контейнере Ollama.
#
#   docker compose build                                   # CPU
#   docker compose build --build-arg TORCH_INDEX=https://download.pytorch.org/whl/cu126   # GPU (NVIDIA)
#   docker compose build --build-arg PRELOAD_MODELS=1      # веса моделей внутри образа (сервер без интернета)

FROM python:3.12-slim

ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu
ARG PRELOAD_MODELS=0
ARG APP_UID=1000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MODEL_DIR=/opt/models \
    DATA_DIR=/app/data

# ffmpeg — декодирование любых аудиоформатов; git — установка GigaAM из репозитория
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# зависимости отдельным слоем: правка кода не пересобирает torch
COPY transcriber/requirements.txt transcriber/requirements.txt
RUN pip install --index-url "${TORCH_INDEX}" "torch==2.10.*" "torchaudio==2.10.*" \
    && pip install -r transcriber/requirements.txt

COPY transcriber transcriber

RUN useradd --create-home --uid "${APP_UID}" app \
    && mkdir -p /opt/models /app/data /inbox \
    && chown -R app:app /opt/models /app/data /inbox
USER app

RUN if [ "${PRELOAD_MODELS}" = "1" ]; then python transcriber/scripts/download_models.py; fi

EXPOSE 8000

# первый старт без PRELOAD_MODELS качает веса (~1 ГБ), поэтому start-period большой
HEALTHCHECK --interval=30s --timeout=10s --start-period=600s --retries=3 \
    CMD python -c "import json, urllib.request as u; s = json.load(u.urlopen('http://localhost:8000/health', timeout=5)); exit(0 if s['status'] == 'ok' else 1)"

CMD ["python", "-m", "uvicorn", "app.main:app", "--app-dir", "transcriber", "--host", "0.0.0.0", "--port", "8000"]
