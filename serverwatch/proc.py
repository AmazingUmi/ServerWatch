"""Read Linux procfs counters without third-party packages or shell commands."""

from __future__ import annotations

from datetime import datetime, timezone
import errno
import os
from pathlib import Path
import pwd
import socket
import time


class CollectorError(RuntimeError):
    """A required system counter could not be read."""


def parse_process_stat(text: str) -> dict:
    """Parse stat using the final ')' because comm may contain spaces and ')'s."""
    opening, closing = text.find("("), text.rfind(")")
    if opening < 1 or closing <= opening:
        raise ValueError("invalid process stat record")
    pid = int(text[:opening].strip())
    fields = text[closing + 1 :].split()
    if len(fields) < 22:
        raise ValueError("truncated process stat record")
    start_ticks = int(fields[19])
    cpu_ticks = int(fields[11]) + int(fields[12])
    if pid < 1 or start_ticks < 0 or cpu_ticks < 0:
        raise ValueError("invalid process counters")
    return {
        "pid": pid,
        "name": text[opening + 1 : closing],
        "state": fields[0],
        "start_ticks": start_ticks,
        "cpu_ticks": cpu_ticks,
        "rss_pages": max(0, int(fields[21])),
    }


class ProcCollector:
    def __init__(
        self,
        proc_root: Path | str = "/proc",
        clock_ticks: int | None = None,
        page_size: int | None = None,
    ):
        self.root = Path(proc_root)
        self.clock_ticks = (
            int(os.sysconf("SC_CLK_TCK")) if clock_ticks is None else clock_ticks
        )
        self.page_size = (
            int(os.sysconf("SC_PAGE_SIZE")) if page_size is None else page_size
        )
        if self.clock_ticks <= 0 or self.page_size <= 0:
            raise ValueError("clock_ticks and page_size must be positive")
        self.hostname = socket.gethostname()
        self._users: dict[int, str] = {}

    def _read_required(self, name: str) -> str:
        try:
            return (self.root / name).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise CollectorError(f"Cannot read {self.root / name}: {exc.strerror}") from exc

    def _user(self, uid: int) -> str:
        if uid not in self._users:
            try:
                self._users[uid] = pwd.getpwuid(uid).pw_name
            except KeyError:
                self._users[uid] = str(uid)
        return self._users[uid]

    def _cpu(self) -> dict:
        lines = self._read_required("stat").splitlines()
        try:
            aggregate = next(line.split()[1:] for line in lines if line.startswith("cpu "))
            values = [int(value) for value in aggregate]
            cores = []
            for line in lines:
                parts = line.split()
                if not parts or not parts[0].startswith("cpu") or not parts[0][3:].isdigit():
                    continue
                counters = [int(value) for value in parts[1:]]
                if len(counters) < 4 or min(counters) < 0:
                    raise ValueError("invalid per-core CPU counters")
                cores.append({
                    "id": parts[0],
                    "total_ticks": sum(counters[:8]),
                    "idle_ticks": counters[3],
                    "iowait_ticks": counters[4] if len(counters) > 4 else 0,
                })
            if len(values) < 4 or min(values) < 0 or not cores:
                raise ValueError("missing or invalid CPU counters")
        except (StopIteration, ValueError) as exc:
            raise CollectorError(f"Invalid CPU counters in {self.root / 'stat'}") from exc
        return {
            # guest and guest_nice already belong to user and nice: do not add twice.
            "total_ticks": sum(values[:8]),
            "idle_ticks": values[3],
            "iowait_ticks": values[4] if len(values) > 4 else 0,
            "logical_cores": len(cores),
            "cores": cores,
        }

    def _memory(self, warnings: list[str]) -> dict:
        fields: dict[str, int] = {}
        for line in self._read_required("meminfo").splitlines():
            key, separator, value = line.partition(":")
            if not separator:
                continue
            parts = value.split()
            # Only byte-sized entries are used below. Ignore other meminfo units.
            if len(parts) != 2 or parts[1] != "kB":
                continue
            try:
                amount = int(parts[0])
            except ValueError:
                continue
            if amount >= 0:
                fields[key] = amount * 1024
        total = fields.get("MemTotal")
        if total is None or total <= 0:
            raise CollectorError(f"Invalid MemTotal in {self.root / 'meminfo'}")
        available = fields.get("MemAvailable")
        if available is None:
            if all(key in fields for key in ("MemFree", "Buffers", "Cached")):
                available = (
                    fields["MemFree"] + fields["Buffers"] + fields["Cached"]
                    + fields.get("SReclaimable", 0) - fields.get("Shmem", 0)
                )
                warnings.append("MemAvailable missing; available memory is an estimate")
            else:
                warnings.append("Available memory counters are unavailable")
        if available is not None:
            available = max(0, min(total, available))
        swap_total, swap_free = fields.get("SwapTotal"), fields.get("SwapFree")
        if swap_total is None or swap_free is None:
            warnings.append("Swap counters are unavailable")
            swap_used = None
        else:
            swap_used = max(0, swap_total - min(swap_total, swap_free))
        return {
            "total_bytes": total,
            "available_bytes": available,
            "used_bytes": total - available if available is not None else None,
            "swap_total_bytes": swap_total,
            "swap_used_bytes": swap_used,
        }

    def _process(self, name: str) -> dict:
        # Opening a proc PID directory pins its identity even if the PID is reused.
        # The stat file and ownership are then read from that same directory.
        directory_fd = os.open(self.root / name, os.O_RDONLY | os.O_DIRECTORY)
        try:
            uid = os.fstat(directory_fd).st_uid
            stat_fd = os.open("stat", os.O_RDONLY, dir_fd=directory_fd)
            with os.fdopen(stat_fd, encoding="utf-8", errors="replace") as stream:
                process = parse_process_stat(stream.read())
            if process["pid"] != int(name):
                raise ValueError("process PID mismatch")
            process["rss_bytes"] = process.pop("rss_pages") * self.page_size
            process["uid"] = uid
            process["user"] = self._user(uid)
            return process
        finally:
            os.close(directory_fd)

    def sample(self) -> dict:
        warnings: list[str] = []
        cpu = self._cpu()
        sampled_at = time.monotonic()
        timestamp = datetime.now(timezone.utc).isoformat()
        memory = self._memory(warnings)
        try:
            load = [float(value) for value in (self.root / "loadavg").read_text().split()[:3]]
            if len(load) != 3 or any(not (0 <= value < float("inf")) for value in load):
                raise ValueError("invalid load average")
        except (OSError, ValueError):
            load = None
            warnings.append("Load average is unavailable")
        cpu["load_average"] = load
        try:
            uptime = float((self.root / "uptime").read_text().split()[0])
            if not (0 <= uptime < float("inf")):
                raise ValueError("invalid uptime")
        except (OSError, ValueError, IndexError):
            uptime = None
            warnings.append("Uptime is unavailable")
        try:
            boot_id = (self.root / "sys/kernel/random/boot_id").read_text().strip()
        except OSError:
            boot_id = ""
        try:
            with os.scandir(self.root) as entries:
                pids = [entry.name for entry in entries if entry.name.isascii() and entry.name.isdigit()]
        except OSError as exc:
            raise CollectorError(f"Cannot list processes in {self.root}: {exc.strerror}") from exc
        processes = []
        denied = malformed = other_errors = 0
        for pid in pids:
            try:
                processes.append(self._process(pid))
            except PermissionError:
                denied += 1
            except OSError as exc:
                # Exits while collecting are normal; inaccessible processes are not zeros.
                if exc.errno not in (errno.ENOENT, errno.ESRCH):
                    other_errors += 1
            except ValueError:
                malformed += 1
        if denied:
            warnings.append(f"Permission denied for {denied} process(es); rankings are partial")
        if malformed or other_errors:
            warnings.append(f"Skipped {malformed + other_errors} unreadable process record(s)")
        return {
            "monotonic": sampled_at,
            "time": timestamp,
            "hostname": self.hostname,
            "boot_id": boot_id,
            "uptime_seconds": uptime,
            "cpu": cpu,
            "memory": memory,
            "processes": processes,
            "warnings": warnings,
            "clock_ticks": self.clock_ticks,
        }
