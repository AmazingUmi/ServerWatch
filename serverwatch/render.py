"""Small, dependency-free terminal views with width-aware untrusted text."""
from __future__ import annotations

import math
from datetime import datetime, timezone
import unicodedata

PANELS = ("overview", "cpu", "memory", "gpu", "tasks")


def safe(value) -> str:
    """Remove terminal controls, line breaks, and invisible format characters."""
    return "".join(" " if unicodedata.category(c).startswith("C") or c in "\u2028\u2029" else c
                   for c in str(value))


def cell_width(text: str) -> int:
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1
               for c in text)


def clip(value, width: int) -> str:
    text = safe(value)
    if width <= 0:
        return ""
    if cell_width(text) <= width:
        return text
    limit = width - 1
    result = []
    used = 0
    for c in text:
        size = 0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1
        if used + size > limit:
            break
        result.append(c)
        used += size
    return "".join(result) + "~"


def _pad(value, width, right=False):
    text = clip(value, width)
    space = " " * max(0, width - cell_width(text))
    return space + text if right else text + space


def _num(value, digits=1, suffix=""):
    if value is None or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "N/A"
    return f"{value:.{digits}f}{suffix}"


def _bytes(value):
    if value is None:
        return "N/A"
    number = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(number) < 1024 or unit == "PiB":
            return f"{number:.1f}{unit}" if unit != "B" else f"{number:.0f}B"
        number /= 1024


def _duration(value):
    if value is None:
        return "N/A"
    seconds = int(value)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    return (f"{days}d " if days else "") + f"{hours:02d}:{minutes:02d}"


def _ratio(used, total):
    return _num(used / total * 100, suffix="%") if used is not None and total else "N/A"


def _section(body, title, width):
    if body and body[-1][0]:
        body.append(("", "normal"))
    heading = clip(title + "  ", width)
    body.append((heading + "─" * max(0, width - cell_width(heading)), "accent"))


def _table(body, rows, fields, width):
    """Name-first tables with explicit widths chosen for the current terminal."""
    body.append(("  ".join(_pad(label, size, right) for label, _, size, right in fields), "muted"))
    body.append(("─" * width, "muted"))
    for row in rows:
        cells = []
        for _, key, size, right in fields:
            value = key(row) if callable(key) else row.get(key, "-")
            cells.append(_pad(value, size, right))
        body.append(("  ".join(cells), "normal"))
    if not rows:
        body.append(("No observed processes.", "muted"))


def _process_table(body, rows, width, memory=False):
    if width >= 70:
        fields = [("TASK", "name", width - 43, False), ("USER", "user", 10, False),
                  ("PID", "pid", 7, True), ("CPU%", lambda p: _num(p.get("cpu_percent")), 7, True),
                  ("RSS", lambda p: _bytes(p.get("rss_bytes")), 11, True)]
    elif width >= 50:
        fields = [("TASK", "name", width - 31, False), ("PID", "pid", 7, True),
                  ("CPU%", lambda p: _num(p.get("cpu_percent")), 7, True),
                  ("RSS", lambda p: _bytes(p.get("rss_bytes")), 11, True)]
    else:
        fields = [("TASK", "name", max(1, width - 18), False), ("PID", "pid", 6, True),
                  ("RSS" if memory else "CPU%", lambda p: _bytes(p.get("rss_bytes")) if memory else _num(p.get("cpu_percent")), 8, True)]
    _table(body, rows, fields, width)


def _recent_table(body, rows, width):
    state = ("STATE", lambda p: "run" if p.get("running") else "ended", 5, False)
    seconds = ("CPU(s)", lambda p: _num(p.get("cpu_seconds"), 2), 8, True)
    if width >= 85:
        fields = [("TASK", "name", width - 54, False), ("PID", "pid", 7, True), state,
                  ("NOW%", lambda p: _num(p.get("cpu_percent")), 6, True),
                  ("AVG%", lambda p: _num(p.get("window_cpu_percent")), 6, True), seconds,
                  ("PEAK RSS", lambda p: _bytes(p.get("peak_rss_bytes")), 10, True)]
    elif width >= 65:
        fields = [("TASK", "name", width - 45, False), ("PID", "pid", 6, True), state,
                  ("AVG%", lambda p: _num(p.get("window_cpu_percent")), 6, True), seconds,
                  ("PEAK RSS", lambda p: _bytes(p.get("peak_rss_bytes")), 10, True)]
    else:
        fields = [("TASK", "name", max(1, width - 25), False), ("PID", "pid", 6, True), seconds, state]
    _table(body, rows, fields, width)


def _cards(body, cards, width, compact=False):
    """Shared numerical cards; no duplicated text progress bars."""
    count = len(cards)
    if width >= max(30, count * 26):
        base, extra = divmod(width - count - 1, count)
        sizes = [base + (index < extra) for index in range(count)]
        tops = [clip(" " + title + " ", size).ljust(size, "─") for (title, _, _), size in zip(cards, sizes)]
        body.append(("┌" + "┬".join(tops) + "┐", "accent"))
        for row, style in ((1, "section"), (2, "muted")):
            cells = [" " + _pad(card[row], size - 2) + " " for card, size in zip(cards, sizes)]
            body.append(("│" + "│".join(cells) + "│", style))
        body.append(("└" + "┴".join("─" * size for size in sizes) + "┘", "muted"))
    else:
        body.append(("┌" + "─" * max(0, width - 2) + "┐", "muted"))
        for label, value, detail in cards:
            line = _pad(label, 11) + " " + value
            separate_detail = bool(detail) and not compact
            combined = line + "  " + detail
            if width >= 50 and cell_width(combined) <= width - 4:
                line = combined
                separate_detail = False
            body.append(("│ " + _pad(line, max(0, width - 4)) + " │", "normal"))
            if separate_detail:
                body.append(("│ " + _pad(detail, max(0, width - 4)) + " │", "muted"))
        body.append(("└" + "─" * max(0, width - 2) + "┘", "muted"))


def _box(body, title, rows, width):
    heading = clip(" " + title + " ", max(0, width - 2))
    body.append(("┌" + heading + "─" * max(0, width - 2 - cell_width(heading)) + "┐", "accent"))
    for text in rows:
        body.append(("│ " + _pad(text, max(0, width - 4)) + " │", "normal"))
    body.append(("└" + "─" * max(0, width - 2) + "┘", "muted"))


def _clock(value):
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return stamp.strftime("%H:%M:%S")
        return stamp.astimezone(timezone.utc).strftime("%H:%M:%S UTC")
    except (ValueError, TypeError):
        return "N/A"


def _memory_pair(used, total):
    """A short same-unit pair for small resource cards."""
    if used is None or total is None:
        return "N/A"
    scale, unit = 1, "B"
    for candidate in ("KiB", "MiB", "GiB", "TiB"):
        if total < scale * 1024:
            break
        scale, unit = scale * 1024, candidate
    return f"{used / scale:.1f}/{total / scale:.1f}{unit}"


def _resource_cards(body, cpu, mem, gpu, width):
    cpu_used = cpu.get("used_percent")
    used, total = mem.get("used_bytes"), mem.get("total_bytes")
    mem_used = used / total * 100 if used is not None and total else None
    devices = gpu.get("devices", [])
    gpu_used = None
    gpu_detail = "No telemetry"
    if devices:
        values = [d.get("utilization_percent") for d in devices]
        if all(value is not None for value in values):
            gpu_used = max(values)
        used_values = [d.get("memory_used_bytes") for d in devices]
        total_values = [d.get("memory_total_bytes") for d in devices]
        complete = all(v is not None for v in used_values + total_values)
        pair = _memory_pair(sum(used_values), sum(total_values)) if complete else "N/A"
        gpu_detail = f"{len(devices)} GPUs  {pair}" if len(devices) > 1 else f"{pair} VRAM"
    gpu_label = ("Peak " if len(devices) > 1 and gpu_used is not None else "") + _num(gpu_used, suffix="%")
    _cards(body, [
        ("CPU [1]", _num(cpu_used, suffix="%"), f"{cpu.get('logical_cores', '?')} cores"),
        ("MEMORY [2]", _num(mem_used, suffix="%"), f"{_bytes(used)} / {_bytes(total)}"),
        ("GPU [3]", gpu_label, gpu_detail),
    ], width, compact=True)


def _overview_tasks(body, snapshot, width):
    rows = snapshot.get("recent", {}).get("processes", [])[:5]
    recent = bool(rows)
    if not rows:
        rows = snapshot.get("top_cpu", [])[:5]
    body.append(("", "normal"))
    body.append(("TOP TASKS  /  " + ("recent CPU" if recent else "current CPU"), "section"))
    # Keep names prominent; remove optional columns before narrowing names.
    if width >= 60:
        fields = [("TASK", "name", width - 31, False), ("PID", "pid", 7, True),
                  ("CPU%", "cpu", 7, True), ("RAM", "ram", 11, True)]
    elif width >= 38:
        fields = [("TASK", "name", width - 22, False), ("CPU%", "cpu", 7, True), ("RAM", "ram", 11, True)]
    else:
        fields = [("TASK", "name", max(1, width - 9), False), ("CPU%", "cpu", 7, True)]
    body.append(("  ".join(_pad(label, size, right) for label, _, size, right in fields), "muted"))
    body.append(("─" * width, "muted"))
    for process in rows:
        ended = recent and not process.get("running", True)
        values = {"name": process.get("name", "?"), "pid": process.get("pid", "?"),
                  "cpu": "ended" if ended else _num(process.get("cpu_percent")),
                  "ram": "-" if ended else _bytes(process.get("rss_bytes"))}
        body.append(("  ".join(_pad(values[key], size, right) for _, key, size, right in fields), "normal"))
    if not rows:
        body.append(("Waiting for samples...", "muted"))


def render(snapshot: dict, panel: str = "overview", width: int = 100,
           height: int | None = None, color: bool = False,
           interactive: bool = False, scroll: int = 0) -> str:
    if panel not in PANELS:
        panel = "overview"
    width = max(1, int(width))
    if height is not None and height <= 0:
        return ""
    cpu, mem, gpu = (snapshot.get(key, {}) for key in ("cpu", "memory", "gpu"))
    recent = snapshot.get("recent", {})
    age = snapshot.get("state_age_seconds", snapshot.get("cache_age_seconds"))
    stale = "  STALE" if snapshot.get("stale") else ""
    status = f"  cache {_num(age)}s old" if snapshot.get("stale") and age is not None else ""
    prefix, suffix = "ServerWatch  |  ", f"  |  {panel.upper()}{stale}"
    if cell_width(prefix + suffix) + 1 > width:
        prefix = "ServerWatch | "
    host = clip(snapshot.get("hostname", "?"), max(1, width - cell_width(prefix + suffix)))
    header = [(prefix + host + suffix, "title"),
              (f"{_clock(snapshot.get('time'))}  |  up {_duration(snapshot.get('uptime_seconds'))}{status}", "muted"),
              ("", "normal")]
    body = []
    if panel == "overview":
        _resource_cards(body, cpu, mem, gpu, width)
        _overview_tasks(body, snapshot, width)
    elif panel == "cpu":
        loads = " / ".join(_num(x, 2) for x in (cpu.get("load_average") or [])) or "N/A"
        _cards(body, [
            ("CPU", _num(cpu.get("used_percent"), suffix="%"), f"{cpu.get('logical_cores', '?')} logical cores"),
            ("LOAD", loads, "1 / 5 / 15 min"),
            ("I/O WAIT", _num(cpu.get("iowait_percent"), suffix="%"), f"Sample {_num(snapshot.get('sample_seconds'))}s"),
        ], width)
        _section(body, "LOGICAL CORES", width)
        cores = cpu.get("cores", [])
        per_row = max(1, (width + 3) // 19)
        size = max(1, (width - 3 * (per_row - 1)) // per_row)
        for offset in range(0, len(cores), per_row):
            cells = [_pad(f"{core.get('id', '?')}  {_num(core.get('used_percent'), suffix='%')}", size)
                     for core in cores[offset:offset + per_row]]
            body.append((" │ ".join(cells), "normal"))
        if not cores:
            body.append(("N/A", "muted"))
        _section(body, "TOP CPU  /  100% = 1 core", width)
        _process_table(body, snapshot.get("top_cpu", []), width)
    elif panel == "memory":
        used, total = mem.get("used_bytes"), mem.get("total_bytes")
        available = mem.get("available_bytes")
        swap_used, swap_total = mem.get("swap_used_bytes"), mem.get("swap_total_bytes")
        _cards(body, [
            ("RAM", _ratio(used, total), f"{_bytes(used)} / {_bytes(total)}"),
            ("AVAILABLE", _bytes(available), _ratio(available, total)),
            ("SWAP", f"{_bytes(swap_used)} / {_bytes(swap_total)}", "Disabled" if swap_total == 0 else _ratio(swap_used, swap_total)),
        ], width)
        _section(body, "TOP MEMORY  /  RSS", width)
        _process_table(body, snapshot.get("top_memory", []), width, memory=True)
    elif panel == "gpu":
        devices = gpu.get("devices", [])
        sampled = f"  /  sample {_num(gpu.get('age_seconds'))}s ago" if gpu.get("age_seconds") is not None else ""
        if not devices:
            _box(body, "GPU" + sampled, ["N/A  ·  No telemetry"], width)
        else:
            _section(body, "DEVICES" + sampled, width)
            for index, device in enumerate(devices):
                if index:
                    body.append(("", "normal"))
                utilization = "Usage " + _num(device.get("utilization_percent"), suffix="%")
                vram = "VRAM " + _memory_pair(device.get("memory_used_bytes"), device.get("memory_total_bytes"))
                temp = "Temp " + _num(device.get("temperature_c"), 0, "C")
                power = "Power " + _num(device.get("power_w"), 1, "W")
                rows = [utilization + "  |  " + vram, temp + "  |  " + power] if width >= 65 else [utilization, vram, temp, power]
                _box(body, f"{device.get('id', '?')}  {device.get('name', '?')}", rows, width)
        gpu_processes = snapshot.get("top_gpu", [])
        if devices or gpu_processes:
            _section(body, "COMPUTE PROCESSES  /  NVIDIA VRAM", width)
            if width >= 70:
                fields = [("TASK", "name", width - 34, False), ("PID", "pid", 6, True),
                          ("GPU", "gpu_id", 12, False), ("VRAM", lambda p: _bytes(p.get("memory_used_bytes")), 10, True)]
            else:
                fields = [("TASK", "name", max(1, width - 20), False), ("PID", "pid", 6, True),
                          ("VRAM", lambda p: _bytes(p.get("memory_used_bytes")), 10, True)]
            _table(body, gpu_processes, fields, width)
    else:
        _cards(body, [
            ("OBSERVED", _num(recent.get("observed_seconds"), 1, "s"), f"Sample {_num(snapshot.get('sample_seconds'))}s"),
            ("WINDOW", _num(recent.get("window_seconds"), 0, "s"), "Rolling"),
            ("RANKING", "CPU time", "100% = 1 core"),
        ], width)
        _section(body, "RECENT TASKS", width)
        _recent_table(body, recent.get("processes", []), width)
    warnings = list(dict.fromkeys(snapshot.get("warnings", [])))
    if warnings:
        if panel == "overview":
            body.append((f"! {len(warnings)} notices  /  details: 1-4", "warning"))
        else:
            _section(body, "NOTICES", width)
            for warning in warnings:
                body.append((f"! {warning}", "warning"))
    keys = "0 Overview   1 CPU   2 Memory   3 GPU   4 Tasks   j/k Scroll   q Quit"
    if panel == "overview":
        keys = "1 CPU   2 Memory   3 GPU   4 Tasks   q Quit"
    if width < 65:
        keys = "0..4 Panels  j/k Scroll  q Quit"
    footer = [(keys if interactive else f"{snapshot.get('process_count', 0)} visible processes  |  {panel}", "muted")]
    rule = "─"
    if height is None:
        lines = header + body + [(rule * width, "muted")] + footer
    elif height < len(header) + 2:
        lines = (header[:max(0, height - 1)] + footer)[:height]
    else:
        available = max(0, height - len(header) - 2)
        start = min(max(0, scroll), max(0, len(body) - available))
        visible = body[start:start + available]
        count = f"Rows {start + 1}-{start + len(visible)}/{len(body)}  |  " if len(body) > available else ""
        separator = clip(count, width).ljust(width, rule)
        lines = header + visible + [(separator, "muted")] + footer
    colors = {"title": "\x1b[1;36m", "accent": "\x1b[36m", "section": "\x1b[1m", "muted": "\x1b[2m", "warning": "\x1b[33m"}
    output = []
    for value, style in lines:
        value = clip(value, width).rstrip()
        output.append(colors[style] + value + "\x1b[0m" if color and style in colors and value else value)
    return "\n".join(output)
