"""
GPU and memory meters for the control panel (no torch).

  gpu()            name, driver, total / used VRAM (nvidia-smi)
  ProcessMeter     VRAM per process from the Windows "GPU Process Memory" counters (the same
                   numbers Task Manager shows; nvidia-smi can't report them per process on
                   Windows), RAM per process from psutil (private working set, as Task Manager).
"""

import ctypes
import re
import shutil
import subprocess
import sys

NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def gpu() -> dict:
    """{"name", "driver", "cuda", "total_mb", "used_mb"} of the first NVIDIA GPU, or {} without one."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return {}
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name,driver_version,memory.total,memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, creationflags=NO_WINDOW,
        ).stdout.strip().splitlines()
        name, driver, total, used = [x.strip() for x in out[0].split(",")]
        info = {"name": name, "driver": driver, "total_mb": int(float(total)), "used_mb": int(float(used))}
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return {}
    try:
        head = subprocess.run([exe], capture_output=True, text=True, timeout=5, creationflags=NO_WINDOW).stdout
        m = re.search(r"CUDA (?:UMD )?Version:\s*([\d.]+)", head)
        info["cuda"] = m.group(1) if m else ""
    except (OSError, subprocess.SubprocessError):
        info["cuda"] = ""
    return info


class _CounterValue(ctypes.Structure):
    _fields_ = [("CStatus", ctypes.c_ulong), ("largeValue", ctypes.c_longlong)]


class _CounterItem(ctypes.Structure):
    _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", _CounterValue)]


class ProcessMeter:
    PDH_FMT_LARGE = 0x00000400
    PDH_MORE_DATA = 0x800007D2

    def __init__(self):
        self.query = None
        self.counter = None
        if sys.platform != "win32":
            return
        try:
            self.pdh = ctypes.WinDLL("pdh")
            q, c = ctypes.c_void_p(), ctypes.c_void_p()
            if self.pdh.PdhOpenQueryW(None, None, ctypes.byref(q)) != 0:
                return
            path = r"\GPU Process Memory(*)\Dedicated Usage"
            if self.pdh.PdhAddEnglishCounterW(q, path, None, ctypes.byref(c)) != 0:
                self.pdh.PdhCloseQuery(q)
                return
            self.query, self.counter = q, c
        except (OSError, AttributeError):
            self.query = None

    def vram_by_pid(self) -> dict[int, int]:
        """{pid: dedicated VRAM bytes} for every process using the GPU."""
        if self.query is None:
            return {}
        pdh = self.pdh
        if pdh.PdhCollectQueryData(self.query) != 0:
            return {}
        size, count = ctypes.c_ulong(0), ctypes.c_ulong(0)
        rc = pdh.PdhGetFormattedCounterArrayW(self.counter, self.PDH_FMT_LARGE, ctypes.byref(size),
                                              ctypes.byref(count), None)
        if (rc & 0xFFFFFFFF) != self.PDH_MORE_DATA or size.value == 0:
            return {}
        buf = (ctypes.c_byte * size.value)()
        rc = pdh.PdhGetFormattedCounterArrayW(self.counter, self.PDH_FMT_LARGE, ctypes.byref(size),
                                              ctypes.byref(count), buf)
        if rc != 0:
            return {}
        items = ctypes.cast(buf, ctypes.POINTER(_CounterItem))
        out: dict[int, int] = {}
        for i in range(count.value):
            m = re.match(r"pid_(\d+)_", items[i].szName or "")
            if m:
                pid = int(m.group(1))
                out[pid] = out.get(pid, 0) + max(0, items[i].FmtValue.largeValue)
        return out

    @staticmethod
    def ram(pid: int | None) -> int:
        """Private working set in bytes (what Task Manager's Memory column shows)."""
        if not pid:
            return 0
        try:
            import psutil
            p = psutil.Process(pid)
            try:
                return p.memory_full_info().uss
            except psutil.AccessDenied:
                return p.memory_info().rss
        except Exception:
            return 0
