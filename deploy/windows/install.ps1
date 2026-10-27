<#
Установка сервиса транскрибации на Windows Server без Docker: Python, зависимости, ffmpeg,
веса GigaAM, Ollama с LLM и две службы Windows (call-transcriber-ollama, call-transcriber).

Запуск из папки с кодом, PowerShell от имени администратора:
    powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1 -Proxy http://адрес-прокси:3128

Без прокси — без -Proxy. Повторный запуск безопасен: скачанное и установленное пропускается,
службы перезапускаются с новым кодом (так же ставится обновление).

По шагам: -Step 0 — только проверка сервера и доступа к сайтам, ничего не меняет;
-Step 1, -Step 2 … -Step 8 — отдельные шаги установки; без -Step — проверка и все шаги подряд.
#>
param(
    [int[]]$Step = (0..8),
    # прокси для скачивания (pip, модели, Ollama); службе транскрибации не передаётся
    [string]$Proxy = "",
    # 127.0.0.1 — API доступен только с этого сервера (1С здесь же); 0.0.0.0 — из сети
    [string]$ListenHost = "127.0.0.1",
    [int]$Port = 8000,
    [string]$OllamaVersion = "0.40.11",
    [string]$PythonVersion = "3.12.10"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
# обращения к localhost (Ollama, сервис) — напрямую, мимо прокси из настроек IE
[Net.WebRequest]::DefaultWebProxy = New-Object Net.WebProxy
Add-Type -AssemblyName System.IO.Compression.FileSystem

$Root = (Resolve-Path "$PSScriptRoot\..\..").Path
$Dl = "$Root\downloads"
$Tools = "$Root\tools"
$Svc = "$Root\services"
$Py = "$Root\python\python.exe"
$VPy = "$Root\venv\Scripts\python.exe"
$FfBin = "$Tools\ffmpeg\bin"
$Ollama = "$Root\ollama\ollama.exe"
$EnvFile = "$Root\.env"
$Utf8 = New-Object Text.UTF8Encoding($false)

function Step($n, $text) {
    if ($Step -notcontains $n) { return $false }
    Write-Host "`n== $n/8 $text" -ForegroundColor Cyan
    return $true
}

function Check($ok, $text) {
    if ($ok) { Write-Host "   [OK]   $text" -ForegroundColor Green } else { Write-Host "   [!!]   $text" -ForegroundColor Red }
    if (-not $ok) { $script:problems++ }
}

function Run($exe, [string[]]$argList) {
    & $exe @argList
    if ($LASTEXITCODE -ne 0) { throw "$(Split-Path $exe -Leaf) $($argList -join ' '): код $LASTEXITCODE" }
}

function Download($url, $file) {
    if ((Test-Path $file) -and (Get-Item $file).Length -gt 0) { Write-Host "   уже скачано: $(Split-Path $file -Leaf)"; return }
    Write-Host "   скачиваю $url"
    $wc = New-Object Net.WebClient
    if ($Proxy) { $wc.Proxy = New-Object Net.WebProxy($Proxy) }
    $wc.DownloadFile($url, "$file.part")
    Move-Item "$file.part" $file -Force
}

function Extract($zip, $dest) {
    if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
    [IO.Compression.ZipFile]::ExtractToDirectory($zip, $dest)
}

function Read-EnvValue($name) {
    foreach ($line in [IO.File]::ReadAllLines($EnvFile, $Utf8)) {
        if ($line -match "^$name=(.*)$") { return $Matches[1].Trim() }
    }
    return ""
}

function Wait-Http($url, $seconds, $what) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        try { return Invoke-RestMethod $url -TimeoutSec 5 } catch { Start-Sleep 3 }
    }
    throw "$what не ответил за $seconds с — смотрите логи в $Root\logs"
}

# Служба через WinSW: services\<id>.exe + services\<id>.xml. Уже установленная — останавливается
# и запускается с новым xml (путь к exe тот же, переустанавливать не нужно).
function Install-Service($id, $name, $description, $exe, $arguments, [hashtable]$envs, $depend) {
    $base = "$Svc\$id"
    $esc = { param($s) [Security.SecurityElement]::Escape([string]$s) }
    $envXml = ($envs.GetEnumerator() | Sort-Object Name | ForEach-Object {
        "  <env name=`"$($_.Name)`" value=`"$(& $esc $_.Value)`"/>" }) -join "`r`n"
    $dependXml = if ($depend) { "  <depend>$depend</depend>" } else { "" }
    $xml = @"
<service>
  <id>$id</id>
  <name>$(& $esc $name)</name>
  <description>$(& $esc $description)</description>
  <executable>$(& $esc $exe)</executable>
  <arguments>$(& $esc $arguments)</arguments>
  <workingdirectory>$(& $esc $Root)</workingdirectory>
  <startmode>Automatic</startmode>
$dependXml
$envXml
  <onfailure action="restart" delay="10 sec"/>
  <onfailure action="restart" delay="30 sec"/>
  <resetfailure>1 hour</resetfailure>
  <stoptimeout>20 sec</stoptimeout>
  <logpath>$(& $esc "$Root\logs")</logpath>
  <log mode="roll-by-size">
    <sizeThreshold>10240</sizeThreshold>
    <keepFiles>5</keepFiles>
  </log>
</service>
"@
    $exists = [bool](Get-Service $id -ErrorAction SilentlyContinue)
    if ($exists) { Stop-Service $id -Force -ErrorAction SilentlyContinue }
    Copy-Item "$Dl\WinSW.NET461.exe" "$base.exe" -Force
    [IO.File]::WriteAllText("$base.xml", $xml, $Utf8)
    if (-not $exists) { Run "$base.exe" @("install") }
    Start-Service $id
    Write-Host "   служба $id запущена"
}

# ---------------------------------------------------------------------------
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { throw "Запустите PowerShell от имени администратора" }
if (-not (Test-Path "$Root\transcriber\app\main.py")) { throw "Не найден код сервиса в $Root\transcriber" }
Set-Location $Root
if ($Proxy) {
    # для pip, загрузки весов и ollama pull; localhost — напрямую
    $env:HTTP_PROXY = $Proxy; $env:HTTPS_PROXY = $Proxy; $env:NO_PROXY = "localhost,127.0.0.1"
}
Write-Host "Папка установки: $Root"

if (Step 0 "Проверка сервера и доступа к сайтам (ничего не меняет)") {
    $script:problems = 0
    $os = Get-CimInstance Win32_OperatingSystem
    Write-Host "   $($os.Caption), сборка $($os.BuildNumber)"
    Check ($Root -notmatch '[^\x21-\x7E\\:]') "путь без кириллицы и пробелов: $Root"
    $freeRam = [math]::Round($os.FreePhysicalMemory / 1MB, 1)
    Check ($freeRam -ge 9) "свободная память $freeRam ГБ (нужно ~9–10 ГБ: LLM ~7, распознавание ~2)"
    $drive = Get-PSDrive ($Root.Substring(0, 1))
    $freeDisk = [math]::Round($drive.Free / 1GB, 1)
    Check ($freeDisk -ge 30) "свободно на диске $($drive.Name): $freeDisk ГБ (нужно ~25 ГБ)"
    $busy = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object LocalPort -in $Port, 11434
    $ours = @(Get-Service call-transcriber, call-transcriber-ollama -ErrorAction SilentlyContinue)
    Check (-not $busy -or $ours) "порты $Port и 11434 свободны$(if ($busy) { ' (заняты: ' + (($busy.LocalPort | Sort-Object -Unique) -join ', ') + ')' })"
    if ($ours) { Write-Host "   службы уже установлены: $(($ours | ForEach-Object { "$($_.Name)=$($_.Status)" }) -join ', ') — будет обновление" }
    $gigaam = ([IO.File]::ReadAllLines("$Root\transcriber\requirements.txt", $Utf8) |
        Where-Object { $_ -match '^gigaam @ git\+(https://github\.com/[^@\s]+)@([0-9a-f]+)' } | Select-Object -First 1) -replace
        '^gigaam @ git\+(https://github\.com/[^@\s]+)@([0-9a-f]+).*', '$1/archive/$2.zip'
    $sites = [ordered]@{
        "Python"                = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe"
        "VC++ Redistributable"  = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
        "PyTorch"               = "https://download.pytorch.org/whl/cpu/torch/"
        "PyPI"                  = "https://pypi.org/simple/fastapi/"
        "PyPI (файлы)"          = "https://files.pythonhosted.org/"
        "GigaAM (код)"          = $gigaam
        "GigaAM (веса)"         = "https://cdn.chatwm.opensmodel.sberdevices.ru/GigaAM/v3_e2e_rnnt_tokenizer.model"
        "ffmpeg"                = "https://github.com/GyanD/codexffmpeg/releases/download/9.0.2/ffmpeg-9.0.2-essentials_build.zip"
        "Ollama"                = "https://github.com/ollama/ollama/releases/download/v$OllamaVersion/ollama-windows-amd64.zip"
        "WinSW"                 = "https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW.NET461.exe"
        "Hugging Face (LLM)"    = "https://huggingface.co/api/models/ai-sage/GigaChat3.1-10B-A1.8B-GGUF"
    }
    foreach ($name in $sites.Keys) {
        $req = @{ Uri = $sites[$name]; Method = "Head"; UseBasicParsing = $true; TimeoutSec = 30 }
        if ($Proxy) { $req.Proxy = $Proxy }
        try { $code = [int](Invoke-WebRequest @req).StatusCode }
        catch { $code = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { $_.Exception.Message } }
        # 403/404/405 — сервер ответил (не любит HEAD), значит доступ есть
        Check ($code -is [int] -and $code -lt 500) ("{0,-22} {1}" -f $name, $code)
    }
    if ($script:problems) { Write-Host "`nЕсть замечания: $($script:problems) — разобраться до установки." -ForegroundColor Yellow }
    else { Write-Host "`nВсё в порядке, можно ставить." -ForegroundColor Green }
}

if ($Step | Where-Object { $_ -gt 0 }) {
    foreach ($d in $Dl, $Tools, $Svc, "$Root\logs", "$Root\models", "$Root\data", "$Root\inbox") {
        New-Item $d -ItemType Directory -Force | Out-Null
    }
}

if (Step 1 "Microsoft Visual C++ Redistributable (нужен PyTorch)") {
    $vc = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" -ErrorAction SilentlyContinue
    if ($vc -and $vc.Installed -eq 1 -and $vc.Minor -ge 30) {
        Write-Host "   уже установлен: $($vc.Version)"
    } else {
        Download "https://aka.ms/vs/17/release/vc_redist.x64.exe" "$Dl\vc_redist.x64.exe"
        $p = Start-Process "$Dl\vc_redist.x64.exe" -ArgumentList "/install /quiet /norestart" -Wait -PassThru
        if ($p.ExitCode -eq 3010) { Write-Warning "VC++ установлен, Windows просит перезагрузку — можно сделать позже" }
        elseif ($p.ExitCode -notin 0, 1638) { throw "vc_redist: код $($p.ExitCode)" }
        else { Write-Host "   установлен" }
    }
}

if (Step 2 "Python $PythonVersion в $Root\python") {
    if (Test-Path $Py) {
        Write-Host "   уже установлен"
    } else {
        $exe = "$Dl\python-$PythonVersion-amd64.exe"
        Download "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe" $exe
        $p = Start-Process $exe -Wait -PassThru -ArgumentList (
            "/quiet InstallAllUsers=0 TargetDir=`"$Root\python`" Include_launcher=0 Include_test=0 " +
            "Include_doc=0 Include_tcltk=0 Shortcuts=0 AssociateFiles=0 PrependPath=0")
        if ($p.ExitCode -ne 0 -or -not (Test-Path $Py)) { throw "Установщик Python: код $($p.ExitCode)" }
    }
    & $Py --version
}

if (Step 3 "Библиотеки (PyTorch CPU, GigaAM, silero-vad, FastAPI) — ~1 ГБ") {
    if (-not (Test-Path $Py)) { throw "Сначала шаг 2 (Python)" }
    if (-not (Test-Path $VPy)) { Run $Py @("-m", "venv", "$Root\venv") }
    Run $VPy @("-m", "pip", "install", "--disable-pip-version-check", "-q", "-U", "pip")
    Run $VPy @("-m", "pip", "install", "--disable-pip-version-check", "--index-url", "https://download.pytorch.org/whl/cpu",
               "torch==2.10.*", "torchaudio==2.10.*")
    # GigaAM в requirements.txt ставится через git; здесь git нет — тот же коммит архивом с GitHub
    $req = [IO.File]::ReadAllLines("$Root\transcriber\requirements.txt", $Utf8) | ForEach-Object {
        if ($_ -match '^gigaam @ git\+(https://github\.com/[^@\s]+)@([0-9a-f]+)') { "gigaam @ $($Matches[1])/archive/$($Matches[2]).zip" }
        else { $_ }
    }
    [IO.File]::WriteAllLines("$Dl\requirements-win.txt", [string[]]$req, $Utf8)
    Run $VPy @("-m", "pip", "install", "--disable-pip-version-check", "-r", "$Dl\requirements-win.txt")
    & $VPy -c "import torch, gigaam, silero_vad, fastapi; print('   torch', torch.__version__, '- библиотеки загружаются')"
    if ($LASTEXITCODE -ne 0) {
        throw "PyTorch не загружается. Обычно не хватает Microsoft Visual C++ Redistributable: https://aka.ms/vs/17/release/vc_redist.x64.exe"
    }
}

if (Step 4 "ffmpeg в $FfBin") {
    if (Test-Path "$FfBin\ffmpeg.exe") {
        Write-Host "   уже установлен"
    } else {
        $zip = "$Dl\ffmpeg.zip"
        # сборка gyan.dev: зеркало на GitHub быстрее самого сайта
        try { Download "https://github.com/GyanD/codexffmpeg/releases/download/9.0.2/ffmpeg-9.0.2-essentials_build.zip" $zip }
        catch { Download "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" $zip }
        Extract $zip "$Dl\ffmpeg"
        $bin = (Get-ChildItem "$Dl\ffmpeg" -Recurse -Filter ffmpeg.exe | Select-Object -First 1).DirectoryName
        New-Item $FfBin -ItemType Directory -Force | Out-Null
        Copy-Item "$bin\*" $FfBin -Force
        Remove-Item "$Dl\ffmpeg" -Recurse -Force
    }
    & "$FfBin\ffmpeg.exe" -hide_banner -version | Select-Object -First 1
}

if (Step 5 "Настройки .env") {
    if (Test-Path $EnvFile) {
        Write-Host "   уже есть: $EnvFile (не трогаю)"
    } else {
        $bytes = New-Object byte[] 15
        (New-Object Security.Cryptography.RNGCryptoServiceProvider).GetBytes($bytes)
        $password = [Convert]::ToBase64String($bytes) -replace '[+/=]', 'x'
        $set = @{
            MODEL_DIR = "$Root\models"; DATA_DIR = "$Root\data"; INBOX_DIR = "$Root\inbox"
            LLM_URL = "http://127.0.0.1:11434"; AUTH_PASSWORD = $password
        }
        $lines = [IO.File]::ReadAllLines("$Root\.env.example", $Utf8) | ForEach-Object {
            if ($_ -match '^([A-Z_]+)=' -and $set.ContainsKey($Matches[1])) { "$($Matches[1])=$($set[$Matches[1]])" } else { $_ }
        }
        [IO.File]::WriteAllLines($EnvFile, [string[]]$lines, $Utf8)
        # пароли в файле: читать только администраторам и службам (SYSTEM)
        & icacls $EnvFile /inheritance:r /grant:r "*S-1-5-32-544:F" "*S-1-5-18:F" | Out-Null
        Write-Host "   создан $EnvFile, пароль API (AUTH_PASSWORD) сгенерирован — он в этом файле"
    }
    foreach ($name in "DATA_DIR", "INBOX_DIR", "MODEL_DIR", "LLM_URL", "LLM_MODEL", "ATS_URL", "ATS_FETCH_AT", "ATS_LIVE_LINES") {
        Write-Host ("   {0,-15} {1}" -f $name, (Read-EnvValue $name))
    }
}

if (Step 6 "Веса моделей распознавания и эмоций в $Root\models (~1 ГБ)") {
    if (-not (Test-Path $EnvFile)) { throw "Сначала шаг 5 (.env)" }
    Run $VPy @("$Root\transcriber\scripts\download_models.py")
}

if (Step 7 "Ollama $OllamaVersion, служба call-transcriber-ollama и LLM (~6.5 ГБ)") {
    if (-not (Test-Path $EnvFile)) { throw "Сначала шаг 5 (.env)" }
    $ollamaZip = "$Dl\ollama-$OllamaVersion-windows-amd64.zip"
    $marker = "$Root\ollama\VERSION.txt"
    if ((Test-Path $Ollama) -and (Test-Path $marker) -and (Get-Content $marker) -eq $OllamaVersion) {
        Write-Host "   Ollama уже установлена"
    } else {
        Download "https://github.com/ollama/ollama/releases/download/v$OllamaVersion/ollama-windows-amd64.zip" $ollamaZip
        Stop-Service call-transcriber-ollama -Force -ErrorAction SilentlyContinue
        Extract $ollamaZip "$Root\ollama"
        Set-Content $marker $OllamaVersion
    }
    Download "https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW.NET461.exe" "$Dl\WinSW.NET461.exe"
    $ollamaEnv = @{
        OLLAMA_HOST = "127.0.0.1:11434"; OLLAMA_MODELS = "$Root\ollama-models"
        # одна модель и один запрос за раз — меньше памяти
        OLLAMA_NUM_PARALLEL = "1"; OLLAMA_MAX_LOADED_MODELS = "1"
    }
    if ($Proxy) { $ollamaEnv += @{ HTTPS_PROXY = $Proxy; HTTP_PROXY = $Proxy; NO_PROXY = "localhost,127.0.0.1" } }
    Install-Service "call-transcriber-ollama" "Call Transcriber: Ollama (LLM)" `
        "LLM для саммари звонков (GigaChat через Ollama), только localhost" `
        $Ollama "serve" $ollamaEnv $null
    $version = Wait-Http "http://127.0.0.1:11434/api/version" 60 "Ollama"
    Write-Host "   Ollama отвечает, версия $($version.version)"
    $model = Read-EnvValue "LLM_MODEL"
    Write-Host "   скачиваю модель $model (~6.5 ГБ, при первой установке долго)"
    Run $Ollama @("pull", $model)
    & $Ollama list
}

if (Step 8 "Служба call-transcriber (API http://${ListenHost}:$Port)") {
    if (-not (Test-Path "$Svc\call-transcriber-ollama.exe")) { throw "Сначала шаг 7 (Ollama)" }
    $path = "$FfBin;" + [Environment]::GetEnvironmentVariable("Path", "Machine")
    Install-Service "call-transcriber" "Call Transcriber" `
        "Транскрибация звонков с АТС, эмоции и саммари; API http://${ListenHost}:$Port" `
        $VPy "-m uvicorn app.main:app --app-dir transcriber --host $ListenHost --port $Port" `
        @{ Path = $path; PYTHONUTF8 = "1"; PYTHONUNBUFFERED = "1" } "call-transcriber-ollama"
    Write-Host "   жду загрузки моделей распознавания (до 3 минут)..."
    $deadline = (Get-Date).AddMinutes(3)
    do {
        Start-Sleep 5
        try { $health = Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 5 } catch { $health = $null }
    } while ((-not $health -or $health.status -eq "starting") -and (Get-Date) -lt $deadline)

    if ($health -and $health.status -eq "ok") {
        Write-Host "`nГотово: сервис работает." -ForegroundColor Green
        Write-Host ("   распознавание: {0}, эмоции: {1}, ffmpeg: {2}, LLM: {3}, АТС: {4}" -f $health.asr.ready, $health.asr.emotion,
            $health.ffmpeg, $health.llm.model_available,
            $(if ($health.ats.enabled) { "следующая загрузка $($health.ats.next_fetch)" } else { "не настроена (ATS_URL в .env)" }))
        Write-Host "Дальше: заполнить ATS_* в .env и перезапустить: deploy\windows\ct.ps1 restart"
    } else {
        Write-Warning "Сервис не стал готов за 3 минуты: $(if ($health) { $health | ConvertTo-Json -Depth 4 } else { 'не отвечает' })"
        Write-Warning "Логи: $Root\logs\call-transcriber.err.log"
    }
}
