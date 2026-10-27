VENV        ?= .venv
PY          := $(VENV)/bin/python
TORCH_INDEX ?= https://download.pytorch.org/whl/cpu
HOST        ?= 0.0.0.0
PORT        ?= 8000
COMPOSE     := docker compose
COMPOSE_GPU := docker compose -f docker-compose.yml -f docker-compose.gpu.yml

DATE        ?= yesterday

.PHONY: install run test samples fetch-ats docker-up docker-up-gpu docker-logs docker-status docker-down docker-test docker-export docker-fetch-ats

# ---------- локальный запуск (venv) ----------
install:
	test -x $(PY) || python3 -m venv $(VENV)
	$(PY) -m pip install -U pip
	$(PY) -m pip install --index-url $(TORCH_INDEX) "torch==2.10.*" "torchaudio==2.10.*"
	$(PY) -m pip install -r transcriber/requirements.txt

run:
	$(PY) -m uvicorn app.main:app --app-dir transcriber --host $(HOST) --port $(PORT)

# юнит-тесты; сквозная проверка сервиса — transcriber/scripts/checks/e2e_api.sh
test:
	$(PY) -m pytest -v -s transcriber/tests

samples:
	$(PY) transcriber/scripts/make_samples.py samples

# записи за день с АТС в папку автоприёма: make fetch-ats DATE=2026-10-26 (по умолчанию вчера)
fetch-ats:
	$(PY) transcriber/scripts/fetch_ats.py --date $(DATE)

# ---------- Docker ----------
# сборка и запуск: сервис + Ollama; при первом старте скачиваются веса (GigaAM ~1 ГБ, LLM 6.5 ГБ)
docker-up:
	test -f .env || cp .env.example .env
	mkdir -p data inbox
	$(COMPOSE) up -d --build

docker-up-gpu:
	test -f .env || cp .env.example .env
	mkdir -p data inbox
	$(COMPOSE_GPU) up -d --build

docker-logs:
	$(COMPOSE) logs -f --tail=100 transcriber ollama-pull

docker-status:
	$(COMPOSE) ps
	curl -s http://localhost:$(PORT)/health; echo

docker-down:
	$(COMPOSE) down

# записи за день с АТС в ./inbox работающего сервиса: make docker-fetch-ats DATE=2026-10-26
docker-fetch-ats:
	$(COMPOSE) run --rm --no-deps transcriber python transcriber/scripts/fetch_ats.py --date $(DATE) --dest /inbox

# юнит-тесты внутри собранного образа
docker-test:
	$(COMPOSE) run --rm --no-deps transcriber python -m pytest -q -p no:cacheprovider transcriber/tests

# перенос на сервер без интернета: образы в архив (веса GigaAM — внутри образа, LLM — отдельным архивом тома)
docker-export:
	PRELOAD_MODELS=1 $(COMPOSE) build
	docker save call-transcriber:latest ollama/ollama:latest | gzip > call-transcriber-images.tar.gz
	docker run --rm -v call-transcriber_ollama:/data -v "$(CURDIR)":/backup alpine tar czf /backup/ollama-models.tar.gz -C /data .
