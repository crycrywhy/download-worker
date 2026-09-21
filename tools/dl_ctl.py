import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dl_control as C

HERE = os.path.dirname(os.path.abspath(__file__))
import dl_config as CFG

DEFAULT_DRIVER = CFG.req('identity.driver', 'driver')


def _paths(driver):
    base = os.environ.get('DL_CTL_DIR') or HERE
    return (os.path.join(base, 'control', driver + '.json'),
            os.path.join(base, 'state', 'driver_' + driver + '.json'))


def _read_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _show(driver, as_json=False):
    ctl_p, st_p = _paths(driver)
    spec = C.load_ctl(ctl_p)
    st = _read_state(st_p)
    ctl_live = (st or {}).get('ctl') or {}
    if as_json:
        print(json.dumps({'control_file': ctl_p, 'control': spec, 'state': st}, ensure_ascii=False, indent=1))
        return 0
    print(f"控制文件: {ctl_p}")
    print(f"  {C.summarize(spec)}")
    if st:
        age = _age_sec(st.get('ts'))
        alive = _pid_alive(st.get('pid')) if st.get('pid') else False
        print(f"driver: {st.get('name')} pid={st.get('pid')} {'存活' if alive else '不在'} "
              f"状态文件 {st.get('ts')}（{age}s 前）")
        print(f"  进度: {st.get('completed')}/{st.get('total')} 完成，队列 {st.get('queue_depth')} 个")
        print(f"  生效: rev_applied={ctl_live.get('rev_applied')} 线数(期望/实际)="
              f"{ctl_live.get('win_lines_desired')}/{ctl_live.get('win_lines_live')}"
              f"（上限 {ctl_live.get('max')}）暂停={ctl_live.get('paused')} "
              f"跳过={','.join(ctl_live.get('skip') or []) or '-'}")
        lines = st.get('lines') or []
        if lines:
            print("  线: " + ' | '.join(
                f"{l.get('backend')}{'/w' + str(l.get('worker')) if l.get('worker') else ''}:"
                f"{l.get('item')}({l.get('platform')})" for l in lines))
        if ctl_live.get('last_ops'):
            print("  上次应用" + ("（✗ 未生效）" if ctl_live.get('ok') is False else "")
                  + ": " + ' ; '.join(ctl_live['last_ops']))
        if ctl_live.get('rev_applied') is not None and spec and spec.get('rev') != ctl_live.get('rev_applied'):
            print(f"  ⚠️ 控制文件 rev={spec.get('rev')} 与已生效 rev={ctl_live.get('rev_applied')} 不一致"
                  f"（driver 未在跑，或刚写还没轮到）")
    else:
        print(f"driver: 状态文件读不到（{st_p}）—— driver 没在跑？")
    return 0


def _age_sec(ts):
    try:
        return int(time.time() - time.mktime(time.strptime(ts, '%Y-%m-%d %H:%M:%S')))
    except Exception:
        return -1


def _confirm(driver, rev, wait):
    _ctl_p, st_p = _paths(driver)
    t0 = time.time()
    while time.time() - t0 < wait:
        st = _read_state(st_p)
        live = (st or {}).get('ctl') or {}
        if live.get('rev_applied') is not None and int(live['rev_applied']) >= rev:
            if int(live.get('last_ops_rev') or -1) == rev and live.get('last_ops'):
                for r in live['last_ops']:
                    print(f"  {'✓' if live.get('ok', True) else '✗'} {r}")
            if not live.get('ok', True):
                print(f"[dl_ctl] ✗ rev={rev} 未被 driver 应用（原因见上）—— 没有生效，请修正后重试",
                      file=sys.stderr)
                return 4
            print(f"[dl_ctl] 已生效 rev={rev}（{time.time() - t0:.1f}s）")
            return 0
        time.sleep(0.5)
    print(f"[dl_ctl] ⚠️ 控制文件已写（rev={rev}），但 {wait}s 内没等到 driver 确认生效。\n"
          f"         若 driver 没在跑，下次启动时会自动读取本文件；用 dl_ctl.py show 复查。", file=sys.stderr)
    return 3


def _apply_command(a, ap, spec):
    ops, fields = [], {}

    if a.cmd == 'lines':
        now = not a.grace
        ops.append({'op': 'lines', 'n': a.n, 'now': now})
        fields['win_lines'] = a.n
    elif a.cmd == 'promote':
        ops.append({'op': 'promote', 'match': a.match, 'limit': a.limit})
    elif a.cmd == 'drop':
        ops.append({'op': 'drop', 'match': a.match})
    elif a.cmd == 'skip':
        ops.append({'op': 'skip', 'match': a.match})
        fields['skip'] = list(spec.get('skip') or []) + [m for m in a.match
                                                         if m not in (spec.get('skip') or [])]
    elif a.cmd == 'unskip':
        if a.all:
            ops.append({'op': 'unskip', 'all': True})
            fields['skip'] = []
        else:
            if not a.match:
                ap.error('unskip 需要一个子串，或用 --all 清空')
            ops.append({'op': 'unskip', 'match': a.match})
            fields['skip'] = [s for s in (spec.get('skip') or [])
                              if not any(m.lower() in s.lower() for m in a.match)]
    elif a.cmd == 'pause':
        ops.append({'op': 'pause'})
        fields['paused'] = True
    elif a.cmd == 'resume':
        ops.append({'op': 'resume'})
        fields['paused'] = False
    elif a.cmd == 'avoid':
        ops.append({'op': 'avoid', 'ports': list(a.ports)})
        cur = [p for p in (spec.get('avoid_ports') or [])]
        fields['avoid_ports'] = sorted(set(cur) | set(a.ports))
    elif a.cmd == 'unavoid':
        if a.all:
            ops.append({'op': 'unavoid', 'ports': list(spec.get('avoid_ports') or [])})
            fields['avoid_ports'] = []
        else:
            if not a.ports:
                ap.error('unavoid 需要端口号，或用 --all 清空')
            ops.append({'op': 'unavoid', 'ports': list(a.ports)})
            fields['avoid_ports'] = [p for p in (spec.get('avoid_ports') or [])
                                     if p not in set(a.ports)]
    elif a.cmd == 'broker-lines':
        fields['broker_max_lines'] = None if a.n is None else int(a.n)
    elif a.cmd == 'add':
        source = a.source or ('SOURCE_B' if a.platform == 'gamma' else 'SOURCE_A')
        entry = {'name': a.item, 'source': source, 'platform': a.platform, 'outdir': a.outdir,
                 'prefix': a.prefix, 'build': a.build, 'object_key': a.object_key, 'url': a.url}
        try:
            C.build_task(entry)
        except ValueError as e:
            print(f"[dl_ctl] add 参数不合法：{e}", file=sys.stderr)
            return None
        ops.append({'op': 'add', 'entries': [entry], 'front': bool(a.front), 'force': bool(a.force)})
        fields['paused'] = spec.get('paused', False)

    spec = C.bump(spec, ops, **fields)
    spec['by'] = 'dl_ctl.py@%s' % os.uname()[1]
    return spec


def main(argv=None):
    ap = argparse.ArgumentParser(prog='dl_ctl.py', description='driver 运行时控制')
    ap.add_argument('--driver', default=DEFAULT_DRIVER,
                    help='driver 名（默认取配置 identity.driver，即 %(default)s；也可用 DRIVER_NAME 环境变量）')
    ap.add_argument('--wait', type=float, default=10.0, help='等待 driver 确认生效的秒数（默认 10）')
    ap.add_argument('--lock-timeout', type=float, default=10.0,
                    help='等控制文件锁的秒数（默认 10；多个 agent 并发提交时排队）')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('show', help='看控制文件 + driver 实况')
    p.add_argument('--json', action='store_true')

    p = sub.add_parser('lines', help='改 windows 线数（默认立刻中断多余线）')
    p.add_argument('n', type=int)
    p.add_argument('--grace', action='store_true', help='优雅退休：让多余线把手头文件跑完再退')
    p.add_argument('--now', action='store_true', help='（默认行为）立刻中断并重排队')

    for name, helptext in (('promote', '匹配任务提到队头（插队）'), ('drop', '匹配任务移出队列')):
        p = sub.add_parser(name, help=helptext)
        p.add_argument('match', nargs='+')
        if name == 'promote':
            p.add_argument('--limit', type=int, default=None)

    p = sub.add_parser('skip', help='运行时跳过（item 子串）')
    p.add_argument('match', nargs='+')
    p = sub.add_parser('unskip', help='取消跳过')
    p.add_argument('match', nargs='*')
    p.add_argument('--all', action='store_true')

    sub.add_parser('pause', help='暂停取新任务')
    sub.add_parser('resume', help='恢复取任务')

    p = sub.add_parser('avoid', help='避开 worker 口（只影响下次取口，不打断在途）')
    p.add_argument('ports', nargs='+', type=int)
    p = sub.add_parser('unavoid', help='取消避开')
    p.add_argument('ports', nargs='*', type=int)
    p.add_argument('--all', action='store_true')

    p = sub.add_parser('broker-lines', help='broker 建议的线数上限（driver 取 min(win_lines, 它)）')
    p.add_argument('n', nargs='?', type=int, default=None, help='省略 = 清掉该字段（回到不管）')

    p = sub.add_parser('lease', help='worker 口租约与争用（只读；看谁在占哪个口）')
    p.add_argument('action', nargs='?', default='show', choices=['show', 'why'])
    p.add_argument('port', nargs='?', type=int, help='lease why 需要的端口号')
    p.add_argument('--json', action='store_true')
    p.add_argument('--traffic', action='store_true', help='顺带做 2s 流量采样（慢一点）')

    p = sub.add_parser('add', help='注入新下载任务')
    p.add_argument('--item', required=True)
    p.add_argument('--platform', required=True, choices=list(C.PLATFORMS))
    p.add_argument('--outdir', required=True, help='绝对路径（落地目录）')
    p.add_argument('--source', default=None, choices=list(C.SOURCE_KINDS), help='默认按 --platform 推')
    p.add_argument('--prefix', default=None, help='SOURCE_A：对象前缀，如 item-001')
    p.add_argument('--build', default=None, help='SOURCE_A alpha：构建名')
    p.add_argument('--object-key', default=None, help='SOURCE_A beta：对象键')
    p.add_argument('--url', default=None, help='SOURCE_B：http(s) 直链')
    p.add_argument('--front', action='store_true', help='插到队头而不是队尾')
    p.add_argument('--force', action='store_true', help='允许与队列/在途重复')

    a = ap.parse_args(argv)
    driver, wait = a.driver, a.wait
    ctl_p, _ = _paths(driver)

    if a.cmd == 'show':
        return _show(driver, a.json)

    if a.cmd == 'lease':
        import dl_broker_observe as OBS
        if a.action == 'why':
            if not a.port:
                ap.error('lease why 需要端口号，例如：dl_ctl.py lease why <端口>')
            return OBS.why(a.port)
        st = OBS.observe(OBS.load_cfg(), with_traffic=bool(a.traffic))
        if a.json:
            print(json.dumps(st, ensure_ascii=False, indent=1))
        else:
            OBS.print_state(st)
        return 0

    try:
        with C.locked(ctl_p, timeout=a.lock_timeout):
            spec = C.load_ctl(ctl_p) or C.new_spec()
            spec = _apply_command(a, ap, spec)
            if spec is None:
                return 4
            ok, msg = C.validate(spec)
            if not ok:
                print(f"[dl_ctl] 生成的控制文件没通过校验，未写入：{msg}", file=sys.stderr)
                return 4
            C.save_ctl(ctl_p, spec)
            rev = int(spec['rev'])
    except TimeoutError as e:
        print(f"[dl_ctl] ✗ 没拿到控制文件锁：{e}\n"
              f"         另一个 agent 正在提交同一份控制文件，稍后重试或加大 --lock-timeout。",
              file=sys.stderr)
        return 4
    return _confirm(driver, rev, wait)


if __name__ == '__main__':
    sys.exit(main())
