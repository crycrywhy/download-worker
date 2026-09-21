"""Site configuration for the download chain.

Every value that changes from one deployment to the next lives here, resolved in
this order and no other:

    environment variable  ->  site.json  ->  neutral default in this file

Nothing in the chain may hardcode a path, a host, a port or a store layout.
Code asks for a key by name; this module decides where the value comes from.

``site.json`` is looked up at ``$DL_SITE_CONFIG``, else
``$XDG_CONFIG_HOME/download-worker/site.json``, else
``~/.config/download-worker/site.json``.  Generate a template to fill in with::

    python3 dl_config.py --export-example > site.json

A key that is not registered here is reported as a typo rather than silently
read, so a misspelled name fails loudly instead of falling back to a default.
"""
import argparse
import json
import os
import shlex
import sys
from collections import namedtuple

HERE = os.path.dirname(os.path.abspath(__file__))

UNSET = object()
REQUIRED = object()

SITE_CONFIG = os.environ.get('DL_SITE_CONFIG') or os.path.join(
    os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config'), 'download-worker', 'site.json')


def _state_home():
    """Where this chain keeps its own runtime files.

    Follows the XDG state convention when the environment sets it, so a
    deployment can put mutable state somewhere other than a home directory.
    """
    return os.environ.get('XDG_STATE_HOME') or os.path.join(
        os.path.expanduser('~'), '.local', 'state')


Spec = namedtuple('Spec', 'kind default env empty_is_value doc')


def _env_names(spec):
    e = spec.env
    if not e:
        return ()
    return (e,) if isinstance(e, str) else tuple(e)


SPEC = {
    # ---- where the work comes from -------------------------------------
    'paths.manifest': Spec('path', REQUIRED, None, False,
                           'CSV listing the items to fetch: one row per item, columns '
                           'name/key/group/tier/build/tag_status'),
    'paths.manifest_alt': Spec('path', '', None, True,
                               'optional second CSV holding direct URLs; empty = that '
                               'source is skipped entirely'),
    'paths.outdir': Spec('path', REQUIRED, None, False, 'root directory for primary transfers'),
    'paths.outdir_alt': Spec('path', '', None, True,
                             'root directory for secondary transfers; empty = same as outdir'),
    'paths.outdir_template': Spec('str', '{outdir}/{tier}/{name}_{key}', None, False,
                                  'where one finished item lands, relative to a root; '
                                  'placeholders: outdir, group, tier, name, key'),
    'paths.data_roots': Spec('list', [], None, False,
                             'extra roots scanned by the "already on disk?" check; '
                             'outdir and outdir_alt are always included'),
    'paths.shared_roots': Spec('list', [], None, False,
                               'subset of the roots that other writers also touch: a file '
                               'there is only trusted once its container trailer is intact'),
    'paths.stage_dir': Spec('path', '', 'STAGE_DIR', True,
                            'staging root on fast local storage; empty = write straight '
                            'to the destination'),
    'paths.alerts_file': Spec('path', os.path.join(_state_home(), 'download-worker', 'alerts.jsonl'),
                              ('WORKER_ALERTS', 'WORKER_ALERT_JSONL', 'WORKER_ALERTS_JSONL'), False,
                              'append-only alert stream; three env names accepted, first '
                              'non-empty wins'),
    'paths.notify_spool_dir': Spec('path', os.path.join(_state_home(), 'download-worker', 'notify'),
                                   None, True,
                                   'spool directory for completion notices; empty = '
                                   'notifications off'),

    # ---- object store ---------------------------------------------------
    'endpoints.s3_base': Spec('str', '', None, True,
                              'base URL of the object store the manifest points into; '
                              'empty = not configured'),
    'endpoints.prefix_template': Spec('str', 'objects/{prefix}/{build}/{flavor}/', None, False,
                                      'key layout inside the store; placeholders: prefix, '
                                      'build, flavor. Change it here rather than in code'),
    'endpoints.flavor_primary': Spec('str', 'primary', None, True,
                                     'per-source sub-directory for primary transfers'),
    'endpoints.flavor_secondary': Spec('str', 'secondary', None, True,
                                       'per-source sub-directory for secondary transfers'),
    'endpoints.object_suffix': Spec('str', '.bin', None, True,
                                    'only keys ending in this are treated as transfers'),
    'endpoints.tag_probe_pattern': Spec('str', 'TAG:Z:', None, True,
                                        'substring that marks a usable record in an object '
                                        'header; site-specific'),
    'endpoints.preferred_tags': Spec('list', ['tagged', 'primary'], None, False,
                                     'key substrings worth preferring, most preferred first'),
    'endpoints.generation_patterns': Spec('list', ['generation-2', 'generation-1'], None, False,
                                          'header markers naming a writer generation, most '
                                          'preferred first'),
    'endpoints.default_worker': Spec('str', '', None, True,
                                     'worker base URL used when --worker is omitted; '
                                     'empty = the flag is required'),
    'endpoints.channels': Spec('list', ['linux'], None, False,
                               'channels shown by the status views (local plus worker ports)'),
    'endpoints.default_registry': Spec('map', {}, None, False,
                                       'fallback worker registry used when the registry file '
                                       'is missing; empty = none'),

    # ---- external tools -------------------------------------------------
    'tools.stream_checker': Spec('path', '', 'STREAM_CHECKER', True,
                                 'program that dumps container headers/records, used to '
                                 'probe a remote object before fetching it'),
    'tools.s3_prefix_downloader': Spec('path', '', None, True,
                                       'prefix downloader used for the object-store channel'),
    'tools.integrity_checker': Spec('path', '', None, True,
                                    'program run on a finished file to verify it; empty = '
                                    'the transfer-time checks are all that ran, and that is said'),
    'tools.hole_scanner': Spec('path', '', None, True,
                               'program that reports zero-filled regions inside a file'),
    'tools.range_patcher': Spec('path', '', None, True,
                                'program that re-fetches a byte range in place'),
    'tools.status_collector': Spec('path', '', None, True,
                                   'optional hook run after each completed transfer'),
    'tools.inventory_refresh': Spec('path', '', None, True,
                                    'optional periodic inventory refresh hook'),
    'tools.tailscale_bin': Spec('path', 'tailscale', 'TS_BIN', False,
                                'tailscale executable; give a full path when it is not on PATH'),
    'tools.tailscale_sock': Spec('path', '', 'TS_SOCK', True,
                                 'tailscaled socket path, needed in userspace-networking mode'),

    # ---- identity -------------------------------------------------------
    'identity.driver': Spec('str', 'driver', ('DRIVER_NAME', 'DL_BROKER_TARGET'), False,
                            'name of this driver instance; it selects the state and control '
                            'files, so two drivers must not share one name'),
    'identity.owner': Spec('str', 'agent', 'DL_OWNER', False,
                           'owner label attached to submitted requests'),
    'domain.priority_groups': Spec('list', [], None, False,
                                   'groups scheduled ahead of everything else'),
    'domain.broker_default_cfg': Spec('map', {'v': 1, 'cap': {}, 'driver_owners': {},
                                              'external_script_owners': {},
                                              'contention_alert_after': 2}, None, False,
                                      'fallback policy used when the policy file is missing'),

    # ---- scheduling -----------------------------------------------------
    'ops.win_lines': Spec('int', 3, 'WIN_LINES', False,
                          'remote lines at startup; the control file overrides this at runtime'),
    'ops.win_lines_max': Spec('int', 8, 'WIN_LINES_MAX', False, 'hard ceiling on remote lines'),
    'ops.win_probe_interval': Spec('int', 600, 'WIN_PROBE_INTERVAL', False,
                                   'seconds between worker liveness probes'),
    'ops.worker_port_cap': Spec('int', 1, None, False,
                                'concurrent transfers allowed per worker port; each transfer '
                                'opens its own connections on top of this'),
    'ops.win_connections': Spec('int', 4, 'WIN_CONNECTIONS', False,
                                'connections per transfer on the remote path'),
    'ops.local_lines': Spec('int', 0, 'LOCAL_LINES', False,
                            'local lines; only used when there are no remote lines'),
    'ops.max_concurrent': Spec('int', 3, None, False, 'concurrency ceiling for local work'),
    'ops.win_gate': Spec('str', '', 'WIN_GATE', True,
                         'remote lines wait for this sentinel file to appear; empty = start now'),
    'ops.win_gate_max_wait': Spec('int', 48 * 3600, 'WIN_GATE_MAX_WAIT', False,
                                  'longest wait for the sentinel, in seconds'),
    'ops.stage_high_gb': Spec('float', 100.0, 'STAGE_HIGH_GB', False,
                              'staging usage above this pauses new work'),
    'ops.stage_low_gb': Spec('float', 60.0, 'STAGE_LOW_GB', False,
                             'drain to this level before resuming'),
    'ops.stage_max_file_gb': Spec('float', 90.0, 'STAGE_MAX_FILE_GB', False,
                                  'files larger than this bypass staging'),
    'ops.record_ledger': Spec('bool', False, None, False,
                              'write a ledger record per finished file; needs the ledger '
                              'module, which is a separate chain and not part of this package'),
    'ops.inventory_check_interval': Spec('int', 600, 'INVENTORY_CHECK_INTERVAL', False,
                                         'seconds between inventory refresh checks'),
}

ENV_KEYS = {n for s in SPEC.values() for n in _env_names(s)}
_KINDS = {'path': str, 'int': int, 'float': float, 'bool': bool, 'str': str, 'list': list, 'map': dict}

_cache = None
_problems = []
_warned = set()


def _warn(msg):
    if msg not in _warned:
        _warned.add(msg)
        print(f"[dl_config] {msg}", file=sys.stderr)


def _load_file():
    try:
        with open(SITE_CONFIG, encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        _problems.append({'key': '<site.json>', 'kind': 'badjson', 'detail': f'{type(e).__name__}: {e}',
                          'src': SITE_CONFIG})
        _warn(f"config file unreadable (treating as absent, defaults apply): "
              f"{SITE_CONFIG}: {type(e).__name__}: {e}")
        return {}


def _dig(d, dotted):
    cur = d
    for part in dotted.split('.'):
        if not isinstance(cur, dict) or part not in cur:
            return UNSET
        cur = cur[part]
    return cur


def _flatten(d, prefix=''):
    out = {}
    if isinstance(d, dict):
        if prefix and prefix[:-1] in SPEC and SPEC[prefix[:-1]].kind == 'map':
            out[prefix[:-1]] = d
            return out
        for k, v in d.items():
            if isinstance(k, str) and k.startswith('_'):
                continue
            out.update(_flatten(v, f'{prefix}{k}.'))
    elif prefix:
        out[prefix[:-1]] = d
    return out


def _coerce(key, spec, raw, src):
    want = _KINDS[spec.kind]
    try:
        if spec.kind == 'path':
            v = os.path.expanduser(str(raw))
            if v and not os.path.isabs(v):
                _problems.append({'key': key, 'kind': 'notabsolute', 'detail': v, 'src': src})
                _warn(f"{key} is not absolute ({v}) -- a relative path moves with the "
                      f"working directory, use an absolute one")
            return v
        if spec.kind == 'int':
            return int(raw)
        if spec.kind == 'float':
            return float(raw)
        if spec.kind == 'bool':
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() in ('1', 'true', 'yes', 'on')
        if spec.kind == 'str':
            return str(raw)
        if spec.kind == 'list':
            if isinstance(raw, (list, tuple)):
                return list(raw)
            return [x.strip() for x in str(raw).split(',') if x.strip()]
        if spec.kind == 'map':
            if isinstance(raw, dict):
                return raw
            return json.loads(str(raw))
    except Exception as e:
        _problems.append({'key': key, 'kind': 'badtype', 'detail': f'{want.__name__} <- {raw!r}: {e}',
                          'src': src})
        _warn(f"{key} has the wrong type ({raw!r}, expected {want.__name__}) -- using the default")
        return UNSET
    return raw


def _resolve(key):
    spec = SPEC[key]
    for name in _env_names(spec):
        raw = os.environ.get(name)
        if raw is not None and (raw != '' or spec.empty_is_value):
            v = _coerce(key, spec, raw, f'env:{name}')
            if v is not UNSET:
                return v, f'env {name}'
    raw = _dig(_cache, key)
    if raw is not UNSET:
        v = _coerce(key, spec, raw, f'file:{SITE_CONFIG}')
        if v is not UNSET:
            return v, 'site.json'
    if spec.default is REQUIRED:
        return UNSET, 'REQUIRED(missing)'
    return spec.default, 'default'


def reload():
    global _cache
    del _problems[:]
    _warned.clear()
    _cache = _load_file()
    known = set(SPEC)
    for k in _flatten(_cache):
        if k not in known:
            _problems.append({'key': k, 'kind': 'unknown',
                              'detail': 'unknown key in site.json (typo?)',
                              'src': SITE_CONFIG})
    return _cache


def _resolve_all():
    if _cache is None:
        reload()
    for k in SPEC:
        _resolve(k)


def get(key, default=UNSET):
    if _cache is None:
        reload()
    if key not in SPEC:
        _warn(f"code asked for an unregistered key: {key} (add it to dl_config.SPEC)")
        return None if default is UNSET else default
    v, _src = _resolve(key)
    if v is UNSET:
        return None if default is UNSET else default
    return v


def explain(key):
    if _cache is None:
        reload()
    if key not in SPEC:
        return f"{key}: **unregistered key** (not in SPEC)"
    v, src = _resolve(key)
    spec = SPEC[key]
    shown = '<unset>' if v is UNSET else repr(v)
    return (f"{key}\n  value  : {shown}\n  source : {src}\n  kind   : {spec.kind}"
            f"{' (empty string is a real value)' if spec.empty_is_value else ''}\n"
            f"  env    : {' / '.join(_env_names(spec)) or '(none)'}\n  doc    : {spec.doc}")


def problems():
    _resolve_all()
    return list(_problems)


def req(key, who=''):
    if _cache is None:
        reload()
    v = get(key)
    if v is None or v == '':
        spec = SPEC.get(key)
        env = ' / '.join(_env_names(spec)) if spec else ''
        env = env or '(this key has no env variable)'
        who_s = f' (needed by {who})' if who else ''
        print(f"ERROR: missing configuration {key}{who_s}\n"
              f"  Put it in {SITE_CONFIG} (dots are nesting, e.g. "
              f"\"paths\": {{\"manifest\": \"...\"}}),\n"
              f"  or set the environment variable {env}.\n"
              f"  About: {spec.doc if spec else ''}", file=sys.stderr)
        raise SystemExit(2)
    return v


def req_path(key, who=''):
    return req(key, who)


def path(key, default=None):
    v = get(key, default)
    return v


def get_int(key, default=0):
    v = get(key, default)
    try:
        return int(v)
    except Exception:
        return default


def get_float(key, default=0.0):
    v = get(key, default)
    try:
        return float(v)
    except Exception:
        return default


def get_bool(key, default=False):
    v = get(key, default)
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ('1', 'true', 'yes', 'on')


def get_list(key, default=None):
    v = get(key, default if default is not None else [])
    if isinstance(v, (list, tuple)):
        return list(v)
    if v is None:
        return []
    return [x.strip() for x in str(v).split(',') if x.strip()]


def get_map(key, default=None):
    v = get(key, default if default is not None else {})
    return v if isinstance(v, dict) else {}


_SECRET_HINT = ('token', 'secret', 'password', 'passwd')


def dump_all(mask_secrets=True):
    if _cache is None:
        reload()
    out = {}
    for k in SPEC:
        v, src = _resolve(k)
        shown = None if v is UNSET else v
        hay = ' '.join((k,) + _env_names(SPEC[k])).lower()
        if mask_secrets and any(h in hay for h in _SECRET_HINT):
            shown = '***'
        out[k] = {'value': shown, 'source': src, 'kind': SPEC[k].kind, 'set': v is not UNSET}
    return out


def _export_env():
    lines = []
    for k in SPEC:
        names = _env_names(SPEC[k])
        if not names:
            continue
        env = names[0]
        v, _ = _resolve(k)
        if v is UNSET:
            continue
        if isinstance(v, (list, tuple)):
            v = ','.join(str(x) for x in v)
        elif isinstance(v, dict):
            v = json.dumps(v, ensure_ascii=False)
        elif isinstance(v, bool):
            v = '1' if v else ''
        lines.append(f'export {env}={shlex.quote(str(v))}')
    return lines


def export_example():
    ph = {
        'path': lambda k, s: f'/absolute/path/to/{k.split(".")[-1]}',
        'str': lambda k, s: f'<{k.split(".")[-1]}>',
        'int': lambda k, s: 0, 'float': lambda k, s: 0.0, 'bool': lambda k, s: False,
        'list': lambda k, s: [], 'map': lambda k, s: {},
    }
    flat, docs = {}, {}
    for k, s in SPEC.items():
        flat[k] = ph[s.kind](k, s)
        docs[k] = s.doc + ('  [required]' if s.default is REQUIRED else '')
    return {'_note': 'Template site configuration. Copy to '
                     '~/.config/download-worker/site.json (or point DL_SITE_CONFIG at it) '
                     'and fill in your own values. Only deployment-specific values belong '
                     'here; the operational knobs under ops.* can be left out and the '
                     'built-in defaults will apply.',
            '_docs': docs,
            **nest(flat)}


def nest(flat):
    out = {}
    for k, v in flat.items():
        cur = out
        parts = k.split('.')
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = v
    return out


def main():
    ap = argparse.ArgumentParser(
        description='download-chain site configuration (env -> site.json -> neutral default)')
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--show', action='store_true', help='print every resolved value and where it came from')
    g.add_argument('--explain', metavar='KEY', help='explain one key')
    g.add_argument('--check', action='store_true', help='validate only: missing required, wrong type, unknown keys')
    g.add_argument('--export-env', action='store_true', help='print export lines')
    g.add_argument('--export-example', action='store_true', help='print a template site.json')
    g.add_argument('--get', metavar='KEY', help='print one resolved value, for shell callers')
    ap.add_argument('--json', action='store_true', help='--show as JSON')
    ap.add_argument('--all', action='store_true', help='--show including unset keys')
    a = ap.parse_args()
    reload()

    if a.explain:
        print(explain(a.explain))
        return 0
    if a.get:
        if a.get not in SPEC:
            print(f"unknown key: {a.get} (use --show to list them)", file=sys.stderr)
            return 1
        _v, _src = _resolve(a.get)
        if _v is UNSET:
            print(f"{a.get} is unset (env {SPEC[a.get].env or 'none'} / {SITE_CONFIG})", file=sys.stderr)
            return 1
        if isinstance(_v, bool):
            print('1' if _v else '')
        elif isinstance(_v, (list, dict)):
            print(json.dumps(_v, ensure_ascii=False))
        else:
            print(_v)
        return 0
    if a.export_env:
        for ln in _export_env():
            print(ln)
        return 0
    if a.export_example:
        print(json.dumps(export_example(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if a.check:
        d = dump_all()
        missing = [k for k, r in d.items() if not r['set'] and SPEC[k].default is REQUIRED]
        probs = problems()
        print(f"config file: {SITE_CONFIG}"
              f"{'' if os.path.exists(SITE_CONFIG) else ' (absent -- defaults apply)'}")
        print(f"registered: {len(SPEC)}   set: {sum(1 for r in d.values() if r['set'])}   "
              f"required-missing: {len(missing)}   problems: {len(probs)}")
        for k in missing:
            print(f"  x required but unset: {k}   (env {SPEC[k].env or 'none'})  {SPEC[k].doc}")
        for p in probs:
            print(f"  ! [{p['kind']}] {p['key']}: {p['detail']}")
        return 1 if (missing or probs) else 0
    d = dump_all()
    if a.json:
        print(json.dumps({'config_file': SITE_CONFIG, 'values': d, 'problems': problems()},
                         ensure_ascii=False, indent=1, sort_keys=True))
        return 0
    print(f"config file: {SITE_CONFIG}{'' if os.path.exists(SITE_CONFIG) else ' (absent)'}")
    for k in sorted(d):
        r = d[k]
        if not r['set'] and not a.all:
            continue
        mark = ' ' if r['set'] else '.'
        val = r['value']
        if isinstance(val, (list, dict)) and len(str(val)) > 70:
            val = f"<{r['kind']} len={len(val)}>"
        print(f"{mark} {k:<34} = {val!r:<48} [{r['source']}]")
    for p in problems():
        print(f"  ! [{p['kind']}] {p['key']}: {p['detail']}", file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
