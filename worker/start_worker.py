"""Start the Worker (uvicorn) bound to this machine's Tailscale IPv4 address.

Bind address and port come from worker-config.json; nothing is machine-specific.
"""
import sys

import uvicorn

from worker_config import load_config, resolve_worker_host


def main():
    cfg = load_config()
    host = resolve_worker_host(cfg)

    if not host:
        print(
            "ERROR: cannot determine this machine's Tailscale IPv4 address. "
            "Make sure Tailscale is installed and logged in, or set \"worker_host\" "
            "in worker-config.json.",
            flush=True,
        )
        return 1

    print(f"Starting Worker on {host}:{int(cfg['worker_port'])}", flush=True)
    uvicorn.run("worker:app", host=host, port=int(cfg["worker_port"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
