"""Sync this machine's logs to the Linux host (same incremental logic as the reference).

Log directory, SSH target and remote directory all come from worker-config.json,
so no Linux username / IP / path is baked into the package.

Which files go over: worker.log (+ its rotations) plus the two supervisor
logs and the tunnel log.  The tunnel / supervisor logs are the ones that answer
"why did the outlet die overnight" without a human having to log into the PC - exactly
the question every outage kept raising.
"""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from worker_config import INSTALL_DIR, load_config

LOG_DIR = INSTALL_DIR / "logs"
STATE_FILE = LOG_DIR / "worker.log.sync-state"

MAX_HISTORY = 5

# append-only logs that are shipped incrementally (offset recorded in the state file)
STREAM_LOGS = (
    "worker.log",
    "tunnel.log",
    "worker_supervisor.log",
    "tunnel_supervisor.log",
)

SSH_TARGET = ""
REMOTE_DIR = ""


def sha256_file(path):
    h = hashlib.sha256()

    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)

            if not chunk:
                break

            h.update(chunk)

    return h.hexdigest()


def load_state():
    if not STATE_FILE.exists():
        return {}

    try:
        return json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        )
    except Exception:
        return {}


def save_state(state):
    temp = STATE_FILE.with_suffix(".tmp")

    temp.write_text(
        json.dumps(
            state,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    temp.replace(STATE_FILE)


def ssh_run(command, data=None):
    ssh = [
        r"C:\Windows\System32\OpenSSH\ssh.exe",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=15",
        SSH_TARGET,
        command,
    ]

    result = subprocess.run(
        ssh,
        input=data,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    return result.returncode == 0


def append_remote(filename, data):
    remote_file = f"{REMOTE_DIR}/{filename}"

    return ssh_run(
        f"cat >> '{remote_file}'",
        data,
    )


def replace_remote(filename, data):
    remote_file = f"{REMOTE_DIR}/{filename}"

    return ssh_run(
        f"cat > '{remote_file}'",
        data,
    )


def delete_remote(filename):
    remote_file = f"{REMOTE_DIR}/{filename}"

    return ssh_run(
        f"rm -f '{remote_file}'",
    )


def ensure_remote_dir():
    """Create the remote directory when it is missing.

    Without this every sync fails silently if the Linux-side directory has not been
    created yet (or was moved by a re-organisation) - the state that shipped earlier:
    the task ran, `cat >>` failed, and nothing ever arrived.
    """
    return ssh_run(f"mkdir -p '{REMOTE_DIR}'")


def sync_current_log(state, name):
    path = LOG_DIR / name

    if not path.exists():
        return True

    size = path.stat().st_size

    old_state = state.get(name, {})
    old_size = old_state.get("size", 0)

    # log 被截断或重新创建
    if old_size > size:
        old_size = 0

    if old_size < size:
        with open(path, "rb") as f:
            f.seek(old_size)
            data = f.read()

        if data:
            if not append_remote(name, data):
                return False

    state[name] = {
        "size": size,
    }

    return True


def sync_rotated_logs(state):
    for i in range(1, MAX_HISTORY + 1):
        filename = f"worker.log.{i}"
        path = LOG_DIR / filename

        if not path.exists():
            if filename in state:
                if not delete_remote(filename):
                    return False

                state.pop(filename)

            continue

        digest = sha256_file(path)

        old_state = state.get(filename, {})
        old_digest = old_state.get("sha256")

        if digest != old_digest:
            with open(path, "rb") as f:
                data = f.read()

            if not replace_remote(filename, data):
                return False

            state[filename] = {
                "size": len(data),
                "sha256": digest,
            }

    return True


def main():
    global SSH_TARGET, REMOTE_DIR

    cfg = load_config()
    sync_cfg = cfg["log_sync"]
    linux_user = str(cfg.get("linux_user") or "").strip()
    linux_host = str(cfg.get("linux_host") or "").strip()
    remote_dir = str(sync_cfg.get("remote_dir") or "").strip()

    if not sync_cfg.get("enabled", True):
        print("Log sync is disabled in worker-config.json.", flush=True)
        return 0

    if not (linux_user and linux_host and remote_dir):
        print(
            "ERROR: log sync configuration incomplete "
            "(linux_user / linux_host / log_sync.remote_dir).",
            flush=True,
        )
        return 1

    SSH_TARGET = f"{linux_user}@{linux_host}"
    REMOTE_DIR = remote_dir

    state = load_state()

    # 远端目录可能还没建（或换代后搬走了）——先确保它在
    if not ensure_remote_dir():
        return 1

    # 先同步 worker.log 的历史轮转文件
    if not sync_rotated_logs(state):
        return 1

    # 再增量同步各追加型日志（worker / tunnel / 两个 supervisor）
    for name in STREAM_LOGS:
        if not sync_current_log(state, name):
            return 1

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
