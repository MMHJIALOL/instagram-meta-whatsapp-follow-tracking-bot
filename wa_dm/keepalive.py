"""Idempotent local launcher; one supervisor only, independent of existing dashboards."""
import ctypes
import os
import subprocess
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA = HERE / "data"
DATA.mkdir(exist_ok=True)
kernel = ctypes.windll.kernel32
kernel.CreateMutexW.restype = ctypes.c_void_p
mutex = kernel.CreateMutexW(None, False, "Local\\KalakritiWhatsAppDmBot8003")
if not mutex or kernel.GetLastError() == 183:
    raise SystemExit(0)
python = str(ROOT / ".venv" / "Scripts" / "python.exe")


def ours_running():
    try:
        with urllib.request.urlopen("http://127.0.0.1:8003/api/state", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


try:
    while True:
        if ours_running():
            time.sleep(10)
            continue
        with open(DATA / "dashboard.log", "ab") as log:
            server = subprocess.Popen([python,"-m","uvicorn","wa_dm.app:app","--host","127.0.0.1","--port","8003","--no-access-log"],
                cwd=ROOT, stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
            server.wait()
        time.sleep(5)
finally:
    kernel.CloseHandle(ctypes.c_void_p(mutex))
