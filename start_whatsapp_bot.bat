@echo off
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" "wa_dm\keepalive.py"
powershell -NoProfile -Command "for($i=0;$i -lt 40;$i++){try{Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8003/api/state -TimeoutSec 2 | Out-Null; break}catch{Start-Sleep -Seconds 1}}; Start-Process 'http://127.0.0.1:8003/'"
