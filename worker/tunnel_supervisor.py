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
  5s -> 10s -> 15s -> 20s -> 30s instead of hammering the Linux sshd.  The
  ceiling was 60s until 2026-09-18: a failed attempt costs one TCP round-trip,
  so waiting a full minute only adds dead time after the Linux side has let go
  of the port (the usual reason a rebind fails - it frees itself, and the next
  attempt can then succeed);
* on a short-lived exit it copies the last line of tunnel.log into its own log,
  so `tunnel_supervisor.log` alone says *why* the tunnel dropped (timeout vs
  port already in use vs connection reset) instead of just "exit 255";
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
TUNNEL_LOG = BASE_DIR / "logs" / "tunnel.log"

RESTART_DELAYS = (5, 10, 15, 20, 30)  # backoff ladder for fast-failing exits (seconds)
STABLE_RUN_SECONDS = 60             # a run this long counts as healthy: reset backoff
PAUSE_POLL_SECONDS = 30             # while paused, re-check the flag this often
IMMEDIATE_EXIT_SECONDS = 3          # under this, ssh never got a working session


def log(message):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as handle:
        handle.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}\n")


def last_tunnel_line(window=8192):
    """Last non-empty line of tunnel.log, or "" if it cannot be read.

    Only the tail of the file is read: tunnel.log is rotated at 10 MB x5, and this
    runs on every restart.
    """
    try:
        size = TUNNEL_LOG.stat().st_size
        with open(TUNNEL_LOG, "r", encoding="utf-8", errors="replace") as handle:
            if size > window:
                handle.seek(size - window)
                handle.readline()               # discard the partial first line
            lines = [line.strip() for line in handle if line.strip()]
    except OSError:
        return ""
    return lines[-1][:300] if lines else ""


def main():
    log("tunnel supervisor started (restart on exit; backoff "
        + "/".join(str(d) for d in RESTART_DELAYS) + "s)")
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

        detail = ""
        if uptime < STABLE_RUN_SECONDS:
            reason = last_tunnel_line()
            if reason:
                detail = f" | ssh: {reason}"
            if uptime < IMMEDIATE_EXIT_SECONDS:
                detail += (" | (exited at once: usually the Linux side still holds the port "
                           "- it frees itself, then a later attempt binds)")

        log(f"tunnel exited (code {return_code}, ran {uptime:.0f}s) - "
            f"restarting in {delay}s{detail}")
        time.sleep(delay)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:      # never die silently: the log is the only witness
        log(f"supervisor crashed: {error!r}")
        raise
