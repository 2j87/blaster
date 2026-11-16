"""Device-free tests for bot.stream_capture (fake socket + fake decoder)."""
import socket
import struct
import threading
import time

import numpy as np
import pytest

from bot import stream_capture as sc


def handshake(name=b"SM-A245F", codec=sc.CODEC_H264, dummy=True):
    return (b"\0" if dummy else b"") + name.ljust(64, b"\0") + struct.pack(">I", codec)


def session(w, h, client=False):
    return struct.pack(">III", 0x80000000 | int(client), w, h)


def media(payload, pts=0, config=False, key=False):
    v = sc.FLAG_CONFIG if config else pts | (sc.FLAG_KEY_FRAME if key else 0)
    return struct.pack(">QI", v, len(payload)) + payload


class ChunkSock:
    """Socket-like object returning the data in small chunks, then EOF (or blocking)."""

    def __init__(self, data, chunk=7, block_at_end=False):
        self.data, self.pos, self.chunk = data, 0, chunk
        self.block_at_end = block_at_end
        self.closed = threading.Event()

    def recv(self, n):
        if self.pos >= len(self.data):
            if self.block_at_end:
                if self.closed.wait(0.05):
                    return b""
                raise socket.timeout()
            return b""
        out = self.data[self.pos:self.pos + min(n, self.chunk)]
        self.pos += len(out)
        return out

    def settimeout(self, t):
        pass

    def close(self):
        self.closed.set()


class FakeDecoder:
    """Each frame payload b'F<v>' decodes to a 4x6 frame filled with v; config is recorded."""

    def __init__(self):
        self.calls = []

    def decode(self, data):
        self.calls.append(data)
        if b"BAD" in data:
            raise ValueError("corrupt")
        i = data.find(b"F")
        if i < 0:
            return []
        return [np.full((4, 6, 3), data[i + 1], dtype=np.uint8)]


class FakeCapture(sc.StreamCapture):
    def __init__(self, stream_bytes, block_at_end=True, **kw):
        self.decoders = []

        def factory():
            d = FakeDecoder()
            self.decoders.append(d)
            return d

        kw.setdefault("adb_path", "adb-not-used")
        kw.setdefault("first_frame_timeout_s", 2.0)
        super().__init__(decoder_factory=factory, **kw)
        self.stream_bytes = stream_bytes
        self.block_at_end = block_at_end
        self.launched = self.torn_down = 0

    def _launch_server(self):
        self.launched += 1
        self.port = 1234

    def _connect(self, timeout=10.0):
        s = ChunkSock(self.stream_bytes, block_at_end=self.block_at_end)
        self.device_name, codec = sc.read_handshake(s)
        return s

    def _teardown_server(self):
        self.torn_down += 1
        self.port = None


def stream(*frames, w=6, h=4):
    return handshake() + session(w, h) + media(b"SPSPPS", config=True) + b"".join(frames)


# --------------------------------------------------------------------------- protocol

def test_read_handshake_and_packets():
    s = ChunkSock(handshake() + session(1080, 2340) + media(b"cfg", config=True)
                  + media(b"abc", pts=12345, key=True) + media(b"", pts=7), chunk=3)
    assert sc.read_handshake(s) == ("SM-A245F", sc.CODEC_H264)
    p = sc.read_packet(s)
    assert (p.kind, p.width, p.height) == ("session", 1080, 2340)
    p = sc.read_packet(s)
    assert (p.kind, p.payload) == ("config", b"cfg")
    p = sc.read_packet(s)
    assert (p.kind, p.pts_us, p.key, p.payload) == ("frame", 12345, True, b"abc")
    p = sc.read_packet(s)
    assert (p.kind, p.pts_us, p.key, p.payload) == ("frame", 7, False, b"")
    with pytest.raises(sc.StreamError):
        sc.read_packet(s)


@pytest.mark.parametrize("code", [0, 1])
def test_handshake_disabled_or_error(code):
    with pytest.raises(sc.StreamError):
        sc.read_handshake(ChunkSock(handshake(codec=code)))


def test_server_args_are_read_only():
    cap = sc.StreamCapture(adb_path="adb", serial="X", max_size=1280, max_fps=30)
    args = cap.server_args()
    assert args[0] == sc.DEFAULT_SERVER_VERSION
    for a in ("audio=false", "control=false", "power_on=false", "tunnel_forward=true",
              "max_size=1280", "max_fps=30", "video_codec=h264"):
        assert a in args
    assert args[1] == f"scid={cap.scid:08x}"
    assert cap._adb_cmd(["shell", "x"]) == ["adb", "-s", "X", "shell", "x"]


# --------------------------------------------------------------------------- capture

def test_latest_frame_and_config_merge():
    cap = FakeCapture(stream(media(b"F\x0a", 1, key=True), media(b"F\x14", 2), media(b"F\x1e", 3)))
    with cap:
        assert cap.running
        assert cap.wait_frame(2, 2.0) == 3
        img = cap.screencap()
        assert img.shape == (4, 6, 3) and img.dtype == np.uint8 and img[0, 0, 0] == 30
        assert cap.last_source == "stream"
        assert cap.stream_size == (6, 4)
        assert cap.device_name == "SM-A245F"
        assert cap.decoders[0].calls[0] == b"SPSPPSF\x0a"   # config prepended to first frame
        assert cap.decoders[0].calls[1] == b"F\x14"
    assert cap.torn_down >= 1 and not cap.running


def test_rgb_conversion_is_cached():
    cap = FakeCapture(stream(media(b"F\x05", 1, key=True)))
    with cap:
        a = cap.screencap()
        b = cap.screencap()
        assert a is b


def test_output_size_resizes():
    cap = FakeCapture(stream(media(b"F\x05", 1, key=True)), output_size=(12, 8))
    with cap:
        assert cap.screencap().shape == (8, 12, 3)


def test_decode_error_is_skipped():
    cap = FakeCapture(stream(media(b"BAD", 1), media(b"F\x09", 2, key=True)))
    with cap:
        cap.wait_frame(0, 2.0)
        assert cap.screencap()[0, 0, 0] == 9
        assert cap.decode_errors == 1


def test_stale_frame_uses_fallback():
    fb = np.zeros((2340, 1080, 3), np.uint8)
    cap = FakeCapture(stream(media(b"F\x05", 1, key=True)), fallback=lambda: fb, max_age_s=0.05)
    with cap:
        time.sleep(0.15)
        assert cap.screencap() is fb
        assert cap.last_source == "fallback" and cap.fallback_count == 1


def test_start_failure_falls_back_without_raising():
    fb = np.ones((3, 3, 3), np.uint8)
    cap = FakeCapture(handshake(), block_at_end=False, fallback=lambda: fb, restart_interval_s=None)
    assert cap.start() is False
    assert cap.error and not cap.running
    assert cap.screencap() is fb
    assert cap.torn_down >= 1


def test_stream_eof_switches_to_fallback_and_restarts():
    fb = np.ones((3, 3, 3), np.uint8)
    cap = FakeCapture(stream(media(b"F\x05", 1, key=True)), block_at_end=False,
                      fallback=lambda: fb, restart_interval_s=0.0)
    cap.start()  # first frame arrives, then EOF stops the reader
    deadline = time.time() + 2
    while cap.running and time.time() < deadline:
        time.sleep(0.01)
    assert not cap.running and "closed" in (cap.error or "")
    cap.stream_bytes = stream(media(b"F\x07", 1, key=True))
    cap.block_at_end = True
    img = cap.screencap()          # triggers an automatic restart
    assert cap.restarts == 1 and cap.launched == 2
    assert img[0, 0, 0] == 7
    cap.stop()


def test_newer_than_waits_for_fresh_frame():
    cap = FakeCapture(stream(media(b"F\x01", 1, key=True)))
    with cap:
        t_action = time.perf_counter()
        sock = cap._sock

        def feed():
            time.sleep(0.1)
            sock.data += media(b"F\x02", 2)

        threading.Thread(target=feed).start()
        img = cap.screencap(newer_than=t_action)
        assert img[0, 0, 0] == 2


def test_streaming_device_forwards_other_calls():
    class Dev:
        serial, adb_path = "S", "adb"

        def screencap(self):
            return np.zeros((1, 1, 3), np.uint8)

        def focus(self):
            return "focus"

    d = sc.StreamingDevice(Dev(), restart_interval_s=None)
    # not started: screencap must use the device's own screencap as fallback
    assert d.focus() == "focus"
    assert d.screencap().shape == (1, 1, 3)
    assert d.stream.last_source == "fallback"
