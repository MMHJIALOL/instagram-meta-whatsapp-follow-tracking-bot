"""Keeps the ad manager (http://127.0.0.1:8002) running. Started at logon from the Startup folder (no window).

Every few seconds it asks /health. If nothing answers twice in a row, it starts the ad manager again. If something
else already answers on 8002 (e.g. a server another tool started), it leaves it alone. Only one copy of this runs at
a time. stop_admanager.bat stops this too.
"""
import ctypes
import subprocess
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
URL = "http://127.0.0.1:8002/health"


def healthy():
    try:
        with urllib.request.urlopen(URL, timeout=4) as r:
            return r.status == 200
    except OSError:
        return False


def start():
    log = open(HERE / "server.log", "ab")
    return subprocess.Popen([PY, "-m", "uvicorn", "admanager:app", "--app-dir", str(HERE), "--host", "127.0.0.1",
                             "--port", "8002", "--no-access-log"], cwd=ROOT, stdout=log, stderr=log,
                            creationflags=subprocess.CREATE_NO_WINDOW)


def note(text):
    with open(HERE / "keepalive.log", "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")


def main():
    # one copy only: a second start (Startup + start_admanager.bat) just exits
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\resin-admanager-keepalive")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return
    note("keepalive started")
    proc, misses = None, 0
    while True:
        if healthy():
            misses = 0
        else:
            misses += 1
            if misses >= 2:
                if proc and proc.poll() is None:
                    proc.kill()  # ours, but stuck
                proc = start()
                note(f"ad manager wasn't answering, started it again (pid {proc.pid})")
                misses = 0
                time.sleep(10)  # give it time to come up
        time.sleep(5)


if __name__ == "__main__":
    main()
