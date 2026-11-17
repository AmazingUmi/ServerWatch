"""Interval metrics and bounded, PID-reuse-safe recent activity."""
from collections import deque
from copy import deepcopy


def cpu_delta(old, new):
    total = new['total_ticks'] - old['total_ticks']
    idle = new['idle_ticks'] - old['idle_ticks']
    wait = new['iowait_ticks'] - old['iowait_ticks']
    if total <= 0 or idle < 0:
        return None, None
    # Linux iowait may decrease; don't let that create >100% utilization.
    wait = max(0, wait)
    return round(max(0.0, min(100.0, (total-idle-wait)/total*100)), 1), round(min(100.0, wait/total*100), 1)


class Monitor:
    def __init__(self, window=60.0, top=5):
        self.window = window
        self.top = top
        self.previous = None
        self.started = None
        self.intervals = deque()

    def update(self, raw, gpu=None):
        now = raw['monotonic']
        old = self.previous
        if old and (raw['boot_id'] != old['boot_id'] or now <= old['monotonic']):
            old = None
            self.intervals.clear()
        if old is None:
            self.started = now
        elapsed = now - old['monotonic'] if old else 0.0
        old_procs = {(p['pid'], p['start_ticks']): p for p in old['processes']} if old else {}
        current = []
        contributions = {}
        for proc in raw['processes']:
            p = dict(proc)
            key = (p['pid'], p['start_ticks'])
            before = old_procs.get(key)
            ticks = p['cpu_ticks'] - before['cpu_ticks'] if before else None
            seconds = ticks / raw['clock_ticks'] if ticks is not None and ticks >= 0 else None
            p['cpu_percent'] = round(seconds / elapsed * 100, 1) if seconds is not None and elapsed else None
            total_mem = raw['memory']['total_bytes']
            p['memory_percent'] = round(p['rss_bytes'] / total_mem * 100, 2) if total_mem else None
            current.append(p)
            if old:
                contributions[key] = {'process': p, 'cpu_seconds': seconds or 0.0,
                                      'peak_rss_bytes': max(p['rss_bytes'], before['rss_bytes'] if before else 0),
                                      'last_seen': raw['time']}
        if old:
            self.intervals.append((old['monotonic'], now, contributions))
        cutoff = now - self.window
        while self.intervals and self.intervals[0][1] <= cutoff:
            self.intervals.popleft()
        recent = {}
        for start, end, items in self.intervals:
            fraction = (end - max(cutoff, start)) / (end - start)
            for key, item in items.items():
                record = recent.setdefault(key, {'cpu_seconds': 0.0, 'peak_rss_bytes': 0})
                record['cpu_seconds'] += item['cpu_seconds'] * fraction
                record['peak_rss_bytes'] = max(record['peak_rss_bytes'], item['peak_rss_bytes'])
                record['process'] = item['process']
                record['last_seen'] = item['last_seen']
        active = {(p['pid'], p['start_ticks']): p for p in current}
        coverage = min(self.window, now - self.started)
        history = []
        for key, record in recent.items():
            if record['cpu_seconds'] <= 0:
                continue
            p = dict(record.pop('process'))
            p.update(record)
            p['running'] = key in active
            p['cpu_percent'] = active[key]['cpu_percent'] if key in active else None
            p['window_cpu_percent'] = round(p['cpu_seconds'] / coverage * 100, 1) if coverage else None
            p['cpu_seconds'] = round(p['cpu_seconds'], 3)
            history.append(p)
        history.sort(key=lambda p: (-p['cpu_seconds'], p['pid']))
        cpu = dict(raw['cpu'])
        cpu['used_percent'], cpu['iowait_percent'] = cpu_delta(old['cpu'], cpu) if old else (None, None)
        old_cores = {p['id']: p for p in old['cpu'].get('cores', [])} if old else {}
        cores = []
        for core in cpu.get('cores', []):
            c = dict(core)
            c['used_percent'], c['iowait_percent'] = cpu_delta(old_cores[c['id']], c) if c['id'] in old_cores else (None, None)
            cores.append(c)
        cpu['cores'] = cores
        gpu = deepcopy(gpu or {'devices': [], 'processes': [], 'warnings': []})
        gpu_procs = sorted(gpu.get('processes', []), key=lambda p: (-(p.get('memory_used_bytes') or 0), p['pid']))
        snapshot = {
            'schema_version': 1, 'time': raw['time'], 'hostname': raw['hostname'],
            'uptime_seconds': raw['uptime_seconds'], 'sample_seconds': round(elapsed, 3),
            'cpu': cpu, 'memory': dict(raw['memory']), 'gpu': gpu,
            'process_count': len(current), 'top_count': self.top,
            'top_cpu': sorted(current, key=lambda p: (-(p['cpu_percent'] if p['cpu_percent'] is not None else -1), -p['rss_bytes'], p['pid']))[:self.top],
            'top_memory': sorted(current, key=lambda p: (-p['rss_bytes'], p['pid']))[:self.top],
            'top_gpu': gpu_procs[:self.top],
            'recent': {'window_seconds': self.window, 'observed_seconds': round(coverage, 3), 'processes': history[:self.top]},
            'warnings': list(raw.get('warnings', [])) + list(gpu.get('warnings', [])),
        }
        self.previous = raw
        return snapshot
