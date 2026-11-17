"""Optional GPU telemetry without Python packages or privileged operations.

NVIDIA uses two bounded nvidia-smi queries. AMD uses the amdgpu sysfs ABI.
Unsupported measurements remain None rather than being interpreted as zero.
"""
from __future__ import annotations

import csv
import io
import math
from pathlib import Path
import re
import shutil
import subprocess

_MIB = 1024 * 1024
_GPU_FIELDS = "index,uuid,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw"
_PROCESS_FIELDS = "gpu_uuid,pid,process_name,used_gpu_memory"


def _number(value: str, scale: float = 1, maximum: float | None = None) -> float | None:
    try:
        number = float(value.strip()) * scale
    except (ValueError, TypeError):
        return None
    if not math.isfinite(number) or number < 0 or (maximum is not None and number > maximum):
        return None
    return number


def _bytes(value: str, scale: float = 1) -> int | None:
    number = _number(value, scale)
    return int(number) if number is not None else None


def _text(value: str) -> str | None:
    value = value.strip()
    if not value or value.lower() in {"n/a", "[n/a]", "not supported", "[not supported]", "unknown"}:
        return None
    return value


class GpuCollector:
    def __init__(self, backend: str = "auto", sys_root: str | Path = "/sys", timeout: float = 1.5):
        if backend not in {"auto", "nvidia", "amd", "none"}:
            raise ValueError("GPU backend must be auto, nvidia, amd, or none")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("GPU query timeout must be a positive finite number")
        self.backend = backend
        self.sys_root = Path(sys_root)
        self.timeout = timeout
        self.nvidia_smi = shutil.which("nvidia-smi") if backend in {"auto", "nvidia"} else None

    def sample(self) -> dict:
        result: dict = {"devices": [], "processes": [], "warnings": []}
        if self.backend in {"auto", "nvidia"}:
            self._nvidia(result)
        if self.backend in {"auto", "amd"}:
            self._amd(result)
        return result

    def _query(self, query: str, label: str, warnings: list[str]) -> list[list[str]] | None:
        try:
            completed = subprocess.run(
                [self.nvidia_smi, query, "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            warnings.append(f"NVIDIA {label} query timed out after {self.timeout:g}s")
            return None
        except OSError:
            warnings.append(f"NVIDIA {label} query could not start")
            return None
        if completed.returncode != 0:
            warnings.append(f"NVIDIA {label} query failed (exit {completed.returncode}); check driver access")
            return None
        try:
            return [row for row in csv.reader(io.StringIO(completed.stdout), skipinitialspace=True, strict=True) if row]
        except csv.Error:
            warnings.append(f"NVIDIA {label} query returned malformed CSV")
            return None

    def _nvidia(self, result: dict) -> None:
        warnings = result["warnings"]
        if self.nvidia_smi is None:
            if self.backend == "nvidia":
                warnings.append("NVIDIA telemetry unavailable: nvidia-smi was not found")
            return
        rows = self._query(f"--query-gpu={_GPU_FIELDS}", "device", warnings)
        if rows is None:
            return
        ids = {}
        for row in rows:
            if len(row) != 8 or not row[0].strip().isdigit():
                warnings.append("NVIDIA device query returned an invalid row")
                continue
            index, uuid, name, util, used, total, temp, power = row
            device_id = f"nvidia:{int(index)}"
            uuid = _text(uuid)
            if uuid is not None:
                ids[uuid] = device_id
            result["devices"].append({
                "id": device_id,
                "uuid": uuid,
                "vendor": "NVIDIA",
                "name": _text(name) or f"NVIDIA GPU {int(index)}",
                "utilization_percent": _number(util, maximum=100),
                "memory_used_bytes": _bytes(used, _MIB),
                "memory_total_bytes": _bytes(total, _MIB),
                "temperature_c": _number(temp),
                "power_w": _number(power),
            })
        if not result["devices"]:
            return
        process_rows = self._query(f"--query-compute-apps={_PROCESS_FIELDS}", "compute-process", warnings)
        if process_rows is None:
            return
        for row in process_rows:
            if len(row) != 4 or not row[1].strip().isdigit() or int(row[1]) <= 0:
                warnings.append("NVIDIA compute-process query returned an invalid row")
                continue
            uuid, pid, name, used = row
            uuid = _text(uuid)
            result["processes"].append({
                "pid": int(pid),
                # MIG UUIDs may not occur in the physical-device query. Retain
                # the UUID instead of incorrectly attributing to another GPU.
                "gpu_id": ids.get(uuid, uuid or "nvidia:unknown"),
                "gpu_uuid": uuid,
                "name": _text(name) or "unknown",
                "memory_used_bytes": _bytes(used, _MIB),
            })

    @staticmethod
    def _read(path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            return None

    def _amd(self, result: dict) -> None:
        found = 0
        try:
            cards = sorted((self.sys_root / "class/drm").glob("card[0-9]*"))
        except OSError:
            result["warnings"].append("AMD telemetry unavailable: cannot enumerate DRM devices")
            return
        for card in cards:
            if re.fullmatch(r"card\d+", card.name) is None:
                continue
            device = card / "device"
            if self._read(device / "vendor") != "0x1002":
                continue
            found += 1
            read = lambda name: self._read(device / name) or ""
            temperature = power = None
            try:
                sensors = sorted((device / "hwmon").glob("hwmon*"))
            except OSError:
                sensors = []
            for hwmon in sensors:
                if temperature is None:
                    temperature = _number(self._read(hwmon / "temp1_input") or "", 0.001)
                if power is None:
                    power = _number(self._read(hwmon / "power1_average") or "", 0.000001)
                    if power is None:
                        power = _number(self._read(hwmon / "power1_input") or "", 0.000001)
            result["devices"].append({
                "id": f"amd:{card.name}",
                "uuid": None,
                "vendor": "AMD",
                "name": self._read(device / "product_name") or f"AMD {card.name}",
                "utilization_percent": _number(read("gpu_busy_percent"), maximum=100),
                "memory_used_bytes": _bytes(read("mem_info_vram_used")),
                "memory_total_bytes": _bytes(read("mem_info_vram_total")),
                "temperature_c": temperature,
                "power_w": power,
            })
        if found:
            result["warnings"].append("AMD per-process GPU usage is unavailable with the sysfs backend")
        elif self.backend == "amd":
            result["warnings"].append("AMD telemetry unavailable: no visible AMD DRM device")
