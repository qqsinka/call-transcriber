<#
Управление сервисом транскрибации на Windows (PowerShell от имени администратора — .env читают только админы).

  deploy\windows\ct.ps1 status                                состояние: модели, LLM, очередь, АТС
  deploy\windows\ct.ps1 logs [-Lines 40] [-Follow]            лог сервиса (-Follow — следить, Ctrl+C — выход)
  deploy\windows\ct.ps1 restart | stop | start                службы (после правки .env — restart)
  deploy\windows\ct.ps1 fetch -Date 2026-10-26 -Limit 2 [-Dry]  скачать записи с АТС сейчас (-Dry — только список)
  deploy\windows\ct.ps1 job <call_id>                         результат звонка (call_id — имя файла без .mp3)
  deploy\windows\ct.ps1 list [-Date 2026-10-28]               звонки за день: время, номер, приоритет, тон, тема
  deploy\windows\ct.ps1 export [-Date 2026-10-28]             выгрузить звонки за день в txt (папка export\<дата>)
#>
param(
    [Parameter(Position = 0)]
    [ValidateSet("status", "logs", "restart", "stop", "start", "fetch", "job", "list", "export")]
    [string]$Command = "status",
    [Parameter(Position = 1)][string]$Id,
    [string]$Date,
    [int]$Days = 1,
    [int]$Limit = 0,
    [switch]$Dry,
    [int]$Lines = 40,
    [switch]$Follow
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.WebRequest]::DefaultWebProxy = New-Object Net.WebProxy
$Root = (Resolve-Path "$PSScriptRoot\..\..").Path
$Utf8 = New-Object Text.UTF8Encoding($false)

function Read-EnvValue($name) {
    foreach ($line in [IO.File]::ReadAllLines("$Root\.env", $Utf8)) {
        if ($line -match "^$name=(.*)$") { return $Matches[1].Trim() }
    }
    return ""
}

# порт — из настроек службы (install.ps1 -Port)
$port = 8000
$svcXml = "$Root\services\call-transcriber.xml"
if ((Test-Path $svcXml) -and ((Get-Content $svcXml -Raw) -match '--port (\d+)')) { $port = [int]$Matches[1] }
$Base = "http://127.0.0.1:$port"

# Invoke-RestMethod в PowerShell 5.1 портит кириллицу (ответ без charset читает как Latin-1) — декодируем сами
function Api($method, $path, $timeout = 30) {
    $headers = @{}
    $user = Read-EnvValue "AUTH_USER"
    if ($user) {
        $pair = "${user}:$(Read-EnvValue 'AUTH_PASSWORD')"
        $headers.Authorization = "Basic " + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($pair))
    }
    try {
        $resp = Invoke-WebRequest "$Base$path" -Method $method -Headers $headers -UseBasicParsing -TimeoutSec $timeout
        return [Text.Encoding]::UTF8.GetString($resp.RawContentStream.ToArray()) | ConvertFrom-Json
    } catch [Net.WebException] {
        $r = $_.Exception.Response
        if (-not $r) { throw "Сервис не отвечает на $Base — служба запущена? (ct.ps1 status / logs)" }
        $body = (New-Object IO.StreamReader($r.GetResponseStream(), [Text.Encoding]::UTF8)).ReadToEnd()
        throw "HTTP $([int]$r.StatusCode): $body"
    }
}

switch ($Command) {
    "status" {
        Get-Service call-transcriber-ollama, call-transcriber -ErrorAction SilentlyContinue |
            Format-Table Name, Status -AutoSize
        Api GET "/health" | ConvertTo-Json -Depth 6
    }
    "logs" {
        $log = "$Root\logs\call-transcriber.err.log"
        if ($Follow) { Get-Content $log -Tail $Lines -Encoding UTF8 -Wait } else { Get-Content $log -Tail $Lines -Encoding UTF8 }
    }
    { $_ -in "restart", "stop", "start" } {
        if ($Command -ne "start") { Stop-Service call-transcriber, call-transcriber-ollama -Force }
        if ($Command -ne "stop") { Start-Service call-transcriber-ollama, call-transcriber }
        Get-Service call-transcriber-ollama, call-transcriber | Format-Table Name, Status -AutoSize
    }
    "fetch" {
        $query = "days=$Days"
        if ($Date) { $query += "&day=$Date" }
        if ($Limit -gt 0) { $query += "&limit=$Limit" }
        if ($Dry) { $query += "&dry_run=true" }
        $r = Api POST "/ats/fetch?$query" 900
        $r.reports
        if ($r.files) { $(if ($Dry) { "К скачиванию:" } else { "Скачано:" }); $r.files | ForEach-Object { "  $_" } }
    }
    { $_ -in "list", "export" } {
        # читает базу напрямую, без сервиса; вывод Python — в UTF-8, иначе кириллица ломается в конвейере
        $env:PYTHONIOENCODING = "utf-8"
        [Console]::OutputEncoding = [Text.Encoding]::UTF8
        Push-Location $Root
        try {
            & "$Root\venv\Scripts\python.exe" "$Root\transcriber\scripts\report.py" $Command --date $(if ($Date) { $Date } else { "today" })
        } finally { Pop-Location }
    }
    "job" {
        if (-not $Id) { throw "Укажите call_id: ct.ps1 job 26-10-26_09-02-47_..." }
        $j = Api GET "/jobs/$([Uri]::EscapeDataString($Id))"
        "Статус:     $($j.status)$(if ($j.error) { " — $($j.error)" })"
        "Звонок:     $($j.call_date)  номер $($j.phone)  линия $($j.ext)  $([math]::Round([double]$j.duration_s)) с"
        "Обработка:  $($j.processing_s) с, фраз $(@($j.phrases).Count)"
        if ($j.summary) {
            "Тема:       $($j.summary.topic)"
            "Приоритет:  $($j.summary.priority)   Тон: $($j.summary.tone)   Негатив: $($j.summary.negative_score)/10"
            if ($j.summary.risks) { "Риски:      $($j.summary.risks -join '; ')" }
            ""
            $j.summary.summary
        } elseif ($j.warning) { "Саммари нет: $($j.warning)" }
    }
}
