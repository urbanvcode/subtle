"""
Windows 10 2004+ / 11: record the sound of ONE program (and the programs it started), e.g. only Discord,
with Windows' process loopback (ActivateAudioInterfaceAsync + AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK).
The rest of what the PC plays is not heard.

ProcessRecorder(pid).record(numframes) returns float32 mono at 16 kHz, like soundcard's recorder, so
pcaudio.py can use either.
"""

import ctypes
import threading
import time
from ctypes import POINTER, byref, c_float, c_uint32, c_uint64, c_void_p, wintypes

import numpy as np

import comtypes
from comtypes import COMMETHOD, GUID, HRESULT, IUnknown

SR = 16000

# ---- COM interfaces ---------------------------------------------------------------------------
class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [("wFormatTag", wintypes.WORD), ("nChannels", wintypes.WORD), ("nSamplesPerSec", wintypes.DWORD),
                ("nAvgBytesPerSec", wintypes.DWORD), ("nBlockAlign", wintypes.WORD),
                ("wBitsPerSample", wintypes.WORD), ("cbSize", wintypes.WORD)]


class IAudioClient(IUnknown):
    _iid_ = GUID("{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}")
    _methods_ = [
        COMMETHOD([], HRESULT, "Initialize", (["in"], c_uint32, "ShareMode"), (["in"], wintypes.DWORD, "StreamFlags"),
                  (["in"], ctypes.c_longlong, "hnsBufferDuration"), (["in"], ctypes.c_longlong, "hnsPeriodicity"),
                  (["in"], POINTER(WAVEFORMATEX), "pFormat"), (["in"], POINTER(GUID), "AudioSessionGuid")),
        COMMETHOD([], HRESULT, "GetBufferSize", (["out"], POINTER(c_uint32), "pNumBufferFrames")),
        COMMETHOD([], HRESULT, "GetStreamLatency", (["out"], POINTER(ctypes.c_longlong), "phnsLatency")),
        COMMETHOD([], HRESULT, "GetCurrentPadding", (["out"], POINTER(c_uint32), "pNumPaddingFrames")),
        COMMETHOD([], HRESULT, "IsFormatSupported", (["in"], c_uint32, "ShareMode"),
                  (["in"], POINTER(WAVEFORMATEX), "pFormat"), (["out"], POINTER(POINTER(WAVEFORMATEX)), "ppClosest")),
        COMMETHOD([], HRESULT, "GetMixFormat", (["out"], POINTER(POINTER(WAVEFORMATEX)), "ppDeviceFormat")),
        COMMETHOD([], HRESULT, "GetDevicePeriod", (["out"], POINTER(ctypes.c_longlong), "a"),
                  (["out"], POINTER(ctypes.c_longlong), "b")),
        COMMETHOD([], HRESULT, "Start"),
        COMMETHOD([], HRESULT, "Stop"),
        COMMETHOD([], HRESULT, "Reset"),
        COMMETHOD([], HRESULT, "SetEventHandle", (["in"], wintypes.HANDLE, "eventHandle")),
        COMMETHOD([], HRESULT, "GetService", (["in"], POINTER(GUID), "riid"), (["out"], POINTER(c_void_p), "ppv")),
    ]


class IAudioCaptureClient(IUnknown):
    _iid_ = GUID("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetBuffer", (["out"], POINTER(POINTER(ctypes.c_byte)), "ppData"),
                  (["out"], POINTER(c_uint32), "pNumFramesToRead"), (["out"], POINTER(wintypes.DWORD), "pdwFlags"),
                  (["out"], POINTER(c_uint64), "pu64DevicePosition"), (["out"], POINTER(c_uint64), "pu64QPCPosition")),
        COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], c_uint32, "NumFramesRead")),
        COMMETHOD([], HRESULT, "GetNextPacketSize", (["out"], POINTER(c_uint32), "pNumFramesInNextPacket")),
    ]


class IActivateAudioInterfaceAsyncOperation(IUnknown):
    _iid_ = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetActivateResult", (["out"], POINTER(HRESULT), "activateResult"),
                  (["out"], POINTER(POINTER(IUnknown)), "activatedInterface")),
    ]


class IActivateAudioInterfaceCompletionHandler(IUnknown):
    _iid_ = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
    _methods_ = [
        COMMETHOD([], HRESULT, "ActivateCompleted",
                  (["in"], POINTER(IActivateAudioInterfaceAsyncOperation), "activateOperation")),
    ]


class IAgileObject(IUnknown):
    _iid_ = GUID("{94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90}")
    _methods_ = []


class _Done(comtypes.COMObject):
    _com_interfaces_ = [IActivateAudioInterfaceCompletionHandler, IAgileObject]

    def __init__(self):
        super().__init__()
        self.event = threading.Event()

    def ActivateCompleted(self, op):
        self.event.set()
        return 0


# AUDIOCLIENT_ACTIVATION_PARAMS inside a PROPVARIANT (VT_BLOB)
class _LoopbackParams(ctypes.Structure):
    _fields_ = [("TargetProcessId", wintypes.DWORD), ("ProcessLoopbackMode", c_uint32)]


class _ActivationParams(ctypes.Structure):
    _fields_ = [("ActivationType", c_uint32), ("ProcessLoopbackParams", _LoopbackParams)]


class _Blob(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.ULONG), ("pBlobData", c_void_p)]


class _PropVariant(ctypes.Structure):
    _fields_ = [("vt", wintypes.USHORT), ("r1", wintypes.WORD), ("r2", wintypes.WORD), ("r3", wintypes.WORD),
                ("blob", _Blob)]


VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"
ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
INCLUDE_TARGET_PROCESS_TREE = 0
AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
WAVE_FORMAT_IEEE_FLOAT = 3
VT_BLOB = 65


def supported() -> bool:
    """Windows 10 2004 (build 19041) or newer."""
    try:
        return ctypes.windll.ntdll and _build() >= 19041
    except Exception:
        return False


def _build() -> int:
    class OSV(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("major", wintypes.DWORD), ("minor", wintypes.DWORD),
                    ("build", wintypes.DWORD), ("platform", wintypes.DWORD), ("csd", wintypes.WCHAR * 128)]
    v = OSV()
    v.size = ctypes.sizeof(OSV)
    ctypes.windll.ntdll.RtlGetVersion(byref(v))
    return int(v.build)


class ProcessRecorder:
    """Context manager; record(numframes) -> float32 [numframes, 1] at 16 kHz (blocks until there is audio
    or ~0.1 s passed: silence is returned while the program plays nothing)."""

    def __init__(self, pid: int):
        self.pid = int(pid)
        self.client = None
        self.capture = None
        self.pending = np.zeros(0, np.float32)

    def __enter__(self):
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except OSError:
            pass                           # this thread already uses COM (comtypes sets up the main thread)
        params = _ActivationParams(ACTIVATION_TYPE_PROCESS_LOOPBACK, _LoopbackParams(self.pid, INCLUDE_TARGET_PROCESS_TREE))
        pv = _PropVariant()
        pv.vt = VT_BLOB
        pv.blob.cbSize = ctypes.sizeof(params)
        pv.blob.pBlobData = ctypes.cast(byref(params), c_void_p)
        done = _Done()
        op = POINTER(IActivateAudioInterfaceAsyncOperation)()
        activate = ctypes.windll.mmdevapi.ActivateAudioInterfaceAsync
        activate.argtypes = [wintypes.LPCWSTR, POINTER(GUID), c_void_p, c_void_p, c_void_p]
        activate.restype = HRESULT
        handler = done.QueryInterface(IActivateAudioInterfaceCompletionHandler)
        activate(VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK, byref(IAudioClient._iid_), byref(pv),
                 ctypes.cast(handler, c_void_p), byref(op))
        if not done.event.wait(5):
            raise TimeoutError("Windows did not open the program's sound")
        hr, unk = op.GetActivateResult()
        if hr != 0:
            raise OSError(f"Windows refused to record that program (0x{hr & 0xffffffff:08x})")
        self.client = unk.QueryInterface(IAudioClient)
        fmt = WAVEFORMATEX(WAVE_FORMAT_IEEE_FLOAT, 1, SR, SR * 4, 4, 32, 0)
        self.client.Initialize(AUDCLNT_SHAREMODE_SHARED,
                               AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM
                               | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY,
                               2_000_000, 0, byref(fmt), None)          # 200 ms buffer
        ptr = self.client.GetService(byref(IAudioCaptureClient._iid_))
        self.capture = ctypes.cast(ptr, POINTER(IAudioCaptureClient))
        self.client.Start()
        return self

    def __exit__(self, *a):
        try:
            if self.client is not None:
                self.client.Stop()
        except Exception:
            pass
        self.capture = self.client = None
        return False

    def _drain(self):
        got = []
        while True:
            n = self.capture.GetNextPacketSize()
            if not n:
                break
            data, frames, flags, _, _ = self.capture.GetBuffer()
            if frames:
                if flags & AUDCLNT_BUFFERFLAGS_SILENT:
                    got.append(np.zeros(frames, np.float32))
                else:
                    buf = ctypes.cast(data, POINTER(c_float * frames)).contents
                    got.append(np.frombuffer(buf, dtype=np.float32).copy())
            self.capture.ReleaseBuffer(frames)
        if got:
            self.pending = np.concatenate([self.pending] + got)

    def record(self, numframes: int) -> np.ndarray:
        # The stream only has packets while the program plays something: after a short wait the gap is
        # filled with silence, so the speech engine's clock keeps running.
        deadline = time.monotonic() + numframes / SR * 1.5
        while self.pending.size < numframes:
            self._drain()
            if self.pending.size >= numframes:
                break
            if time.monotonic() > deadline:
                self.pending = np.concatenate([self.pending, np.zeros(numframes - self.pending.size, np.float32)])
                break
            time.sleep(0.008)
        out, self.pending = self.pending[:numframes], self.pending[numframes:]
        return out.reshape(-1, 1)


def audio_programs() -> list[dict]:
    """Programs that have an audio session right now: [{"pid", "name"}], one per program name."""
    try:
        import psutil
    except ImportError:
        return []
    names = {}
    try:
        from comtypes import CLSCTX_ALL  # noqa: F401
        sessions = _session_pids()
    except Exception:
        sessions = set()
    for pid in sessions:
        try:
            p = psutil.Process(pid)
            name = p.name()
            # several processes of one program (browsers): list the program once, by its top process
            while True:
                parent = p.parent()
                if parent is None or parent.name() != name:
                    break
                p = parent
            names.setdefault(name.lower(), {"pid": p.pid, "name": name.rsplit(".", 1)[0]})
        except Exception:
            continue
    return sorted(names.values(), key=lambda d: d["name"].lower())


def _session_pids() -> set[int]:
    """Process ids with an audio session on the default speakers (IAudioSessionManager2)."""
    from comtypes import CLSCTX_ALL, CoCreateInstance

    class IMMDevice(IUnknown):
        _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
        _methods_ = [COMMETHOD([], HRESULT, "Activate", (["in"], POINTER(GUID), "iid"), (["in"], wintypes.DWORD, "ctx"),
                               (["in"], c_void_p, "params"), (["out"], POINTER(c_void_p), "ppv"))]

    class IMMDeviceEnumerator(IUnknown):
        _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
        _methods_ = [COMMETHOD([], HRESULT, "EnumAudioEndpoints", (["in"], c_uint32, "a"), (["in"], wintypes.DWORD, "b"),
                               (["out"], POINTER(c_void_p), "c")),
                     COMMETHOD([], HRESULT, "GetDefaultAudioEndpoint", (["in"], c_uint32, "flow"), (["in"], c_uint32, "role"),
                               (["out"], POINTER(POINTER(IMMDevice)), "dev"))]

    class IAudioSessionControl2(IUnknown):
        _iid_ = GUID("{bfb7ff88-7239-4fc9-8fa2-07c950be9c6d}")
        _methods_ = [COMMETHOD([], HRESULT, "GetState", (["out"], POINTER(c_uint32), "s"))] + \
                    [COMMETHOD([], HRESULT, f"_r{i}") for i in range(8)] + \
                    [COMMETHOD([], HRESULT, "GetSessionIdentifier", (["out"], POINTER(c_void_p), "a")),
                     COMMETHOD([], HRESULT, "GetSessionInstanceIdentifier", (["out"], POINTER(c_void_p), "a")),
                     COMMETHOD([], HRESULT, "GetProcessId", (["out"], POINTER(wintypes.DWORD), "pid"))]

    class IAudioSessionEnumerator(IUnknown):
        _iid_ = GUID("{E2F5BB11-0570-40CA-ACDD-3AA01277DEE8}")
        _methods_ = [COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(ctypes.c_int), "n")),
                     COMMETHOD([], HRESULT, "GetSession", (["in"], ctypes.c_int, "i"), (["out"], POINTER(POINTER(IUnknown)), "s"))]

    class IAudioSessionManager2(IUnknown):
        _iid_ = GUID("{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}")
        _methods_ = [COMMETHOD([], HRESULT, "_g1"), COMMETHOD([], HRESULT, "_g2"),
                     COMMETHOD([], HRESULT, "GetSessionEnumerator", (["out"], POINTER(POINTER(IAudioSessionEnumerator)), "e"))]

    comtypes.CoInitialize()
    enum = CoCreateInstance(GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}"), IMMDeviceEnumerator, CLSCTX_ALL)
    dev = enum.GetDefaultAudioEndpoint(0, 1)                     # eRender, eMultimedia
    mgr = ctypes.cast(dev.Activate(byref(IAudioSessionManager2._iid_), CLSCTX_ALL, None), POINTER(IAudioSessionManager2))
    sessions = mgr.GetSessionEnumerator()
    pids = set()
    for i in range(sessions.GetCount()):
        try:
            ctl = sessions.GetSession(i).QueryInterface(IAudioSessionControl2)
            pid = ctl.GetProcessId()
            if pid:
                pids.add(int(pid))
        except Exception:
            continue
    return pids
