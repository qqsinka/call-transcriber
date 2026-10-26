#!/bin/bash
# Сквозная проверка сервиса: API + очередь + папка автоприёма + транскрибация + эмоции + саммари.
# Запуск (WSL, из корня проекта): bash transcriber/scripts/checks/e2e_api.sh <звонок_для_API> <звонок_для_папки>
# Нужна запущенная Ollama (LLM_URL) с моделью из настроек. Данные — во временных папках, после проверки удаляются.
set -u
cd "$(dirname "$0")/../../.."
API_FILE="$1"; INBOX_FILE="$2"
PY=${PY:-$HOME/venvs/transcriber/bin/python}
export DATA_DIR=/tmp/e2e_data INBOX_DIR=/tmp/e2e_inbox INBOX_POLL_S=2 AUTH_USER=e2e AUTH_PASSWORD=e2e-pass PORT=8012
export LLM_URL=${LLM_URL:-http://192.168.0.1:11434}
B=localhost:$PORT; AUTH=e2e:e2e-pass
rm -rf $DATA_DIR $INBOX_DIR; mkdir -p $INBOX_DIR

j() { "$PY" -c "import sys,json; d=json.load(sys.stdin); print($1)"; }
status() { curl -s -u $AUTH $B/jobs/$1 | j 'd.get("status")'; }
wait_done() {
  for i in $(seq 1 900); do s=$(status "$1"); case $s in done|error) echo "  $1 -> $s за ~${i} с"; return;; esac; sleep 1; done
  echo "  $1 -> ТАЙМАУТ"
}

echo "== 1. Старт сервиса"
nohup make run VENV=$(dirname $(dirname $PY)) > /tmp/e2e.log 2>&1 &
for i in $(seq 1 120); do curl -s $B/health | grep -q '"ready":true' && break; sleep 1; done
curl -s $B/health | j 'json.dumps(d, ensure_ascii=False)'

echo "== 2. Авторизация: без пароля"
curl -s -o /dev/null -w "  HTTP %{http_code} (ожидаем 401)\n" -F call_id=x -F file=@"$API_FILE" $B/jobs

echo "== 3. Отправка через API + повтор без телефона"
curl -s -u $AUTH -F call_id=e2e-api -F phone=+70000000000 -F ext=101 -F call_date=2026-10-22T13:00:00 -F file=@"$API_FILE" $B/jobs; echo
curl -s -u $AUTH -F call_id=e2e-api -F file=@"$API_FILE" $B/jobs; echo

echo "== 4. Файл в папку автоприёма"
cp "$INBOX_FILE" $INBOX_DIR/e2e-inbox.mp3

echo "== 5. Ожидание результатов"
wait_done e2e-api
for i in $(seq 1 30); do [ "$(status e2e-inbox)" != "None" ] && break; sleep 1; done
wait_done e2e-inbox

for id in e2e-api e2e-inbox; do
  echo "== Результат $id"
  curl -s -u $AUTH $B/jobs/$id | "$PY" transcriber/scripts/checks/show_job.py
done

echo "== 6. База и папки"
"$PY" -c "import sqlite3; print('  строки в jobs:', sqlite3.connect('$DATA_DIR/jobs.db').execute('select call_id, version, status from jobs').fetchall())"
echo "  INBOX_DIR: $(ls $INBOX_DIR | tr '\n' ' ')"; echo "  INBOX_DIR/done: $(ls $INBOX_DIR/done | tr '\n' ' ')"
echo "  uploads (запись удаляется после обработки): $(ls $DATA_DIR/uploads | wc -l) файлов"
grep -E "готово|Из папки|переотправлена|ошибка" /tmp/e2e.log | sed 's/^/  лог: /'

pkill -f "uvicorn app.main:app --app-dir transcriber --host 0.0.0.0 --port $PORT"
rm -rf $DATA_DIR $INBOX_DIR
