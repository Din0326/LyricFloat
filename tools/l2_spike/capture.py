"""WASAPI process loopback: capture only one process tree's audio (Windows 10 2004+).

Spike code for L2 (SPEC v1.18). Audio stays in memory; nothing is written to disk.
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from ctypes import POINTER, Structure, byref, c_int, c_uint32, c_uint64, c_ushort, c_void_p, wintypes

sys.coinit_flags = 0  # COINIT_MULTITHREADED: the activation completes on a worker thread

import comtypes  # noqa: E402
from comtypes import COMMETHOD, GUID, HRESULT, IUnknown, COMObject  # noqa: E402

VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"
AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0
AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
VT_BLOB = 65
WAVE_FORMAT_PCM = 1


class WAVEFORMATEX(Structure):
    _fields_ = [
        ("wFormatTag", wintypes.WORD),
        ("nChannels", wintypes.WORD),
        ("nSamplesPerSec", wintypes.DWORD),
        ("nAvgBytesPerSec", wintypes.DWORD),
        ("nBlockAlign", wintypes.WORD),
        ("wBitsPerSample", wintypes.WORD),
        ("cbSize", wintypes.WORD),
    ]


class PROCESS_LOOPBACK_PARAMS(Structure):
    _fields_ = [("TargetProcessId", wintypes.DWORD), ("ProcessLoopbackMode", c_int)]


class ACTIVATION_PARAMS(Structure):
    _fields_ = [("ActivationType", c_int), ("ProcessLoopbackParams", PROCESS_LOOPBACK_PARAMS)]


class BLOB(Structure):
    _fields_ = [("cbSize", wintypes.ULONG), ("pBlobData", c_void_p)]


class PROPVARIANT(Structure):
    _fields_ = [("vt", c_ushort), ("r1", c_ushort), ("r2", c_ushort), ("r3", c_ushort), ("blob", BLOB)]


class IAudioCaptureClient(IUnknown):
    _iid_ = GUID("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetBuffer",
                  (["out"], POINTER(POINTER(ctypes.c_byte)), "data"),
                  (["out"], POINTER(c_uint32), "frames"),
                  (["out"], POINTER(wintypes.DWORD), "flags"),
                  (["out"], POINTER(c_uint64), "device_position"),
                  (["out"], POINTER(c_uint64), "qpc_position")),
        COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], c_uint32, "frames")),
        COMMETHOD([], HRESULT, "GetNextPacketSize", (["out"], POINTER(c_uint32), "frames")),
    ]


class IAudioClient(IUnknown):
    _iid_ = GUID("{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}")
    _methods_ = [
        COMMETHOD([], HRESULT, "Initialize",
                  (["in"], c_int, "share_mode"),
                  (["in"], wintypes.DWORD, "flags"),
                  (["in"], ctypes.c_longlong, "buffer_duration"),
                  (["in"], ctypes.c_longlong, "periodicity"),
                  (["in"], POINTER(WAVEFORMATEX), "format"),
                  (["in"], POINTER(GUID), "session")),
        COMMETHOD([], HRESULT, "GetBufferSize", (["out"], POINTER(c_uint32), "frames")),
        COMMETHOD([], HRESULT, "GetStreamLatency", (["out"], POINTER(ctypes.c_longlong), "latency")),
        COMMETHOD([], HRESULT, "GetCurrentPadding", (["out"], POINTER(c_uint32), "frames")),
        COMMETHOD([], HRESULT, "IsFormatSupported", (["in"], c_int, "mode"), (["in"], POINTER(WAVEFORMATEX), "fmt"),
                  (["out"], POINTER(POINTER(WAVEFORMATEX)), "closest")),
        COMMETHOD([], HRESULT, "GetMixFormat", (["out"], POINTER(POINTER(WAVEFORMATEX)), "fmt")),
        COMMETHOD([], HRESULT, "GetDevicePeriod", (["out"], POINTER(ctypes.c_longlong), "default"),
                  (["out"], POINTER(ctypes.c_longlong), "minimum")),
        COMMETHOD([], HRESULT, "Start"),
        COMMETHOD([], HRESULT, "Stop"),
        COMMETHOD([], HRESULT, "Reset"),
        COMMETHOD([], HRESULT, "SetEventHandle", (["in"], wintypes.HANDLE, "event")),
        COMMETHOD([], HRESULT, "GetService", (["in"], POINTER(GUID), "iid"), (["out"], POINTER(POINTER(IUnknown)), "service")),
    ]


class IActivateAudioInterfaceAsyncOperation(IUnknown):
    _iid_ = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetActivateResult",
                  (["out"], POINTER(HRESULT), "result"),
                  (["out"], POINTER(POINTER(IUnknown)), "interface")),
    ]


class IActivateAudioInterfaceCompletionHandler(IUnknown):
    _iid_ = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
    _methods_ = [
        COMMETHOD([], HRESULT, "ActivateCompleted", (["in"], POINTER(IActivateAudioInterfaceAsyncOperation), "operation")),
    ]


class IAgileObject(IUnknown):
    _iid_ = GUID("{94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90}")
    _methods_ = []


class _Handler(COMObject):
    _com_interfaces_ = [IActivateAudioInterfaceCompletionHandler, IAgileObject]

    def __init__(self) -> None:
        super().__init__()
        self.done = threading.Event()

    def ActivateCompleted(self, operation):  # noqa: N802 (COM name)
        self.done.set()
        return 0


_mmdevapi = ctypes.WinDLL("Mmdevapi")
_ActivateAudioInterfaceAsync = _mmdevapi.ActivateAudioInterfaceAsync
_ActivateAudioInterfaceAsync.restype = HRESULT
_ActivateAudioInterfaceAsync.argtypes = [
    wintypes.LPCWSTR,
    POINTER(GUID),
    POINTER(PROPVARIANT),
    POINTER(IActivateAudioInterfaceCompletionHandler),
    POINTER(POINTER(IActivateAudioInterfaceAsyncOperation)),
]
_kernel32 = ctypes.WinDLL("kernel32")


def activate(pid: int) -> IAudioClient:
    params = ACTIVATION_PARAMS(
        AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK,
        PROCESS_LOOPBACK_PARAMS(pid, PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE),
    )
    variant = PROPVARIANT(vt=VT_BLOB, blob=BLOB(ctypes.sizeof(params), ctypes.cast(byref(params), c_void_p)))
    handler = _Handler()
    op = POINTER(IActivateAudioInterfaceAsyncOperation)()
    iid = IAudioClient._iid_
    _ActivateAudioInterfaceAsync(
        VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,
        byref(iid),
        byref(variant),
        handler.QueryInterface(IActivateAudioInterfaceCompletionHandler),
        byref(op),
    )
    if not handler.done.wait(5):
        raise TimeoutError("audio interface activation did not complete")
    hr, unknown = op.GetActivateResult()
    if hr != 0:
        raise OSError(f"activation failed: 0x{hr & 0xFFFFFFFF:08X}")
    return unknown.QueryInterface(IAudioClient)


class Capture:
    """16-bit PCM from one process tree, converted by the audio engine to the asked format
    (16 kHz mono for recognition by default; L3 asks 44.1 kHz stereo for vocal separation)."""

    RATE = 16000

    def __init__(self, pid: int, rate: int = RATE, channels: int = 1) -> None:
        self.rate = rate
        self.channels = channels
        self.client = activate(pid)
        block = 2 * channels
        fmt = WAVEFORMATEX(WAVE_FORMAT_PCM, channels, rate, rate * block, block, 16, 0)
        flags = (
            AUDCLNT_STREAMFLAGS_LOOPBACK
            | AUDCLNT_STREAMFLAGS_EVENTCALLBACK
            | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM
            | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY
        )
        self.client.Initialize(AUDCLNT_SHAREMODE_SHARED, flags, 2_000_000, 0, byref(fmt), None)  # 200 ms buffer
        self.event = _kernel32.CreateEventW(None, False, False, None)
        self.client.SetEventHandle(self.event)
        service = self.client.GetService(byref(IAudioCaptureClient._iid_))
        self.capture = service.QueryInterface(IAudioCaptureClient)

    def record(self, seconds: float, keep_going=None) -> tuple[bytes, float]:
        """PCM bytes plus time.monotonic() of the first sample's arrival. `keep_going()`
        is asked about once a second and can end the recording early."""
        chunks: list[bytes] = []
        first_at: float | None = None
        wanted = int(seconds * self.rate)
        got = 0
        checked = time.monotonic()
        self.client.Start()
        try:
            deadline = time.monotonic() + seconds + 5
            while got < wanted and time.monotonic() < deadline:
                if keep_going is not None and time.monotonic() - checked >= 1.0:
                    checked = time.monotonic()
                    if not keep_going():
                        break
                _kernel32.WaitForSingleObject(self.event, 200)
                while True:
                    frames = self.capture.GetNextPacketSize()
                    if not frames:
                        break
                    data, frames, flags, _dev, _qpc = self.capture.GetBuffer()
                    if first_at is None:
                        first_at = time.monotonic() - frames / self.rate
                    size = frames * 2 * self.channels
                    if flags & AUDCLNT_BUFFERFLAGS_SILENT:
                        chunks.append(bytes(size))
                    else:
                        chunks.append(ctypes.string_at(data, size))
                    self.capture.ReleaseBuffer(frames)
                    got += frames
        finally:
            self.client.Stop()
        return b"".join(chunks)[: wanted * 2 * self.channels], first_at or time.monotonic()

    def close(self) -> None:
        _kernel32.CloseHandle(self.event)


def root_pid(exe_name: str) -> int | None:
    """The top process of an app (Chrome and Spotify play audio from child processes)."""
    import psutil

    procs = [p for p in psutil.process_iter(["pid", "name", "ppid"]) if (p.info["name"] or "").lower() == exe_name.lower()]
    pids = {p.info["pid"] for p in procs}
    roots = [p.info["pid"] for p in procs if p.info["ppid"] not in pids]
    return roots[0] if roots else None


if __name__ == "__main__":
    import array

    exe = sys.argv[1] if len(sys.argv) > 1 else "Spotify.exe"
    pid = root_pid(exe)
    print("pid", pid)
    cap = Capture(pid)
    pcm, at = cap.record(3.0)
    cap.close()
    samples = array.array("h", pcm)
    peak = max((abs(s) for s in samples), default=0)
    rms = (sum(s * s for s in samples) / max(1, len(samples))) ** 0.5
    print(f"{len(samples) / Capture.RATE:.2f}s captured, peak {peak}, rms {rms:.0f}")
