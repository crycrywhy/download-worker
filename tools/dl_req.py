import argparse
import contextlib
import io
import json
import os
import random
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import dl_control as C
import dl_broker_policy as POL
import dl_config as CFG

INBOX_DIR = os.environ.get('DL_INBOX_DIR') or os.path.join(HERE, 'inbox')
DEFAULT_DRIVER = CFG.req('identity.driver', 'driver')
DEFAULT_OWNER = CFG.get('identity.owner', 'agent')

ENV_MODEL = ('DL_MODEL', 'ANTHROPIC_MODEL', 'CLAUDE_MODEL', 'KIMI_MODEL')
ENV_HARNESS = ('DL_HARNESS', 'CLAUDE_CODE_ENTRYPOINT', 'KIMI_CODE_ENTRYPOINT',
               'KIMI_CODE_SESSION_ENTRYPOINT')
ENV_SESSION = ('DL_SESSION_ID', 'CLAUDE_CODE_SESSION_ID', 'KIMI_SESSION_ID')


def _env_first(names):
    for nm in names:
        v = os.environ.get(nm)
        if v and v.strip():
            return v.strip()
    return ''


def detect_origin(a):
    return {'model': (a.model or _env_first(ENV_MODEL)),
            'harness': (a.harness or _env_first(ENV_HARNESS)),
            'session_id': (a.session_id or _env_first(ENV_SESSION))}


def fmt_origin(origin):
    o = origin or {}
    return '模型=%s harness=%s session=%s' % (o.get('model') or '-', o.get('harness') or '-',
                                              o.get('session_id') or '-')


def now8(fmt='%Y-%m-%dT%H:%M:%S'):
    return time.strftime(fmt, time.localtime(time.time() + 8 * 3600))


def _slug(s):
    out = ''.join(ch if (ch.isalnum() or ch in '-_') else '' for ch in str(s or ''))
    return out[:24] or 'anon'


def make_id(owner):
    return '%s-%s-%04d' % (time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()),
                           _slug(owner), random.randint(0, 9999))


def entry_from_args(a):
    return {'name': a.item, 'source': a.source or ('SOURCE_B' if a.platform == 'gamma' else 'SOURCE_A'),
            'platform': a.platform, 'outdir': a.outdir, 'prefix': a.prefix, 'build': a.build,
            'object_key': a.object_key, 'url': a.url}


def resolve_priority(a):
    if a.level is not None:
        v = C.parse_level(a.level, None)
        if v is None:
            raise ValueError('认不出的等级 %r（可用：%s，或等价的中文名 加急/高/普通/低/批量）'
                             % (a.level, '/'.join(C.level_codes())))
        msg = ''
        if a.priority is not None and int(a.priority) != v:
            msg = ('（--level %s=%d 覆盖了 --priority %s）' % (a.level, v, a.priority))
        return v, msg
    if a.priority is not None:
        return int(a.priority), ''
    return C.DEFAULT_PRIORITY, ''


def cmd_submit(a):
    entry = entry_from_args(a)
    try:
        C.build_task(entry)
    except ValueError as e:
        print(f"[dl_req] 申请不合法，未提交：{e}", file=sys.stderr)
        return 4
    try:
        prio, note = resolve_priority(a)
    except ValueError as e:
        print(f"[dl_req] 参数不合法，未提交：{e}", file=sys.stderr)
        return 4
    rid = a.id or make_id(a.owner)
    origin = detect_origin(a)
    origin['rid'] = rid
    item = {'v': 1, 'id': rid, 'owner': a.owner, 'priority': prio,
            'level': C.level_name(prio),
            'ts': now8(), 'ts_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'task': entry,
            'res': {'est_gb': a.est_gb, 'needs': (['win'] if a.prefer_win else [])},
            'target_driver': a.driver,
            'origin': origin,
            'note': a.note or ''}
    os.makedirs(INBOX_DIR, exist_ok=True)
    dst = os.path.join(INBOX_DIR, rid + '.json')
    if os.path.exists(dst):
        print(f"[dl_req] 同名申请已存在：{dst}（换一个 --id，或先 dl_req.py show {rid}）",
              file=sys.stderr)
        return 4
    tmp = dst + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(item, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dst)
    except OSError as e:
        print(f"[dl_req] 写申请失败：{type(e).__name__}: {e}", file=sys.stderr)
        _try_rm(tmp)
        return 4
    print(f"已提交: {rid}")
    print(f"  归属 {a.owner}  等级 {C.level_name(prio)}（priority={prio}）  "
          f"提交时间 {item['ts'][11:16]}（UTC+8）  目标 driver {a.driver}{note}")
    print(f"  完成通知 {fmt_origin(origin)}")
    print(f"  任务 {entry['name']} [{entry['platform']}] → {entry['outdir']}")
    print(f"  文件 {dst}")
    print(f"  看状态: python3 dl_req.py show {rid}   （受理后移到 inbox/done/，被拒到 inbox/rejected/）")
    return 0


def _try_rm(p):
    try:
        os.unlink(p)
    except OSError:
        pass


def _read(p):
    try:
        with open(p, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def _sub_dir(sub):
    return os.path.join(INBOX_DIR, sub)


def cmd_list(a):
    q = []
    try:
        for n in sorted(os.listdir(INBOX_DIR)):
            if n.endswith('.json') and not n.startswith('.'):
                it = _read(os.path.join(INBOX_DIR, n))
                if it:
                    q.append(it)
    except OSError:
        pass
    q = POL.order_inbox(q)
    print(f"排队中（{len(q)}，按**派遣顺序**排：等级降 → 同级后提交者先）：" + ("" if q else "（空）"))
    for it in q:
        t = it.get('task') or {}
        print("  %-5s %-3s %-34s %-22s %-5s [%s] → %s"
              % (C.fmt_hm(it.get('ts')),
                 it.get('level') or C.level_name(it.get('priority')),
                 it.get('id'), t.get('name') or t.get('item'),
                 t.get('platform'), it.get('owner'), t.get('outdir')))
    for sub in ('rejected', 'done'):
        d = _sub_dir(sub)
        try:
            names = sorted((n for n in os.listdir(d) if n.endswith('.json')),
                           key=lambda n: os.path.getmtime(os.path.join(d, n)), reverse=True)
        except OSError:
            names = []
        if not names:
            continue
        show = names[:a.recent]
        print(f"\n最近 {sub}（{len(names)} 条，显示 {len(show)}）：")
        for n in show:
            it = _read(os.path.join(d, n)) or {}
            extra = ('  ← ' + it['_rejected']) if it.get('_rejected') else ''
            print("  %-34s %s%s" % (n[:-5], (it.get('_rejected_at') or ''), extra))
    return 0


def _find(rid):
    for sub in ('', 'rejected', 'done'):
        p = os.path.join(INBOX_DIR, sub, rid + '.json')
        if os.path.exists(p):
            return p, _read(p), (sub or 'queued')
    return None, None, None


def cmd_show(a):
    p, it, where = _find(a.id)
    if not p:
        print(f"[dl_req] 找不到申请 {a.id}（在 {INBOX_DIR} 下找过 queued/done/rejected）",
              file=sys.stderr)
        return 4
    print(f"位置: {where}\n文件: {p}")
    print(json.dumps(it, ensure_ascii=False, indent=1))
    return 0


def cmd_cancel(a):
    p, it, where = _find(a.id)
    if not p:
        print(f"[dl_req] 找不到申请 {a.id}", file=sys.stderr)
        return 4
    if where == 'queued':
        _try_rm(p)
        print(f"已撤销（删除排队中的申请）：{a.id}")
        return 0
    if where == 'done':
        print(f"[dl_req] {a.id} 已被 driver 受理（在 done/）——撤销请用 "
              f"dl_ctl.py drop <物种子串> 把它移出队列", file=sys.stderr)
        return 4
    print(f"[dl_req] {a.id} 已被拒（{it.get('_rejected')}），无需撤销", file=sys.stderr)
    return 0


MAX_BATCH = 1000

_BATCH_FIELDS = ('item', 'platform', 'outdir', 'source', 'prefix', 'build', 'object_key',
                 'url', 'owner', 'level', 'priority', 'driver', 'est_gb',
                 'prefer_win', 'note', 'id',
                 'model', 'harness', 'session_id')


def _ns_from_item(item, base):
    kw = {}
    for f in _BATCH_FIELDS:
        v = item.get(f, None)
        if v in (None, ''):
            v = getattr(base, f, None)
        kw[f] = v
    if kw['est_gb'] not in (None, ''):
        kw['est_gb'] = float(kw['est_gb'])
    else:
        kw['est_gb'] = None
    if kw['priority'] not in (None, ''):
        kw['priority'] = int(kw['priority'])
    else:
        kw['priority'] = None
    kw['prefer_win'] = bool(kw['prefer_win'])
    return argparse.Namespace(**kw)


def _load_batch(path):
    raw = sys.stdin.read() if path == '-' else open(path, encoding='utf-8').read()
    spec = json.loads(raw)
    if isinstance(spec, dict):
        items = spec.get('items')
        defaults = spec.get('defaults') or {}
    else:
        items, defaults = spec, {}
    if not isinstance(items, list) or not items:
        raise ValueError('清单里没有 items（应为 JSON 数组，或 {"items": [...]}）')
    if not isinstance(defaults, dict):
        raise ValueError('defaults 必须是对象')
    if len(items) > MAX_BATCH:
        raise ValueError('一次最多 %d 条（给了 %d 条）——拆成几批，或直接用 driver 的批量注入'
                         % (MAX_BATCH, len(items)))
    return items, defaults


def cmd_submit_batch(a):
    try:
        items, defaults = _load_batch(a.batch_file)
    except Exception as e:
        print('[dl_req] 批量清单读不了：%s: %s' % (type(e).__name__, e), file=sys.stderr)
        return 2

    t0 = time.time()
    ids, errs = [], []
    for i, it in enumerate(items, 1):
        if not isinstance(it, dict):
            errs.append({'index': i, 'error': 'item 不是对象'})
            print('[%3d/%d] ✗ （不是对象）' % (i, len(items)), flush=True)
            continue
        merged = dict(defaults)
        merged.update(it)
        sp = str(merged.get('item') or '')
        ns = _ns_from_item(merged, a)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                rc = cmd_submit(ns)
        except Exception as e:
            rc = 4
            buf.write('[dl_req] %s: %s\n' % (type(e).__name__, e))
        txt = buf.getvalue().strip()
        m = re.search(r'^已提交:\s*(\S+)', txt, re.M)
        rid = m.group(1) if m else None
        if rc == 0 and rid:
            ids.append(rid)
            print('[%3d/%d] ✓ %s  %s' % (i, len(items), rid, sp), flush=True)
        else:
            lines = [l for l in txt.splitlines() if l.strip()]
            emsg = next((l for l in lines if l.startswith('[dl_req]')),
                        lines[0] if lines else '未知错误')
            emsg = re.sub(r'^\[dl_req\]\s*', '', emsg)
            errs.append({'index': i, 'item': sp, 'id': merged.get('id'), 'error': emsg})
            print('[%3d/%d] ✗ %s：%s' % (i, len(items), sp or '?', emsg), flush=True)

    dt = time.time() - t0
    print('提交完成：成功 %d / 失败 %d（共 %d 条，用时 %.1fs）'
          % (len(ids), len(errs), len(items), dt))
    if errs:
        print('失败明细见上（申请**没有**写进收件箱；修好字段后重投即可）')
    if getattr(a, 'json', False):
        print(json.dumps({'total': len(items), 'submitted': len(ids), 'failed': len(errs),
                          'ids': ids[:50], 'ids_truncated': max(0, len(ids) - 50),
                          'errors': errs, 'elapsed_s': round(dt, 1),
                          'origin': detect_origin(a)},
                         ensure_ascii=False))
    return 0 if not errs else 4


def main(argv=None):
    ap = argparse.ArgumentParser(prog='dl_req.py', description='下载申请的统一提交入口（写 inbox/）')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('submit', help='提交下载申请（单条，或用 --batch-file 批量）')
    p.add_argument('--item', default=None, help='目标名（单条必填；批量时可省）')
    p.add_argument('--platform', default=None, choices=list(C.PLATFORMS),
                   help='数据平台（单条必填；批量时可省）')
    p.add_argument('--outdir', default=None, help='绝对路径（单条必填；批量时可省）')
    p.add_argument('--batch-file', default=None, metavar='FILE',
                   help='**批量提交**：JSON 清单文件，`-` 表示从 stdin 读。'
                        '格式：[{...}, ...] 或 {"defaults": {...}, "items": [{...}, ...]}；'
                        '每条 item 字段与命令行同名（item/platform/outdir/prefix/build/url/level/...），'
                        'item 里没给的用 defaults（再退到命令行给的 --owner/--level/--driver 等）。'
                        '上限 %d 条' % MAX_BATCH)
    p.add_argument('--json', action='store_true', help='批量时输出机器可读汇总（MCP 用）')
    p.add_argument('--source', default=None, choices=list(C.SOURCE_KINDS), help='默认按 --platform 推')
    p.add_argument('--prefix', default=None, help='SOURCE_A：对象前缀')
    p.add_argument('--build', default=None, help='SOURCE_A alpha：构建名')
    p.add_argument('--object-key', default=None, help='SOURCE_A beta：对象键')
    p.add_argument('--url', default=None, help='SOURCE_B：http(s) 直链')
    p.add_argument('--owner', default=DEFAULT_OWNER, help=f'申请人（默认 {DEFAULT_OWNER}）')
    p.add_argument('--level', default=None,
                   help='下载等级：' + C.level_desc() + '；**同等级内后提交的先跑**（默认 p2）')
    p.add_argument('--priority', type=int, default=None,
                   help='老写法（裸数值，0-100+）：越大越先派，≥90 插队；与 --level 二选一即可，'
                        '不传则按等级默认值 %d' % C.DEFAULT_PRIORITY)
    p.add_argument('--driver', default=DEFAULT_DRIVER, help=f'目标 driver（默认 {DEFAULT_DRIVER}）')
    p.add_argument('--est-gb', type=float, default=None, help='预估大小 GB（信息性）')
    p.add_argument('--prefer-win', action='store_true', help='标注"更适合 windows 线"（信息性）')
    p.add_argument('--note', default=None, help='备注（写进申请单，便于事后追溯）')
    p.add_argument('--id', default=None, help='自定义 id（默认 <UTC时间戳>-<owner>-<4位随机>）')
    p.add_argument('--model', default=None,
                   help='提交方模型名（默认从 env 探测：DL_MODEL / ANTHROPIC_MODEL / …）')
    p.add_argument('--harness', default=None,
                   help='提交方客户端（默认从 env 探测：DL_HARNESS / CLAUDE_CODE_ENTRYPOINT / …）')
    p.add_argument('--session-id', default=None, dest='session_id',
                   help='提交方 session id（默认从 env 探测：DL_SESSION_ID / CLAUDE_CODE_SESSION_ID；'
                        '**不传且探测不到 ⇒ 该申请不发完成通知**）')

    p = sub.add_parser('list', help='看排队中的申请（+ 最近 done/rejected）')
    p.add_argument('--recent', type=int, default=5, help='done/rejected 各显示几条（默认 5）')

    p = sub.add_parser('show', help='看一份申请的详情')
    p.add_argument('id')
    p = sub.add_parser('cancel', help='撤销一份还没被受理的申请')
    p.add_argument('id')

    a = ap.parse_args(argv)
    if a.cmd == 'submit':
        if getattr(a, 'batch_file', None):
            return cmd_submit_batch(a)
        if not (a.item and a.platform and a.outdir):
            ap.error('单条提交需要 --item / --platform / --outdir；'
                     '批量提交用 --batch-file（见 --help）')
    return {'submit': cmd_submit, 'list': cmd_list, 'show': cmd_show, 'cancel': cmd_cancel}[a.cmd](a)


if __name__ == '__main__':
    sys.exit(main())
