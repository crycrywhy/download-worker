import asyncio
import ipaddress
import json
import socket
import time
from collections import namedtuple
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import logging
from logging.handlers import RotatingFileHandler

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

BANDWIDTH_CONFIG = LOG_DIR / "bandwidth.json"

DEFAULT_BANDWIDTH_PERCENT = 100
MAX_BANDWIDTH_MBPS = 10.0


# ---------------------------------------------------------------------------
# Proxy / route layer
#
# A download has at most two exits:
#
#   WORKER_PROXY - the optional proxy configured for this Worker in
#                  worker-config.json ("proxy.url"), reached directly over the
#                  network (http:// or socks5h://).  It is tried FIRST: this
#                  PC's own route to the origin is often far too slow, and the
#                  proxy exists precisely to avoid that path.
#   DIRECT       - this PC's own connection, the fallback.
#
# This PC's *private* proxy is never a download exit:
#
#   * every client is built with trust_env=False, so HTTP_PROXY / HTTPS_PROXY /
#     ALL_PROXY in the machine environment can never carry download traffic
#     (the r4 bug: httpx inherited them and quietly burned the private quota)
#   * a configured url that points at loopback is refused outright, so the
#     private proxy's usual 127.0.0.1:7897 cannot be wired in even by accident
#   * the WINDOWS_PRIVATE_PROXY route stays disabled and is only reachable when
#     somebody deliberately turns it on for small files
#
# Only transport-level failures (DNS / timeout / refused / unreachable) switch
# routes; HTTP 4xx/5xx do not.  Every decision is logged as
# [NETWORK] ... route=<NAME>; URLs are logged without query strings and proxies
# without credentials.
# ---------------------------------------------------------------------------

DEFAULT_PROXY_CONFIG = {
    "url": "",                               # "" = DIRECT only
    "retries": 2,                            # attempts on the first route before switching
    "windows_private": {
        "enabled": False,                    # data downloads: keep False (project rule)
    },
    "large_file_threshold_bytes": 1024 * 1024 * 1024,   # 1 GiB, private proxy only
}

# Proxy schemes this Worker accepts.  socks5h lets the proxy resolve DNS.
PROXY_SCHEMES = ("http", "socks5", "socks5h")

DIRECT_RETRY_DELAY_SECONDS = 1.5

try:                                          # deployment config lives next to this file
    from worker_config import load_config as _load_deploy_config
except Exception:                             # worker.py stays runnable on its own
    def _load_deploy_config():
        return {}


def proxy_config():
    """worker-config.json "proxy" block merged over DEFAULT_PROXY_CONFIG."""
    merged = json.loads(json.dumps(DEFAULT_PROXY_CONFIG))

    try:
        data = _load_deploy_config().get("proxy") or {}
    except Exception:
        data = {}

    if not isinstance(data, dict):
        return merged

    for key, value in data.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value

    return merged


class Route:
    """One network exit: WORKER_PROXY, DIRECT or WINDOWS_PRIVATE_PROXY."""

    __slots__ = ("name", "proxy_url", "target")

    def __init__(self, name, proxy_url=None, target="-"):
        self.name = name
        self.proxy_url = proxy_url
        self.target = target

    @property
    def is_direct(self):
        return self.name == "DIRECT"


def _is_loopback(host):
    """True for literal loopback addresses (the loopback network, ::1)."""
    try:
        return ipaddress.ip_address(str(host).strip()).is_loopback
    except ValueError:
        return False


ProxyTarget = namedtuple("ProxyTarget", "scheme host port username password")


def parse_proxy_url(url):
    """ProxyTarget for a usable proxy url, or None when unusable.

    Unusable means: empty, unsupported scheme, missing host/port, or a loopback
    target.  Loopback is refused on purpose - this PC's private proxy lives on
    127.0.0.1:7897 and must never carry download traffic, so neither a hand
    edited config nor the command line can wire it in by accident.
    """
    text = str(url or "").strip()

    if not text:
        return None

    if "://" not in text:
        text = "http://" + text

    parsed = urlparse(text)
    scheme = (parsed.scheme or "").strip().lower()

    if scheme not in PROXY_SCHEMES:
        return None

    host = (parsed.hostname or "").strip()

    try:
        port = int(parsed.port or 0)
    except (TypeError, ValueError):
        return None

    if not host or port <= 0 or port > 65535:
        return None

    if host.lower() == "localhost" or _is_loopback(host):
        logger.warning(
            "[NETWORK] proxy url 指向本机回环（%s）- 已忽略：私人代理不得承载下载流量",
            host,
        )
        return None

    return ProxyTarget(scheme, host, port, parsed.username or "", parsed.password or "")


def worker_proxy_route(cfg):
    """Route for the Worker's own proxy, or None when none is configured.

    Credentials embedded in the url (http://user:pass@host:port) are kept in the
    client url but never appear in the route's logged target.
    """
    target = parse_proxy_url(cfg.get("url"))

    if target is None:
        return None

    credentials = ""

    if target.username:
        credentials = f"{unquote(target.username)}:{unquote(target.password)}@"

    return Route(
        "WORKER_PROXY",
        f"{target.scheme}://{credentials}{target.host}:{target.port}",
        f"{target.host}:{target.port}",
    )


def _windows_private_route(cfg, content_length):
    """Route for the PC's own proxy - only when explicitly enabled AND not a large file.

    Default is disabled and that is the project rule: download data must not burn
    the private proxy quota.  When someone does enable it, section 10 of the
    redesign brief applies: Content-Length above the threshold (or unknown size)
    keeps the private proxy out of the plan entirely.
    """
    private = cfg.get("windows_private") or {}

    if not private.get("enabled"):
        return None

    try:
        threshold = int(cfg.get("large_file_threshold_bytes") or 0)
    except (TypeError, ValueError):
        threshold = 0

    if content_length is None or threshold <= 0 or content_length >= threshold:
        return None

    host = str(private.get("host") or "127.0.0.1").strip()
    try:
        port = int(private.get("port") or 7897)
    except (TypeError, ValueError):
        port = 7897

    return Route("WINDOWS_PRIVATE_PROXY", f"http://{host}:{port}", f"{host}:{port}")


def build_route_plan(cfg, content_length=None):
    """Routes to try, in order: the Worker's own proxy first when one is
    configured, then DIRECT, then - only if somebody deliberately enabled it and
    the file is small - this PC's private proxy."""
    plan = []

    proxy = worker_proxy_route(cfg)
    if proxy is not None:
        plan.append(proxy)

    plan.append(Route("DIRECT"))

    private = _windows_private_route(cfg, content_length)
    if private is not None:
        plan.append(private)

    return plan


def make_client(route):
    """httpx client for one route.  trust_env=False everywhere: the environment's
    HTTP_PROXY / HTTPS_PROXY / ALL_PROXY must never carry download traffic."""
    kwargs = {
        "follow_redirects": True,
        "timeout": None,
        "trust_env": False,
    }

    if route.proxy_url:
        kwargs["proxy"] = route.proxy_url

    return httpx.AsyncClient(**kwargs)


def safe_url(url):
    """URL without query string / credentials - safe to log (S3 presigned URLs
    carry credentials in the query)."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname or "<unknown>"
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return f"{parsed.scheme}://{host}{parsed.path or '/'}"
    except Exception:
        return "<unparsable-url>"


def log_plan(url, headers, plan):
    logger.info(
        "[NETWORK] plan=%s url=%s host=%s range=%s",
        ",".join(route.name for route in plan),
        safe_url(url),
        urlparse(url).hostname or "<unknown>",
        headers.get("Range", "-"),
    )


async def probe_content_length(url, headers, plan):
    """Total size of the remote object, or None.

    Only called when the private proxy is explicitly enabled, so the default
    deployment pays nothing for it.  Unknown size is treated as "large": the
    private proxy must never be the route for a big download (brief, section 10).
    """
    probe_headers = {"Range": "bytes=0-0"}

    for route in plan:
        if not route.is_direct:
            continue

        try:
            client = make_client(route)
        except Exception:
            return None

        try:
            request = client.build_request("GET", url, headers=probe_headers)
            response = await client.send(request, stream=True)
            try:
                content_range = response.headers.get("content-range") or ""
                if "/" in content_range:
                    total = content_range.rsplit("/", 1)[-1].strip()
                    if total.isdigit():
                        return int(total)
                content_length = response.headers.get("content-length")
                if content_length and content_length.isdigit():
                    return int(content_length)
            finally:
                await response.aclose()
        except Exception:
            return None
        finally:
            await client.aclose()

    return None


async def open_upstream(url, headers, plan):
    """Try the routes in order; return (client, response, route).

    Only transport-level errors (httpx.TransportError: DNS failure, connect /
    read timeout, connection refused, network unreachable, ...) move on to the
    next route.  An HTTP response - including 4xx/5xx - is returned as is: a 404
    is not a reason to retry through a proxy (brief, section 9).
    """
    cfg = proxy_config()

    try:
        first_route_attempts = max(1, int(cfg.get("retries") or 1))
    except (TypeError, ValueError):
        first_route_attempts = 1

    last_error = None

    for index, route in enumerate(plan):
        # The first exit gets a couple of tries - a one-off DNS hiccup should
        # not push the request onto the next route; fallbacks get one each.
        attempts = first_route_attempts if index == 0 else 1

        for attempt in range(1, attempts + 1):
            try:
                client = make_client(route)
            except Exception as exc:
                # e.g. socks5 without httpx[socks]: unusable route, not a fatal error
                logger.warning(
                    "[NETWORK] route=%s unavailable (%s: %s) - 跳过该出口",
                    route.name, type(exc).__name__, exc,
                )
                break

            try:
                request = client.build_request("GET", url, headers=headers)
                response = await client.send(request, stream=True)
            except httpx.TransportError as exc:
                await client.aclose()
                last_error = exc
                logger.warning(
                    "[NETWORK] route=%s attempt=%d/%d url=%s proxy=%s error=%s: %s",
                    route.name, attempt, attempts, safe_url(url), route.target,
                    type(exc).__name__, exc,
                )
                if attempt < attempts:
                    await asyncio.sleep(DIRECT_RETRY_DELAY_SECONDS)
                continue
            except Exception:
                await client.aclose()
                raise

            logger.info(
                "[NETWORK] route=%s url=%s host=%s status=%s range=%s proxy=%s",
                route.name,
                safe_url(url),
                urlparse(url).hostname or "<unknown>",
                response.status_code,
                headers.get("Range", "-"),
                route.target,
            )
            return client, response, route

        if index + 1 < len(plan):
            logger.warning(
                "[NETWORK] route=%s 失败 - 切换下一出口 %s",
                route.name, plan[index + 1].name,
            )

    if last_error is not None:
        raise last_error

    raise HTTPException(status_code=502, detail="No usable network route")


def load_bandwidth_percent():
    if not BANDWIDTH_CONFIG.exists():
        return DEFAULT_BANDWIDTH_PERCENT

    try:
        data = json.loads(
            BANDWIDTH_CONFIG.read_text(encoding="utf-8")
        )

        value = int(data.get("percent", DEFAULT_BANDWIDTH_PERCENT))

        return max(0, min(100, value))

    except Exception:
        return DEFAULT_BANDWIDTH_PERCENT


def get_bandwidth_limit_bytes():
    percent = load_bandwidth_percent()

    mb_per_second = (
        MAX_BANDWIDTH_MBPS * percent / 100
    )

    return mb_per_second * 1024 * 1024

class GlobalBandwidthLimiter:
    def __init__(self):
        self.next_available_time = time.monotonic()
        self.lock = asyncio.Lock()

    async def acquire(self, amount):
        while True:
            limit = get_bandwidth_limit_bytes()

            if limit <= 0:
                await asyncio.sleep(0.5)
                continue

            async with self.lock:
                now = time.monotonic()

                start_time = max(
                    now,
                    self.next_available_time,
                )

                delay = amount / limit

                self.next_available_time = (
                    start_time + delay
                )

                wait_time = start_time - now

            if wait_time > 0:
                await asyncio.sleep(wait_time)

            return


bandwidth_limiter = GlobalBandwidthLimiter()

logger = logging.getLogger("local-download-worker")
logger.setLevel(logging.INFO)

handler = RotatingFileHandler(
    LOG_DIR / "worker.log",
    maxBytes=10 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
)

formatter = logging.Formatter(
    "%(asctime)s | %(levelname)s | %(message)s"
)

handler.setFormatter(formatter)
logger.addHandler(handler)

app = FastAPI(title="Local Download Worker")


# ---------------------------------------------------------------------------
# Event-loop responsiveness probe (replaces a failed heartbeat)
#
# The symptom: external probes get connect=0-1 ms but no HTTP response for 1-10 s, on
# every endpoint at once.  Two very different causes look identical from outside:
#   (a) requests ARE reaching the app but the event loop is busy, so handling is
#       delayed -- the delay is inside this process; or
#   (b) responses ARE produced quickly but something below the app delays them
#       (kernel, Tailscale userspace path) -- the delay is outside this process.
#
# A thread-based heartbeat could NOT answer this: CPython hands the GIL to a waking
# thread within ~5 ms even while another thread spins, so a sleeping instrument never
# notices the loop being busy (measured: a deliberate 4 s GIL-holding busy loop was
# undetectable).  It was removed for that reason.
#
# What DOES separate (a) from (b): the moment the handler actually runs.  External
# probes fire on a fixed cadence (the 2 s /health probe).  If the handler records the
# gap since its own previous invocation, then
#   * gap ~= the probe cadence      -> the loop was keeping up; a stall must be (b);
#   * gap >> the probe cadence      -> the requests were queued unhandled for that
#                                      long, which only happens inside the process.
#
# Cost: one monotonic clock read and a comparison per /health request, plus a log line
# only when the gap is abnormal.  Nothing is written on the steady-state path.
LOOP_STALL_MIN_SECONDS = 5.0
STREAM_SLOW_ITER_SECONDS = 0.5      # per-1MiB overhead above this is abnormal at ~10 Mbps
STREAM_SLOW_CLEANUP_SECONDS = 0.5   # aclose should be near-instant; this flags the pool teardown
_last_health_at = None


@app.get("/health")
async def health():
    global _last_health_at

    now = time.monotonic()
    if _last_health_at is not None:
        gap = now - _last_health_at
        if gap >= LOOP_STALL_MIN_SECONDS:
            logger.warning(
                "[LOOPSTALL] /health handled only after a %.2fs gap with no request handled "
                "(previous /health %.2fs ago) - requests were queued unhandled",
                gap,
                gap,
            )
    _last_health_at = now

    # How long THIS call took to execute is not visible from outside: a probe's
    # timeout only tells us the total round trip.  Measuring it here separates
    # "the loop was busy and the request waited" (gap large, work tiny) from
    # "this handler itself is slow" (gap normal, work large).
    t_work = time.monotonic()
    body = {
        "status": "ok",
        "worker": "local-download-worker",
    }
    work = time.monotonic() - t_work
    if work >= 0.05:
        logger.warning("[HEALTH-SLOW] handler executed for %.3fs", work)

    return body


# ---------------------------------------------------------------------------
# DNS off the event loop
#
# socket.getaddrinfo() is a BLOCKING call, and it used to run directly inside the
# async /stream handler (validate_public_url).  Measured on a reference host:
#
#     <bucket>.s3.amazonaws.com    11040 ms   (first lookup, then 0 ms from cache)
#     s3.amazonaws.com             11027 ms
#     www.google.com               11027 ms
#
# while an unresolvable name failed in 1 ms.  The 11 s comes from name resolution
# being attempted against a first-choice DNS server that black-holes queries: a
# higher-priority network interface (metric 1, vs the physical NIC's 35) points the
# resolver there, so Windows waits out that server before falling back to a working one.
#
# Those 11 s were the whole "tunnel instability" mystery.  A py-spy snapshot taken
# while the worker was unresponsive showed every thread parked on:
#
#     getaddrinfo (socket.py:989)  <- blocking, event loop held
#     validate_public_url (worker.py)  <- async handler, should never block
#     stream (worker.py)
#
# With a single event loop that means every other request waits too: /health stalls,
# the Linux-side watchdog's 6 s probe times out, and a perfectly healthy tunnel gets
# declared DOWN and torn down (its death then leaves the CLOSE-WAIT listener behind
# that makes the next bind fail).  It also explains why the freezes were ~11-13 s and
# independent of download load, and why they followed chunk completions: a completion
# makes the driver open new Range requests, and each new hostname needs a lookup.
#
# Two things are done here.  The lookup moves to a worker thread via asyncio.to_thread,
# so the event loop stays free and the wait only delays the ONE request that needed the
# name.  And successful lookups are cached, so with the few S3 hostnames this host
# downloads, the 11 s is paid about once each instead of on every new stream.
#
# The network is deliberately NOT touched: the host's DNS configuration belongs to
# other software, and repointing it could break that host's connectivity.
# ---------------------------------------------------------------------------

DNS_CACHE_TTL_SECONDS = 600.0   # how long a resolved hostname stays usable
DNS_TIMEOUT_SECONDS = 20.0      # bound the thread so a stuck resolver cannot pile up
_dns_cache = {}                 # hostname -> (expires_at_monotonic, addresses)
_dns_inflight = {}              # hostname -> Future, so parallel requests share one lookup
_dns_lock = None                # created lazily: binding a Lock to a loop is deprecated here


def _dns_lookup_blocking(hostname, port):
    """Runs in a worker thread.  The only blocking part is getaddrinfo itself."""
    return socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)


async def resolve_hostname(hostname, port):
    """Resolve off the event loop, cached, and de-duplicated across parallel requests.

    Raises socket.gaierror exactly like socket.getaddrinfo, so callers keep their
    existing behaviour: a name only the proxy can resolve still falls through to it.
    """
    global _dns_lock

    loop = asyncio.get_running_loop()
    if _dns_lock is None:
        _dns_lock = asyncio.Lock()

    now = time.monotonic()
    cached = _dns_cache.get(hostname)
    if cached is not None:
        if cached[0] > now:
            return cached[1]
        _dns_cache.pop(hostname, None)

    async with _dns_lock:
        # Re-check: another task may have filled the cache while we waited.
        cached = _dns_cache.get(hostname)
        if cached is not None and cached[0] > time.monotonic():
            return cached[1]

        # The downloader opens several parallel Range requests for the same URL, so
        # without this every one of them would start its own lookup and they would all
        # pay the full 11 s instead of sharing a single one.
        inflight = _dns_inflight.get(hostname)
        if inflight is None:
            inflight = loop.create_future()
            _dns_inflight[hostname] = inflight
            owner = True
        else:
            owner = False

    if not owner:
        return await asyncio.shield(inflight)

    def _absorb(fut):
        # If nobody is left awaiting the shared future (every waiter cancelled, or the
        # request was aborted), asyncio would otherwise log "Future exception was never
        # retrieved" for the failure we are about to store.  Retrieving it here marks it
        # handled; the real callers still get the exception from their own await.
        if not fut.cancelled():
            fut.exception()

    started = time.monotonic()
    try:
        addresses = await asyncio.wait_for(
            asyncio.to_thread(_dns_lookup_blocking, hostname, port),
            timeout=DNS_TIMEOUT_SECONDS,
        )
        elapsed = time.monotonic() - started
        _dns_cache[hostname] = (time.monotonic() + DNS_CACHE_TTL_SECONDS, addresses)
        if elapsed >= 1.0:
            # Worth a line: this is the delay that used to freeze the whole worker.
            logger.info(
                "[DNS] resolved host=%s in %.2fs (now cached %.0fs; the wait no longer "
                "blocks the event loop)",
                hostname,
                elapsed,
                DNS_CACHE_TTL_SECONDS,
            )
        inflight.add_done_callback(_absorb)
        inflight.set_result(addresses)
        return addresses
    except asyncio.TimeoutError as exc:
        inflight.add_done_callback(_absorb)
        inflight.set_exception(exc)
        raise socket.gaierror(
            f"DNS lookup for {hostname} exceeded {DNS_TIMEOUT_SECONDS:.0f}s"
        ) from exc
    except BaseException as exc:  # includes gaierror; must reach every waiter
        if not inflight.done():
            inflight.add_done_callback(_absorb)
            inflight.set_exception(exc)
        raise
    finally:
        _dns_inflight.pop(hostname, None)


async def validate_public_url(url: str):
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise HTTPException(
            status_code=400,
            detail="Only http/https URLs are allowed",
        )

    if not parsed.hostname:
        raise HTTPException(
            status_code=400,
            detail="URL must contain a hostname",
        )

    hostname = parsed.hostname

    try:
        # await, not a bare call: see "DNS off the event loop" above.  Blocking here is
        # what used to stall every other endpoint for ~11 s and get the tunnel killed.
        addresses = await resolve_hostname(
            hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
        )
    except socket.gaierror:
        # A name that only the proxy can resolve must still be able to go out
        # through it - but only when a proxy is actually configured.
        if worker_proxy_route(proxy_config()) is not None:
            logger.warning(
                "[NETWORK] dns-fail host=%s 本地解析失败 - 有 WORKER_PROXY 兜底，"
                "跳过 IP 检查，交远端解析",
                hostname,
            )
            return parsed

        raise HTTPException(
            status_code=400,
            detail="Hostname could not be resolved",
        )

    ips = {item[4][0] for item in addresses}

    for ip in ips:
        addr = ipaddress.ip_address(ip)

        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
            or addr.is_unspecified
        ):
            raise HTTPException(
                status_code=403,
                detail="Target resolves to a non-public IP address",
            )

    return parsed


# ---------------------------------------------------------------------------
# Live transfer snapshot - the data behind GET /status
#
# The Linux side's status broker polls /status to show what
# each PC is transferring right now ("在传 <url>（worker 自报）").  Three rules
# shaped this code:
#
#   * never block.  /status waits on no lock and touches no network - it only
#     reads a snapshot the transfer handlers keep current.
#   * never buffer.  /stream's streaming behaviour is untouched: the snapshot
#     is a dict field assignment next to the existing byte counter, so no chunk
#     is held back, split or copied.
#   * one entry per in-flight request.  The Linux downloader opens several
#     parallel Range requests (connections=2 by default, up to 4), so /status
#     reports the most recently active one; url / task / client are identical
#     across them anyway.
#
# uvicorn serves this app in a single process with a single event loop
# (start_worker.py -> uvicorn.run(..., no workers=)), and the helpers below
# never await, so a plain dict is safe here.  A lock would only add a way for
# /status to block, which is exactly what it must not do.
# ---------------------------------------------------------------------------

_transfers = {}          # request id -> snapshot; "_"-prefixed keys are internal
_transfer_ids = 0


def transfer_begin(url, request):
    """Register one in-flight transfer.  Returns (id, snapshot); never awaits."""
    global _transfer_ids

    _transfer_ids += 1
    snapshot = {
        "state": "downloading",
        "url": url,
        "task": request.headers.get("x-task") or "",
        "client": request.headers.get("x-client") or "",
        "range": request.headers.get("range") or "",
        "bytes": 0,
        "total": None,
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        # The request's own task, used as a liveness backstop by /status.  The
        # endpoint and the response body run in the same task, so "task done"
        # means "this transfer is over" - even on the one path the streaming
        # iterator's finally cannot cover: a client that disconnects before the
        # response body ever starts, which leaves that generator un-started.
        "_task": asyncio.current_task(),
        "_touched": time.monotonic(),
    }

    _transfers[_transfer_ids] = snapshot

    return _transfer_ids, snapshot


def transfer_progress(snapshot, sent):
    """Bytes handed to the client so far, and the 'most recently active' stamp."""
    snapshot["bytes"] = sent
    snapshot["_touched"] = time.monotonic()


def transfer_end(transfer_id):
    """Drop one transfer.  A plain dict pop: safe to call twice, never blocks."""
    _transfers.pop(transfer_id, None)


def content_length_of(response):
    """Content-Length of an upstream response as an int, or None."""
    try:
        return int(response.headers.get("content-length"))
    except (TypeError, ValueError):
        return None


class DownloadRequest(BaseModel):
    url: str
    output: str


@app.get("/status")
async def status():
    """What this PC is transferring right now.  Unauthenticated, like /health.

    {"state": "idle"} when nothing is in flight, otherwise the most recently
    active transfer with url / task / client / range / bytes / total / started.

    Must stay `async def`: FastAPI runs a *sync* handler in a worker thread, and
    iterating _transfers from another thread while the event loop adds or drops
    an entry would raise "dictionary changed size during iteration".  A
    coroutine with no await runs to completion on the event loop, which is also
    why no lock is needed - and no lock means nothing here can block.
    """
    # Backstop before answering: a transfer whose request task has finished is
    # over, so drop it even if its streaming iterator never got the chance to
    # clean up.  Building the list first keeps the dict from changing size
    # mid-iteration (there is no await here, so this is the only writer).
    for key in [key for key, item in _transfers.items()
                if item["_task"] is not None and item["_task"].done()]:
        del _transfers[key]

    if not _transfers:
        return {"state": "idle"}

    snapshot = max(_transfers.values(), key=lambda item: item["_touched"])

    return {key: value for key, value in snapshot.items() if not key.startswith("_")}


@app.post("/download")
async def download(req: DownloadRequest, request: Request):
    output = Path(req.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    plan = build_route_plan(proxy_config())

    transfer_id, snapshot = transfer_begin(req.url, request)

    try:
        client, response, route = await open_upstream(req.url, {}, plan)
    except httpx.TransportError as exc:
        transfer_end(transfer_id)
        raise HTTPException(
            status_code=500,
            detail="{}: {} (routes tried: {})".format(
                type(exc).__name__, exc,
                ",".join(item.name for item in plan),
            ),
        )
    except BaseException:
        # Unexpected failure, or a caller that vanished mid-connect: drop the
        # snapshot as it propagates, or /status would report it forever.
        transfer_end(transfer_id)
        raise

    try:
        if response.status_code >= 400:
            detail = await response.aread()
            raise HTTPException(
                status_code=response.status_code,
                detail=detail.decode(errors="replace"),
            )

        snapshot["total"] = content_length_of(response)

        written = 0
        with open(output, "wb") as f:
            async for chunk in response.aiter_bytes(1024 * 1024):
                f.write(chunk)
                written += len(chunk)
                transfer_progress(snapshot, written)
    finally:
        transfer_end(transfer_id)
        await response.aclose()
        await client.aclose()

    return {
        "status": "completed",
        "url": req.url,
        "output": str(output),
        "route": route.name,
    }


@app.get("/stream")
async def stream(request: Request, url: str):
    await validate_public_url(url)
    headers = {}

    if "range" in request.headers:
        headers["Range"] = request.headers["range"]

    start_time = time.monotonic()
    hostname = urlparse(url).hostname or "<unknown>"
    range_header = request.headers.get("range")

    cfg = proxy_config()
    private = cfg.get("windows_private") or {}

    if private.get("enabled"):
        # Section 10: never let a large (or unknown-size) download reach the
        # private proxy.  This probe only runs when someone enabled it.
        content_length = await probe_content_length(url, headers, [Route("DIRECT")])
        plan = build_route_plan(cfg, content_length)
    else:
        plan = build_route_plan(cfg)

    log_plan(url, headers, plan)

    # Snapshotted before the upstream connect, not after: a connect that spends
    # ~10s in DNS / route retries would otherwise read as "idle" on the broker
    # board while this PC is in fact already working on the request.
    transfer_id, snapshot = transfer_begin(url, request)

    try:
        client, response, route = await open_upstream(url, headers, plan)
    except httpx.TransportError as exc:
        transfer_end(transfer_id)
        # Same status code as before the route layer existed; the body now names
        # the exits that were tried, which is what made past outages hard to read.
        raise HTTPException(
            status_code=500,
            detail="{}: {} (routes tried: {})".format(
                type(exc).__name__, exc,
                ",".join(item.name for item in plan),
            ),
        )
    except BaseException:
        # Unexpected failure, or a client that vanished mid-connect: drop the
        # snapshot as it propagates, or /status would report it forever.
        transfer_end(transfer_id)
        raise

    logger.info(
        "STREAM start host=%s status=%s range=%s",
        hostname,
        response.status_code,
        range_header or "-",
    )

    if response.status_code >= 400:
        status_code = response.status_code
        detail = await response.aread()
        await response.aclose()
        await client.aclose()
        transfer_end(transfer_id)

        raise HTTPException(
            status_code=status_code,
            detail=detail.decode(errors="replace"),
        )

    snapshot["total"] = content_length_of(response)

    passthrough_headers = {}

    for name in (
        "content-length",
        "content-range",
        "content-type",
        "accept-ranges",
    ):
        if name in response.headers:
            passthrough_headers[name] = response.headers[name]

    # Lets the caller (and the acceptance tests) see which exit served the
    # request without reading this PC's log file.
    passthrough_headers["x-worker-route"] = route.name

    async def iterator():
        bytes_sent = 0

        # --- data-path instrumentation -------------------------------------
        # The worker's event loop freezes for a consistent ~9-11 s on this PC and
        # EVERY endpoint stalls with it, so the freeze is somewhere on the download
        # data path -- but "somewhere" is not good enough to fix it.  These monotonic
        # clocks split each iteration into its four phases so the next freeze names
        # the operation responsible instead of leaving it to inference:
        #   pull   = waiting for the next 1 MiB from the upstream (network; must not
        #            block the loop, but a stuck socket or a blocking resolve shows here)
        #   limit  = the global bandwidth limiter (asyncio.Lock + asyncio.sleep)
        #   yield  = handing the chunk to the client; this is where the generator is
        #            suspended while the response is written, so it includes flow control
        #   rest   = bookkeeping, plus however long this task waited to be scheduled
        #            again (that last part is the fingerprint of an event loop that is
        #            occupied by SOMETHING ELSE -- it is unattributed time by design)
        # `outside` is the kicker: it accumulates the time this iterator spent NOT
        # running.  If the freeze is not inside any of the labelled phases, it has to
        # show up there.
        stats = {
            "iters": 0, "pull": 0.0, "limit": 0.0, "yield": 0.0,
            "rest": 0.0, "outside": 0.0, "slow": 0, "worst": 0.0,
            "prev": None, "worst_phase": "",
        }

        def note(phase, secs):
            stats[phase] += secs
            if secs > stats["worst"]:
                stats["worst"] = secs
                stats["worst_phase"] = phase

        try:
            async for chunk in response.aiter_bytes(1024 * 1024):
                t0 = time.monotonic()
                if stats["prev"] is not None:
                    stats["outside"] += max(0.0, t0 - stats["prev"])
                stats["prev"] = t0
                stats["iters"] += 1

                await bandwidth_limiter.acquire(len(chunk))
                t1 = time.monotonic()
                note("limit", t1 - t0)

                bytes_sent += len(chunk)
                transfer_progress(snapshot, bytes_sent)
                yield chunk
                t2 = time.monotonic()
                note("yield", t2 - t1)

                stats["rest"] += time.monotonic() - t2
                stats["prev"] = time.monotonic()

            elapsed = time.monotonic() - start_time

            # Only speak up when the iteration was slow: at ~10 Mbps a 1 MiB chunk is
            # ~1 s, so 0.5 s of overhead is abnormal and stands out in the log.
            per_iter = (time.monotonic() - start_time) / max(stats["iters"], 1)
            if stats["iters"] and per_iter > STREAM_SLOW_ITER_SECONDS:
                logger.warning(
                    "[STREAM-SLOW] total=%.2fs iters=%d avg_iter=%.3fs worst=%.3fs(%s) "
                    "pull=%.2f limit=%.2f yield=%.2f rest=%.2f outside=%.2f bytes=%d",
                    elapsed, stats["iters"], per_iter, stats["worst"], stats["worst_phase"],
                    stats["pull"], stats["limit"], stats["yield"], stats["rest"],
                    stats["outside"], bytes_sent,
                )

            logger.info(
                "STREAM complete host=%s status=%s bytes=%d elapsed=%.2fs route=%s",
                hostname,
                response.status_code,
                bytes_sent,
                elapsed,
                route.name,
            )

        except Exception as exc:
            elapsed = time.monotonic() - start_time

            logger.exception(
                "STREAM failed host=%s status=%s bytes=%d elapsed=%.2fs error=%s",
                hostname,
                response.status_code,
                bytes_sent,
                elapsed,
                type(exc).__name__,
            )

            raise

        finally:
            # Synchronous and first: a client aborting mid-stream is a normal
            # way for a transfer to end here, and /status must stop reporting
            # it even if the closes below are cancelled.
            t_cleanup = time.monotonic()
            transfer_end(transfer_id)
            await response.aclose()
            t_resp_closed = time.monotonic()
            await client.aclose()
            cleanup = time.monotonic() - t_cleanup
            if cleanup > STREAM_SLOW_CLEANUP_SECONDS:
                logger.warning(
                    "[STREAM-CLEANUP] aclose took %.2fs (response=%.2fs client=%.2fs) "
                    "iters=%d bytes=%d worst_iter=%.3fs(%s) outside=%.2f",
                    cleanup, t_resp_closed - t_cleanup, time.monotonic() - t_resp_closed,
                    stats["iters"], bytes_sent, stats["worst"], stats["worst_phase"],
                    stats["outside"],
                )

    return StreamingResponse(
        iterator(),
        status_code=response.status_code,
        headers=passthrough_headers,
        media_type=response.headers.get("content-type"),
    )
