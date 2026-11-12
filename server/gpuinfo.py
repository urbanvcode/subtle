"""
GPU and memory meters for the control panel (no torch).

  gpu()            name, driver, total / used VRAM (NVML, the driver's library; nvidia-smi as fallback)
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


class _NvmlMemory(ctypes.Structure):
    """nvmlMemory_v2_t: its "used" leaves out the driver's reserve, so it is the number nvidia-smi shows."""
    _fields_ = [("version", ctypes.c_uint), ("total", ctypes.c_ulonglong), ("reserved", ctypes.c_ulonglong),
                ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong)]


class _Nvml:
    """The NVIDIA driver's own library (what nvidia-smi itself uses), read in place: a memory reading
    takes microseconds instead of starting nvidia-smi twice (~120 ms of CPU every few seconds)."""

    def __init__(self):
        if sys.platform == "win32":
            names = ["nvml.dll", r"C:\Program Files\NVIDIA Corporation\NVSMI\nvml.dll"]
        else:
            names = ["libnvidia-ml.so.1", "libnvidia-ml.so"]
        lib = None
        for n in names:
            try:
                lib = ctypes.WinDLL(n) if sys.platform == "win32" else ctypes.CDLL(n)
                break
            except OSError:
                continue
        if lib is None or lib.nvmlInit_v2() != 0:
            raise OSError("NVML not available")
        h = ctypes.c_void_p()
        if lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(h)) != 0:
            raise OSError("no NVIDIA GPU")
        buf = ctypes.create_string_buffer(96)
        lib.nvmlDeviceGetName(h, buf, 96)
        name = buf.value.decode(errors="replace")
        lib.nvmlSystemGetDriverVersion(buf, 96)
        driver = buf.value.decode(errors="replace")
        v = ctypes.c_int(0)
        ok = lib.nvmlSystemGetCudaDriverVersion_v2(ctypes.byref(v)) == 0 and v.value
        self.lib, self.handle = lib, h
        self.static = {"name": name, "driver": driver, "cuda": f"{v.value // 1000}.{v.value % 1000 // 10}" if ok else ""}
        self.memory()                      # an old driver without the v2 call fails here, not on every refresh

    def memory(self) -> tuple[int, int]:
        m = _NvmlMemory()
        m.version = ctypes.sizeof(_NvmlMemory) | (2 << 24)
        if self.lib.nvmlDeviceGetMemoryInfo_v2(self.handle, ctypes.byref(m)) != 0:
            raise OSError("NVML memory query failed")
        return m.total >> 20, m.used >> 20


_nvml: _Nvml | None = None
_nvml_tried = False
_cuda: str | None = None              # from nvidia-smi's header: it doesn't change while Subtle runs


def gpu() -> dict:
    """{"name", "driver", "cuda", "total_mb", "used_mb"} of the first NVIDIA GPU, or {} without one."""
    global _nvml, _nvml_tried, _cuda
    if not _nvml_tried:
        _nvml_tried = True
        try:
            _nvml = _Nvml()
        except (OSError, AttributeError):
            _nvml = None
    if _nvml is not None:
        try:
            total, used = _nvml.memory()
            return dict(_nvml.static, total_mb=total, used_mb=used)
        except OSError:
            pass
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
    if _cuda is None:
        try:
            head = subprocess.run([exe], capture_output=True, text=True, timeout=5, creationflags=NO_WINDOW).stdout
            m = re.search(r"CUDA (?:UMD )?Version:\s*([\d.]+)", head)
            _cuda = m.group(1) if m else ""
        except (OSError, subprocess.SubprocessError):
            _cuda = ""
    info["cuda"] = _cuda
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
