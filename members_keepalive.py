"""Keeps the members dashboard (127.0.0.1:8001) and the WhatsApp listener running.
Started at logon from the Startup folder (no window), separately from keepalive.py so restarting this
never touches the bot or the Cloudflare tunnel.
"""
import subprocess
import ctypes
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = str(HERE / ".venv" / "Scripts" / "python.exe")
NODE = r"C:\Program Files\nodejs\node.exe"
NO_WINDOW = subprocess.CREATE_NO_WINDOW

# Repeated Startup/manual launches must never compete for the saved WA profile.
kernel = ctypes.windll.kernel32
kernel.CreateMutexW.restype = ctypes.c_void_p
mutex = kernel.CreateMutexW(None, False, "Local\\ResinMembersDashboard8001")
if not mutex or kernel.GetLastError() == 183:
    raise SystemExit(0)


def start_dashboard():
    log = open(HERE / "dashboard.log", "ab")
    return subprocess.Popen([PY, "-m", "uvicorn", "dashboard:app", "--host", "127.0.0.1", "--port", "8001",
                             "--no-access-log"], cwd=HERE, stdout=log, stderr=log, creationflags=NO_WINDOW)


def start_listener():
    # A crashed Node process can leave its dedicated headless browser behind.
    # Match only this listener's exact profile; personal browsers are unaffected.
    profile = str(HERE / "wa" / ".wwebjs_auth" / "session").replace("'", "''")
    script = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'msedge.exe' "
              "-and $_.CommandLine.Contains('" + profile + "') } | "
              "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    subprocess.run(["powershell", "-NoProfile", "-Command", script], creationflags=NO_WINDOW,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    log = open(HERE / "wa" / "listener.log", "ab")
    return subprocess.Popen([NODE, "listener.js"], cwd=HERE / "wa", stdout=log, stderr=log, creationflags=NO_WINDOW)


dashboard = start_dashboard()
time.sleep(3)  # let the dashboard come up before the listener reports to it
listener = start_listener()
while True:
    time.sleep(10)
    if dashboard.poll() is not None:
        dashboard = start_dashboard()
    if listener.poll() is not None:
        time.sleep(20)  # don't hammer WhatsApp if it keeps failing
        listener = start_listener()
