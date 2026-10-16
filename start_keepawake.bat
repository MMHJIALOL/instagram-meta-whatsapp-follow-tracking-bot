@echo off
rem Keeps the laptop awake: presses Ctrl 4 times every 30 minutes, hidden in the background.
rem Stop it with stop_keepawake.bat. Running this twice is harmless (only one copy runs).
start "" powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "%~dp0keep_awake.ps1"
