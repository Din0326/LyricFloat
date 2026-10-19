"""WASAPI process loopback (Windows 10 2004+): only one process tree's audio.

Used by the L3 recorder on its own thread; comtypes is imported there, never
on the Qt thread, because importing it initialises COM for the calling thread.
The audio engine converts to the asked format (16-bit PCM).
"""

from __future__ import annotations

import ctypes
import functools
import sys
import threading
import time
from collections.abc import Callable
from ctypes import POINTER, Structure, byref, c_int, c_uint32, c_uint64, c_ushort, c_void_p, wintypes

VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"
AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
VT_BLOB = 65
WAVE_FORMAT_PCM = 1
BUFFER_100NS = 2_000_000  # 200 ms engine buffer

Sink = Callable[[bytes, float], None]  # PCM chunk, time.monotonic() of its first frame


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


class _LoopbackParams(Structure):
    _fields_ = [("TargetProcessId", wintypes.DWORD), ("ProcessLoopbackMode", c_int)]


class _ActivationParams(Structure):
    _fields_ = [("ActivationType", c_int), ("ProcessLoopbackParams", _LoopbackParams)]


class _Blob(Structure):
    _fields_ = [("cbSize", wintypes.ULONG), ("pBlobData", c_void_p)]


class _PropVariant(Structure):
    _fields_ = [("vt", c_ushort), ("r1", c_ushort), ("r2", c_ushort), ("r3", c_ushort), ("blob", _Blob)]


@functools.cache
def _interfaces():
    """COM interface declarations, built once, on first use (on a recording thread)."""
    if "comtypes" not in sys.modules:
        sys.coinit_flags = 0  # comtypes initialises COM on import: multithreaded, as activation needs
    import comtypes
    from comtypes import COMMETHOD, GUID, HRESULT, IUnknown, COMObject

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
            COMMETHOD([], HRESULT, "GetService", (["in"], POINTER(GUID), "iid"),
                      (["out"], POINTER(POINTER(IUnknown)), "service")),
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
            COMMETHOD([], HRESULT, "ActivateCompleted",
                      (["in"], POINTER(IActivateAudioInterfaceAsyncOperation), "operation")),
        ]

    class IAgileObject(IUnknown):
        _iid_ = GUID("{94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90}")
        _methods_ = []

    class Handler(COMObject):
        # Agile: the activation completes on a worker thread.
        _com_interfaces_ = [IActivateAudioInterfaceCompletionHandler, IAgileObject]

        def __init__(self) -> None:
            super().__init__()
            self.done = threading.Event()

        def ActivateCompleted(self, operation):  # noqa: N802 (COM name)
            self.done.set()
            return 0

    activate = ctypes.WinDLL("Mmdevapi").ActivateAudioInterfaceAsync
    activate.restype = HRESULT
    activate.argtypes = [
        wintypes.LPCWSTR,
        POINTER(GUID),
        POINTER(_PropVariant),
        POINTER(IActivateAudioInterfaceCompletionHandler),
        POINTER(POINTER(IActivateAudioInterfaceAsyncOperation)),
    ]
    return comtypes, IAudioClient, IAudioCaptureClient, IActivateAudioInterfaceAsyncOperation, IActivateAudioInterfaceCompletionHandler, Handler, activate


class LoopbackStream:
    """Create, `run` and `close` on one thread (the COM objects belong to it)."""

    def __init__(self, pid: int, rate: int = 44100, channels: int = 2) -> None:
        (comtypes, IAudioClient, IAudioCaptureClient, IOperation, IHandler, Handler, activate) = _interfaces()
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        self._comtypes = comtypes
        self.rate, self.channels = rate, channels
        self.block = 2 * channels
        params = _ActivationParams(
            AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK,
            _LoopbackParams(pid, PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE),
        )
        variant = _PropVariant(vt=VT_BLOB, blob=_Blob(ctypes.sizeof(params), ctypes.cast(byref(params), c_void_p)))
        handler = Handler()
        op = POINTER(IOperation)()
        activate(VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK, byref(IAudioClient._iid_), byref(variant), handler.QueryInterface(IHandler), byref(op))
        if not handler.done.wait(5):
            raise OSError("audio interface activation timed out")
        hr, unknown = op.GetActivateResult()
        if hr != 0:
            raise OSError(f"audio interface activation failed: 0x{hr & 0xFFFFFFFF:08X}")
        self.client = unknown.QueryInterface(IAudioClient)
        fmt = WAVEFORMATEX(WAVE_FORMAT_PCM, channels, rate, rate * self.block, self.block, 16, 0)
        flags = (
            AUDCLNT_STREAMFLAGS_LOOPBACK
            | AUDCLNT_STREAMFLAGS_EVENTCALLBACK
            | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM
            | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY
        )
        self.client.Initialize(0, flags, BUFFER_100NS, 0, byref(fmt), None)
        self._kernel32 = ctypes.WinDLL("kernel32")
        self.event = self._kernel32.CreateEventW(None, False, False, None)
        self.client.SetEventHandle(self.event)
        self.capture = self.client.GetService(byref(IAudioCaptureClient._iid_)).QueryInterface(IAudioCaptureClient)

    def run(self, sink: Sink, stop: threading.Event) -> None:
        """Deliver audio to `sink` until `stop` is set. Silence arrives as zeros."""
        self.client.Start()
        try:
            while not stop.is_set():
                self._kernel32.WaitForSingleObject(self.event, 100)
                while True:
                    frames = self.capture.GetNextPacketSize()
                    if not frames:
                        break
                    data, frames, flags, _dev, _qpc = self.capture.GetBuffer()
                    size = frames * self.block
                    chunk = bytes(size) if flags & AUDCLNT_BUFFERFLAGS_SILENT else ctypes.string_at(data, size)
                    self.capture.ReleaseBuffer(frames)
                    sink(chunk, time.monotonic() - frames / self.rate)
        finally:
            self.client.Stop()

    def close(self) -> None:
        self._kernel32.CloseHandle(self.event)
        self.capture = None
        self.client = None
        self._comtypes.CoUninitialize()
