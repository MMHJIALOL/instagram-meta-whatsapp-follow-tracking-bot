@echo off
rem Stops the keep-awake Ctrl presser.
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" | Where-Object { $_.CommandLine -like '*keep_awake.ps1*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo Keep-awake stopped.
