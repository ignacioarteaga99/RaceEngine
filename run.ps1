# RaceEngine V2 — arranque en Windows (PowerShell)
#   Uso:  .\run.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# Busca un Python 3.12+ entre los lanzadores habituales de Windows
$py = $null
foreach ($cand in @(
    @("py", @("-3.13")), @("py", @("-3.12")),
    @("python", @()), @("python3", @())
)) {
    $exe = $cand[0]; $pre = $cand[1]
    try {
        $v = & $exe @pre -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and [version]$v -ge [version]"3.12") {
            $py = @($exe) + $pre; break
        }
    } catch { }
}

if (-not $py) {
    Write-Host "──────────────────────────────────────────────────────" -ForegroundColor Red
    Write-Host "  No se encontró Python 3.12 o superior." -ForegroundColor Red
    Write-Host ""
    Write-Host "  Descárgalo de https://www.python.org/downloads/"
    Write-Host "  IMPORTANTE: marca 'Add python.exe to PATH' al instalar."
    Write-Host "──────────────────────────────────────────────────────" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path ".venv")) {
    Write-Host "> creando entorno virtual..." -ForegroundColor Cyan
    & $py[0] @($py[1..($py.Length-1)]) -m venv .venv
    & ".\.venv\Scripts\python.exe" -m pip install -q --upgrade pip
    & ".\.venv\Scripts\python.exe" -m pip install -q -r requirements.txt
}

if (-not (Test-Path "web\vendor\leaflet.js")) {
    Write-Host "> descargando Leaflet..." -ForegroundColor Cyan
    try {
        New-Item -ItemType Directory -Force -Path "web\vendor" | Out-Null
        Invoke-WebRequest "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"  -OutFile "web\vendor\leaflet.js"
        Invoke-WebRequest "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" -OutFile "web\vendor\leaflet.css"
    } catch { Write-Host "  (sin red: se usará el CDN al abrir la app)" -ForegroundColor DarkGray }
}

& ".\.venv\Scripts\python.exe" serve.py @args
