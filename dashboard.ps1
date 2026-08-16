# Launcher for the PSB dashboard (local). Run from the repo root:
#   powershell -ExecutionPolicy Bypass -File dashboard.ps1
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
if (-not $env:DASHBOARD_PORT) { $env:DASHBOARD_PORT = '3737' }

python dashboard\server.py
