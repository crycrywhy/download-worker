"""Keep the SSH reverse tunnel alive: run start_tunnel.py, restart it whenever it exits.

Why this file exists
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
  5s -> 10s -> 15s -> 20s -> 30s instead of hammering the Linux sshd;
* **backoff by cause** (see below): "the Linux side still holds
  the port" is a *waiting* condition - retrying it every 30s burns TCP
  round-trips for the whole time the port is held, which on this line has been
  measured at up to 68 minutes in one stretch, and floods the log with hundreds
  of identical lines.  That case now uses a longer ladder and stops repeating the
  same reason line after the first few attempts.  Every other failure (reset,
  timeout) keeps the short ladder, because those follow a tunnel that *was*
  working and usually recover immediately;
* **active health watchdog**: the parent used to call
  process.wait() with no timeout, so a child that stayed alive while the forward
  was already dead kept the supervisor silent for as long as the child lived.
  Measured on this line: one session logged "starting tunnel", then nothing at
  all for 68 minutes, and only afterwards "tunnel exited ... ran 4103s" - during
  that window nothing was watching and nothing would have restarted the tunnel.
  A watchdog thread now probes the Worker every HEALTH_INTERVAL_SECONDS and kills
  the child after HEALTH_FAILS_BEFORE_KILL consecutive failures, so a zombie
  session is replaced in ~1-2 minutes instead of waiting for the child to notice;
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
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from worker_config import load_config, resolve_worker_host

BASE_DIR = Path(__file__).resolve().parent
TUNNEL_SCRIPT = BASE_DIR / "start_tunnel.py"
PAUSE_FLAG = BASE_DIR / "paused.flag"
LOG_FILE = BASE_DIR / "logs" / "tunnel_supervisor.log"
TUNNEL_LOG = BASE_DIR / "logs" / "tunnel.log"

RESTART_DELAYS = (5, 10, 15, 20, 30)   # ordinary failures (follow a tunnel that was working)
# "Linux side still holds the port": this is a WAITING state, so do not hammer -- but do not
# oversleep either.  Measured: the port is held by a *userspace* leak on the Linux
# side (sshd never close()s its end of the accepted socket -- `ss` shows CLOSE-WAIT with the
# listener still up), so it is NOT released by a kernel timeout we can predict.  The old
# ladder topped out at 240 s, which means that after the port finally frees, this side can sit
# idle for up to four more minutes.  With the port-held state lasting minutes-to-hours, a flat
# 60 s ceiling keeps roughly a minute of dead time while still not hammering:
PORT_HELD_DELAYS = (10, 30, 45, 60)   # caps at 60 s instead of 240 s
PORT_HELD_LOG_EVERY = 3                # only every Nth identical port-held line reaches the log
STABLE_RUN_SECONDS = 60                # a run this long counts as healthy: reset backoff
PAUSE_POLL_SECONDS = 30                # while paused, re-check the flag this often
IMMEDIATE_EXIT_SECONDS = 3             # under this, ssh never got a working session

# --- health watchdog -------------------------------------------------------
# Probe the Worker on its own Tailscale address: if the forward is dead this is
# the same request the Linux side makes, so it is a true end-to-end check of the
# thing we are supervising (not merely "is the ssh process still alive").
def _health_target():
    """The Worker's own reachable address, from worker-config.json.

    The watchdog must probe the same address the Linux side probes (the Worker
    binds only its own private-network address), so it is resolved from config
    rather than hard-coded -- every install reads its own address.  If config
    cannot be read the watchdog is disabled: an unknown address must never be
    mistaken for a dead Worker and get a healthy tunnel killed.
    """
    try:
        cfg = load_config()
        host = resolve_worker_host(cfg)
        port = int(cfg.get("worker_port") or 0)
        if host and port:
            return str(host), port
    except Exception:
        pass
    return None, 0


HEALTH_HOST, HEALTH_PORT = _health_target()
HEALTH_INTERVAL_SECONDS = 20
HEALTH_TIMEOUT_SECONDS = 5
HEALTH_FAILS_BEFORE_KILL = 3           # 3 x 20s = ~60s of continuous failure before killing

_stop_watchdog = threading.Event()
_kill_reason = {"text": ""}


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


def worker_alive():
    """One plain-TCP /health probe against the local Worker.  Standard library only.

    Written by hand rather than with urllib: no system proxy must ever sit between
    this check and the Worker, and a raw socket keeps the timeout honest.
    """
    if not HEALTH_HOST:                 # no address to probe: never report the Worker dead
        return True
    try:
        with socket.create_connection((HEALTH_HOST, HEALTH_PORT), HEALTH_TIMEOUT_SECONDS) as sock:
            sock.settimeout(HEALTH_TIMEOUT_SECONDS)
            sock.sendall(
                f"GET /health HTTP/1.0\r\nHost: {HEALTH_HOST}\r\nConnection: close\r\n\r\n".encode("ascii")
            )
            data = b""
            while len(data) < 4096:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        return b" 200 " in data.split(b"\r\n", 1)[0] and b'"status":"ok"' in data
    except Exception:
        return False


def watchdog(process_holder):
    """Kill a zombie tunnel: child alive but the forward no longer answering.

    Runs in its own thread so the parent's blocking wait() can be interrupted by
    terminating the child (which makes wait() return).
    """
    consecutive = 0
    while not _stop_watchdog.is_set():
        _stop_watchdog.wait(HEALTH_INTERVAL_SECONDS)
        if _stop_watchdog.is_set():
            return
        process = process_holder.get("process")
        if process is None or process.poll() is not None:
            consecutive = 0
            continue
        if worker_alive():
            consecutive = 0
            continue
        consecutive += 1
        if consecutive >= HEALTH_FAILS_BEFORE_KILL:
            _kill_reason["text"] = (
                f"health watchdog: /health unresponsive {consecutive} times "
                f"(~{consecutive * HEALTH_INTERVAL_SECONDS}s) - killing the tunnel to force a rebind"
            )
            log(_kill_reason["text"])
            try:
                process.terminate()
            except Exception as error:
                log(f"health watchdog: terminate failed: {error!r}")
            consecutive = 0


def main():
    log("tunnel supervisor started (restart on exit; backoff "
        + "/".join(str(d) for d in RESTART_DELAYS)
        + "s; port-held backoff "
        + "/".join(str(d) for d in PORT_HELD_DELAYS)
        + "s; health watchdog every "
        + f"{HEALTH_INTERVAL_SECONDS}s)")
    delay_index = 0
    port_held_repeats = 0
    process_holder = {"process": None}

    watcher = threading.Thread(target=watchdog, args=(process_holder,), daemon=True)
    watcher.start()

    while True:
        if PAUSE_FLAG.exists():
            log(f"paused ({PAUSE_FLAG.name} present) - tunnel stays down; "
                f"re-checking in {PAUSE_POLL_SECONDS}s")
            time.sleep(PAUSE_POLL_SECONDS)
            continue

        started = time.time()
        log(f"starting tunnel: {sys.executable} {TUNNEL_SCRIPT}")
        _kill_reason["text"] = ""

        try:
            process = subprocess.Popen([sys.executable, str(TUNNEL_SCRIPT)], cwd=str(BASE_DIR))
            process_holder["process"] = process
        except OSError as error:
            log(f"failed to start the tunnel: {error!r}")
            process = None
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
            finally:
                process_holder["process"] = None

        uptime = time.time() - started

        if PAUSE_FLAG.exists():
            log(f"tunnel exited (code {return_code}, ran {uptime:.0f}s) - "
                f"paused by flag, not restarting")
            continue

        if uptime >= STABLE_RUN_SECONDS:
            delay_index = 0
            port_held_repeats = 0

        detail = ""
        port_held = False
        if uptime < STABLE_RUN_SECONDS:
            reason = last_tunnel_line()
            if reason:
                detail = f" | ssh: {reason}"
            # The Linux side refusing to bind our port is a *waiting* condition, not a
            # fault of ours: it frees itself and the next attempt succeeds.  Classify it
            # so the retry ladder and the log volume both match reality.
            port_held = "remote port forwarding failed" in reason
            if uptime < IMMEDIATE_EXIT_SECONDS and not port_held:
                detail += (" | (exited at once: usually the Linux side still holds the port "
                           "- it frees itself, then a later attempt binds)")

        if port_held:
            port_held_delay = PORT_HELD_DELAYS[min(delay_index, len(PORT_HELD_DELAYS) - 1)]
            port_held_repeats += 1
            delay_index += 1
            # Repeat the full reason only occasionally; keep the tick visible so the log
            # still proves the supervisor is alive and retrying.
            if port_held_repeats <= 2 or port_held_repeats % PORT_HELD_LOG_EVERY == 0:
                log(f"tunnel exited (code {return_code}, ran {uptime:.0f}s) - "
                    f"port held by the Linux side, retrying in {port_held_delay}s "
                    f"(attempt {port_held_repeats}){detail}")
            else:
                log(f"port still held, retrying in {port_held_delay}s (attempt {port_held_repeats})")
            time.sleep(port_held_delay)
            continue

        delay = RESTART_DELAYS[min(delay_index, len(RESTART_DELAYS) - 1)]
        delay_index += 1

        if _kill_reason["text"]:
            detail += f" | {_kill_reason['text']}"

        log(f"tunnel exited (code {return_code}, ran {uptime:.0f}s) - "
            f"restarting in {delay}s{detail}")
        time.sleep(delay)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:      # never die silently: the log is the only witness
        log(f"supervisor crashed: {error!r}")
        raise
