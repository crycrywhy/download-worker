"""Keep the SSH reverse tunnel alive: run start_tunnel.py, restart it whenever it exits.

Why this file exists (2026-09-14)
---------------------------------
The Worker task already ran worker_supervisor.py, but the Tunnel task ran
start_tunnel.py *directly*: when the ssh process exited (link dropped, Linux box
rebooted, PC slept, port taken), nothing brought it back - the task simply showed
"Ready" until the next logon, and the Linux side silently lost that outlet for
hours.  A dead tunnel is the one failure the Linux side **cannot** repair (only
Windows can open a reverse tunnel), so the tunnel now gets the same supervision
as the Worker.

Policy
------
* restart on exit (like worker_supervisor.py), with a backoff so a tunnel that
  dies immediately (bad config, port already taken, key refused) is retried at
  5s -> 10s -> 30s -> 60s instead of hammering the Linux sshd;
* an exit *after* a stable run (>= 60s) resets the backoff to 5s;
* pause flag: `download-worker off` creates <InstallDir>\\paused.flag.  While it
  exists the tunnel is deliberately left down and this supervisor keeps its hands
  off (it polls instead of restarting), so the manual switch always wins;
* every start / exit / restart decision is appended to
  logs\\tunnel_supervisor.log with a local timestamp.

Paths are derived from this file's own location, so the package runs from any
install directory; the interpreter is the one running this script (pythonw.exe
from the venv, exactly like the Task Scheduler action does).
"""
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
TUNNEL_SCRIPT = BASE_DIR / "start_tunnel.py"
PAUSE_FLAG = BASE_DIR / "paused.flag"
LOG_FILE = BASE_DIR / "logs" / "tunnel_supervisor.log"

RESTART_DELAYS = (5, 10, 30, 60)    # backoff ladder for fast-failing exits (seconds)
STABLE_RUN_SECONDS = 60             # a run this long counts as healthy: reset backoff
PAUSE_POLL_SECONDS = 30             # while paused, re-check the flag this often


def log(message):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as handle:
        handle.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}\n")


def main():
    log("tunnel supervisor started (restart on exit; backoff 5/10/30/60s)")
    delay_index = 0

    while True:
        if PAUSE_FLAG.exists():
            log(f"paused ({PAUSE_FLAG.name} present) - tunnel stays down; "
                f"re-checking in {PAUSE_POLL_SECONDS}s")
            time.sleep(PAUSE_POLL_SECONDS)
            continue

        started = time.time()
        log(f"starting tunnel: {sys.executable} {TUNNEL_SCRIPT}")

        try:
            process = subprocess.Popen([sys.executable, str(TUNNEL_SCRIPT)], cwd=str(BASE_DIR))
        except OSError as error:
            log(f"failed to start the tunnel: {error!r}")
            return_code = -1
        else:
            try:
                return_code = process.wait()
            except KeyboardInterrupt:
                log("supervisor interrupted - stopping the tunnel")
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                log("supervisor stopped")
                return

        uptime = time.time() - started

        if PAUSE_FLAG.exists():
            log(f"tunnel exited (code {return_code}, ran {uptime:.0f}s) - "
                f"paused by flag, not restarting")
            continue

        if uptime >= STABLE_RUN_SECONDS:
            delay_index = 0

        delay = RESTART_DELAYS[min(delay_index, len(RESTART_DELAYS) - 1)]
        delay_index += 1
        log(f"tunnel exited (code {return_code}, ran {uptime:.0f}s) - restarting in {delay}s")
        time.sleep(delay)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:      # never die silently: the log is the only witness
        log(f"supervisor crashed: {error!r}")
        raise
