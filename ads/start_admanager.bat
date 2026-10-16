@echo off
rem Starts the ad manager on http://127.0.0.1:8002 (this computer only) and opens it.
rem It runs in the background through admanager_keepalive.py, which brings it back if it ever stops.
rem It also starts by itself at logon (Startup folder). Use stop_admanager.bat to stop it.
cd /d "%~dp0.."
start "" ".venv\Scripts\pythonw.exe" "ads\admanager_keepalive.py"
powershell -NoProfile -Command "for($i=0;$i -lt 30;$i++){try{Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8002/health -TimeoutSec 2 | Out-Null; break}catch{Start-Sleep 1}}; Start-Process 'http://127.0.0.1:8002'"
