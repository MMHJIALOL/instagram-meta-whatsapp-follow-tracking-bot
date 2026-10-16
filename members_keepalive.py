"""Keeps the members dashboard (127.0.0.1:8001) and the WhatsApp listener running.
Started at logon from the Startup folder (no window), separately from keepalive.py so restarting this
never touches the bot or the Cloudflare tunnel.
"""
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = str(HERE / ".venv" / "Scripts" / "python.exe")
NODE = r"C:\Program Files\nodejs\node.exe"
NO_WINDOW = subprocess.CREATE_NO_WINDOW


def start_dashboard():
    log = open(HERE / "dashboard.log", "ab")
    return subprocess.Popen([PY, "-m", "uvicorn", "dashboard:app", "--host", "127.0.0.1", "--port", "8001",
                             "--no-access-log"], cwd=HERE, stdout=log, stderr=log, creationflags=NO_WINDOW)


def start_listener():
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
