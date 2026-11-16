"""Fast, read-only frame source based on the scrcpy video stream.

The scrcpy server bundled with the installed scrcpy client (same version, the
protocol has no compatibility guarantees) is pushed to /data/local/tmp, started
with `app_process` in video-only mode (audio=false, control=false, power_on=false)
and its H.264 stream is read over `adb forward`. A background thread decodes the
stream with PyAV and keeps only the most recent frame, so `screencap()` returns in
a few milliseconds instead of the 0.3-0.8 s of `adb exec-out screencap`.

No input or control event is ever sent to the device. The server jar is removed
on stop (the server also deletes it itself at startup).

Wire format (scrcpy 5.0.1, tunnel_forward=true, all big-endian):
    1 byte    dummy byte (0x00, first socket only)
    64 bytes  device name, NUL padded
    4 bytes   codec id (0x68323634 = "h264")
    then a sequence of 12-byte headers:
      session packet: u32 flags (MSB set), u32 width, u32 height
      media packet:   u64 pts_and_flags (bit 62 config, bit 61 key frame,
                      low 61 bits pts in microseconds), u32 size, payload

`screencap()` returns an HxWx3 uint8 RGB array, the same format as
`capture.decode_screencap` / `AdbDevice.screencap`, which is what vision.py uses.

Usage:
    dev = AdbDevice()
    with StreamCapture(device=dev) as cap:
        img = cap.screencap()

or as a drop-in device wrapper (forwards everything else to the AdbDevice):
    dev = StreamingDevice(AdbDevice())
    dev.start(); ...; dev.stop()
"""
from __future__ import annotations

import os
import random
import socket
import struct
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

DEFAULT_SERVER_PATH = r"E:\tools\scrcpy\scrcpy-server"
DEFAULT_SERVER_VERSION = "5.0.1"
DEVICE_JAR = "/data/local/tmp/scrcpy-server-stream.jar"

CODEC_H264 = 0x68323634
FLAG_SESSION = 1 << 63
FLAG_CONFIG = 1 << 62
FLAG_KEY_FRAME = 1 << 61
PTS_MASK = FLAG_KEY_FRAME - 1
DEVICE_NAME_LEN = 64


class StreamError(RuntimeError):
    pass


# --------------------------------------------------------------------------- protocol

@dataclass
class Packet:
    kind: str                 # "session", "config" or "frame"
    pts_us: Optional[int] = None
    key: bool = False
    payload: bytes = b""
    width: int = 0
    height: int = 0


def recv_exact(sock, n: int) -> bytes:
    """Read exactly n bytes from a socket-like object (raises StreamError on EOF)."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise StreamError("stream closed")
        buf += chunk
    return bytes(buf)


def read_handshake(sock, dummy_byte: bool = True) -> tuple:
    """Read dummy byte + device meta + codec id. Returns (device_name, codec_id)."""
    if dummy_byte:
        recv_exact(sock, 1)
    name = recv_exact(sock, DEVICE_NAME_LEN).split(b"\0", 1)[0].decode("utf-8", "replace")
    (codec,) = struct.unpack(">I", recv_exact(sock, 4))
    if codec == 0:
        raise StreamError("video stream disabled by the server")
    if codec == 1:
        raise StreamError("server reported a configuration error")
    return name, codec


def read_packet(sock) -> Packet:
    hdr = recv_exact(sock, 12)
    (pts_flags,) = struct.unpack(">Q", hdr[:8])
    if pts_flags & FLAG_SESSION:
        w, h = struct.unpack(">II", hdr[4:])
        return Packet("session", width=w, height=h)
    (size,) = struct.unpack(">I", hdr[8:])
    payload = recv_exact(sock, size) if size else b""
    if pts_flags & FLAG_CONFIG:
        return Packet("config", payload=payload)
    return Packet("frame", pts_us=pts_flags & PTS_MASK, key=bool(pts_flags & FLAG_KEY_FRAME), payload=payload)


# --------------------------------------------------------------------------- decoder

class AvH264Decoder:
    """PyAV H.264 decoder. `decode(bytes)` returns a list of av.VideoFrame."""

    def __init__(self):
        import av
        self._av = av
        self.ctx = av.CodecContext.create("h264", "r")
        try:  # output frames as soon as they are decoded (no reordering delay)
            self.ctx.flags |= av.codec.context.Flags.low_delay
        except Exception:
            pass

    def decode(self, data: bytes) -> list:
        return list(self.ctx.decode(self._av.Packet(data)))


def frame_to_rgb(frame) -> np.ndarray:
    """av.VideoFrame (or anything with to_ndarray / a ndarray) -> HxWx3 uint8 RGB."""
    if isinstance(frame, np.ndarray):
        return frame
    return frame.to_ndarray(format="rgb24")


# --------------------------------------------------------------------------- capture

class StreamCapture:
    """Latest-frame video capture from scrcpy, with automatic screencap fallback.

    Parameters
    ----------
    device: optional AdbDevice; supplies serial, adb path and the fallback screencap.
    max_size: longest side of the stream (0 = native resolution).
    output_size: optional (width, height); frames are resized to it (e.g. the native
        screen size when max_size downscales) so pixel calibrations still match.
    max_age_s: a frame older than this is considered stale -> fallback screencap.
    fallback: callable returning an RGB frame; defaults to device.screencap.
    """

    def __init__(self, device=None, serial: Optional[str] = None, adb_path: Optional[str] = None,
                 server_path: str = DEFAULT_SERVER_PATH, server_version: str = DEFAULT_SERVER_VERSION,
                 max_size: int = 0, bit_rate: int = 8_000_000, max_fps: float = 0,
                 output_size: Optional[tuple] = None, max_age_s: float = 2.0,
                 first_frame_timeout_s: float = 10.0, restart_interval_s: float = 30.0,
                 fallback: Optional[Callable[[], np.ndarray]] = None,
                 decoder_factory: Callable = AvH264Decoder, log: Optional[Callable[[str], None]] = None):
        self.device = device
        self.serial = serial or getattr(device, "serial", None)
        if adb_path is None:
            adb_path = getattr(device, "adb_path", None)
            if adb_path is None:
                from .capture import find_adb
                adb_path = find_adb()
        self.adb_path = adb_path
        self.server_path = server_path
        self.server_version = server_version
        self.max_size = int(max_size)
        self.bit_rate = int(bit_rate)
        self.max_fps = max_fps
        self.output_size = tuple(output_size) if output_size else None
        self.max_age_s = max_age_s
        self.first_frame_timeout_s = first_frame_timeout_s
        self.restart_interval_s = restart_interval_s
        self.fallback = fallback or (device.screencap if device is not None else None)
        self.decoder_factory = decoder_factory
        self.log = log or (lambda msg: None)

        self.scid = random.randint(1, 0x7FFFFFFF)
        self.port: Optional[int] = None
        self.device_name: Optional[str] = None
        self.stream_size: Optional[tuple] = None      # (w, h) from the last session packet
        self._proc: Optional[subprocess.Popen] = None
        self._sock = None
        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self._cond = threading.Condition()
        self._frame = None            # latest decoded frame (av.VideoFrame or ndarray)
        self._frame_t = 0.0           # time.perf_counter() when it was decoded
        self._frame_pts = None
        self._seq = 0                 # number of decoded frames
        self._rgb_cache = (None, -1)  # (array, seq)
        self.error: Optional[str] = None
        self.running = False
        self.last_restart = 0.0

        # statistics (same attribute name as AdbDevice for drop-in use)
        self.last_capture_ms = 0.0
        self.last_frame_age_ms = 0.0
        self.last_source = None       # "stream" or "fallback"
        self.fallback_count = 0
        self.restarts = 0
        self.decode_errors = 0

    # -- adb helpers ----------------------------------------------------------
    def _adb_cmd(self, args) -> list:
        return [self.adb_path] + (["-s", self.serial] if self.serial else []) + list(args)

    def _adb(self, args, timeout: float = 20.0) -> str:
        r = subprocess.run(self._adb_cmd(args), capture_output=True, timeout=timeout)
        if r.returncode != 0:
            raise StreamError(f"adb {' '.join(args)} failed: {r.stderr.decode(errors='replace').strip()}")
        return r.stdout.decode(errors="replace")

    def server_args(self) -> list:
        args = [self.server_version, f"scid={self.scid:08x}", "log_level=info",
                "video=true", "audio=false", "control=false", "tunnel_forward=true",
                "video_codec=h264", f"video_bit_rate={self.bit_rate}", f"max_size={self.max_size}",
                "power_on=false", "stay_awake=false", "show_touches=false", "cleanup=true",
                "send_device_meta=true", "send_frame_meta=true", "send_stream_meta=true",
                "send_dummy_byte=true"]
        if self.max_fps:
            args.append(f"max_fps={self.max_fps:g}")
        return args

    def _launch_server(self):
        """Push the server, create the forward and start app_process (overridable in tests)."""
        if not os.path.exists(self.server_path):
            raise StreamError(f"scrcpy server not found: {self.server_path}")
        self._adb(["push", self.server_path, DEVICE_JAR], timeout=60)
        out = self._adb(["forward", "tcp:0", f"localabstract:scrcpy_{self.scid:08x}"]).strip()
        self.port = int(out.splitlines()[-1])
        cmd = (f"CLASSPATH={DEVICE_JAR} app_process / com.genymobile.scrcpy.Server "
               + " ".join(self.server_args()))
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._proc = subprocess.Popen(self._adb_cmd(["shell", cmd]), stdin=subprocess.DEVNULL,
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=flags)
        threading.Thread(target=self._drain_server_log, args=(self._proc,), daemon=True).start()

    def _drain_server_log(self, proc):
        try:
            for line in proc.stdout:
                self.log("server: " + line.decode(errors="replace").rstrip())
        except Exception:
            pass

    def _connect(self, timeout: float = 10.0):
        """Connect to the forwarded socket and read the handshake (overridable in tests).

        Until the server listens, adb accepts the TCP connection and closes it at once,
        so a failed handshake is retried."""
        t0 = time.perf_counter()
        last = None
        while time.perf_counter() - t0 < timeout:
            if self._proc is not None and self._proc.poll() is not None:
                raise StreamError(f"server exited with code {self._proc.returncode}")
            s = None
            try:
                s = socket.create_connection(("127.0.0.1", self.port), timeout=5.0)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                name, codec = read_handshake(s)
                if codec != CODEC_H264:
                    raise StreamError(f"unexpected codec id 0x{codec:08x}")
                self.device_name = name
                return s
            except (OSError, StreamError) as ex:
                last = ex
                if s is not None:
                    s.close()
                time.sleep(0.1)
        raise StreamError(f"cannot connect to scrcpy server: {last}")

    def _teardown_server(self):
        if self._proc is not None:
            if self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            self._proc = None
        # only our own server instance (matched by scid), never another scrcpy session
        for args, t in ((["shell", f"pkill -f 'scid={self.scid:08x}' ; rm -f {DEVICE_JAR}"], 10),
                        (["forward", "--remove", f"tcp:{self.port}"] if self.port else None, 10)):
            if args is None:
                continue
            try:
                subprocess.run(self._adb_cmd(args), capture_output=True, timeout=t)
            except Exception:
                pass
        self.port = None

    # -- lifecycle ------------------------------------------------------------
    def start(self, wait_first_frame: bool = True) -> bool:
        """Start the stream. Returns False (and keeps using the fallback) on failure."""
        if self.running:
            return True
        self.last_restart = time.perf_counter()
        self.error = None
        self._stop_evt.clear()
        try:
            self._launch_server()
            self._sock = self._connect()
            self._sock.settimeout(1.0)
            decoder = self.decoder_factory()
            self.running = True
            self._thread = threading.Thread(target=self._reader, args=(self._sock, decoder),
                                            name="scrcpy-reader", daemon=True)
            self._thread.start()
            if wait_first_frame and self.wait_frame(0, self.first_frame_timeout_s) is None:
                raise StreamError("no frame received from the stream")
            self.log(f"stream started: {self.device_name} {self.stream_size} port {self.port}")
            return True
        except Exception as ex:
            self.error = str(ex)
            self.log(f"stream start failed: {ex}")
            self.stop()
            return False

    def stop(self):
        self._stop_evt.set()
        self.running = False
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)
        self._thread = None
        self._sock = None
        self._teardown_server()

    def restart(self) -> bool:
        self.stop()
        self.restarts += 1
        return self.start()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
        return False

    # -- reader thread ----------------------------------------------------------
    def _reader(self, sock, decoder):
        config = b""
        try:
            while not self._stop_evt.is_set():
                try:
                    pkt = read_packet(sock)
                except socket.timeout:
                    continue  # static screen and no repeated frame: keep waiting
                if pkt.kind == "session":
                    self.stream_size = (pkt.width, pkt.height)
                    continue
                if pkt.kind == "config":
                    config = pkt.payload  # SPS/PPS: prepend to the next frame
                    continue
                data = config + pkt.payload
                config = b""
                try:
                    frames = decoder.decode(data)
                except Exception as ex:  # corrupt packet: wait for the next key frame
                    self.decode_errors += 1
                    self.log(f"decode error: {ex}")
                    continue
                if frames:
                    with self._cond:
                        self._frame = frames[-1]
                        self._frame_t = time.perf_counter()
                        self._frame_pts = pkt.pts_us
                        self._seq += 1
                        self._cond.notify_all()
        except Exception as ex:
            if not self._stop_evt.is_set():
                self.error = f"reader: {ex}"
                self.log(f"stream reader stopped: {ex}")
        finally:
            self.running = False
            with self._cond:
                self._cond.notify_all()

    # -- frames -------------------------------------------------------------------
    @property
    def seq(self) -> int:
        return self._seq

    def frame_age_s(self) -> Optional[float]:
        return None if self._seq == 0 else time.perf_counter() - self._frame_t

    def wait_frame(self, after_seq: int, timeout: float) -> Optional[int]:
        """Block until a frame with seq > after_seq exists. Returns its seq or None."""
        deadline = time.perf_counter() + timeout
        with self._cond:
            while self._seq <= after_seq:
                left = deadline - time.perf_counter()
                if left <= 0 or (not self.running and self._thread is None):
                    return None
                self._cond.wait(min(left, 0.2))
                if not self.running and self._seq <= after_seq:
                    return None
            return self._seq

    def _latest_rgb(self):
        with self._cond:
            frame, seq, t, pts = self._frame, self._seq, self._frame_t, self._frame_pts
        arr, cseq = self._rgb_cache
        if cseq != seq:
            arr = frame_to_rgb(frame)
            if self.output_size and (arr.shape[1], arr.shape[0]) != self.output_size:
                import cv2
                arr = cv2.resize(arr, self.output_size, interpolation=cv2.INTER_LINEAR)
            self._rgb_cache = (arr, seq)
        return arr, seq, t, pts

    def get_frame(self, newer_than: Optional[float] = None, timeout: float = 1.0):
        """Return (rgb, seq, perf_counter_time, pts_us) of the latest frame, or None.

        newer_than: a time.perf_counter() value; waits up to `timeout` for a frame decoded
        after it (use the time just after an input action to avoid stale frames)."""
        if self._seq == 0:
            return None
        if newer_than is not None and self._frame_t <= newer_than:
            deadline = time.perf_counter() + timeout
            while self._frame_t <= newer_than and time.perf_counter() < deadline and self.running:
                self.wait_frame(self._seq, deadline - time.perf_counter())
        return self._latest_rgb()

    def screencap(self, newer_than: Optional[float] = None) -> np.ndarray:
        """Drop-in for AdbDevice.screencap(): HxWx3 uint8 RGB.

        Uses the latest stream frame when it is fresh, otherwise the fallback."""
        t0 = time.perf_counter()
        if not self.running and self.restart_interval_s is not None \
                and time.perf_counter() - self.last_restart >= self.restart_interval_s:
            self.restarts += 1
            self.stop()      # clean up a server whose stream died
            self.start()
        res = None
        if self.running:
            res = self.get_frame(newer_than)
        if res is not None:
            arr, _seq, ft, _pts = res
            age = time.perf_counter() - ft
            if age <= self.max_age_s or self.fallback is None:
                self.last_frame_age_ms = age * 1000
                self.last_source = "stream"
                self.last_capture_ms = (time.perf_counter() - t0) * 1000
                return arr
        if self.fallback is None:
            raise StreamError(f"no stream frame and no fallback ({self.error})")
        img = self.fallback()
        self.fallback_count += 1
        self.last_frame_age_ms = 0.0
        self.last_source = "fallback"
        self.last_capture_ms = (time.perf_counter() - t0) * 1000
        return img

    def stats(self) -> dict:
        age = self.frame_age_s()
        return {"running": self.running, "frames": self._seq, "stream_size": self.stream_size,
                "frame_age_ms": None if age is None else round(age * 1000, 1),
                "fallbacks": self.fallback_count, "restarts": self.restarts,
                "decode_errors": self.decode_errors, "error": self.error}


class StreamingDevice:
    """Wraps an AdbDevice: `screencap` comes from the stream, everything else is forwarded."""

    def __init__(self, device, **kwargs):
        self._dev = device
        self.stream = StreamCapture(device=device, **kwargs)

    def start(self) -> bool:
        return self.stream.start()

    def stop(self):
        self.stream.stop()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
        return False

    def screencap(self, newer_than: Optional[float] = None) -> np.ndarray:
        return self.stream.screencap(newer_than)

    @property
    def last_capture_ms(self) -> float:
        return self.stream.last_capture_ms

    def reconnect(self, *a, **k) -> bool:
        ok = self._dev.reconnect(*a, **k)
        if ok:
            self.stream.restart()
        return ok

    def __getattr__(self, name):
        return getattr(self._dev, name)
