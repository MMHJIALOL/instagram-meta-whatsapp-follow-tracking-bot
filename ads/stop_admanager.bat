@echo off
rem Stops the ad manager and its keepalive (only those; the bot and members dashboard are left alone).
rem It starts again at the next logon, or with start_admanager.bat.
powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe' or Name='cmd.exe'\" | Where-Object { $_.CommandLine -like '*admanager_keepalive*' -or $_.CommandLine -like '*admanager:app*' }; if ($p) { $p | Sort-Object { $_.CommandLine -notlike '*keepalive*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; 'Ad manager stopped.' } else { 'Ad manager was not running.' }"
pause
