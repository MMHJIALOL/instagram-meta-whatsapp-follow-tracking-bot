@echo off
rem Starts the members dashboard (http://127.0.0.1:8001) and the WhatsApp listener again after they were turned
rem off from the dashboard's Background apps card, puts them back in the Startup folder, and opens the page.
cd /d "%~dp0"
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
if exist "disabled_startup\Resin members dashboard.lnk" move /Y "disabled_startup\Resin members dashboard.lnk" "%STARTUP%\" >nul
start "" ".venv\Scripts\pythonw.exe" "members_keepalive.py"
powershell -NoProfile -Command "for($i=0;$i -lt 30;$i++){try{Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8001/ -TimeoutSec 2 | Out-Null; break}catch{Start-Sleep 1}}; Start-Process 'http://127.0.0.1:8001'"
