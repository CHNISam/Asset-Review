@echo off
setlocal EnableExtensions

set "AV_ROOT=%~dp0"
set "ASSET_VIEWER_HOME=%AV_ROOT%state"
set "ASSET_VIEWER_CACHE=%AV_ROOT%cache"
set "ASSET_VIEWER_EXE=%AV_ROOT%.venv\Scripts\asset-viewer.exe"

if not exist "%ASSET_VIEWER_EXE%" (
  echo Asset Viewer is not installed in this checkout.
  echo Run the Windows setup steps in README.md first.
  pause
  exit /b 1
)

if not exist "%ASSET_VIEWER_HOME%" mkdir "%ASSET_VIEWER_HOME%"
if not exist "%ASSET_VIEWER_CACHE%" mkdir "%ASSET_VIEWER_CACHE%"

rem Start only one localhost-only review service for this checkout.
powershell -NoProfile -Command "$listener = Get-NetTCPConnection -LocalPort 8160 -State Listen -ErrorAction SilentlyContinue; if (-not $listener) { Start-Process -FilePath $env:ASSET_VIEWER_EXE -ArgumentList @('serve','--host','127.0.0.1','--port','8160') -WorkingDirectory $env:AV_ROOT -WindowStyle Hidden -RedirectStandardOutput (Join-Path $env:ASSET_VIEWER_HOME 'server.log') -RedirectStandardError (Join-Path $env:ASSET_VIEWER_HOME 'server-error.log') }"
if errorlevel 1 goto :failed

powershell -NoProfile -Command "$deadline = (Get-Date).AddSeconds(15); do { try { $response = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8160/' -TimeoutSec 2; if ($response.StatusCode -eq 200) { exit 0 } } catch {}; Start-Sleep -Milliseconds 250 } while ((Get-Date) -lt $deadline); exit 1"
if errorlevel 1 goto :failed

start "" "http://127.0.0.1:8160/"
exit /b 0

:failed
echo Asset Viewer did not become available at http://127.0.0.1:8160/.
echo Check "%ASSET_VIEWER_HOME%\server.log" and "server-error.log".
pause
exit /b 1
