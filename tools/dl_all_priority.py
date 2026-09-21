import csv, hashlib, json, os, re, shutil, subprocess, sys, threading, time, tempfile
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import disk_trust as _disk_trust
except Exception:
    _disk_trust = None

import dl_config as CFG
HERE = os.path.dirname(os.path.abspath(__file__))

DRIVER_NAME = CFG.req('identity.driver', 'driver')
OWNER = CFG.get('identity.owner', 'agent')

MANIFEST = CFG.req_path('paths.manifest', 'driver')
MANIFEST_ALT = CFG.get('paths.manifest_alt', '')
OUTDIR = CFG.req_path('paths.outdir', 'driver')
OUTDIR_ALT = CFG.get('paths.outdir_alt', '')
S3_BASE = CFG.get('endpoints.s3_base', '')

# 「上游 / 内容问题」而不是 worker 故障：这类失败既不重排队（下轮重建任务时自然重试），
# 也不该去探活 worker —— 内容问题跟出口健不健康无关。
#
# 前缀**必须与产出处逐字一致**。产出与消费各写一份字面量是这里最容易踩的坑：
# 改文案时只改一头，下游就静默失配，于是把「上游没这个对象」误判成 worker 故障，
# 白白探活甚至熔断一台健康的机器。所以两边共用这一份。
UPSTREAM_ISSUE_PREFIXES = (
    'store listing failed',
    'key prefix empty',
    'no tagged object upstream',
)
# Layout of the object store, as a template.  {prefix} and {build} come from the
# manifest row; {flavor} is the per-source sub-directory.  Keeping this in config
# means a different store layout needs no code change.
PREFIX_TEMPLATE = CFG.get('endpoints.prefix_template', 'objects/{prefix}/{build}/{flavor}/')
# Only objects with this suffix are considered downloads.  ".bin" is a stand-in:
# set it to whatever your store actually uses.
SUFFIX = CFG.get('endpoints.object_suffix', '.bin')
# Trailer of a gzip-framed container -- what a complete file ends with.  Lets a
# finished transfer be told from one that stopped mid-frame without reading it.
CONTAINER_TRAILER = bytes.fromhex('1f8b08040000000000ff060042430200')
TOOL = CFG.get('tools.s3_prefix_downloader', '')
STREAM_CHECKER = CFG.get('tools.stream_checker', '')
MAX_CONCURRENT = CFG.get_int('ops.max_concurrent', 3)
WIN_LINES = CFG.get_int('ops.win_lines', 3)
WIN_WORKERS = os.environ.get('WIN_WORKERS', 'off')
WIN_PROBE_INTERVAL = CFG.get_int('ops.win_probe_interval', 600)
WORKER_PORT_CAP = max(1, CFG.get_int('ops.worker_port_cap', 2))
WIN_CONNECTIONS = CFG.get_int('ops.win_connections', 4)
LOCAL_LINES = CFG.get_int('ops.local_lines', 0)
WIN_GATE = CFG.get('ops.win_gate', '')
WIN_GATE_MAX_WAIT = CFG.get_int('ops.win_gate_max_wait', 48 * 3600)
WIN_DOWNLOADER = os.path.join(HERE, 'linux_downloader.py')
# Optional external tools.  None of them ship with this package; each is a
# separate program the operator points at through the config file.  Leaving one
# unset degrades the matching check to a no-op that says so once -- never a
# silent pass.
INTEGRITY     = CFG.get('tools.integrity_checker', '')
HOLE_SCANNER  = CFG.get('tools.hole_scanner', '')
RANGE_PATCHER = CFG.get('tools.range_patcher', '')
HOLE_MARGIN   = 2097152
STAGE_DIR         = CFG.get('paths.stage_dir', '')
STAGE_HIGH_GB     = CFG.get_float('ops.stage_high_gb', 100.0)
STAGE_LOW_GB      = CFG.get_float('ops.stage_low_gb', 60.0)
STAGE_MAX_FILE_GB = CFG.get_float('ops.stage_max_file_gb', 90.0)
STREAM_LOCAL      = os.environ.get('STREAM_LOCAL', '0') == '1'
STREAM_CONNS      = int(os.environ.get('STREAM_CONNS', '4'))
STREAM_CHUNK      = int(os.environ.get('STREAM_CHUNK_MB', '64')) * 1024 * 1024
STREAM_SYNC       = int(os.environ.get('STREAM_SYNC_MB', '512')) * 1024 * 1024
STREAM_WINDOW     = int(os.environ.get('STREAM_WINDOW_MB', '512')) * 1024 * 1024
if STREAM_WINDOW < STREAM_SYNC + (STREAM_CONNS + 2) * STREAM_CHUNK:
    STREAM_WINDOW = STREAM_SYNC + (STREAM_CONNS + 2) * STREAM_CHUNK
PRIORITY_GROUPS = set(CFG.get_list('domain.priority_groups', []))
PLATFORM_RANK = {'alpha': 0, 'beta': 1, 'gamma': 2}

# Optional bookkeeping hook.  The ledger is a separate chain that does not ship
# here; when it is off (the default) nothing is recorded and transfers still run.
_ledger = None
if CFG.get_bool('ops.record_ledger', False):
    try:
        sys.path.insert(0, HERE)
        import verify_downloads as _ledger
    except Exception:
        _ledger = None

# Roots the "already on disk?" search walks, and the subset of them that other
# writers also touch (a file there is only trusted once its container trailer is
# intact).  Configured rather than derived: the shape of a data tree is a site
# decision, not something this program should assume.
DATA_ROOTS = [p for p in CFG.get_list('paths.data_roots', []) if p]
for _p in (OUTDIR, OUTDIR_ALT):
    if _p and _p not in DATA_ROOTS:
        DATA_ROOTS.append(_p)
SHARED_ROOTS = [p for p in CFG.get_list('paths.shared_roots', []) if p]

# Per-source sub-directory inside one build's key space.
FLAVOR_PRIMARY   = CFG.get('endpoints.flavor_primary', 'primary')
FLAVOR_SECONDARY = CFG.get('endpoints.flavor_secondary', 'secondary')

def _store_prefix(prefix, build, flavor=''):
    """Key prefix for one (prefix, build) pair inside the object store.

    The layout is a template in config, so a store that nests differently needs
    no code change.  ``flavor`` picks the per-source sub-directory and may be
    empty, in which case the template's own separators are left to stand.
    """
    p = PREFIX_TEMPLATE.format(prefix=prefix, build=build, flavor=flavor)
    if p.endswith('//'):
        p = p[:-1]
    return p if p.endswith('/') else p + '/'

def curl(url, timeout=20, retries=3):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode()
        except Exception:
            if i < retries - 1:
                time.sleep(3 * (i + 1))
    return None

TAG_PATTERN = CFG.get('endpoints.tag_probe_pattern', 'TAG:Z:')

def probe_tag_header(object_key, retries=2):
    """Count tag records in the head of an object, or -1 if it can't be read.

    The tag grammar is site-specific, so the pattern is config, not code.
    """
    url = f"{S3_BASE}/{object_key}"
    for i in range(retries):
        try:
            req = urllib.request.Request(url)
            req.add_header('Range', 'bytes=0-204800')
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
            tmp = tempfile.mktemp(suffix='.bin')
            with open(tmp, 'wb') as f:
                f.write(data)
            result = subprocess.run([STREAM_CHECKER, 'view', tmp], capture_output=True, text=True, timeout=20)
            os.unlink(tmp)
            return sum(1 for l in result.stdout.split('\n') if TAG_PATTERN in l)
        except Exception:
            if i < retries - 1:
                time.sleep(2)
    return -1

def list_primary_objects(store_prefix, build):
    prefix = _store_prefix(store_prefix, build, FLAVOR_PRIMARY)
    url = f"{S3_BASE}/?list-type=2&prefix={prefix}"
    resp = curl(url)
    if resp is None:
        return None
    root = ET.fromstring(resp)
    ns = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""
    return [item.find(f"{ns}Key").text for item in root.findall(f"{ns}Contents")
            if item.find(f"{ns}Key") is not None and item.find(f"{ns}Key").text
            and item.find(f"{ns}Key").text.endswith(SUFFIX)]

_DIR_INDEX = None
_OBJECT_INDEX = None

def _walk_all(base):
    seen = set()
    for root, dirs, files in os.walk(base, followlinks=True):
        rp = os.path.realpath(root)
        if rp in seen:
            dirs[:] = []
            continue
        seen.add(rp)
        yield root, dirs, files

def _object_index():
    global _OBJECT_INDEX
    if _OBJECT_INDEX is None:
        idx = {}
        for base in DATA_ROOTS:
            if not os.path.isdir(base):
                continue
            for root, _dirs, files in _walk_all(base):
                for x in files:
                    if x.endswith(SUFFIX):
                        idx.setdefault(x, []).append(os.path.join(root, x))
        _OBJECT_INDEX = idx
    return _OBJECT_INDEX

def _container_tail_ok(local_path, win=65536):
    EOF = CONTAINER_TRAILER
    try:
        sz = os.path.getsize(local_path)
        with open(local_path, 'rb') as f:
            f.seek(max(0, sz - win))
            return EOF in f.read()
    except OSError:
        return False

def _landed(local_path):
    if any(local_path.startswith(sr) for sr in SHARED_ROOTS):
        return _container_tail_ok(local_path)
    return (not os.path.exists(local_path + '.aria2')
            and not os.path.exists(local_path + '.download.json'))

def _object_exists_anywhere(basename):
    return any(_landed(p) for p in _object_index().get(basename, []))

def _stamp_bad(local_path):
    try:
        with open(local_path + '.verified.json') as f:
            st = json.load(f)
    except Exception:
        return False
    return not str(st.get('result', 'OK')).startswith('OK')


def already_downloaded(store_prefix):
    for _bn in _object_index():
        if store_prefix in _bn and _object_exists_anywhere(_bn):
            return True
    for d, path in _dir_index_by_dir(store_prefix):
        files = os.listdir(path)
        objects = [x for x in files if x.endswith(SUFFIX)]
        partial = [x for x in files if x.endswith(('.aria2', '.download.json'))]
        good = [x for x in objects if not _stamp_bad(os.path.join(path, x))]
        if good and not partial:
            return True
    return False

_DIR_BY_NAME = None

def _dir_index_by_dir(store_prefix):
    global _DIR_BY_NAME
    if _DIR_BY_NAME is None:
        m = {}
        for base in DATA_ROOTS:
            if not os.path.isdir(base):
                continue
            for root, dirs, _files in _walk_all(base):
                for d in dirs:
                    m.setdefault(d, []).append(os.path.join(root, d))
        _DIR_BY_NAME = m
    out = []
    for d, paths in _DIR_BY_NAME.items():
        if store_prefix in d:
            out.extend((d, p) for p in paths)
    return out

# Tag markers worth preferring, in order.  Site-specific, so config-driven.
PREFERRED_TAGS = CFG.get_list('endpoints.preferred_tags', ['tagged', 'primary'])

def pick_primary_object(keys):
    for tag in PREFERRED_TAGS:
        for k in [x for x in keys if tag in x]:
            if probe_tag_header(k) > 0:
                return k
    return None

def list_secondary_objects(store_prefix, build):
    prefix = _store_prefix(store_prefix, build, FLAVOR_SECONDARY)
    out = []
    token = ''
    while True:
        url = f"{S3_BASE}/?list-type=2&prefix={prefix}&max-keys=1000"
        if token:
            url += '&continuation-token=' + urllib.parse.quote(token, safe='')
        resp = curl(url, timeout=40)
        if resp is None:
            return None if not out else out
        try:
            root = ET.fromstring(resp)
        except ET.ParseError:
            return None if not out else out
        ns = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""
        for c in root.findall(f"{ns}Contents"):
            k = c.find(f"{ns}Key"); s = c.find(f"{ns}Size")
            if k is not None and k.text and k.text.endswith(SUFFIX) and s is not None:
                out.append((k.text, int(s.text)))
        nt = root.find(f"{ns}NextContinuationToken")
        token = nt.text if (nt is not None and nt.text) else ''
        if not token:
            break
    out.sort(key=lambda x: -x[1])
    return out

# Header markers that separate reader generations, most preferred first.
# Configured because they are instrument-specific strings.
GEN_PATTERNS = CFG.get_list('endpoints.generation_patterns',
                            ['generation-2', 'generation-1'])

def _generation_of(header):
    for i, pat in enumerate(GEN_PATTERNS):
        if re.search(pat, header, re.I):
            return i
    return None

def probe_object(object_key, retries=2):
    """Return (declares_tag, generation_rank) for an object, or (None, None)."""
    url = f"{S3_BASE}/{object_key}"
    for i in range(retries):
        try:
            req = urllib.request.Request(url)
            req.add_header('Range', 'bytes=0-204800')
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
            tmp = tempfile.mktemp(suffix='.bin')
            with open(tmp, 'wb') as f:
                f.write(data)
            recs = subprocess.run([STREAM_CHECKER, 'view', tmp], capture_output=True, text=True, timeout=40).stdout
            hdr = subprocess.run([STREAM_CHECKER, 'view', '-H', tmp], capture_output=True, text=True, timeout=40).stdout
            os.unlink(tmp)
            has = TAG_PATTERN in recs
            return has, _generation_of(hdr)
        except Exception:
            if i < retries - 1:
                time.sleep(2)
    return None, None

def _object_partial(outdir, base):
    for d in ([outdir] + ([STAGE_DIR] if STAGE_DIR else [])):
        for cand in (os.path.join(d, base), os.path.join(d, base, base)):
            if os.path.exists(cand + '.aria2') or os.path.exists(cand + '.download.json'):
                return True
    return False

def list_raw_objects(store_prefix, build):
    prefix = _store_prefix(store_prefix, build, FLAVOR_SECONDARY)
    resp = curl(f"{S3_BASE}/?list-type=2&prefix={prefix}&max-keys=1000", timeout=40)
    if not resp:
        return []
    try:
        root = ET.fromstring(resp)
    except ET.ParseError:
        return []
    ns = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""
    out = []
    for c in root.findall(f"{ns}Contents"):
        k = c.find(f"{ns}Key")
        if k is not None and k.text and os.path.basename(k.text).endswith('.raw'):
            out.append(os.path.basename(k.text))
    return out

def s3_content_length(object_key, timeout=30, retries=2):
    url = f"{S3_BASE}/{object_key}"
    for i in range(retries):
        try:
            req = urllib.request.Request(url, method='HEAD', headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                v = r.headers.get('Content-Length')
                return int(v) if v else None
        except Exception:
            if i < retries - 1:
                time.sleep(2)
    return None

def _try_rm(p):
    try:
        os.remove(p)
    except OSError:
        pass

def _looks_partial(path):
    try:
        sz = os.path.getsize(path)
        with open(path, 'rb') as f:
            f.seek(max(0, sz - 4096))
            tail = f.read(4096)
        return tail.strip(b'\x00') == b''
    except OSError:
        return False

def _quarantine(path):
    q = f"{path}.corrupt-{int(time.time())}"
    try:
        os.rename(path, q)
        return q
    except OSError:
        return path

_integrity_warned = [False]

def _integrity_ok(path, rec=None):
    """Run the optional external integrity checker on a finished file.

    The checker is a separate program that is not part of this package.  When it
    is not configured the file is accepted on the strength of the transfer-time
    checks alone -- and that is printed once, so it can never be mistaken for a
    verification that actually ran.

    ``rec``, when given, routes the run through the per-task recorder so its
    output lands in that task's log the way a download's does.
    """
    if not INTEGRITY or not os.path.exists(INTEGRITY):
        if not _integrity_warned[0]:
            print("[verify] no external integrity checker configured; accepting "
                  "transfer-time checks only", flush=True)
            _integrity_warned[0] = True
        return True
    try:
        if rec is None:
            return subprocess.run(['bash', INTEGRITY, path],
                                  capture_output=True, text=True).returncode == 0
        return _run_capture(['bash', INTEGRITY, path], rec, 'verify')[0] == 0
    except OSError:
        return True

def find_holes(path):
    if not HOLE_SCANNER or not os.path.exists(HOLE_SCANNER):
        return []
    r = subprocess.run(['python3', HOLE_SCANNER, path], capture_output=True, text=True)
    return [(int(a), int(b)) for a, b in re.findall(r'HOLE\s+(\d+)\s*\.\.\s*(\d+)', r.stdout)]

def repair_holes(path, object_key, sp):
    if not RANGE_PATCHER or not os.path.exists(RANGE_PATCHER):
        print(f"    {sp}: zero-run found but no range patcher configured; "
              f"leaving for manual review", flush=True)
        return False
    holes = find_holes(path)
    if not holes:
        print(f"    {sp}: integrity failed but NO zero-holes found (not a 卡洞); leaving for manual review", flush=True)
        return False
    size = os.path.getsize(path)
    url = f"{S3_BASE}/{object_key}"
    print(f"    {sp}: 卡洞 detected ({len(holes)} hole(s)); auto-repairing", flush=True)
    for s, e in holes:
        ps = max(0, s - HOLE_MARGIN); pe = min(size - 1, e + HOLE_MARGIN)
        print(f"    {sp}: patching {ps}..{pe} ({(pe-ps+1)/1e9:.1f} GB)", flush=True)
        subprocess.run(['python3', RANGE_PATCHER, path, url, str(ps), str(pe)])
    return len(find_holes(path)) == 0

_refresh_lock = threading.Lock()

STATUS_COLLECTOR = CFG.get('tools.status_collector', '')

def _refresh_status():
    if not STATUS_COLLECTOR or not os.path.exists(STATUS_COLLECTOR):
        return
    if not _refresh_lock.acquire(blocking=False):
        return
    try:
        subprocess.run(['python3', STATUS_COLLECTOR],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
    except Exception:
        pass
    finally:
        _refresh_lock.release()

INVENTORY_REFRESH = CFG.get('tools.inventory_refresh', '')
INVENTORY_CHECK_INTERVAL = CFG.get_int('ops.inventory_check_interval', 600)
_inv_last = [0.0]

def _refresh_inventory_auto():
    if not INVENTORY_REFRESH or not os.path.exists(INVENTORY_REFRESH):
        return
    now = time.time()
    if now - _inv_last[0] < INVENTORY_CHECK_INTERVAL:
        return
    _inv_last[0] = now
    try:
        subprocess.Popen(['python3', INVENTORY_REFRESH, '--auto', '--detach'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except Exception:
        pass

def _read_manifest(path):
    with open(path) as f:
        return list(csv.DictReader(f))

rows = _read_manifest(MANIFEST)

verbatim_info = {}
if MANIFEST_ALT and os.path.exists(MANIFEST_ALT):
    for row in _read_manifest(MANIFEST_ALT):
        verbatim_info[row['name']] = row

def _group_of(row):
    g = (row.get('group') or '').strip()
    if g:
        return g
    nm = (row.get('name') or '').strip()
    return nm.split()[0] if nm else ''

# Where a finished item lands.  A site convention, so it is a template.
OUT_TEMPLATE = CFG.get('paths.outdir_template', '{outdir}/{tier}/{name}_{key}')

def _outdir_for(row, base):
    return OUT_TEMPLATE.format(
        outdir=base,
        group=_group_of(row),
        tier=(row.get('tier') or 'misc').strip() or 'misc',
        name=(row.get('name') or '').strip().replace(' ', '_'),
        key=(row.get('key') or '0').strip() or '0')

def _sort_key(t):
    tier = 0 if t[5] in PRIORITY_GROUPS else 1
    return (tier, PLATFORM_RANK.get(t[4], 9))

tasks6 = []

landed_names = {_fn for _fn, _paths in _object_index().items()
                if any(_landed(_p) for _p in _paths)}

for row in rows:
    sp = (row.get('name') or '').strip()
    if not sp:
        continue
    if row.get('tag_status') != 'TAGGED':
        continue
    build = (row.get('build') or '').strip()
    if not build or build == 'NA':
        continue
    store_prefix = sp.replace(' ', '_')
    group = _group_of(row)
    if not already_downloaded(store_prefix):
        tasks6.append((sp, 'SOURCE_A', (store_prefix, build, None), _outdir_for(row, OUTDIR), 'alpha', group))
    if group in PRIORITY_GROUPS:
        secondary = list_secondary_objects(store_prefix, build)
        if secondary:
            sec_out = _outdir_for(row, OUTDIR_ALT)
            pending = []
            for key, _sz in secondary:
                _bn = os.path.basename(key)
                if _bn in landed_names:
                    pending = []
                    break
                pending.append((key, _object_partial(sec_out, _bn)))
            if pending:
                probed = []
                for k, _p in pending[:5]:
                    _tags, _gen = probe_object(k)
                    probed.append((k, _p, _tags, _gen))
                probed += [(k, p, None, None) for k, p in pending[5:]]
                bad = [x for x in probed if x[2] is False]
                ok = [x for x in probed if x[2] is not False]
                if bad:
                    print(f"[TAG-DIFF] {sp}: {', '.join(os.path.basename(x[0]) for x in bad)} "
                          f"declare no tag in the first 200 KB; skipped this run", flush=True)
                if ok:
                    ok.sort(key=lambda x: (x[3] if x[3] is not None else 99,
                                           0 if x[1] else 1,
                                           -dict(secondary).get(x[0], 0)))
                    pick, _is_part, _tags, _gen = ok[0]
                    print(f"[SECONDARY] {sp}: {len(secondary)} object(s) upstream -> picked "
                          f"{os.path.basename(pick)} ({dict(secondary).get(pick, 0)/1e9:.1f} GB"
                          f"{'，续传半成品' if _is_part else ''})", flush=True)
                    tasks6.append((sp, 'SOURCE_A', (store_prefix, build, pick), sec_out, 'beta', group))
                else:
                    _sig = list_raw_objects(store_prefix, build)
                    if _sig:
                        _hint = f"；同前缀另有原始流文件（{', '.join(_sig[:3])}），可自行转换后再入队"
                    else:
                        _hint = "；同前缀也没有原始流文件"
                    print(f"[TAG-DIFF] {sp}: none of the candidates declare a tag, skipping{_hint}", flush=True)
            else:
                print(f"[SECONDARY] {sp}: already complete on disk, skipping", flush=True)

if MANIFEST_ALT and os.path.exists(MANIFEST_ALT):
    for row in rows:
        sp = (row.get('name') or '').strip()
        e = verbatim_info.get(sp, {})
        tag = e.get('tag_status', '')
        if tag not in ('TAGGED', 'PLAIN'):
            continue
        if already_downloaded(sp.replace(' ', '_')):
            continue
        url = e.get('url', '')
        if url.startswith('mirror') or url.startswith('ftp.'):
            url = 'https://' + url
        elif url.startswith('ftp://'):
            url = url.replace('ftp://', 'https://')
        if not url.startswith('http'):
            continue
        tasks6.append((sp, 'SOURCE_B', url, _outdir_for(row, OUTDIR), 'gamma', _group_of(row)))

tasks6.sort(key=_sort_key)
tasks = [t[:5] for t in tasks6]

SKIP_SUBSTR = [s for s in os.environ.get('SKIP_SUBSTR', '').split(',') if s]
if SKIP_SUBSTR:
    _before = len(tasks)
    tasks = [t for t in tasks if not any(s in t[0] for s in SKIP_SUBSTR)]
    print(f"SKIP_SUBSTR 排除 {_before - len(tasks)} 个任务: {SKIP_SUBSTR}", flush=True)

print(f"Total tasks: {len(tasks)} (alpha: {sum(1 for t in tasks if t[4]=='alpha')}, "
      f"beta: {sum(1 for t in tasks if t[4]=='beta')}, "
      f"gamma: {sum(1 for t in tasks if t[4]=='gamma')})", flush=True)
print(f"下载配置: WIN_LINES={WIN_LINES}, WIN_WORKERS={WIN_WORKERS}, LOCAL_LINES={LOCAL_LINES}, "
      f"WIN_GATE={WIN_GATE or '无'}, WIN_PROBE_INTERVAL={WIN_PROBE_INTERVAL}s, WIN_CONNECTIONS={WIN_CONNECTIONS}"
      + (f" | SSD中转={STAGE_DIR} (高水位{STAGE_HIGH_GB:.0f}GB/低{STAGE_LOW_GB:.0f}GB, 单文件≤{STAGE_MAX_FILE_GB:.0f}GB)"
         if STAGE_DIR else ""), flush=True)

if os.environ.get('DRY_RUN'):
    for i, t in enumerate(tasks):
        sp, source, payload, outdir, platform = t
        print(f"  [{i:3d}] {platform:5s} {source:10s} {sp}", flush=True)
    print("DRY_RUN: exiting (no downloads, no object-store listing)", flush=True)
    sys.exit(0)

if os.environ.get('DL_CTL_SELFTEST'):
    try:
        import dl_control as _c
        _drv = DRIVER_NAME
        _ctlpath = os.environ.get('DL_CTL_FILE', os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                              'control', _drv + '.json'))
        _spec = _c.load_ctl(_ctlpath)
        print(f"[selftest] 控制文件 {_ctlpath}\n[selftest]   {_c.summarize(_spec)}", flush=True)
        if _spec:
            _vok, _vmsg = _c.validate(_spec)
            print(f"[selftest] 校验: {'通过' if _vok else '不通过 → ' + _vmsg}", flush=True)
            if _vok:
                _r = _c.apply_ops(tasks, _spec.get('ops') or [])
                print(f"[selftest] 队列预演: {len(tasks)} → {len(_r['pending'])} 个任务", flush=True)
                for _rep in _r['reports']:
                    print(f"[selftest]   · {_rep}", flush=True)
        print("[selftest] 结构自检: " + ("通过" if all(_c.payload_ok(t) for t in tasks[:200]) else "有畸形任务"),
              flush=True)
    except Exception as _e:
        print(f"[selftest] 失败: {type(_e).__name__}: {_e}", flush=True)
    print("DL_CTL_SELFTEST: exiting", flush=True)
    sys.exit(0)

def _pid_guard_or_die():
    if os.environ.get('DL_NO_PIDGUARD') == '1':
        return
    me = os.getpid()
    pf = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'state', _pg_name() + '.pid')
    try:
        if os.path.exists(pf):
            old = int((open(pf).read().strip() or '0'))
            if old and old != me and _pid_is_driver(old):
                print(f"[pidguard] ✗ 同一 driver({_pg_name()}) 已有活实例 pid={old}（pidfile {pf}）——"
                      f"本进程(pid={me})退出，绝不并发跑两个 driver。\n"
                      f"[pidguard]   要重启请用 restart_driver_ds4_1.sh（它会先停旧实例）；"
                      f"确知要跳过本闸可设 DL_NO_PIDGUARD=1", flush=True)
                sys.exit(1)
        os.makedirs(os.path.dirname(pf), exist_ok=True)
        with open(pf, 'w') as f:
            f.write(str(me))
    except SystemExit:
        raise
    except Exception as e:
        print(f"[pidguard] 自检异常（不阻断）：{type(e).__name__}: {e}", flush=True)


def _pg_name():
    return DRIVER_NAME


def _pid_is_driver(pid):
    try:
        with open('/proc/%d/cmdline' % pid, 'rb') as f:
            cl = f.read().decode('utf-8', 'replace')
        return 'dl_all_priority' in cl
    except Exception:
        return False


def _stage_used_gb(exclude=None):
    return _dir_used_gb(STAGE_DIR, exclude=exclude)

def _dir_used_gb(root, exclude=None):
    tot = 0
    ex = os.path.abspath(exclude) if exclude else None
    for r, ds, fs in os.walk(root):
        if ex and os.path.abspath(r) == ex:
            ds[:] = []
            continue
        for f in fs:
            if '.corrupt-' in f:
                continue
            try:
                tot += os.stat(os.path.join(r, f)).st_blocks * 512
            except OSError:
                pass
    return tot / 1e9

def _stream_reserve_gb():
    win = max(STREAM_WINDOW, (STREAM_CONNS + 2) * STREAM_CHUNK)
    return (win + (STREAM_CONNS + 1) * STREAM_CHUNK) / 1e9

def _admit_stream(own_dir=None):
    reserve = _stream_reserve_gb()
    while True:
        used = max(_stage_used_gb(exclude=own_dir), 0.0)
        if used + reserve <= STAGE_HIGH_GB:
            return
        print(f"[stream] 准入等待：他方占用 {used:.1f} GB + 本文件预留 {reserve:.2f} GB > "
              f"{STAGE_HIGH_GB:.0f} GB", flush=True)
        time.sleep(60)

def _stage_gate():
    while True:
        used = _stage_used_gb()
        if used <= STAGE_HIGH_GB:
            break
        print(f"[stage] SSD 中转占用 {used:.1f} GB > {STAGE_HIGH_GB:.0f} GB，暂停取任务等待腾空", flush=True)
        while _stage_used_gb() > STAGE_LOW_GB:
            time.sleep(30)
    _stage_supervise()
    _gc_windows()

def _stage_supervise():
    try:
        ssd = os.path.dirname(STAGE_DIR.rstrip('/')) or STAGE_DIR
        mine = _stage_used_gb()
        total = _dir_used_gb(ssd)
        other = total - mine
        if mine > STAGE_HIGH_GB:
            print(f"[quota] ⚠️ 本线 SSD 占用 {mine:.1f} GB 超上限 {STAGE_HIGH_GB:.0f} GB", flush=True)
        if total > 150:
            print(f"[quota] ⚠️ SSD_tmp 总量 {total:.1f} GB 超硬上限 150 GB"
                  f"（本线 {mine:.1f} GB + 他方 {other:.1f} GB）——只报不删，请人工处理", flush=True)
    except Exception:
        pass

def _gc_windows(max_age_h=24):
    now = time.time()
    try:
        entries = [e for e in os.listdir(STAGE_DIR)
                   if os.path.isdir(os.path.join(STAGE_DIR, e))]
    except Exception:
        return
    if not entries:
        return
    try:
        ps = subprocess.run(['pgrep', '-af', 'linux_downloader.py'],
                            capture_output=True, text=True).stdout or ''
    except Exception:
        return
    for e in entries:
        p = os.path.join(STAGE_DIR, e)
        try:
            mt = os.path.getmtime(p)
            for f in os.listdir(p):
                mt = max(mt, os.path.getmtime(os.path.join(p, f)))
        except OSError:
            continue
        if now - mt < max_age_h * 3600 or e in ps:
            continue
        shutil.rmtree(p, ignore_errors=True)
        print(f"[stream] 清理孤儿窗口 {e}（>{max_age_h}h 无活动进程）", flush=True)

def _stream_eligible(final):
    sc = final + '.download.json'
    if os.path.exists(sc):
        try:
            with open(sc) as f:
                old = json.load(f)
        except Exception:
            return False
        return old.get('mode') == 'stream'
    if os.path.exists(final):
        return False
    if os.path.exists(final + '.aria2') or os.path.exists(final + '.partial'):
        return False
    if os.path.isfile(os.path.join(STAGE_DIR, os.path.basename(final))):
        return False
    _sub = os.path.join(STAGE_DIR, os.path.basename(final))
    if os.path.isdir(_sub):
        _nm = os.path.basename(final)
        if os.path.exists(os.path.join(_sub, _nm + '.aria2')) or \
           os.path.exists(os.path.join(_sub, '.ready.json')):
            return False
    return True

def _drop_stream_residue_for_old_path(final, quiet_s=120):
    sc = final + '.download.json'
    if not os.path.exists(sc):
        return
    try:
        with open(sc) as f:
            if json.load(f).get('mode') != 'stream':
                return
    except Exception:
        return
    name = os.path.basename(final)
    wdir = os.path.join(STAGE_DIR, name)
    wfile = os.path.join(wdir, name)
    if os.path.exists(os.path.join(wdir, '.ready.json')) or os.path.exists(wfile + '.aria2'):
        return
    try:
        if os.path.isfile(wfile) and time.time() - os.path.getmtime(wfile) < quiet_s:
            return
    except OSError:
        pass
    print(f"[stage] {name}: 回滚场景——清掉流式残留（可丢弃窗口 + NFS .partial + stream sidecar），"
          f"老路重下", flush=True)
    if os.path.isdir(wdir):
        shutil.rmtree(wdir, ignore_errors=True)
    for p in (final + '.partial', sc):
        try:
            if os.path.exists(p):
                os.unlink(p)
        except OSError as e:
            print(f"[stage] {name}: 删 {os.path.basename(p)} 失败: {e}", flush=True)

def _run_stream(sp, final, url, md5=None, tag='driver:stream'):
    _admit_stream(own_dir=os.path.join(STAGE_DIR, os.path.basename(final)))
    cmd = ['python3', WIN_DOWNLOADER, url, '-o', final, '--direct', '--stream',
           '--window-dir', STAGE_DIR, '--connections', str(STREAM_CONNS),
           '--chunk-size', str(STREAM_CHUNK), '--window-max', str(STREAM_WINDOW)]
    if md5 and ';' not in md5:
        cmd += ['--md5', md5]
    print(f"[stream] {sp}: 开下 → 窗口 {STAGE_DIR}（上限 {STREAM_WINDOW/2**30:.2f} GiB，"
          f"预留 {_stream_reserve_gb():.2f} GB）", flush=True)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=None)
    if r.returncode != 0 or not os.path.exists(final):
        full = (r.stdout or '') + (r.stderr or '')
        return (False, sp, "stream download failed: " + _dl_err_tail(full, r.returncode)[:200])
    if not _integrity_ok(final):
        q = _quarantine(final)
        return (False, sp, f"CORRUPT after stream download: {os.path.basename(final)}"
                           f" → 已隔离 {os.path.basename(q)}")
    if _ledger:
        try:
            _ledger.record(final, 'OK', '流式下载(边下边传, 下载中逐member CRC+零串) + 落位后全量CRC', tag)
        except Exception:
            pass
    return (True, sp, f"OK (stream, {os.path.getsize(final)/1e9:.1f} GB)")

def _admit_stage(size_bytes, own_path=None):
    own_gb = 0.0
    if own_path:
        try:
            own_gb = os.path.getsize(own_path) / 1e9
        except OSError:
            own_gb = 0.0
    while True:
        used = max(_stage_used_gb() - own_gb, 0.0)
        cur = size_bytes / 1e9
        if used + cur <= STAGE_HIGH_GB:
            return
        print(f"[stage] 准入等待：他方占用 {used:.1f} GB + 本文件 {cur:.1f} GB > "
              f"{STAGE_HIGH_GB:.0f} GB，等 mover 排空", flush=True)
        time.sleep(60)

def http_content_length(url, timeout=30):
    try:
        r = subprocess.run(['curl', '-sIL', '--max-time', str(timeout), url],
                           capture_output=True, text=True)
        for line in reversed((r.stdout or '').splitlines()):
            if line.lower().startswith('content-length:'):
                return int(line.split(':', 1)[1].strip())
    except Exception:
        pass
    return None

def run_one(task):
    sp, source, payload, outdir, platform = task
    try:
        os.makedirs(outdir, exist_ok=True)
        downloaded = []
        stage_info = None
        def _finish_ok(msg):
            if stage_info:
                src, dst = stage_info
                with open(os.path.join(os.path.dirname(src), '.ready.json'), 'w') as f:
                    json.dump({'src': src, 'dst': dst, 'item': sp,
                               'size': os.path.getsize(src), 'ts': time.time()}, f)
                return (True, sp, f"{msg}（已落 SSD {os.path.getsize(src)/1e9:.1f} GB，待 mover 上传）")
            return (True, sp, msg)
        if source == 'SOURCE_B':
            url = payload
            fname = os.path.basename(url)
            final_path = os.path.join(outdir, fname)
            local_path = final_path
            if STREAM_LOCAL and STAGE_DIR and _stream_eligible(final_path):
                _e = verbatim_info.get(sp, {}) or {}
                _md5 = _e.get('md5') or _e.get('md5sum')
                r = _run_stream(sp, final_path, url, md5=_md5, tag='driver:stream-verbatim')
                if r[0]:
                    return r
                if '已隔离' in (r[2] or '') or os.path.exists(final_path + '.download.json'):
                    return r
                print(f"[stream] {sp} SOURCE_B 流式失败且无残留，回落 dl_block.sh: {r[2][:160]}", flush=True)
            if STAGE_DIR:
                _drop_stream_residue_for_old_path(final_path)
            _blkdir = os.path.join(outdir, f".blocks_{fname}")
            r = subprocess.run(['bash', os.path.join(HERE, 'dl_block.sh'),
                                url, local_path, '100', _blkdir],
                               capture_output=True, text=True, timeout=None)
            ok = r.returncode == 0 and os.path.exists(local_path)
            downloaded = [local_path]
        else:
            store_prefix, build, object_key = payload
            if platform == 'beta':
                best = object_key
                prefix = _store_prefix(store_prefix, build, FLAVOR_SECONDARY)
            else:
                keys = list_primary_objects(store_prefix, build)
                if keys is None:
                    return (False, sp, "store listing failed (transient, will retry next run)")
                if not keys:
                    return (False, sp, "key prefix empty (no object to fetch)")
                best = pick_primary_object(keys)
                if not best:
                    return (False, sp, "no tagged object upstream (objects present, tag probe failed)")
                prefix = _store_prefix(store_prefix, build, FLAVOR_PRIMARY)
            _final = os.path.join(outdir, os.path.basename(best))
            _use_stream = bool(STREAM_LOCAL and STAGE_DIR and _stream_eligible(_final))
            best_path = _final
            if (os.path.exists(best_path) and os.path.getsize(best_path) > 0
                    and not _looks_partial(best_path)):
                _cl = s3_content_length(best)
                if _cl is not None and os.path.getsize(best_path) == _cl:
                    if _integrity_ok(best_path):
                        _try_rm(best_path + '.aria2')
                        if _ledger:
                            try: _ledger.record(best_path, 'OK', '字节完整件复核(驱动内)', 'driver:verify')
                            except Exception: pass
                        return _finish_ok("OK (byte-complete, verified; no download needed)")
                    repair_holes(best_path, best, sp)
                    if _integrity_ok(best_path):
                        _try_rm(best_path + '.aria2')
                        if _ledger:
                            try: _ledger.record(best_path, 'OK', '卡洞修复后复核(驱动内)', 'driver:repair')
                            except Exception: pass
                        return _finish_ok("OK (卡洞 auto-repaired + verified)")
                    return (False, sp, f"卡洞 repair FAILED (no clean recheck): {os.path.basename(best_path)}")
            if _use_stream:
                r = _run_stream(sp, _final, f"{S3_BASE}/{best}")
                if r[0]:
                    return r
                if '已隔离' in (r[2] or '') or os.path.exists(_final + '.download.json'):
                    return r
                print(f"[stream] {sp} 失败且无残留半成品，回落老路(aria2c 直下 NFS): {r[2][:160]}", flush=True)
            if not TOOL:
                return (False, sp, "tools.s3_prefix_downloader is not configured; "
                                   "set it in site.json to fetch through the object store")
            _drop_stream_residue_for_old_path(_final)
            url = f"{S3_BASE}/index.html?prefix={prefix}"
            pattern = re.escape(os.path.basename(best)) + '$'
            cmd = ["python3", TOOL, url, os.path.dirname(best_path), "--path-regex", pattern,
                   "-j", "4", "-x", "8", "-s", "8", "--no-recursive"]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=None)
            ok = r.returncode == 0
            downloaded = [best_path]

        if ok:
            for path in downloaded:
                if not os.path.exists(path):
                    continue
                if not _integrity_ok(path):
                    q = _quarantine(path)
                    return (False, sp, f"CORRUPT after download: {os.path.basename(path)} → 已隔离 {os.path.basename(q)}")
            if _ledger and not stage_info:
                for path in downloaded:
                    if os.path.exists(path):
                        try:
                            _ledger.record(path, 'OK', '下载后全量CRC(驱动内)', 'driver:local')
                        except Exception:
                            pass
            return _finish_ok("OK")
        return (False, sp, "download failed")
    except Exception as e:
        return (False, sp, str(e)[:150])

def _run_capture(cmd, rec=None, phase='download'):
    if rec is None:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=None)
        return r.returncode, r.stdout or '', r.stderr or ''
    if phase == 'download':
        rec['phase'] = 'download'
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        rec['proc'] = p
        try:
            out, err = p.communicate()
        finally:
            rec['proc'] = None
            rec['phase'] = None
        return p.returncode, out or '', err or ''
    rec['phase'] = 'verify'
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=None)
        return r.returncode, r.stdout or '', r.stderr or ''
    finally:
        rec['phase'] = None


def run_one_windows(task, worker, rec=None):
    if WIN_GATE:
        t0 = time.time()
        while not os.path.exists(WIN_GATE) and time.time() - t0 < WIN_GATE_MAX_WAIT:
            if _run.is_set() or (rec is not None and rec['retire'].is_set()):
                return (False, task[0], "线回收：WIN_GATE 未出现")
            if rec is not None:
                rec['retire'].wait(30)
            else:
                time.sleep(30)
        if not os.path.exists(WIN_GATE):
            print(f"WARN: WIN_GATE({WIN_GATE}) 等待超时 {WIN_GATE_MAX_WAIT}s，windows 线照常开始", flush=True)
    sp, source, payload, outdir, platform = task
    try:
        os.makedirs(outdir, exist_ok=True)
        md5 = None
        key = None
        if source == 'SOURCE_B':
            url = payload
            outpath = os.path.join(outdir, os.path.basename(url))
            e = verbatim_info.get(sp, {}) or {}
            md5 = e.get('md5') or e.get('md5sum')
        else:
            store_prefix, build, object_key = payload
            if platform == 'beta':
                key = object_key
            else:
                keys = list_primary_objects(store_prefix, build)
                if keys is None:
                    return (False, sp, "store listing failed (transient, will retry next run)")
                if not keys:
                    return (False, sp, "key prefix empty (no object to fetch)")
                best = pick_primary_object(keys)
                if not best:
                    return (False, sp, "no tagged object upstream (tag probe failed)")
                key = best
            url = f"{S3_BASE}/{key}"
            outpath = os.path.join(outdir, os.path.basename(key))
        if os.path.exists(outpath + '.download.json'):
            pass
        elif key is not None and os.path.exists(outpath) and os.path.getsize(outpath) > 0:
            _cl = s3_content_length(key)
            if _cl is not None and os.path.getsize(outpath) == _cl:
                if _integrity_ok(outpath, rec):
                    _try_rm(outpath + '.aria2')
                    if _ledger:
                        try:
                            _ledger.record(outpath, 'OK', '下载后全量CRC(windows线)', 'driver:windows')
                        except Exception:
                            pass
                    return (True, sp, "OK (windows: byte-complete, verified)")
                return (False, sp, f"windows: 落地文件未过完整性校验(疑卡洞)，交本地/repair: {os.path.basename(outpath)}")
        cmd = ['python3', WIN_DOWNLOADER, url, '-o', outpath, '--connections', str(WIN_CONNECTIONS), '--worker', worker]
        if md5 and ';' not in md5:
            cmd += ['--md5', md5]
        _rc, _out, _err = _run_capture(cmd, rec, 'download')
        if _rc != 0:
            full = _out + _err
            return (False, sp, "windows download failed: " + _dl_err_tail(full, _rc)[:200])
        if not _integrity_ok(outpath, rec):
            return (False, sp, f"CORRUPT after windows download: {os.path.basename(outpath)}")
        _try_rm(outpath + '.aria2')
        if _ledger:
            try:
                _ledger.record(outpath, 'OK', '下载后全量CRC(windows线)', 'driver:windows')
            except Exception:
                pass
        return (True, sp, "OK (windows)")
    except Exception as e:
        return (False, sp, str(e)[:150])

_lock = threading.Lock()
_pending = list(tasks)
completed = 0

_log_lock = threading.Lock()

os.environ.setdefault('DL_CLIENT', DRIVER_NAME)
STATUS_FILE = os.environ.get('DRIVER_STATUS_FILE',
                             os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          'state', 'driver_%s.json' % DRIVER_NAME))
RANKS_FILE = os.environ.get('DRIVER_RANKS_FILE',
                            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         'state', 'driver_%s.ranks.json' % DRIVER_NAME))
STATUS_INTERVAL = float(os.environ.get('DRIVER_STATUS_INTERVAL', '15'))
_line_state = {}
_events = []

def _line_mark(key, val=None):
    with _lock:
        if val is None:
            _line_state.pop(key, None)
        else:
            _line_state[key] = val

def _event(kind, sp, backend, msg=''):
    with _lock:
        _events.append({'ts': time.strftime('%Y-%m-%d %H:%M:%S'), 'kind': kind,
                        'item': sp, 'backend': backend, 'msg': (msg or '')[:160]})
        del _events[:-40]

def _status_snapshot():
    try:
        with _pool.cv:
            alive, busy = sorted(_pool.alive), sorted(_pool.busy)
    except Exception:
        alive, busy = [], []
    with _lock:
        return {'name': DRIVER_NAME, 'owner': OWNER, 'pid': os.getpid(),
                'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
                'total': len(tasks), 'completed': completed, 'queue_depth': len(_pending),
                'lines': [dict(v) for v in _line_state.values()],
                'events': list(_events[-20:]),
                'workers': {'alive': alive, 'busy': busy,
                            'fail_streak': dict(_worker_fail_streak)},
                'stage_gb': (round(_stage_used_gb(), 1) if STAGE_DIR else None),
                'ctl': {'file': CTL_FILE, 'rev_applied': _ctl_rev_applied,
                        'rev_file': (_ctl_spec or {}).get('rev'),
                        'win_lines_desired': _ctl_desired, 'win_lines_live': len(_lines),
                        'max': WIN_LINES_MAX, 'paused': _ctl_paused.is_set(),
                        'skip': list(_ctl_skip), 'now': _ctl_now,
                        'last_ops': list(_ctl_last_ops), 'last_ops_rev': _ctl_last_ops_rev,
                        'ok': _ctl_ok,
                        'caps': (list(_ctlmod.ALL_CAPS) if _ctlmod is not None else []),
                        'avoid_ports': sorted(_ctl_avoid_manual),
                        'policy_avoid_ports': sorted(_ctl_avoid_policy),
                        'avoid_effective': sorted(_ctl_avoid),
                        'broker_max_lines': _ctl_broker_max,
                        'broker_max_lines_live': _ctl_broker_max_live,
                        'broker_fresh': _broker_fields_ok()},
                'mode': {'win_workers': WIN_WORKERS, 'win_lines': WIN_LINES,
                         'stream_local': STREAM_LOCAL,
                         'local_lines': globals().get('NWORKERS')}}

def _write_status_snapshot():
    try:
        d = os.path.dirname(STATUS_FILE)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = STATUS_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(_status_snapshot(), f, ensure_ascii=False)
        os.replace(tmp, STATUS_FILE)
        return True
    except Exception:
        return False


def _status_writer():
    while True:
        _write_status_snapshot()
        _ranks_save()
        _queue_save()
        time.sleep(STATUS_INTERVAL)

def _take(skip=None):
    with _lock:
        if skip is None:
            for i, t in enumerate(_pending):
                if _ctl_takeable(t):
                    return _pending.pop(i)
            return None
        for i, t in enumerate(_pending):
            if not _ctl_takeable(t):
                continue
            try:
                if skip(t):
                    continue
            except Exception as e:
                _ctl_note(f"[ctl] 取任务谓词异常（本线跳过该任务）: {type(e).__name__}: {e}")
                continue
            return _pending.pop(i)
        return None

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import dl_control as _ctlmod
except Exception as _e:
    _ctlmod = None
    print(f"[ctl] dl_control 不可用（运行时控制关闭）：{type(_e).__name__}: {_e}", flush=True)

CTL_FILE = os.environ.get('DL_CTL_FILE', os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                      'control', DRIVER_NAME + '.json'))
CTL_POLL = float(os.environ.get('DL_CTL_POLL', '5'))
WIN_LINES_MAX = max(1, int(os.environ.get('WIN_LINES_MAX', '8')))
CTL_NO_WORK_COOLDOWN = 60.0

_ctl_spec = None
_ctl_rev_applied = -1
_ctl_last_ops = []
_ctl_last_ops_rev = -1
_ctl_ok = True
_ctl_paused = threading.Event()
_ctl_skip = []
_ctl_desired = WIN_LINES
_ctl_now = False
_ctl_avoid = []
_ctl_avoid_manual = []
_ctl_avoid_policy = []
_ctl_broker_max = None
_ctl_broker_max_live = None
BROKER_STATE = os.environ.get('DL_BROKER_STATE',
                              os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                           'state', 'broker_state.json'))
BROKER_TTL = float(os.environ.get('DL_BROKER_TTL', '300'))


def _broker_fields_ok(now=None):
    try:
        return ((time.time() if now is None else now) - os.path.getmtime(BROKER_STATE)) <= BROKER_TTL
    except OSError:
        return False
_kick = threading.Event()
_run = threading.Event()
_lines = {}
_lines_seq = 0
_ctrl_spawn_block_until = 0.0


def _ctl_note(msg):
    with _log_lock:
        print(msg, flush=True)


def _ctl_takeable(t):
    try:
        if _ctl_skip and any(s.lower() in str(t[0]).lower() for s in _ctl_skip):
            return False
        return _ctlmod.payload_ok(t) if _ctlmod else True
    except Exception:
        return True


def _ctl_takeable_any():
    with _lock:
        return any(_ctl_takeable(t) for t in _pending)


_pending_rank = {}
RANK_NEW = 10 ** 6
RANK_NORMAL = (50, '')
RANK_LOW = (0, '')


def _now_iso8():
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(time.time() + 8 * 3600))


def _level_of_item(it):
    try:
        code = it.get('level') or _ctlmod.level_name(it.get('priority'))
        zh = dict((c, z) for c, _v, z, _e in _ctlmod.LEVELS).get(code, '')
        return ('等级 %s·%s' % (code, zh)) if zh else ('等级 %s' % code)
    except Exception:
        return '等级 ?'


def _rank_of(t):
    try:
        return _pending_rank.get(_ctlmod.key_of(t), RANK_NORMAL)
    except Exception:
        return RANK_NORMAL


def _rank_set(t, prio, ts=None):
    try:
        _pending_rank[_ctlmod.key_of(t)] = (int(prio),
                                            str(ts if ts is not None else _now_iso8()))
    except Exception:
        pass


def _resort_pending():
    try:
        _pending.sort(key=_rank_of, reverse=True)
        live = set()
        for t in _pending:
            try:
                live.add(_ctlmod.key_of(t))
            except Exception:
                pass
        for k in [k for k in _pending_rank if k not in live]:
            _pending_rank.pop(k, None)
    except Exception:
        pass


def _rerank_after_ops(res):
    try:
        promoted = list(res.get('promoted') or [])
        added = list(res.get('added') or [])
        if not promoted and not added:
            return
        ts = _now_iso8()
        for k in promoted:
            _pending_rank[k] = (RANK_NEW, ts)
        for k in added:
            _pending_rank[k] = (RANK_NEW, ts) if res.get('added_front') else (RANK_NORMAL[0], ts)
        _resort_pending()
    except Exception:
        pass


def _ranks_load():
    try:
        with open(RANKS_FILE, encoding='utf-8') as f:
            raw = json.load(f)
        out = {}
        for it in (raw.get('ranks') or []):
            k = it.get('k')
            if isinstance(k, (list, tuple)) and len(k) == 2:
                out[(str(k[0]), str(k[1]))] = (int(it.get('p') or 0), str(it.get('t') or ''))
        return out
    except Exception:
        return {}


def _ranks_save():
    try:
        rows = [{'k': [k[0], k[1]], 'p': int(v[0]), 't': str(v[1])}
                for k, v in list(_pending_rank.items())]
        tmp = RANKS_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'v': 1, 'driver': DRIVER_NAME, 'saved': _now_iso8(), 'ranks': rows}, f,
                      ensure_ascii=False)
        os.replace(tmp, RANKS_FILE)
    except Exception:
        pass


QUEUE_FILE = os.environ.get('DRIVER_QUEUE_FILE',
                            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         'state', 'driver_%s.queue.json' % DRIVER_NAME))


def _task_to_json(t):
    return [list(x) if isinstance(x, tuple) else x for x in t]


def _task_from_json(row):
    return tuple(tuple(x) if isinstance(x, list) else x for x in row)


def _queue_save():
    try:
        with _lock:
            rows = [_task_to_json(t) for t in _pending]
            for v in list(_line_state.values()):
                if v.get('task'):
                    rows.append(_task_to_json(v['task']))
        tmp = QUEUE_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'v': 1, 'driver': DRIVER_NAME, 'saved': _now_iso8(),
                       'count': len(rows), 'tasks': rows}, f, ensure_ascii=False)
        os.replace(tmp, QUEUE_FILE)
    except Exception:
        pass


def _queue_load():
    try:
        with open(QUEUE_FILE, encoding='utf-8') as f:
            raw = json.load(f)
        out = []
        for row in (raw.get('tasks') or []):
            if isinstance(row, (list, tuple)) and len(row) == 5:
                out.append(_task_from_json(row))
        return out
    except Exception:
        return []


def _queue_saved_at():
    try:
        with open(QUEUE_FILE, encoding='utf-8') as f:
            return str((json.load(f) or {}).get('saved') or '')
    except Exception:
        return ''


def _line_release(rec, w, ok=True):
    if w is None:
        return
    if rec is not None and rec.get('w') == w:
        rec['w'] = None
    _pool.release(w, ok)


def _ctl_line_retire(rec, now=False):
    rec['retire'].set()
    if now:
        rec['retire_now'] = True


def _ctl_retire_say(rec, msg):
    if rec.get('_retire_said') == msg:
        return
    rec['_retire_said'] = msg
    _ctl_note(msg)


def _kill_proc(rec):
    p = rec.get('proc')
    if p is None:
        return
    for sig, wait_s in (('terminate', 30), ('kill', 30)):
        if p.poll() is not None:
            return
        try:
            getattr(p, sig)()
        except Exception:
            pass
        t0 = time.time()
        while time.time() - t0 < wait_s:
            if p.poll() is not None:
                return
            time.sleep(0.5)
    _ctl_note(f"[ctl] ⚠️ 线 {rec.get('seq')} 子进程 pid={p.pid} 60s 内未终止（等它自己结束）")


def _spawn_line():
    global _lines_seq
    with _lock:
        _lines_seq += 1
        seq = _lines_seq
        rec = {'seq': seq, 'retire': threading.Event(), 'retire_now': False, 'w': None,
               'task': None, 'proc': None, 'phase': None, 'took': 0, 'pend_at_exit': 0}
        _lines[seq] = rec
    th = threading.Thread(target=_win_line_entry, args=(seq, rec), daemon=True, name='win-line-%d' % seq)
    rec['thread'] = th
    th.start()
    return rec


def _win_line_entry(seq, rec):
    global _ctrl_spawn_block_until
    try:
        windows_loop(rec)
    except Exception as e:
        _ctl_note(f"[ctl] 线 {seq} 异常退出: {type(e).__name__}: {str(e)[:160]}")
    finally:
        _line_release(rec, rec.get('w'), True)
        with _lock:
            _lines.pop(seq, None)
            rec['pend_at_exit'] = len(_pending)
        if rec['took'] == 0 and rec['pend_at_exit'] and not rec['retire'].is_set():
            _ctrl_spawn_block_until = time.time() + CTL_NO_WORK_COOLDOWN
        _kick.set()

def _wport(url):
    return url.rstrip('/').rsplit(':', 1)[-1]

class _WorkerPool:
    def __init__(self, initial, cap=1):
        self.cv = threading.Condition()
        self.cap = max(1, int(cap))
        self.alive = set(initial)
        self.busy = {}
        self.avoid = set()
        self.stop = False

    def _free(self):
        cand = {u for u in self.alive if self.busy.get(u, 0) < self.cap}
        if not self.avoid:
            return cand
        out = set()
        for u in cand:
            try:
                if int(_wport(u)) in self.avoid:
                    continue
            except (TypeError, ValueError):
                pass
            out.add(u)
        return out

    def set_avoid(self, ports):
        want = set()
        for p in (ports or []):
            try:
                want.add(int(p))
            except (TypeError, ValueError):
                pass
        with self.cv:
            added, lifted = want - self.avoid, self.avoid - want
            self.avoid = want
            if added or lifted:
                self.cv.notify_all()
            return added, lifted

    def acquire(self, timeout=30.0):
        with self.cv:
            end = time.time() + timeout
            while not self.stop:
                free = self._free()
                if free:
                    w = min(free, key=lambda u: (self.busy.get(u, 0), u))
                    self.busy[w] = self.busy.get(w, 0) + 1
                    return w
                if not self.alive:
                    return None
                self.cv.wait(timeout=min(5.0, max(0.5, end - time.time())))
                if time.time() >= end:
                    return None
            return None

    def release(self, w, ok=True):
        with self.cv:
            n = self.busy.get(w, 0) - 1
            if n > 0:
                self.busy[w] = n
            else:
                self.busy.pop(w, None)
            if not ok:
                self.alive.discard(w)
            self.cv.notify_all()

    def update(self, live):
        with self.cv:
            new = set(live) - self.alive - set(self.busy)
            gone = self.alive - set(live)
            self.alive |= set(live)
            self.alive -= gone
            if new or gone:
                msgs = []
                if new:
                    msgs.append('+ ' + ' '.join(_wport(u) for u in sorted(new)))
                if gone:
                    msgs.append('- ' + ' '.join(_wport(u) for u in sorted(gone)))
                with _log_lock:
                    print(f"[pool] worker 池变化: {' | '.join(msgs)}（现有 {len(self.alive)} 个）", flush=True)
            self.cv.notify_all()


_pool = None

def _prober_loop():
    import worker_pool as _wp
    while not _pool.stop:
        time.sleep(WIN_PROBE_INTERVAL)
        if _pool.stop:
            return
        with _pool.cv:
            busy = set(_pool.busy)
        try:
            res = _wp.probe_all(skip_busy=[int(_wport(u)) for u in busy])
        except Exception as e:
            with _log_lock:
                print(f"[pool] 探活异常(下轮再试): {e!r}", flush=True)
            continue
        live = [f"http://127.0.0.1:{p}" for p, d in res.items()
                if d.get('ok') is True or d.get('skipped')]
        _pool.update(live)
        try:
            import worker_alert as _wa
            if not _wa.tmux_running():
                _ok, _msg = _wa.ensure_tmux()
                with _log_lock:
                    print(f"[alerts] 告警哨兵 {_msg}", flush=True)
        except Exception:
            pass

DEFER_SLEEP = 300
WATCHDOG_CYCLES = 6

_requeue_count = {}
_worker_fail_streak = {}

_TRANSPORT_MARKS = ('探测失败', 'Connection refused', 'refused', 'timed out', 'timeout',
                    'Cannot connect', 'RemoteDisconnected', 'reset by peer',
                    'ConnectionReset', 'IncompleteRead', 'BadStatusLine', 'BrokenPipe',
                    '重试 3 次仍失败', '背压自锁',
                    '背压',
                    '[no-ERROR-line')

def _is_transport_failure(m):
    return any(k in m for k in _TRANSPORT_MARKS)

def _dl_err_tail(full, rc):
    lines = [ln for ln in (full or '').splitlines() if ln.strip()]
    em = [ln for ln in lines if 'ERROR' in ln]
    if em:
        return em[-1]
    last = lines[-1] if lines else '(无输出)'
    if rc != 0:
        return f"[no-ERROR-line rc={rc}] 末行: {last}"
    return last

REQUEUE_MAX = 8

LOCAL_REQUEUE_DELAY = 900

def _requeue_later(t, reason, delay_s):
    _t = threading.Timer(delay_s, lambda: _requeue(t, reason))
    _t.daemon = True
    _t.start()
    with _log_lock:
        print(f"[requeue] {t[0]}: 传输中断，{delay_s}s 后放回队头 —— {reason}", flush=True)

def _requeue(t, reason, count=True):
    if count:
        key = t
        n = _requeue_count.get(key, 0) + 1
        _requeue_count[key] = n
        if n > REQUEUE_MAX:
            with _log_lock:
                print(f"[requeue] {t[0]}: 超过重排队上限({REQUEUE_MAX})，本轮放弃（下轮重建任务时重试）—— {reason}", flush=True)
            return
        tag = f"第 {n} 次"
    else:
        tag = "线回收"
    with _lock:
        if t in _pending:
            dupe = True
        else:
            _rank_set(t, RANK_NEW)
            _pending.append(t)
            _resort_pending()
            dupe = False
    with _log_lock:
        if dupe:
            print(f"[requeue] {t[0]}: 已在队列中，不重复入队 —— {reason}", flush=True)
        else:
            print(f"[requeue] {t[0]}: 放回队头（{tag}）—— {reason}", flush=True)

def _mark_win_bad(outpath, status, note):
    q = _quarantine(outpath)
    if _ledger:
        try:
            _ledger.record(q, status, note, 'driver:windows')
        except Exception:
            pass
    with _log_lock:
        print(f"[integrity] {os.path.basename(outpath)}: {status} → 隔离为 {os.path.basename(q)}", flush=True)

def _has_win_partial(task):
    sp, source, payload, outdir, platform = task
    if source == 'SOURCE_B':
        names = [os.path.basename(payload)]
    else:
        store_prefix, build, object_key = payload
        names = [os.path.basename(object_key)] if object_key else None
    if names:
        return any(_is_legacy_partial(os.path.join(outdir, n + '.download.json')) for n in names)
    try:
        return any(_is_legacy_partial(os.path.join(outdir, f))
                   for f in os.listdir(outdir) if f.endswith('.download.json'))
    except OSError:
        return False

def _is_legacy_partial(sc_path):
    if not os.path.exists(sc_path):
        return False
    try:
        with open(sc_path) as f:
            return json.load(f).get('mode') != 'stream'
    except Exception:
        return True

def _has_stream_partial(task):
    sp, source, payload, outdir, platform = task
    if source == 'SOURCE_B':
        names = [os.path.basename(payload)]
    else:
        store_prefix, build, object_key = payload
        names = [os.path.basename(object_key)] if object_key else None
    if names:
        return any(_stream_partial(os.path.join(outdir, n + '.download.json')) for n in names)
    try:
        return any(_stream_partial(os.path.join(outdir, f))
                   for f in os.listdir(outdir) if f.endswith('.download.json'))
    except OSError:
        return False

def _stream_partial(sc_path):
    if not os.path.exists(sc_path):
        return False
    try:
        with open(sc_path) as f:
            return json.load(f).get('mode') == 'stream'
    except Exception:
        return False

def _win_partial_name(task):
    sp, source, payload, outdir, platform = task
    if source == 'SOURCE_B':
        names = [os.path.basename(payload)]
    else:
        store_prefix, build, object_key = payload
        names = [os.path.basename(object_key)] if object_key else None
    try:
        if names:
            for n in names:
                if os.path.exists(os.path.join(outdir, n + '.download.json')):
                    return n + '.download.json'
            return (names[0] + '.download.json')
        for f in os.listdir(outdir):
            if f.endswith('.download.json'):
                return f
    except OSError:
        pass
    return '?'

def _report(res, backend, t=None):
    global completed
    ok, sp, msg = res
    with _lock:
        completed += 1
        n = completed
    if ok:
        print(f"[{n}/{len(tasks)}] ✅ {sp} [{backend}]", flush=True)
    else:
        print(f"[{n}/{len(tasks)}] ❌ {sp} [{backend}]: {msg}", flush=True)
    _event('done' if ok else 'fail', sp, backend, msg)
    _refresh_status()
    if ok and t is not None:
        _notify_done(t, res, backend)

def local_loop():
    defers = 0
    stall = 0
    skip_wait = 0
    while True:
        while _ctl_paused.is_set() and not _run.is_set():
            _kick.wait(2.0)
        if _run.is_set():
            with _log_lock:
                print("[local] 收尾标志置位，本地线退出", flush=True)
            return
        if STAGE_DIR:
            _stage_gate()
        t = _take()
        if t is None:
            with _lock:
                left = len(_pending)
            if left:
                skip_wait += 1
                if skip_wait <= WATCHDOG_CYCLES:
                    with _log_lock:
                        print(f"[local] 队列剩 {left} 个任务但当前全被跳过/挡下 → 本地线歇 {DEFER_SLEEP}s"
                              f"（第 {skip_wait}/{WATCHDOG_CYCLES} 轮）", flush=True)
                    time.sleep(DEFER_SLEEP)
                    continue
                with _log_lock:
                    print(f"[local] 跳过状态已持续 {WATCHDOG_CYCLES} 轮（约 {WATCHDOG_CYCLES * DEFER_SLEEP}s），"
                          f"队列剩 {left} 个任务全被挡下 → 本地线退出（全 driver 随之收尾）。"
                          f"若这不是本意，用 dl_ctl.py unskip 取消跳过后重启 driver", flush=True)
            with _log_lock:
                print(f"[local] 队列已空，本地线退出（队列剩余 {left}）", flush=True)
            return
        skip_wait = 0
        with _log_lock:
            print(f"[local] 接任务: {t[0]} ({t[4]})", flush=True)
        if _has_win_partial(t):
            with _lock:
                _rank_set(t, 0, '')
                _pending.append(t)
                _resort_pending()
                n = len(_pending)
            defers += 1
            with _log_lock:
                print(f"[defer] {t[0]}: 目标已有 windows 线半成品({_win_partial_name(t)})，交还队列"
                      f"（本轮第 {defers} 次）", flush=True)
            if defers >= n:
                stall += 1
                if stall >= WATCHDOG_CYCLES and not _local_watchdog_ok(completed, n):
                    return
                with _log_lock:
                    print(f"[defer] 整轮全是 windows 半成品 → 本地线歇 {DEFER_SLEEP}s（第 {stall}/"
                          f"{WATCHDOG_CYCLES} 轮；期间任务完成会重置）", flush=True)
                time.sleep(DEFER_SLEEP)
                defers = 0
            continue
        defers = 0
        stall = 0
        _lk = 'local-%d' % threading.get_ident()
        _line_mark(_lk, {'backend': 'local', 'item': t[0], 'platform': t[4],
                         'task': t,
                         'started': time.strftime('%Y-%m-%d %H:%M:%S')})
        res = run_one(t)
        _line_mark(_lk, None)
        if not res[0]:
            msg = res[2] or ''
            if msg.startswith('stream download failed:') and _is_transport_failure(msg):
                _requeue_later(t, msg[:90], LOCAL_REQUEUE_DELAY)
        _report(res, 'local', t)

def windows_loop(rec=None):
    if rec is None:
        rec = {'seq': 0, 'retire': threading.Event(), 'retire_now': False, 'w': None,
               'task': None, 'proc': None, 'phase': None, 'took': 0}
    if WIN_GATE:
        t0 = time.time()
        while not os.path.exists(WIN_GATE) and time.time() - t0 < WIN_GATE_MAX_WAIT:
            if _run.is_set() or rec['retire'].is_set():
                return
            rec['retire'].wait(30)
        if not os.path.exists(WIN_GATE):
            with _log_lock:
                print(f"WARN: WIN_GATE({WIN_GATE}) 等待超时 {WIN_GATE_MAX_WAIT}s，windows 线照常开始", flush=True)
    while True:
        if _run.is_set() or rec['retire'].is_set():
            _ctl_line_exit(rec, '队列/停线')
            return
        while _ctl_paused.is_set() and not (_run.is_set() or rec['retire'].is_set()):
            _kick.wait(2.0)
        w = _pool.acquire()
        if w is None:
            if _run.is_set() or rec['retire'].is_set() or _pool.stop:
                _ctl_line_exit(rec, '池停/停线')
                return
            if _pending:
                with _pool.cv:
                    alive, busy = sorted(_pool.alive), sorted(_pool.busy)
                with _log_lock:
                    if not alive:
                        print(f"[pool] ⚠️ 无存活 worker（池空）：windows 线等待，队列还剩 {len(_pending)} 个"
                              f"任务；{WIN_PROBE_INTERVAL}s 一轮探活", flush=True)
                    else:
                        print(f"[pool] 存活口全部占用（{'、'.join(alive)} 在下载；本线空闲等待）"
                              f"——队列还剩 {len(_pending)} 个任务", flush=True)
            else:
                _ctl_line_exit(rec, '队列已空')
                return
            rec['retire'].wait(min(60, WIN_PROBE_INTERVAL // 4))
            continue
        rec['w'] = w
        if _run.is_set() or rec['retire'].is_set():
            _line_release(rec, w, True)
            _ctl_line_exit(rec, '停线(借到口即退)')
            return
        t = _take(skip=_has_stream_partial)
        if t is None:
            _line_release(rec, w, True)
            _ctl_line_exit(rec, '无任务可接')
            return
        with _log_lock:
            print(f"[{_wport(w)}] 接任务: {t[0]} ({t[4]})", flush=True)
        rec['task'] = t
        _lk = 'win-%d' % (rec['seq'] or threading.get_ident())
        _line_mark(_lk, {'backend': f'win:{_wport(w)}', 'worker': _wport(w), 'item': t[0],
                         'platform': t[4], 'task': t,
                         'started': time.strftime('%Y-%m-%d %H:%M:%S')})
        try:
            res = run_one_windows(t, w, rec)
        except Exception as e:
            res = (False, t[0], f"{type(e).__name__}: {str(e)[:120]}")
        finally:
            rec['task'] = None
            rec['proc'] = None
            rec['phase'] = None
        _line_mark(_lk, None)
        if rec['retire'].is_set():
            if not res[0]:
                _requeue(t, f"线回收: {(res[2] or '')[:70]}", count=False)
            else:
                rec['took'] += 1
                _report(res, f'win:{_wport(w)}', t)
            _line_release(rec, w, True)
            _ctl_line_exit(rec, '回收(任务已回队)' if not res[0] else '回收(手头任务已完成)')
            return
        rec['took'] += 1
        ok = res[0]
        worker_down = False
        if not ok:
            m = res[2] or ''
            if m.startswith('windows: 落地文件未过完整性校验'):
                p = os.path.join(t[3], os.path.basename(m.rsplit(': ', 1)[-1]))
                _mark_win_bad(p, 'HOLED', 'windows 线完整性校验未过（疑卡洞），隔离待重下')
            elif m.startswith('CORRUPT after windows download'):
                p = os.path.join(t[3], os.path.basename(m.rsplit(': ', 1)[-1]))
                _mark_win_bad(p, 'CORRUPT', 'windows 线下载后全量 CRC 失败，隔离待重下')
            elif any(_p in m for _p in UPSTREAM_ISSUE_PREFIXES):
                pass
            else:
                import worker_pool as _wp
                _p = int(_wport(w))
                chk = _wp.health_check(_p, attempts=2)
                transport = _is_transport_failure(m)
                if transport:
                    _requeue(t, f"{_wport(w)} 传输中断: {m[:90]}")
                if not chk.get('ok'):
                    _worker_fail_streak[_p] = _worker_fail_streak.get(_p, 0) + 1
                    _line_release(rec, w, False)
                    worker_down = True
                    with _log_lock:
                        print(f"[worker] {_wport(w)} down（{chk.get('error', '')}）→ 踢出池；"
                              f"{'任务已归还队列' if transport else '任务归还队列'}", flush=True)
                    if not transport:
                        _requeue(t, f"worker {_wport(w)} down: {chk.get('error', '')}")
                elif transport:
                    _worker_fail_streak[_p] = _worker_fail_streak.get(_p, 0) + 1
                    if _worker_fail_streak[_p] >= 2:
                        _line_release(rec, w, False)
                        worker_down = True
                        with _log_lock:
                            print(f"[worker] {_wport(w)} 连续 {_worker_fail_streak[_p]} 次传输失败 → 判病，"
                                  f"踢出池；任务已归还队列", flush=True)
        if not worker_down:
            _line_release(rec, w, True)
            if ok:
                _worker_fail_streak[int(_wport(w))] = 0
        _report(res, f'win:{_wport(w)}', t)


def _ctl_line_exit(rec, why):
    with _log_lock:
        print(f"[ctl] windows 线 {rec.get('seq')} 退出（{why}；已接 {rec.get('took')} 个任务）", flush=True)


INBOX_DIR = os.environ.get('DL_INBOX_DIR',
                           os.path.join(os.path.dirname(os.path.abspath(__file__)), 'inbox'))
INBOX_DONE_DIR = os.path.join(INBOX_DIR, 'done')
BROKER_CFG_FILE = os.environ.get('DL_BROKER_CFG',
                                 os.path.join(os.path.dirname(os.path.abspath(__file__)), 'broker.json'))
NOTIFY_DIR = CFG.get('paths.notify_spool_dir', '')


try:
    import dl_broker_policy as _pol
except Exception:
    _pol = None


def _broker_cfg():
    cfg = {'dispatch_per_round': 5, 'priority_front_threshold': 90}
    try:
        with open(BROKER_CFG_FILE) as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for k in list(cfg):
                if isinstance(raw.get(k), int) and not isinstance(raw.get(k), bool):
                    cfg[k] = int(raw[k])
    except Exception:
        pass
    return cfg


def _ena_outpath(task):
    try:
        if task[1] == 'SOURCE_B':
            return os.path.join(task[3], os.path.basename(task[2]))
    except Exception:
        pass
    return None


def _ranks_from_inbox():
    out = {}
    try:
        names = os.listdir(INBOX_DONE_DIR)
    except Exception:
        return out
    for n in names:
        if not n.endswith('.json'):
            continue
        try:
            with open(os.path.join(INBOX_DONE_DIR, n), encoding='utf-8') as f:
                it = json.load(f)
            t = it.get('task') or {}
            sp = str(t.get('name') or '')
            if not sp:
                continue
            k = (sp, str(t.get('platform') or ''))
            v = (int(it.get('priority') or 0), str(it.get('ts') or ''))
            if k not in out or v[1] >= out[k][1]:
                out[k] = v
        except Exception:
            continue
    return out


_pending_origin = {}
_notified = set()
_notify_lock = threading.Lock()


def _session_slug(s):
    s = str(s or '')
    if not s:
        return 'anon'
    cleaned = ''.join(ch if (ch.isalnum() or ch in '-_') else '' for ch in s)[:24] or 'anon'
    return '%s-%s' % (cleaned, hashlib.md5(s.encode('utf-8')).hexdigest()[:8])


def _origin_from_item(it):
    o = (it or {}).get('origin')
    if not isinstance(o, dict):
        return None
    sid = str(o.get('session_id') or '').strip()
    if not sid:
        return None
    return {'model': str(o.get('model') or ''), 'harness': str(o.get('harness') or ''),
            'session_id': sid, 'rid': str(o.get('rid') or it.get('id') or '')}


def _origins_from_inbox():
    out, best = {}, {}
    try:
        names = os.listdir(INBOX_DONE_DIR)
    except Exception:
        return out
    for nm in names:
        if not nm.endswith('.json'):
            continue
        try:
            with open(os.path.join(INBOX_DONE_DIR, nm), encoding='utf-8') as f:
                it = json.load(f)
            t = it.get('task') or {}
            sp = str(t.get('name') or '')
            if not sp:
                continue
            o = _origin_from_item(it)
            if not o:
                continue
            k = (sp, str(t.get('platform') or ''))
            v = (str(it.get('ts') or ''), o['rid'])
            if k not in best or v >= best[k]:
                best[k] = v
                out[k] = o
        except Exception:
            continue
    return out


def _notify_write(session_id, rec):
    if session_id:
        d = os.path.join(NOTIFY_DIR, 'by-session')
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, _session_slug(session_id) + '.jsonl')
    else:
        os.makedirs(NOTIFY_DIR, exist_ok=True)
        path = os.path.join(NOTIFY_DIR, '_unattributed.jsonl')
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(rec, ensure_ascii=False) + '\n')


def _notify_done(t, res, backend):
    if not NOTIFY_DIR:
        return
    try:
        if not _notify_lock.acquire(blocking=False):
            return
        try:
            ok, sp, msg = res
            try:
                key = _ctlmod.key_of(t)
            except Exception:
                key = (str(sp), '')
            origin = _pending_origin.pop(key, None)
            nid = '%s:%s:%s' % ((origin or {}).get('rid') or '-', key[0], key[1])
            if nid in _notified:
                return
            _notified.add(nid)
            rec = {'v': 1, 'nid': nid, 'ts': _now_iso8(), 'tsu': time.time(), 'kind': 'done',
                   'rid': (origin or {}).get('rid') or '',
                   'origin': origin or {}, 'sp': key[0], 'platform': key[1],
                   'outdir': (str(t[3]) if len(t) > 3 else ''),
                   'backend': backend, 'msg': str(msg or '')}
            _notify_write((origin or {}).get('session_id') or '', rec)
            with _log_lock:
                print('[notify] %s → %s' % (nid, origin['session_id'] if origin else
                                            '无申请单（只写审计，不唤醒）'), flush=True)
        finally:
            _notify_lock.release()
    except Exception as e:
        try:
            with _log_lock:
                print(f"[notify] 跳过（{type(e).__name__}: {e}）", flush=True)
        except Exception:
            pass


def _inbox_finish(path, sub, item=None, reason=None):
    try:
        d = os.path.join(INBOX_DIR, sub)
        os.makedirs(d, exist_ok=True)
        dst = os.path.join(d, os.path.basename(path))
        if reason is not None and isinstance(item, dict):
            item = dict(item)
            item['_rejected'] = reason
            item['_rejected_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
            tmp = dst + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(item, f, ensure_ascii=False, indent=1)
            os.replace(tmp, dst)
            _try_rm(path)
        else:
            os.rename(path, dst)
        return True
    except Exception:
        return False


def _drain_inbox():
    if _ctlmod is None or _run.is_set():
        return 0
    try:
        names = sorted(n for n in os.listdir(INBOX_DIR)
                       if n.endswith('.json') and not n.startswith('.'))
    except OSError:
        return 0
    if not names:
        return 0

    cfg = _broker_cfg()
    items = []
    for n in names:
        p = os.path.join(INBOX_DIR, n)
        try:
            with open(p) as f:
                it = json.load(f)
        except Exception as e:
            _inbox_finish(p, 'rejected', None, f'JSON 读不出来：{type(e).__name__}: {e}')
            continue
        if not isinstance(it, dict):
            _inbox_finish(p, 'rejected', None, '申请必须是一个 JSON 对象')
            continue
        tgt = it.get('target_driver')
        if tgt and tgt != DRIVER_NAME:
            continue
        items.append((p, it))

    if _pol is not None:
        items = _pol.order_inbox(items, cfg)
    else:
        items.sort(key=lambda pi: str(pi[1].get('ts') or ''), reverse=True)
        items.sort(key=lambda pi: -int(pi[1].get('priority') or 0))
    n_ok = 0
    for p, it in items[:max(1, int(cfg['dispatch_per_round']))]:
        owner = str(it.get('owner') or 'unknown')
        entry = it.get('task') if isinstance(it.get('task'), dict) else it
        try:
            task = _ctlmod.build_task(entry)
        except Exception as e:
            _inbox_finish(p, 'rejected', it, f'任务不合法：{e}')
            with _log_lock:
                print(f"[inbox] 拒绝 {owner} 的申请 {os.path.basename(p)}：{e}", flush=True)
            continue

        op = _ena_outpath(task)
        if op:
            try:
                if (os.path.exists(op) and not os.path.exists(op + '.download.json')
                        and os.path.getsize(op) > 0):
                    _url = task[2] if len(task) > 2 else None
                    v, detail = 'unknown', '未做磁盘可信度判定'
                    if _disk_trust is not None:
                        v, detail, _loc, _rem = _disk_trust.judge_existing(op, _url, timeout=12)
                        _disk_trust.append_ledger(v, op, detail, url=_url or '',
                                                  who=owner, local=_loc, remote=_rem)
                    if v == 'bad':
                        with _log_lock:
                            print(f"[inbox] 盘上 {os.path.basename(op)} 与远端不符（{detail}）"
                                  f" → 受理重下（不拒）", flush=True)
                    else:
                        _inbox_finish(p, 'rejected', it,
                                      f'盘上已有落地文件（{os.path.basename(op)}；{detail}）')
                        with _log_lock:
                            print(f"[inbox] 拒绝 {owner} 的申请 {os.path.basename(p)}："
                                  f'盘上已有落地文件（{detail}）', flush=True)
                        if v == 'unknown':
                            try:
                                import worker_alert as _wa
                                _wa.record({'ts_utc8': time.strftime(
                                                '%Y-%m-%d %H:%M:%S',
                                                time.localtime(time.time() + 8 * 3600)),
                                            'event': 'DISK_TRUST_UNKNOWN',
                                            'detail': (f'盘上文件无法判可信度，已拒但未验：'
                                                       f'{op}（{detail}）')})
                            except Exception:
                                pass
                        continue
            except Exception as e:
                with _log_lock:
                    print(f"[inbox] 盘上查重异常（按未命中处理，不拒）："
                          f"{type(e).__name__}: {e}", flush=True)

        key = _ctlmod.key_of(task)
        with _lock:
            dup = any(_ctlmod.key_of(t) == key for t in _pending)
            if not dup:
                dup = any((m.get('item'), m.get('platform') or '') == key
                          for m in _line_state.values() if m.get('item'))
            if dup:
                pos = None
            else:
                _rank_set(task, it.get('priority') or 0, it.get('ts'))
                _pending.append(task)
                _resort_pending()
                _o = _origin_from_item(it)
                if _o:
                    _pending_origin[key] = _o
                pos = _pending.index(task) + 1
            depth = len(_pending)
        if pos is None:
            _inbox_finish(p, 'rejected', it, '查重命中：已有同物种同平台的任务在队列或在途')
            with _log_lock:
                print(f"[inbox] 拒绝 {owner} 的申请 {os.path.basename(p)}：查重命中（{key[0]}）", flush=True)
            continue
        _inbox_finish(p, 'done')
        n_ok += 1
        with _log_lock:
            print(f"[inbox] 受理 {owner} 的申请 → 第 {pos} 位：{key[0]} [{key[1]}]"
                  f"（{_level_of_item(it)}，{_ctlmod.fmt_hm(it.get('ts'))} 提交，队列 {depth}）", flush=True)
    if n_ok:
        _kick.set()
        try:
            with _pool.cv:
                _pool.cv.notify_all()
        except Exception:
            pass
    return n_ok


def _ctl_refresh_broker():
    global _ctl_avoid, _ctl_broker_max_live
    live = _broker_fields_ok()
    bmax = _ctl_broker_max if live else None
    av = sorted(set(_ctl_avoid_manual) | (set(_ctl_avoid_policy) if live else set()))
    changed = (bmax != _ctl_broker_max_live) or (av != _ctl_avoid)
    had_broker = _ctl_broker_max is not None or bool(_ctl_avoid_policy)
    _ctl_broker_max_live, _ctl_avoid = bmax, av
    try:
        if _pool is not None:
            added, lifted = _pool.set_avoid(_ctl_avoid)
            if added or lifted:
                _ctl_note("[ctl] broker 让路（刷新）：避开 %s；恢复 %s"
                          % (' '.join(str(p) for p in added) or '-',
                             ' '.join(str(p) for p in lifted) or '-'))
    except Exception as e:
        _ctl_note(f"[ctl] set_avoid 失败（忽略，不影响下载，下轮自动重试）：{type(e).__name__}: {e}")
    if not changed:
        return False
    if had_broker and not live:
        _ctl_note("[ctl] ⚠️ broker 账本已过期（>%ds，broker 停了？）→ 它写的让路/上限**自动作废**，"
                  "退回人设线数 %d 说话；重启 broker 即恢复自动让路" % (int(BROKER_TTL), _ctl_desired))
    elif live and not had_broker:
        _ctl_note("[ctl] broker 心跳恢复 → 重新采纳它的让路/上限")
    return True


def _ctl_reconcile():
    global _ctrl_spawn_block_until
    no_work = _run.is_set() or _ctl_paused.is_set() or not _ctl_takeable_any()
    try:
        lim = WIN_LINES_MAX if _ctl_broker_max_live is None else max(0, min(WIN_LINES_MAX, int(_ctl_broker_max_live)))
    except (TypeError, ValueError):
        lim = WIN_LINES_MAX
    want = 0 if no_work else max(0, min(_ctl_desired, lim))
    with _lock:
        live = list(_lines.values())
    n = len(live)
    if n < want and time.time() >= _ctrl_spawn_block_until:
        for _ in range(want - n):
            rec = _spawn_line()
            with _log_lock:
                print(f"[ctl] 补线 → 线 {rec['seq']}（现有 {len(_lines)}/{want} 条；"
                      f"期望 {_ctl_desired}，上限 {WIN_LINES_MAX}"
                      + (f"，broker 建议 {_ctl_broker_max_live}" if _ctl_broker_max_live is not None else '')
                      + "）", flush=True)
    elif n > want and not no_work:
        live.sort(key=lambda r: (r.get('w') is not None, r.get('took', 0)))
        for rec in live[:n - want]:
            _ctl_line_retire(rec, _ctl_now)
            _p = rec.get('proc')
            if rec.get('phase') == 'verify':
                _ctl_retire_say(rec, f"[ctl] 线 {rec['seq']} 正在验证落地文件 → 不打杀，推迟到边界回收")
            elif _p is not None and _ctl_now:
                threading.Thread(target=_kill_proc, args=(rec,), daemon=True,
                                 name='ctl-kill-%d' % rec['seq']).start()
                _ctl_retire_say(rec, f"[ctl] 线 {rec['seq']} 立刻回收：中断其下载子进程 pid={_p.pid}"
                                     f"（sidecar 在，任务回队后续传）")
            elif _p is not None:
                _ctl_retire_say(rec, f"[ctl] 线 {rec['seq']} 手头有在途下载 → 跑完手头文件即退（优雅退休；"
                                     f"要立刻打断用 dl_ctl.py lines {want} --now）")
            else:
                _ctl_retire_say(rec, f"[ctl] 线 {rec['seq']} 回收（空闲/等口，下一检查点退出）")


def _ctl_poll():
    global _ctl_rev_applied, _ctl_last_ops, _ctl_last_ops_rev, _ctl_ok, _ctl_spec, _ctl_paused, _ctl_skip, _ctl_desired, _ctl_now, _ctl_avoid, _ctl_avoid_manual, _ctl_avoid_policy, _ctl_broker_max, _ctl_broker_max_live
    if _ctlmod is None:
        return False
    spec = _ctlmod.load_ctl(CTL_FILE)
    if spec is None:
        return False
    try:
        rev = int(spec.get('rev') or 0)
    except (TypeError, ValueError):
        rev = -1
    if rev == _ctl_rev_applied:
        return False
    ok, msg = _ctlmod.validate(spec)
    if not ok:
        _ctl_rev_applied = rev
        _ctl_last_ops, _ctl_last_ops_rev = [f"控制文件校验失败，未应用：{msg}"], rev
        _ctl_ok = False
        _ctl_note(f"[ctl] ⚠️ 控制文件 rev={rev} 校验失败，不应用：{msg}")
        _write_status_snapshot()
        return False
    _ctl_spec = spec
    _ctl_paused.set() if spec.get('paused') else _ctl_paused.clear()
    _ctl_skip = [s for s in (spec.get('skip') or []) if s]
    if spec.get('win_lines') is not None:
        _ctl_desired = int(spec.get('win_lines'))
    _ctl_avoid_manual = [p for p in (spec.get('avoid_ports') or []) if isinstance(p, int)]
    _ctl_avoid_policy = [p for p in (spec.get('policy_avoid_ports') or []) if isinstance(p, int)]
    _ctl_broker_max = spec.get('broker_max_lines')
    try:
        if _ctl_refresh_broker() and _pool is not None:
            with _pool.cv:
                nfree, nalive = len(_pool._free()), len(_pool.alive)
            _ctl_note("[ctl] broker 字段生效：让路 %s / 上限 %s（只影响下次取口，在途不打断；"
                      "当前可借 %d/%d 个口）"
                      % (' '.join(str(p) for p in _ctl_avoid) or '-',
                         '-' if _ctl_broker_max_live is None else _ctl_broker_max_live, nfree, nalive))
    except Exception as e:
        _ctl_note(f"[ctl] set_avoid 失败（忽略，不影响下载）：{type(e).__name__}: {e}")
    ops = spec.get('ops') or []
    _ctl_now = any(o.get('op') == 'lines' and o.get('now') for o in ops if isinstance(o, dict))
    try:
        with _lock:
            inflight = [(r['task'][0], r['task'][4]) for r in _lines.values() if r.get('task')]
            inflight += [(m.get('item'), m.get('platform') or '')
                         for m in _line_state.values() if m.get('item')]
            res = _ctlmod.apply_ops(_pending, ops, inflight=inflight)
            _pending[:] = res['pending']
            _rerank_after_ops(res)
        reports = res['reports']
        _ctl_ok = True
    except Exception as e:
        reports = [f"ops 应用失败（本轮 ops 全部跳过）：{type(e).__name__}: {e}"]
        _ctl_ok = False
        _ctl_note(f"[ctl] ⚠️ rev={rev} {reports[0]}")
    _ctl_last_ops, _ctl_last_ops_rev = reports or [], rev
    _ctl_rev_applied = rev
    with _log_lock:
        print(f"[ctl] 应用 rev={rev}（来自 {CTL_FILE}）：线数={_ctl_desired} 暂停={_ctl_paused.is_set()} "
              f"跳过={_ctl_skip or '-'}" + ("；" + "；".join(reports) if reports else ""), flush=True)
    _kick.set()
    with _pool.cv:
        _pool.cv.notify_all()
    _write_status_snapshot()
    return True


def _ctl_loop():
    try:
        if _ctl_poll():
            src = ('控制文件覆盖了环境变量 WIN_LINES=%d' % WIN_LINES) if _ctl_desired != WIN_LINES else '与环境变量一致'
            with _log_lock:
                print(f"[ctl] 启动读取控制文件：windows 线数 = {_ctl_desired}（{src}）", flush=True)
    except Exception as e:
        _ctl_note(f"[ctl] 启动读控制文件失败（按环境变量跑）：{type(e).__name__}: {e}")
    while not _run.is_set():
        try:
            _ctl_poll()
            _ctl_refresh_broker()
            _drain_inbox()
            _ctl_reconcile()
            _refresh_inventory_auto()
        except Exception as e:
            _ctl_note(f"[ctl] 监督循环异常（不致命，下轮再试）：{type(e).__name__}: {e}")
        _kick.wait(CTL_POLL)
        _kick.clear()

def _local_watchdog_ok(prev_completed, prev_pending):
    if completed != prev_completed or len(_pending) != prev_pending:
        return True
    with _log_lock:
        print(f"[watchdog] 连续无进展（剩 {len(_pending)} 个任务，windows 池不可用？）→ 退出本线程；"
              f"任务状态已落盘，重启 driver 或用 worker_pool.py 查池后重跑", flush=True)
    return False

_pid_guard_or_die()

_pool_live = []
if WIN_WORKERS == 'auto':
    import worker_pool as _wp0
    _pool_live = _wp0.alive_urls()
    with _log_lock:
        labels = _wp0.load_registry()['workers']
        desc = ' '.join(f"{_wport(u)}={labels.get(int(_wport(u)), {}).get('label', '?')}" for u in _pool_live) or '(无)'
        print(f"[pool] worker 池探活: {len(_pool_live)} 个存活 → {desc}", flush=True)

_pool = _WorkerPool(_pool_live, WORKER_PORT_CAP)

if WIN_WORKERS != 'off' and _pool_live:
    NWORKERS = 1
else:
    NWORKERS = LOCAL_LINES if LOCAL_LINES > 0 else MAX_CONCURRENT

try:
    import worker_alert as _wa
    _wa.boot_check(verbose=True)
except Exception as _e:
    print(f"[alerts] 告警自检失败（不阻断下载）：{type(_e).__name__}: {_e}", flush=True)

try:
    _tk_ranks = _ranks_from_inbox()
    if _tk_ranks:
        _pending_rank.update(_tk_ranks)
        _resort_pending()
    print(f"[rank] 申请单档位：读出 {len(_tk_ranks)} 条（队列 {len(_pending)} 个任务）", flush=True)
except Exception as _e:
    print(f"[rank] 申请单档位失败（当没有，不影响下载）：{type(_e).__name__}: {_e}", flush=True)

try:
    _restored_q = _queue_load()
    _added_q = 0
    if _restored_q:
        _have_k = set()
        for _t in _pending:
            try:
                _have_k.add(_ctlmod.key_of(_t))
            except Exception:
                pass
        for _t in _restored_q:
            try:
                _k = _ctlmod.key_of(_t)
            except Exception:
                continue
            if _k in _have_k:
                continue
            _have_k.add(_k)
            _pending.append(_t)
            try:
                _pending_rank.setdefault(_k, (RANK_NORMAL[0], _queue_saved_at() or _now_iso8()))
            except Exception:
                pass
            _added_q += 1
        if _added_q:
            _resort_pending()
    print(f"[queue] 队列恢复：快照 {len(_restored_q)} 条 → 补回 {_added_q} 条"
          f"（队列 {len(_pending)} 个任务）", flush=True)
except Exception as _e:
    print(f"[queue] 队列恢复失败（当没有，不影响下载）：{type(_e).__name__}: {_e}", flush=True)

try:
    _restored = _ranks_load()
    if _restored:
        _pending_rank.update(_restored)
        _resort_pending()
    print(f"[rank] 档位恢复：读出 {len(_restored)} 条（队列 {len(_pending)} 个任务）", flush=True)
except Exception as _e:
    print(f"[rank] 档位恢复失败（当没有，不影响下载）：{type(_e).__name__}: {_e}", flush=True)

try:
    _restored_o = _origins_from_inbox()
    if _restored_o:
        _pending_origin.update(_restored_o)
    print(f"[notify] 提交方表恢复：{len(_restored_o)} 条（队列 {len(_pending)} 个任务；"
          f"spool {'开启' if NOTIFY_DIR else '关闭'}）", flush=True)
except Exception as _e:
    print(f"[notify] 提交方表恢复失败（当没有，不影响下载）：{type(_e).__name__}: {_e}", flush=True)

threading.Thread(target=_status_writer, daemon=True, name='status-writer').start()

def _drain_win_lines():
    t0 = time.time()
    while True:
        with _lock:
            n = len(_lines)
        if not n:
            return
        if time.time() - t0 > 60:
            t0 = time.time()
            with _lock:
                q = len(_pending)
            _ctl_note(f"[ctl] 收尾等待中：还有 {n} 条 windows 线在手头文件上（队列剩 {q} 个任务）")
        time.sleep(3)


with ThreadPoolExecutor(max_workers=NWORKERS) as lex:
    futs = [lex.submit(local_loop) for _ in range(NWORKERS)]
    _legacy_wex = None
    if WIN_WORKERS == 'auto':
        threading.Thread(target=_prober_loop, daemon=True, name='wp-prober').start()
        threading.Thread(target=_ctl_loop, daemon=True, name='dl-ctl').start()
        with _log_lock:
            print(f"下载线: 本地 {NWORKERS} + windows 受监督（启动 {WIN_LINES} 条，上限 {WIN_LINES_MAX}；"
                  f"存活口 {' '.join(_wport(u) for u in _pool_live) or '无，等探活'}）"
                  f" —— 运行中用 dl_ctl.py 调整", flush=True)
    elif WIN_LINES > 0:
        _legacy_wex = ThreadPoolExecutor(max_workers=max(1, WIN_LINES))
        futs += [_legacy_wex.submit(windows_loop) for _ in range(WIN_LINES)]
    for f in futs:
        f.result()
    _run.set()
    _kick.set()
    _pool.stop = True
    with _pool.cv:
        _pool.cv.notify_all()
    with _lock:
        _q, _done = len(_pending), completed
    _ctl_note(f"[ctl] 本地线收工（完成 {_done}/{len(tasks)}，队列剩 {_q}）→ 停止补线，等 windows 线收尾")
    _drain_win_lines()
    if _legacy_wex is not None:
        _legacy_wex.shutdown(wait=False)

_refresh_status()
print(f"\n=== DONE: {completed}/{len(tasks)} ===", flush=True)