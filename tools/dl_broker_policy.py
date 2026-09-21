import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

WIN_LINES_HARD_MAX = 32

DEFAULT_STAGE_LIMITS = {'mine': 100, 'other': 50, 'total': 150}


def _int(v, default=None):
    if isinstance(v, bool):
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def cap_of(cfg, port):
    c = (cfg or {}).get('cap') or {}
    v = c.get(int(port), c.get(str(port), 1))
    return max(0, _int(v, 1) if _int(v, 1) is not None else 1)


def stage_limits(cfg):
    lim = dict(DEFAULT_STAGE_LIMITS)
    raw = (cfg or {}).get('stage_limits_gb') or {}
    if isinstance(raw, dict):
        for k in lim:
            v = raw.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                lim[k] = float(v)
    return lim


def decide(obs, cfg, ctx):
    cfg = cfg or {}
    ctx = ctx or {}
    prev = ctx.get('prev') or {}
    ports = (obs or {}).get('ports') or {}
    win_max = max(1, _int(ctx.get('win_lines_max'), 8) or 8)
    alive_known = ctx.get('driver_alive_ports') is not None
    alive = sorted({_int(p) for p in (ctx.get('driver_alive_ports') or []) if _int(p) is not None})
    busy = sorted({_int(p) for p in (ctx.get('driver_busy_ports') or []) if _int(p) is not None})
    stage_gb = ctx.get('stage_gb')
    lim = stage_limits(cfg)
    warn_margin = cfg.get('stage_warn_margin_gb', 5)
    warn_margin = float(warn_margin) if isinstance(warn_margin, (int, float)) else 5.0
    avoid_after = max(1, _int(cfg.get('avoid_after'), 2) or 2)

    prev_avoid = sorted({_int(p) for p in (prev.get('policy_avoid_ports') or [])
                         if _int(p) is not None})
    unavoid_after = max(1, _int(cfg.get('unavoid_after'), avoid_after) or avoid_after)
    prev_streak = {}
    for k, v in (prev.get('avoid_streak') or {}).items():
        kk, vv = _int(k), _int(v)
        if kk is not None and vv is not None:
            prev_streak[kk] = vv
    prev_lift = {}
    for k, v in (prev.get('lift_streak') or {}).items():
        kk, vv = _int(k), _int(v)
        if kk is not None and vv is not None:
            prev_lift[kk] = vv
    prev_max = prev.get('broker_max_lines')
    if prev_max is not None:
        prev_max = max(0, min(WIN_LINES_HARD_MAX, _int(prev_max, 0) or 0))
    drop_after = max(1, _int(cfg.get('max_drop_after'), 2) or 2)
    drop_streak = max(0, _int(prev.get('max_drop_streak'), 0) or 0)

    actions, alerts = [], []
    shares, evidence, streak = {}, {}, {}
    lift = {}

    def usable(p, rec):
        return (not alive_known) or (p in alive) or (p in busy)

    for pstr, rec in sorted(ports.items(), key=lambda kv: _int(kv[0], 0) or 0):
        p = _int(pstr)
        if p is None:
            continue
        cap = cap_of(cfg, p)
        ext = _int((rec or {}).get('observed_external'))
        ss_ok = bool((rec or {}).get('ss_ok', True))
        if not usable(p, rec):
            shares[p] = 0
            evidence[p] = 'not-alive'
            streak[p] = 0
            continue
        if ext is None or not ss_ok:
            shares[p] = cap
            evidence[p] = 'blind'
            if p in prev_avoid:
                streak[p] = prev_streak.get(p, 0)
                lift[p] = 0
            continue
        my = max(0, cap - max(0, ext))
        shares[p] = my
        evidence[p] = 'ss'
        if my == 0 and ext > 0:
            n = prev_streak.get(p, 0) + 1
            if p in prev_avoid:
                n = max(n, avoid_after)
            streak[p] = n
            lift[p] = 0
        elif p in prev_avoid:
            n = prev_lift.get(p, 0) + 1
            if n >= unavoid_after:
                streak[p] = 0
                lift[p] = 0
            else:
                shares[p] = 0
                streak[p] = avoid_after
                lift[p] = n
        else:
            streak[p] = 0

    for p in alive:
        if p not in shares:
            shares[p] = cap_of(cfg, p)
            evidence[p] = 'blind'

    for p in prev_avoid:
        if p not in shares:
            shares[p] = 0
            evidence[p] = 'blind'
            streak[p] = max(prev_streak.get(p, 0), avoid_after)
            lift[p] = 0

    new_avoid = sorted(p for p, n in streak.items() if n >= avoid_after)
    total_share = sum(shares.values())

    have_ports = bool(ports) or bool(alive)
    evidence_ok = any(v == 'ss' for v in evidence.values())
    if not have_ports:
        new_max = 0
        actions.append('池空：不派 windows 线（本地线照常）')
        alerts.append(('BROKER_POOL_EMPTY', 'worker 池为空：windows 线数上限 → 0'))
    elif not evidence_ok:
        new_max = prev_max
        actions.append('观测不可用：保持上一轮上限 %s' % ('-' if prev_max is None else prev_max))
        alerts.append(('BROKER_BLIND', 'ss 观测不可用（口都在但读不到客户端）：保持上一轮上限不变'))
    else:
        raw_max = max(0, min(total_share, win_max))
        if prev_max is not None and raw_max < prev_max:
            drop_streak += 1
            if drop_streak < drop_after:
                new_max = prev_max
                actions.append('份额算得上限 %d < 上一轮 %d（连续 %d/%d 轮）→ 暂不降，维持 %d'
                               % (raw_max, prev_max, drop_streak, drop_after, prev_max))
            else:
                new_max = raw_max
                actions.append('可借份额合计 %d（上限 %d）→ windows 线数上限 %d（已连续 %d 轮走低）'
                               % (total_share, win_max, new_max, drop_streak))
        else:
            drop_streak = 0
            new_max = raw_max
            actions.append('可借份额合计 %d（上限 %d）→ windows 线数上限 %d'
                           % (total_share, win_max, new_max))

    if stage_gb is not None:
        used = float(stage_gb)
        if used >= lim['total']:
            new_max = 0
            actions.append('SSD_tmp %.1f/%.0f GB 已到硬上限 → windows 线数上限 0（只报不删）'
                           % (used, lim['total']))
            alerts.append(('BROKER_SSD_FULL',
                           'SSD_tmp %.1f/%.0f GB 超硬上限：停开 windows 线（我方 %.0f，其他(归K3) %.0f）'
                           % (used, lim['total'], lim['mine'], lim['other'])))
        elif used >= lim['total'] - warn_margin and new_max is not None:
            if prev_max is not None:
                new_max = min(new_max, prev_max)
            actions.append('SSD_tmp %.1f/%.0f GB 接近上限 → 只降不升（本轮上限 %d）'
                           % (used, lim['total'], new_max))
            alerts.append(('BROKER_SSD_HIGH',
                           'SSD_tmp %.1f/%.0f GB 接近硬上限：windows 线数只降不升'
                           % (used, lim['total'])))

    added = sorted(set(new_avoid) - set(prev_avoid))
    lifted = sorted(set(prev_avoid) - set(new_avoid))
    if added:
        alerts.append(('BROKER_AVOID', '让路 %s：外部占用已满，本进程不再取这些口'
                       % ' '.join(str(p) for p in added)))
    if lifted:
        alerts.append(('BROKER_UNAVOID', '恢复 %s：外部占用已解除'
                       % ' '.join(str(p) for p in lifted)))

    return {'policy_avoid_ports': new_avoid,
            'avoid_streak': {str(p): n for p, n in sorted(streak.items()) if n},
            'lift_streak': {str(p): n for p, n in sorted(lift.items()) if n},
            'max_drop_streak': drop_streak,
            'broker_max_lines': new_max,
            'shares': {str(p): shares[p] for p in sorted(shares)},
            'evidence': {str(p): evidence[p] for p in sorted(evidence)},
            'actions': actions,
            'alerts': alerts}


def plan_summary(plan):
    av = plan.get('policy_avoid_ports') or []
    return ('上限=%s 让路=%s 份额=%s'
            % ('-' if plan.get('broker_max_lines') is None else plan['broker_max_lines'],
               ','.join(str(p) for p in av) or '-',
               ','.join('%s:%s' % (k, v) for k, v in (plan.get('shares') or {}).items()) or '-'))



def _item_of(pi):
    if isinstance(pi, (tuple, list)) and len(pi) == 2 and isinstance(pi[1], dict):
        return pi[1]
    return pi if isinstance(pi, dict) else {}


def priority_of(item):
    return max(0, _int((item or {}).get('priority'), 0) or 0)


def is_front(item, cfg=None):
    thr = _int((cfg or {}).get('priority_front_threshold'), 90)
    return priority_of(item) >= (thr if thr is not None else 90)


class _RevStr(object):
    __slots__ = ('s',)

    def __init__(self, s):
        self.s = s

    def __lt__(self, other):
        return self.s > other.s

    def __eq__(self, other):
        return self.s == other.s


def rank_key(item):
    it = _item_of(item)
    return (priority_of(it), str(it.get('ts') or ''))


def order_inbox(items, cfg=None):
    return sorted(items, key=lambda pi: (-priority_of(_item_of(pi)),
                                         _RevStr(str(_item_of(pi).get('ts') or '')),
                                         str(_item_of(pi).get('id') or '')))
