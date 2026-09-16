"""Shared deployment configuration for the Local Download Worker.

Single source of truth: <InstallDir>\\worker-config.json, written by
installer/install-worker.ps1.  Nothing machine-specific (IP / user / path) is
hardcoded here, so the same package deploys to any Windows PC.

This module belongs to the packaging layer.  The Worker core (worker.py) keeps
its own LOG_DIR (derived from its location, so the logs always sit next to the
installation) and imports load_config() from here for the proxy/route settings.
"""
import ipaddress
import json
import re
import subprocess
from pathlib import Path

INSTALL_DIR = Path(__file__).resolve().parent
CONFIG_PATH = INSTALL_DIR / "worker-config.json"
TAILSCALE_EXE = r"C:\Program Files\Tailscale\tailscale.exe"

DEFAULTS = {
    "worker_host": "",          # "" or "auto" -> detect this machine's Tailscale IPv4
    "worker_port": 8765,
    "linux_user": "",
    "linux_host": "",
    "linux_tunnel_port": 8766,
    "log_sync": {
        "enabled": True,
        "remote_dir": "",
        "interval_minutes": 5,
    },
    # Network exits for download traffic (2026-09-14, revised 2026-09-15).
    # The Worker has ONE optional proxy of its own ("url"), reached directly;
    # every download tries it first and falls back to DIRECT.  This PC's own /
    # private proxy is never a download exit (see worker.py and README.md
    # "代理出口").  Empty url = DIRECT only.
    "proxy": {
        "url": "",                 # http://host:port | socks5h://host:port | http://user:pass@host:port
        "retries": 2,              # attempts on the first route before switching to the next
        "windows_private": {
            "enabled": False,      # data downloads: keep False (project rule)
        },
        "large_file_threshold_bytes": 1024 * 1024 * 1024,
    },
}


def _merge(base, data):
    """Overlay the config file onto the defaults.

    Keys the defaults do not know are ignored, exactly as before; one nested
    level (log_sync, proxy.windows_private, ...) is merged key by key so a
    hand-edited file that only sets one field keeps the rest.

    The r5 "proxy.linux" block is not in DEFAULTS any more, so a config left
    over from r5 keeps those keys on disk but they are ignored here - the r6
    installer rewrites the block, and worker.py never looks at it.
    """
    for key, value in data.items():
        if key not in base:
            continue

        if isinstance(value, dict) and isinstance(base.get(key), dict):
            for sub_key, sub_value in value.items():
                if isinstance(sub_value, dict) and isinstance(base[key].get(sub_key), dict):
                    base[key][sub_key].update(sub_value)
                else:
                    base[key][sub_key] = sub_value
        else:
            base[key] = value


def load_config():
    """Return the deployment config, falling back to DEFAULTS for anything missing."""
    cfg = json.loads(json.dumps(DEFAULTS))          # deep copy of the defaults

    if not CONFIG_PATH.exists():
        return cfg

    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        print(f"[worker_config] WARNING: cannot read {CONFIG_PATH}: {exc}", flush=True)
        return cfg

    if not isinstance(data, dict):
        return cfg

    _merge(cfg, data)

    return cfg


def is_tailscale_ip(text):
    """True for addresses in the Tailscale CGNAT range 100.64.0.0/10."""
    try:
        return ipaddress.ip_address(str(text).strip()) in ipaddress.ip_network("100.64.0.0/10")
    except ValueError:
        return False


def detect_tailscale_ipv4():
    """Return this machine's Tailscale IPv4 address, or "" when it cannot be found."""
    try:
        result = subprocess.run(
            [TAILSCALE_EXE, "ip", "-4"],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                line = line.strip()
                if is_tailscale_ip(line):
                    return line
    except Exception:
        pass

    # Fall back to parsing ipconfig, which works with localized Windows too.
    try:
        result = subprocess.run(
            ["ipconfig"], capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        for match in re.finditer(r"([0-9]{1,3}(?:\.[0-9]{1,3}){3})", result.stdout):
            if is_tailscale_ip(match.group(1)):
                return match.group(1)
    except Exception:
        pass

    return ""


def resolve_worker_host(cfg):
    """Configured worker_host, or the auto-detected Tailscale IPv4 when blank/auto."""
    host = str(cfg.get("worker_host") or "").strip()
    if host and host.lower() not in ("auto", "detect"):
        return host
    return detect_tailscale_ipv4()
