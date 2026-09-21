"""Restart the Worker whenever it exits (same policy as the reference: 5 second delay).

Paths are derived from this file's own location, so the package runs from any
install directory; the interpreter is the one running this script (the venv).

Pause flag: `download-worker off` creates <InstallDir>\\paused.flag.
While it exists this supervisor does not start (or restart) the Worker, so the
manual switch always wins; `download-worker on` deletes the flag and the Worker
comes back on the next loop.  Every start/exit/decision is appended to
logs\\worker_supervisor.log so the switch's effect is visible after the fact.
"""
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
WORKER_SCRIPT = BASE_DIR / "start_worker.py"
PAUSE_FLAG = BASE_DIR / "paused.flag"
LOG_FILE = BASE_DIR / "logs" / "worker_supervisor.log"
PAUSE_POLL_SECONDS = 30


def log(message):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as handle:
        handle.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}\n")


def main():
    while True:
        if PAUSE_FLAG.exists():
            log(f"paused ({PAUSE_FLAG.name} present) - worker stays down; "
                f"re-checking in {PAUSE_POLL_SECONDS}s")
            time.sleep(PAUSE_POLL_SECONDS)
            continue

        log(f"starting worker: {sys.executable} {WORKER_SCRIPT}")

        try:
            process = subprocess.Popen(
                [sys.executable, str(WORKER_SCRIPT)],
                cwd=str(BASE_DIR),
            )
        except OSError as error:
            log(f"failed to start the worker: {error!r}")
            time.sleep(5)
            continue

        try:
            return_code = process.wait()

        except KeyboardInterrupt:
            log("supervisor stopping Worker...")

            process.terminate()

            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

            log("supervisor stopped")
            return

        if PAUSE_FLAG.exists():
            log(f"worker exited (code {return_code}) - paused by flag, not restarting")
            continue

        log(f"worker exited with code {return_code} - restarting in 5 seconds")

        time.sleep(5)


if __name__ == "__main__":
    main()
