# Установка на Windows Server (без Docker)

Для серверов, где Docker недоступен: Windows Server 2016/2019/2022, Windows 10/11. Скрипт ставит
всё в одну папку и запускает две службы Windows:

| Служба | Что делает |
|---|---|
| `call-transcriber-ollama` | LLM для саммари (GigaChat через Ollama), слушает только `127.0.0.1:11434` |
| `call-transcriber` | сервис транскрибации: API, загрузка с АТС по расписанию, распознавание, эмоции, саммари |

Службы стартуют вместе с Windows и перезапускаются при сбое. Работают от `LocalSystem`, наружу
по умолчанию ничего не открыто: API слушает `127.0.0.1:8000` (1С на этом же сервере обращается к нему локально).

## 0. Что нужно

- Свободно **~10 ГБ памяти** (LLM ~7 ГБ, распознавание ~2 ГБ) и **~25 ГБ на диске**.
  Если на сервере SQL Server — ему нужно ограничить память
  (SSMS → свойства сервера → Память → «Максимальный размер памяти сервера»), иначе он занимает всю.
- Доступ к интернету на время установки (напрямую или через прокси) и к веб-серверу записей АТС.
- PowerShell **от имени администратора**.

Скачивается ~10 ГБ: Python, PyTorch и библиотеки (~0,6 ГБ), ffmpeg (0,1 ГБ), веса GigaAM (0,9 ГБ),
Ollama (1,6 ГБ), GigaChat (6,5 ГБ).

## 1. Код на сервер

Репозиторий приватный, поэтому код удобнее привезти архивом. На своём ПК, в папке проекта:

```powershell
git archive -o call-transcriber.zip HEAD
```

(или на GitHub: Code → Download ZIP). Скопировать архив на сервер (AnyDesk, сетевая папка) и распаковать
в папку **без кириллицы и пробелов**, например `D:\call-transcriber`: внутри должны быть `transcriber\`,
`deploy\`, `.env.example`.

## 2. Установка

```powershell
cd D:\call-transcriber
powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1 -Proxy http://адрес-прокси:3128
```

Без прокси — без `-Proxy`. Займёт 30–60 минут, большую часть — скачивание LLM.

**По шагам с проверкой после каждого** — тот же скрипт с `-Step N` (по одному номеру за запуск):

```powershell
powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1 -Proxy http://адрес-прокси:3128 -Step 0
```

Шаг 0 ничего не меняет: проверяет путь, свободную память и диск, порты и доступ ко всем сайтам, откуда
будет скачиваться. Все строки `[OK]` — можно ставить. Дальше `-Step 1` … `-Step 8`:

1. Microsoft Visual C++ Redistributable (если нет; иногда просит перезагрузку — можно позже);
2. Python 3.12 в `D:\call-transcriber\python` (системный Python не трогается);
3. библиотеки в `venv\`;
4. ffmpeg в `tools\ffmpeg`;
5. `.env` из `.env.example` с путями этого сервера и **случайным паролем API** (`AUTH_PASSWORD`);
   права на файл — только администраторы и SYSTEM;
6. веса моделей распознавания и эмоций в `models\`;
7. Ollama в `ollama\`, служба `call-transcriber-ollama`, скачивание LLM в `ollama-models\`;
8. служба `call-transcriber` и ожидание готовности.

В конце должно быть `Готово: сервис работает.` Если скрипт упал на скачивании — просто запустить ещё раз,
он продолжит с того места.

## 3. Настройки

```powershell
notepad D:\call-transcriber\.env
```

| Параметр | Что вписать |
|---|---|
| `ATS_URL` | адрес записей на АТС, например `http://192.168.x.x:81/record` |
| `ATS_USER`, `ATS_PASSWORD` | учётка к записям на АТС |
| `ATS_LINES` | входящие линии через запятую, например `306090,687755`; пусто — все папки за день |
| `ATS_OUTGOING` | `true` — ночью забирать и исходящие: на АТС папка — номер, на который звонили, поэтому папки внешних номеров (5+ цифр, не входящие линии) — звонки сотрудников клиентам; папки внутренних номеров (`100`, `208`) — разговоры внутри офиса, не берутся |
| `ATS_FETCH_AT` | когда забирать записи, например `20:30` |
| `ATS_LIVE_LINES` | входящие в реальном времени, например `306090,687755` (см. раздел 5); исходящие сюда не входят — только ночью |
| `SUMMARY_DIR` | папка для txt-копий отчётов, например `D:\summary_ATC`: каждый готовый звонок сразу пишется в `вход\` или `исход\`; пусто — выключено |

Пути (`DATA_DIR`, `INBOX_DIR`, `MODEL_DIR`) и пароль API уже заполнены установщиком. После правки:

```powershell
deploy\windows\ct.ps1 restart
```

## 4. Проверка всей цепочки на 2 звонках

Все команды — из `D:\call-transcriber`, PowerShell от имени администратора.

```powershell
deploy\windows\ct.ps1 status                                  # "status": "ok", llm.model_available: true
deploy\windows\ct.ps1 fetch -Date 2026-10-26 -Dry             # только список: «к скачиванию N, пустышек M»
deploy\windows\ct.ps1 fetch -Date 2026-10-26 -Limit 2         # скачать 2 записи
deploy\windows\ct.ps1 logs -Follow                            # «Из папки: …» -> «готово за … с»; Ctrl+C — выход
deploy\windows\ct.ps1 job 26-10-26_09-02-47_79000000000_306090_1700000000.100000   # имя файла без .mp3
dir inbox, inbox\done                                         # пусто — аудио удалено после обработки
```

Повторный `fetch -Limit 2` за ту же дату: эти два звонка попадут в «уже было», скачаются следующие.

## 5. Ночной режим и нагрузка

В `ATS_FETCH_AT` сервис сам забирает записи за сегодня и вчера, обрабатывает по одной и удаляет аудио
после успешной обработки. Утром — `ct.ps1 status`: `ats.last_fetch` и `queue.done`.

На сервере, где работают 1С и SQL Server:
- обе службы работают с приоритетом **ниже обычного** (и LLM `llama-server` тоже): когда процессор нужен
  1С и SQL, Windows отдаёт его им, а транскрибация берёт свободное время. В диспетчере задач загрузка ЦП
  может показывать 100% — это нормально, если 1С отвечает быстро;
- днём реальное время (`ATS_LIVE_LINES`) включать с наблюдением: ~15 звонков в час занимают процессор
  на 35–45 минут каждый час и держат LLM в памяти (~7 ГБ); если очередь (`ct.ps1 list`) растёт или
  1С тормозит — оставить только ночной режим;
- LLM выгружается из памяти через 30 минут простоя (`LLM_KEEP_ALIVE`), днём остаётся только
  модель распознавания (~2 ГБ);
- ограничить ядра для распознавания можно `CPU_THREADS=4` в `.env`;
- после первых ночей проверить, не замедлилась ли 1С: счётчик кэша SQL Server
  ```sql
  SELECT cntr_value AS page_life_expectancy_сек FROM sys.dm_os_performance_counters
  WHERE counter_name = 'Page life expectancy' AND object_name LIKE '%Buffer Manager%';
  ```
  тысячи секунд — нормально; меньше ~300 — SQL не хватает памяти.

## 6. Обслуживание

| Задача | Команда |
|---|---|
| Состояние | `deploy\windows\ct.ps1 status` |
| Звонки за день | `deploy\windows\ct.ps1 list -Date 2026-10-28` (без `-Date` — сегодня): время, номер, приоритет, тон, тема |
| Выгрузить в txt | `deploy\windows\ct.ps1 export -Date 2026-10-28` — в `export\<дата>\`: `_список.txt` и файл на каждый звонок (карточка, эмоции, расшифровка). Там персональные данные — после просмотра удалить |
| Результат одного звонка | `deploy\windows\ct.ps1 job <имя файла без .mp3>` |
| Дописать в `SUMMARY_DIR` уже обработанные | `deploy\windows\ct.ps1 save -Date 2026-10-28` — отчёты готовых звонков за день в `вход\`/`исход\` (новые сервис пишет сам) |
| Лог | `deploy\windows\ct.ps1 logs` (файлы — `logs\`) |
| Перезапуск после правки `.env` | `deploy\windows\ct.ps1 restart` |
| Остановить / запустить | `deploy\windows\ct.ps1 stop` / `start` |
| Обновить версию | распаковать новый архив кода поверх папки (`Expand-Archive <архив> -DestinationPath D:\call-transcriber -Force`; `.env`, `data\`, `models\` не трогаются) и `ct.ps1 restart`; если менялся `install.ps1` — вместо restart снова запустить `install.ps1` (скачанное не качается заново) |
| Резервная копия результатов | скопировать `data\jobs.db` и `data\replacements.txt` |
| Тесты | `venv\Scripts\python -m pytest transcriber\tests -q` |
| Освободить место | папку `downloads\` (установщики, ~2 ГБ) после установки можно удалить |
| Удалить полностью | `ct.ps1 stop`, затем `services\call-transcriber.exe uninstall` и `services\call-transcriber-ollama.exe uninstall`, затем удалить папку. Вне папки установщик меняет только одно: ставит Visual C++ Redistributable, если его не было |

Словарь автозамен `data\replacements.txt` правится на лету, перезапуск не нужен.

## 7. Если что-то не так

| Симптом | Что проверить |
|---|---|
| `install.ps1` упал на скачивании | доступ через прокси; запустить ещё раз — скачанное не качается заново |
| `PyTorch не загружается` | установить Visual C++ Redistributable вручную: https://aka.ms/vs/17/release/vc_redist.x64.exe |
| `status`: `"starting"` долго | `ct.ps1 logs` — грузятся модели или ошибка |
| `"model_available": false` | LLM не скачалась: `ollama\ollama.exe pull <LLM_MODEL из .env>` (качает служба Ollama, прокси у неё уже настроен) |
| АТС: `неверный логин или пароль` | `ATS_USER`/`ATS_PASSWORD` в `.env`, затем `ct.ps1 restart` |
| АТС: `АТС недоступна` | `Test-NetConnection адрес-АТС -Port 81` |
| Служба не стартует | `logs\call-transcriber.err.log`, `logs\call-transcriber-ollama.err.log`; «Просмотр событий» → Windows → Система |
| Нужен доступ к API с другого компьютера | `install.ps1 -ListenHost 0.0.0.0` и правило брандмауэра только для нужного IP: `New-NetFirewallRule -DisplayName call-transcriber -Direction Inbound -Protocol TCP -LocalPort 8000 -RemoteAddress <IP> -Action Allow` |
