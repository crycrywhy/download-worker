import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import dl_config as CFG

STATUS_JSON = os.path.join(HERE, 'worker_status.json')
ALERT_JSONL = CFG.path('paths.alerts_file', 'worker_alert')
TMUX_SESSION = 'wpalert_ds4_1'
TMUX_LOG = f'/tmp/{TMUX_SESSION}.log'
BRIDGE_CMD = f'tail -n0 -F {ALERT_JSONL} | head -1'


def now8():
    return time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time() + 8 * 3600))


def load_status(path):
    try:
        with open(path, encoding='utf-8') as f:
            st = json.load(f)
        return st if isinstance(st, dict) else None
    except Exception:
        return None


def snapshot(st):
    out = {}
    for p, d in (st.get('workers') or {}).items():
        if d.get('enabled') is False:
            continue
        out[str(p)] = bool(d.get('ok'))
    return out


def device_of(port, reg_path=None):
    try:
        with open(reg_path or os.path.join(HERE, 'workers.json'), encoding='utf-8') as f:
            reg = json.load(f)
        return (reg.get('workers', {}).get(str(port)) or {}).get('device', '')
    except Exception:
        return ''


def device_state(dev):
    if not dev:
        return '未登记设备名'
    try:
        import worker_pool as wp
        st = wp.ts_devices()
        v = None if st is None else st.get(dev)
    except Exception:
        v = None
    if v is True:
        return f'{dev} = 在线（机器开着 → 多半是 Windows 侧隧道断了）'
    if v is False:
        return f'{dev} = 明确离线（机器不在线 → 等它开机/唤醒）'
    return f'{dev} = 未知（tailscale 查不到，按实测为准）'


def listener_state(port):
    port_hex = f'{int(port):04X}'
    for path in ('/proc/net/tcp', '/proc/net/tcp6'):
        try:
            with open(path) as f:
                next(f, None)
                for line in f:
                    parts = line.split()
                    if len(parts) < 4:
                        continue
                    if parts[3] != '0A':
                        continue
                    if parts[1].rsplit(':', 1)[-1].upper() == port_hex:
                        return 'listening'
        except OSError:
            return 'unknown'
    return 'gone'


def listener_note(port):
    state = listener_state(port)
    if state == 'listening':
        return (f'本机 127.0.0.1:{port} 监听【仍在】⇒ Windows 侧隧道 ssh 还挂着，'
                '问题在 worker/机器侧（worker 卡死或机器休眠/断网）——重启隧道无用，要看那台机器')
    if state == 'gone':
        return (f'本机 127.0.0.1:{port} 监听【已消失】⇒ Windows 侧隧道进程已退出'
                '——在 Windows 上 start_tunnel / 等守护拉起即可恢复')
    return f'本机 127.0.0.1:{port} 监听状态未知（读 /proc/net/tcp 失败）'


def auto_unstick(port):
    try:
        import unstick_tunnel as ut
    except Exception as e:
        return {'ok': False, 'action': 'import-error',
                'detail': f'导入 unstick_tunnel 失败：{type(e).__name__}: {e}'}
    try:
        return ut.unstick(int(port), quiet=True)
    except Exception as e:
        return {'ok': False, 'action': 'error',
                'detail': f'{type(e).__name__}: {e}'}


def fmt_unstick(rec):
    act = rec.get('action', '')
    ok = rec.get('ok')
    if act == 'killed':
        head = f"✅ 已自动踢掉占口死会话（pid={rec.get('killed_pid')}）"
        tail = '        Windows 守护 ≤60s 会自动绑上，通常无需人工'
    elif act == 'gone':
        head = 'ℹ️ 自动踢口：端口已空（隧道进程已退出）'
        tail = '        等 Windows 守护拉起即可'
    elif act == 'healthy':
        head = 'ℹ️ 自动踢口：两轮探活有一轮通过 ⇒ 只是抖动，未动任何进程'
        tail = '        若该口反复"掉线又恢复"，去那台 Windows 看 worker 负载/网络'
    elif act == 'dry-run':
        head, tail = 'ℹ️ 自动踢口：dry-run，未动手', ''
    else:
        head = f"⚠️ 自动踢口未成功（{act or '未知'}）"
        tail = '        需人工：到那台 Windows 上 download-worker status / restart'
    lines = [f'  自动踢口：{head}']
    if rec.get('detail'):
        lines.append(f'        依据：{rec["detail"]}')
    if tail:
        lines.append(tail)
    return lines


def fmt_alert(port, event, d, dev_note, others, unstick_rec=None, tunnel_note=''):
    label = d.get('label', '')
    head = f'[{now8()} UTC+8] ' + ('⚠️ worker 下线' if event == 'DOWN' else '✅ worker 恢复上线')
    lines = ['=' * 72, f'{head}：{port}({label})']
    if event == 'DOWN':
        lines.append(f"  原因：{d.get('error') or '(未记录)'}"
                     + (f"（下线自 {d.get('down_since')}）" if d.get('down_since') else ''))
        lines.append(f'  诊断：{dev_note}')
        lines.append(f'  隧道：{tunnel_note or listener_note(port)}')
        if unstick_rec:
            lines.extend(fmt_unstick(unstick_rec))
        lines.append('  处置：① 监听已消失 → 在 Windows 上重启隧道：Start-ScheduledTask '
                     '-TaskName "Local Download Tunnel"（指南 §22）')
        lines.append('        ② 监听仍在 → 到那台 Windows 上看 worker 进程/机器状态，重启隧道没用')
        lines.append('        ③ 机器不在线 → 等开机；该线任务已自动重排队，driver 不会丢任务')
    else:
        lines.append(f"  最近成功：{d.get('last_ok_utc8', '')}")
    lines.append(f'  其余口：{others or "（无）"}')
    lines.append('=' * 72)
    return '\n'.join(lines)


def tmux_running(session=TMUX_SESSION):
    try:
        return subprocess.run(['tmux', 'has-session', '-t', session],
                              capture_output=True).returncode == 0
    except Exception:
        return False


def ensure_tmux(session=TMUX_SESSION, script=None):
    script = script or os.path.join(HERE, 'worker_alert.py')
    try:
        if subprocess.run(['tmux', 'has-session', '-t', session],
                          capture_output=True).returncode == 0:
            return True, f'tmux {session} 已在运行 ✓'
        cmd = f'python3 {script} --loop 2>&1 | tee -a {TMUX_LOG}'
        r = subprocess.run(['tmux', 'new-session', '-d', '-s', session, cmd],
                           capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            return True, f'tmux {session} 不在，已自动拉起 ✓（面板：tmux attach -t {session}）'
        return False, f'拉起 tmux {session} 失败：{(r.stderr or "").strip()[:120]}'
    except FileNotFoundError:
        return False, 'tmux 不可用（命令不存在）'
    except Exception as e:
        return False, f'拉起 tmux {session} 异常：{type(e).__name__}: {e}'


def recent_alerts(n=10, path=None):
    path = path or ALERT_JSONL
    try:
        with open(path, encoding='utf-8') as f:
            lines = [ln for ln in f if ln.strip()]
    except OSError:
        return []
    out = []
    for ln in lines[-n:]:
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
    return out


def boot_check(verbose=True):
    st = load_status(STATUS_JSON)
    ok_tmux, tmux_msg = ensure_tmux()
    ports = {}
    if st:
        for p, d in (st.get('workers') or {}).items():
            if d.get('enabled') is False:
                continue
            ports[p] = {'label': d.get('label', ''), 'ok': bool(d.get('ok')),
                        'error': d.get('error', ''), 'down_since': d.get('down_since')}
    alerts = recent_alerts(10)
    if verbose:
        print('─' * 72, flush=True)
        print(f"[alerts] 告警自检（{now8()} UTC+8）｜{tmux_msg}", flush=True)
        if ports:
            desc = '，'.join(f"{p}({v['label']})={'✅在线' if v['ok'] else '❌掉线'}"
                            + (f"（自 {v['down_since']}）" if v.get('down_since') else '')
                            for p, v in sorted(ports.items()))
            print(f"[alerts] 池状态：{desc}", flush=True)
        else:
            print('[alerts] 池状态：读不到 worker_status.json（哨兵没在跑？）', flush=True)
        downs = [(p, v) for p, v in sorted(ports.items()) if not v['ok']]
        if downs:
            print('[alerts] ⚠️ 当前有口掉线：'
                  + '，'.join(f"{p}({v['label']})" + (f"自 {v['down_since']}" if v.get('down_since') else '')
                             for p, v in downs)
                  + ' —— 恢复步骤见 README §1 / 指南 §22（先 tailscale ping 看机器在不在）', flush=True)
        if alerts:
            print(f"[alerts] 最近告警 {len(alerts)} 条（全量见 {ALERT_JSONL}）：", flush=True)
            for a in alerts:
                print(f"    {a.get('ts_utc8', '?')}  {a.get('event', '?'):<4} "
                      f"{a.get('port', '?')}({a.get('label', '')})  {a.get('error', '')}", flush=True)
        else:
            print('[alerts] 最近告警：无历史记录（流水为空；当前是否有口掉线看上面一行）', flush=True)
        print(f"[alerts] ⚠️ 本 session 必做——在后台挂唤醒桥：{BRIDGE_CMD}", flush=True)
        print(f"[alerts]     （它退出=有新掉线/恢复=唤醒你；醒来读 {ALERT_JSONL} 最新行，处置后重新挂上）", flush=True)
        print('─' * 72, flush=True)
    return {'tmux_ok': ok_tmux, 'tmux_msg': tmux_msg, 'ports': ports, 'alerts': alerts}


def record(rec, path=ALERT_JSONL):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except OSError as e:
        print(f'[worker_alert] 写流水失败: {e}', flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--loop', action='store_true', help='常驻模式（不退出）')
    ap.add_argument('--ensure', action='store_true',
                    help='确保告警 tmux 在跑 + 汇报端口与最近告警 + 提醒挂唤醒桥（每个 session 必跑）')
    ap.add_argument('--status', action='store_true', help='只打印当前状态')
    ap.add_argument('--file', default=STATUS_JSON, help='状态文件路径（默认 worker_status.json）')
    ap.add_argument('--interval', type=float, default=30, help='轮询间隔秒（默认 30）')
    ap.add_argument('--no-unstick', action='store_true',
                    help='关掉自动踢口（默认开：DOWN + 本机监听仍在时自动踢掉占口死会话）')
    a = ap.parse_args(argv)

    st = load_status(a.file)
    if a.ensure:
        r = boot_check(verbose=True)
        return 0 if r['tmux_ok'] else 1
    if a.status:
        if not st:
            print(f'读不到状态文件 {a.file}')
            return 2
        print(f"worker 状态（{st.get('updated_utc8', '?')} UTC+8）")
        for p, d in (st.get('workers') or {}).items():
            mark = '✅' if d.get('ok') else ('⏸ 停用' if d.get('enabled') is False else '❌')
            extra = ''
            if not d.get('ok') and d.get('enabled') is not False:
                extra = '  监听[仍在]' if listener_state(p) == 'listening' else '  监听[已消失]'
            print(f"  {mark} {p}({d.get('label', '')})  {d.get('error') or ''}"
                  + (f"  下线自 {d['down_since']}" if d.get('down_since') else '') + extra)
        return 0

    base = snapshot(st) if st else {}
    if base:
        downs = [f"{p}({d.get('label', '')})" for p, d in (st.get('workers') or {}).items()
                 if d.get('enabled') is not False and not d.get('ok')]
        print(f"[worker_alert] 启动（{'常驻' if a.loop else '桥'}模式，{a.interval:.0f}s/轮）"
              f"；基线：{ {p: ('up' if v else 'down') for p, v in base.items()} }"
              + (f'；当前处于下线的口：{", ".join(downs)}' if downs else ''), flush=True)
    else:
        print(f'[worker_alert] 启动：暂时读不到 {a.file}，等它出现…', flush=True)

    while True:
        time.sleep(a.interval)
        st = load_status(a.file)
        if not st:
            continue
        cur = snapshot(st)
        if not cur:
            continue
        if not base:
            base = cur
            continue
        fired = False
        for p in sorted(set(base) | set(cur)):
            if p not in cur:
                continue
            was = base.get(p)
            now = cur[p]
            if was is None or was == now:
                continue
            d = (st.get('workers') or {}).get(p, {})
            ev = 'DOWN' if not now else 'UP'
            listener = listener_state(p) if ev == 'DOWN' else ''
            tunnel_note = listener_note(p) if ev == 'DOWN' else ''
            unstick_rec = {}
            if ev == 'DOWN' and listener == 'listening' and not a.no_unstick:
                unstick_rec = auto_unstick(p)
            others = '，'.join(f"{q}={('up' if v else 'down')}" for q, v in sorted(cur.items()) if q != p)
            dev = device_of(p)
            note = device_state(dev) if ev == 'DOWN' else ''
            print(fmt_alert(p, ev, d, note, others, unstick_rec or None, tunnel_note), flush=True)
            record({'ts_utc8': now8(), 'event': ev, 'port': p, 'label': d.get('label', ''),
                    'error': d.get('error', ''), 'down_since': d.get('down_since'),
                    'device': dev, 'device_state': note,
                    'listener': listener,
                    'unstick': unstick_rec,
                    'others': others})
            fired = True
        base = cur
        if fired and not a.loop:
            return 0


if __name__ == '__main__':
    sys.exit(main())
