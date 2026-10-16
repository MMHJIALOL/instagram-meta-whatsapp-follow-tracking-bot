"""Keeps the bot and the tunnel running. Started at logon from the Startup folder (no window).

If the tunnel restarts, its address changes: the new one is written to webhook_url.txt and shown
in a popup, because Meta's webhook Callback URL must then be updated by hand.
"""
import ctypes
import re
import subprocess
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = str(HERE / ".venv" / "Scripts" / "python.exe")
CLOUDFLARED = r"C:\Program Files (x86)\cloudflared\cloudflared.exe"
URL_FILE = HERE / "webhook_url.txt"
NO_WINDOW = subprocess.CREATE_NO_WINDOW


def start_bot():
    log = open(HERE / "uvicorn.log", "ab")
    return subprocess.Popen([PY, "-m", "uvicorn", "main:app", "--port", "8000", "--no-access-log"],
                            cwd=HERE, stdout=log, stderr=log, creationflags=NO_WINDOW)


def popup(text):
    threading.Thread(target=ctypes.windll.user32.MessageBoxW, args=(0, text, "Resin workshop bot", 0x40000),
                     daemon=True).start()


def watch_tunnel_output(proc):
    """Copy cloudflared output to tunnel.log and report the tunnel address when it appears."""
    with open(HERE / "tunnel.log", "a", encoding="utf-8") as log:
        for line in proc.stderr:
            log.write(line)
            log.flush()
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
            if m:
                url = m.group(0) + "/webhook"
                old = URL_FILE.read_text().strip() if URL_FILE.exists() else ""
                URL_FILE.write_text(url)
                if url != old:
                    popup(f"The bot's tunnel address changed, so Instagram DMs won't reach it until Meta has the "
                          f"new one.\n\nOpen http://127.0.0.1:8001 (Instagram bot card): copy the Callback URL and "
                          f"Verify token, paste both in the Meta app (Instagram > Configure webhooks), then Verify "
                          f"and save.\n\n{url}")


def start_tunnel():
    proc = subprocess.Popen([CLOUDFLARED, "tunnel", "--url", "http://127.0.0.1:8000"], cwd=HERE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                            creationflags=NO_WINDOW)
    threading.Thread(target=watch_tunnel_output, args=(proc,), daemon=True).start()
    return proc


bot, tunnel = start_bot(), start_tunnel()
while True:
    time.sleep(10)
    if bot.poll() is not None:
        bot = start_bot()
    if tunnel.poll() is not None:
        tunnel = start_tunnel()
