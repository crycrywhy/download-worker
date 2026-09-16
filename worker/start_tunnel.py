"""Keep the SSH reverse tunnel open: Linux 127.0.0.1:<linux_tunnel_port> -> this Worker.

All connection parameters come from worker-config.json.  This script blocks for
as long as the tunnel lives; the Task Scheduler restarts it when it exits.
ExitOnForwardFailure makes port-forwarding problems fail fast instead of
pretending to be healthy.
"""
import subprocess
import sys
from datetime import datetime

from worker_config import INSTALL_DIR, load_config, resolve_worker_host

SSH_EXE = r"C:\Windows\System32\OpenSSH\ssh.exe"
LOG_FILE = INSTALL_DIR / "logs" / "tunnel.log"


def main():
    cfg = load_config()
    host = resolve_worker_host(cfg)
    linux_user = str(cfg.get("linux_user") or "").strip()
    linux_host = str(cfg.get("linux_host") or "").strip()

    missing = [
        name for name, value in
        (("worker_host", host), ("linux_user", linux_user), ("linux_host", linux_host))
        if not value
    ]
    if missing:
        print(
            "ERROR: tunnel configuration incomplete: " + ", ".join(missing) +
            ". Run install-worker.ps1 first.",
            flush=True,
        )
        return 1

    forward = f"{int(cfg['linux_tunnel_port'])}:{host}:{int(cfg['worker_port'])}"
    ssh = [
        SSH_EXE,
        "-N",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "BatchMode=yes",
        # TCP-level keepalive on top of the SSH-level one above: cheap, supported by
        # every port, and it lets the OS notice a peer that vanished (sleep / cable out)
        # instead of holding a half-dead connection open.
        "-o", "TCPKeepAlive=yes",
        "-R", forward,
    ]

    ssh.append(f"{linux_user}@{linux_host}")

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    with open(LOG_FILE, "a", encoding="utf-8") as log:
        log.write(
            f"\n===== [{datetime.now():%Y-%m-%d %H:%M:%S}] "
            f"ssh -N -R {forward} {linux_user}@{linux_host}\n"
        )
        log.flush()

        result = subprocess.run(
            ssh,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
