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
        # Dead link is detected in ~45s (3 x 15s).  The probes are suppressed while
        # data is flowing, so a shorter interval costs nothing during a download;
        # it only bounds how long an *idle* tunnel can sit on a link that is gone.
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=3",
        # Bound the TCP connect too: without this a black-holed route leaves ssh
        # hanging in connect() and the supervisor sees a "running" tunnel that is
        # not coming back.
        "-o", "ConnectTimeout=15",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "BatchMode=yes",
        # TCP-level keepalive on top of the SSH-level one above: cheap, supported by
        # every port, and it lets the OS notice a peer that vanished (sleep / cable out)
        # instead of holding a half-dead connection open.
        "-o", "TCPKeepAlive=yes",
        # Leave the *reason* in tunnel.log ("Timeout, server ... not responding",
        # "remote port forwarding failed", "Connection reset by peer", ...).  Without
        # it the log says only "exit 255" and a drop can never be diagnosed.
        "-o", "LogLevel=VERBOSE",
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
