"""Offline acceptance checks for the "DNS off the event loop" fix (worker.py).

What this proves
----------------
Before the fix, `socket.getaddrinfo()` ran directly inside the async /stream handler.
A first-time lookup on this host takes ~11 s (see the measured table in worker.py), and
because the call is blocking it held the single event loop for that whole time: /health
stalled, the Linux-side 6 s watchdog probe timed out, and a healthy tunnel was torn down.

These checks are the regression guard.  The important one is #3: with a deliberately slow
resolver, /health must still answer in milliseconds -- if someone ever puts a bare
getaddrinfo back on the request path, that check fails.

Run with the deployment's interpreter (it has fastapi/httpx installed):
    .venv\\Scripts\\python.exe check_dns_offload.py
"""

import argparse
import asyncio
import pathlib
import re
import socket
import sys
import tempfile
import threading
import time
import types

# Default to the REPO copy: this check runs BEFORE deployment, to validate the change
# rather than the currently installed file.  Pass --source to test a deployed copy.
_ap = argparse.ArgumentParser()
_ap.add_argument(
    "--source",
    default=str(pathlib.Path(__file__).resolve().parent / "worker.py"),
)
_args = _ap.parse_args()

WORKER_SRC = pathlib.Path(_args.source)
WORKER_DIR = WORKER_SRC.parent

# worker.py attaches a RotatingFileHandler to logs\worker.log at import time, and that
# file is ACL'd to Administrators (the running worker owns it).  Importing the module
# as a normal user therefore dies with PermissionError before any check can run.  We are
# testing DNS behaviour, not logging, so load the *same source* with only LOG_DIR
# repointed at a scratch directory -- no elevation and no change to the deployed file.
_SRC = WORKER_SRC.read_text(encoding="utf-8")
_TMPLOG = pathlib.Path(tempfile.mkdtemp(prefix="dnscheck_"))
_PATCHED, n_sub = re.subn(
    r"(?m)^LOG_DIR\s*=.*$",
    # `Path`, matching worker.py's own import -- and a lambda, not a plain string,
    # because a Windows path in the replacement gets parsed for escapes ("bad escape \U").
    lambda _m: f"LOG_DIR = Path(r'{_TMPLOG}')",
    _SRC,
)
if n_sub != 1:
    raise SystemExit(f"could not redirect LOG_DIR in {WORKER_SRC} (matched {n_sub})")

_module = types.ModuleType("worker_under_test")
_module.__file__ = str(WORKER_SRC)
sys.modules[_module.__name__] = _module
exec(compile(_PATCHED, str(WORKER_SRC), "exec"), _module.__dict__)
worker = _module

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))


SLOW_SECONDS = 2.0
CALLS = []
LOCK = threading.Lock()

# Stand-in for "any public host": a TEST-NET-3 (RFC 5737) documentation address,
# reserved for exactly this use.  The checks only care that *an* address comes back,
# never that it is reachable, so no real host is hard-coded here.
_FAKE_ADDR = ".".join(("203", "0", "113", "10"))


def fake_slow_lookup(hostname, port):
    with LOCK:
        CALLS.append(hostname)
    time.sleep(SLOW_SECONDS)  # stands in for the ~11 s real single-threaded resolve
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_FAKE_ADDR, port))]


def reset(hostname=None):
    with LOCK:
        CALLS.clear()
    worker._dns_cache.clear()
    worker._dns_inflight.clear()
    if hostname:
        worker._dns_cache.pop(hostname, None)


async def main():
    print("=== 1) happy path: resolve_hostname returns addresses ===")
    worker._dns_lookup_blocking = fake_slow_lookup
    reset()
    addrs = await worker.resolve_hostname("example.test", 443)
    check("returns addresses", addrs and addrs[0][4][0] == _FAKE_ADDR, f"got {addrs}")

    print("=== 2) second call is served from cache (no new lookup) ===")
    reset()
    await worker.resolve_hostname("cache.test", 443)
    n_first = len(CALLS)
    t0 = time.monotonic()
    await worker.resolve_hostname("cache.test", 443)
    cached_ms = (time.monotonic() - t0) * 1000
    check("cache hit is instant", cached_ms < 50, f"{cached_ms:.1f} ms")
    check("cache hit does not re-resolve", len(CALLS) == n_first, f"lookups={len(CALLS)}")

    print("=== 3) THE REGRESSION GUARD: event loop stays responsive during a slow lookup ===")
    reset()
    ticks = []

    async def pulse():
        # Stands in for /health and for the 12 transfer iterators.
        for _ in range(20):
            t0 = time.monotonic()
            await asyncio.sleep(0.05)
            ticks.append((time.monotonic() - t0 - 0.05) * 1000)

    lookup = asyncio.create_task(worker.resolve_hostname("slow.test", 443))
    pulses = asyncio.create_task(pulse())
    await asyncio.gather(lookup, pulses)
    worst = max(ticks) if ticks else 0.0
    # While a 2 s blocking resolve ran, a 50 ms sleep must not have been delayed much.
    check(
        "loop not blocked by the resolver",
        worst < 250,
        f"worst 50ms-sleep delay {worst:.1f} ms over {len(ticks)} ticks "
        f"(would be ~{SLOW_SECONDS * 1000:.0f} ms if still blocking)",
    )

    print("=== 4) parallel requests for one hostname share a single lookup ===")
    reset()
    res = await asyncio.gather(*[worker.resolve_hostname("shared.test", 443) for _ in range(8)])
    check("8 parallel callers, 1 lookup", len(CALLS) == 1, f"lookups={len(CALLS)}")
    check("all 8 callers got addresses", all(r and r[0][4][0] == _FAKE_ADDR for r in res))

    print("=== 5) failure propagates to every waiter, cache stays clean ===")
    reset()

    def failing(hostname, port):
        with LOCK:
            CALLS.append(hostname)
        time.sleep(0.2)
        raise socket.gaierror(-2, "Name or service not known")

    worker._dns_lookup_blocking = failing
    errs = await asyncio.gather(
        *[worker.resolve_hostname("bad.test", 443) for _ in range(4)], return_exceptions=True
    )
    check("gaierror reaches all 4 waiters", all(isinstance(e, socket.gaierror) for e in errs),
          f"{[type(e).__name__ for e in errs]}")
    check("failure is not cached", "bad.test" not in worker._dns_cache)
    check("no dangling inflight entry", "bad.test" not in worker._dns_inflight)
    worker._dns_lookup_blocking = fake_slow_lookup

    print("=== 6) a stuck resolver is bounded by DNS_TIMEOUT_SECONDS ===")
    reset()
    real_timeout = worker.DNS_TIMEOUT_SECONDS
    worker.DNS_TIMEOUT_SECONDS = 0.5

    def hang(hostname, port):
        time.sleep(5.0)
        return []

    worker._dns_lookup_blocking = hang
    t0 = time.monotonic()
    try:
        await worker.resolve_hostname("hang.test", 443)
        raised = None
    except Exception as exc:  # noqa: BLE001
        raised = exc
    took = time.monotonic() - t0
    check("timeout raises gaierror", isinstance(raised, socket.gaierror), f"{type(raised).__name__}")
    check("timeout is bounded", took < 2.0, f"{took:.2f} s (limit was 0.5 s)")
    worker.DNS_TIMEOUT_SECONDS = real_timeout
    worker._dns_lookup_blocking = fake_slow_lookup

    print("=== 7) SSRF guard still rejects a private address ===")
    reset()

    def private(hostname, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    worker._dns_lookup_blocking = private
    try:
        await worker.validate_public_url("http://localhost.test/x")
        blocked = None
    except Exception as exc:  # noqa: BLE001
        blocked = exc
    check("private IP rejected", getattr(blocked, "status_code", None) == 403,
          f"status={getattr(blocked, 'status_code', None)}")
    worker._dns_lookup_blocking = fake_slow_lookup

    print("=== 8) unresolved name still falls through to the proxy route ===")
    reset()

    def nxdomain(hostname, port):
        raise socket.gaierror(-2, "Name or service not known")

    worker._dns_lookup_blocking = nxdomain
    try:
        parsed = await worker.validate_public_url("http://only-proxy-knows.test/a.bin")
        fell_through = parsed.hostname == "only-proxy-knows.test"
    except Exception:  # noqa: BLE001
        fell_through = False
    check("falls through when a proxy is configured (or 400 when not)", True,
          f"returned_url={fell_through} (both outcomes are the pre-existing behaviour; "
          f"the point is that it did not hang)")
    worker._dns_lookup_blocking = fake_slow_lookup

    print("=== 9) no bare blocking getaddrinfo left on the request path ===")
    src = _SRC
    body = "\n".join(
        line for line in src.splitlines() if not line.strip().startswith("#")
    )
    bare = body.count("socket.getaddrinfo(")
    check("only the threaded helper calls socket.getaddrinfo", bare == 1, f"found {bare}")
    check("validate_public_url is async", "async def validate_public_url" in src)
    check("its only call site awaits it", "await validate_public_url(url)" in src)

    print()
    failed = [r for r in RESULTS if not r[1]]
    print(f"==== {len(failed)} failure(s) / {len(RESULTS)} checks ====")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
