VENV        ?= .venv
PY          := $(VENV)/bin/python
TORCH_INDEX ?= https://download.pytorch.org/whl/cpu
HOST        ?= 0.0.0.0
PORT        ?= 8000

.PHONY: install run test samples

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
