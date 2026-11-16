"""ADB device access: screencap, drag/swipe, focus (ad detection), battery, reconnect.

Primary transport: adbutils (pure Python). Fallback: subprocess calls to the adb binary.
The device object never taps arbitrary UI; `tap` is only called by the session for
template-verified buttons (restart / decline), never for ads or purchases.
"""
from __future__ import annotations

import io
import os
import re
import shutil
import struct
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np

KEY_BACK = 4
KEY_HOME = 3
KEY_WAKEUP = 224


class DeviceError(RuntimeError):
    pass


@dataclass
class FocusInfo:
    package: Optional[str]
    activity: Optional[str]
    raw: str

    def is_package(self, pkg: Optional[str]) -> bool:
        return bool(pkg) and self.package == pkg


@dataclass
class BatteryInfo:
    level: Optional[int]
    temperature_c: Optional[float]
    plugged: Optional[bool]
    status: Optional[int]
    raw: str = ""

    def to_dict(self) -> dict:
        return {"level": self.level, "temperature_c": self.temperature_c,
                "plugged": self.plugged, "status": self.status}


_FOCUS_RE = re.compile(r"mCurrentFocus=Window\{[0-9a-f]+ (?:u\d+ )?([^\s/}]+)(?:/([^\s}]+))?\}")
_FOCUSED_APP_RE = re.compile(r"mFocusedApp=.*?ActivityRecord\{[0-9a-f]+ (?:u\d+ )?([^\s/}]+)/([^\s}]+)")


def parse_focus(text: str) -> FocusInfo:
    m = _FOCUS_RE.search(text)
    if m:
        return FocusInfo(m.group(1), m.group(2), text.strip()[:300])
    m = _FOCUSED_APP_RE.search(text)
    if m:
        return FocusInfo(m.group(1), m.group(2), text.strip()[:300])
    return FocusInfo(None, None, text.strip()[:300])


def parse_battery(text: str) -> BatteryInfo:
    def gi(key):
        m = re.search(rf"^\s*{key}:\s*(-?\d+)", text, re.M)
        return int(m.group(1)) if m else None

    level = gi("level")
    temp = gi("temperature")
    status = gi("status")
    plugged = None
    for k in ("AC powered", "USB powered", "Wireless powered"):
        m = re.search(rf"^\s*{k}:\s*(true|false)", text, re.M)
        if m and m.group(1) == "true":
            plugged = True
        elif m and plugged is None:
            plugged = False
    return BatteryInfo(level, temp / 10.0 if temp is not None else None, plugged, status, text[:500])


def decode_screencap(data: bytes) -> np.ndarray:
    """Decode raw `screencap` output (12/16-byte header + RGBA) or PNG into HxWx3 uint8 RGB."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        from PIL import Image
        return np.asarray(Image.open(io.BytesIO(data)).convert("RGB"))
    if len(data) < 16:
        raise DeviceError("screencap returned too few bytes")
    w, h, _fmt = struct.unpack("<III", data[:12])
    n = w * h * 4
    hdr = len(data) - n
    if w <= 0 or h <= 0 or hdr not in (12, 16):
        # some shells translate \n -> \r\n; try to undo that
        fixed = data.replace(b"\r\n", b"\n")
        if fixed is not data and len(fixed) != len(data):
            return decode_screencap(fixed)
        raise DeviceError(f"unexpected screencap size {len(data)} for {w}x{h}")
    arr = np.frombuffer(data, dtype=np.uint8, count=n, offset=hdr).reshape(h, w, 4)
    return arr[:, :, :3].copy()


def find_adb(explicit: Optional[str] = None) -> str:
    cands = [explicit, os.environ.get("ADB"), shutil.which("adb"),
             r"E:\tools\platform-tools\adb.exe", r"E:\Belgeler\blaster\tools\platform-tools\adb.exe"]
    for c in cands:
        if c and _path_exists(c):
            return c
    return "adb"


def _path_exists(p: str) -> bool:
    try:
        return os.path.exists(p) or shutil.which(p) is not None
    except OSError:
        return False


class AdbDevice:
    """Real phone over USB ADB."""

    def __init__(self, serial: Optional[str] = None, adb_path: Optional[str] = None,
                 use_adbutils: bool = True, drag_method: str = "swipe", timeout: float = 15.0):
        self.serial = serial
        self.adb_path = find_adb(adb_path)
        self.drag_method = drag_method
        self.timeout = timeout
        self._d = None
        self.use_adbutils = use_adbutils
        self.last_capture_ms = 0.0
        self.capture_path: Optional[str] = None
        self.reconnects = 0
        self._connect()

    # -- connection ---------------------------------------------------------
    def _connect(self):
        self._d = None
        if self.use_adbutils:
            try:
                import adbutils
                if self.serial:
                    self._d = adbutils.adb.device(serial=self.serial)
                else:
                    devs = adbutils.adb.device_list()
                    if not devs:
                        raise DeviceError("no device")
                    self._d = devs[0]
                    self.serial = self._d.serial
            except ImportError:
                self._d = None
            except Exception as ex:  # adbutils raises its own error types
                raise DeviceError(f"adbutils connect failed: {ex}") from ex
        if self._d is None and not self.serial:
            out = self._adb(["devices"]).decode(errors="replace")
            lines = [ln.split() for ln in out.splitlines()[1:] if ln.strip()]
            ok = [l[0] for l in lines if len(l) >= 2 and l[1] == "device"]
            if not ok:
                raise DeviceError(f"no authorised device: {out.strip()}")
            self.serial = ok[0]

    def is_connected(self) -> bool:
        try:
            return self.shell("echo ok", timeout=5).strip() == "ok"
        except Exception:
            return False

    def reconnect(self, max_wait_s: float = 300.0, log=None) -> bool:
        """Watchdog: wait for the device to come back (adb reconnect / cable glitch)."""
        t0 = time.time()
        delay = 2.0
        while time.time() - t0 < max_wait_s:
            try:
                try:
                    self._adb(["reconnect"], timeout=10)
                except Exception:
                    pass
                self._adb(["wait-for-device"], timeout=min(30, max_wait_s))
                self._connect()
                if self.is_connected():
                    self.reconnects += 1
                    return True
            except Exception as ex:
                if log:
                    log(f"reconnect attempt failed: {ex}")
            time.sleep(delay)
            delay = min(delay * 1.5, 20)
        return False

    def _adb(self, args, timeout: Optional[float] = None, input_bytes: Optional[bytes] = None) -> bytes:
        cmd = [self.adb_path] + (["-s", self.serial] if self.serial and args[0] not in ("devices",) else []) + list(args)
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout or self.timeout, input=input_bytes)
        except FileNotFoundError as ex:
            raise DeviceError(f"adb not found at {self.adb_path}") from ex
        except subprocess.TimeoutExpired as ex:
            raise DeviceError(f"adb timeout: {' '.join(args)}") from ex
        if r.returncode != 0:
            raise DeviceError(f"adb {' '.join(args)} failed: {r.stderr.decode(errors='replace').strip()}")
        return r.stdout

    # -- shell / capture ----------------------------------------------------
    def shell(self, cmd: str, timeout: Optional[float] = None) -> str:
        if self._d is not None:
            try:
                return self._d.shell(cmd, timeout=timeout or self.timeout)
            except TypeError:
                return self._d.shell(cmd)
            except Exception as ex:
                raise DeviceError(f"shell failed: {ex}") from ex
        return self._adb(["shell", cmd], timeout=timeout).decode(errors="replace")

    def screencap(self) -> np.ndarray:
        """Full-resolution RGB screenshot.

        Fastest path first: `adb exec-out screencap -p` (PNG, ~0.25 MB over USB, ~0.8 s on
        the SM-A245F) instead of the raw 10 MB dump through the adbutils shell (~3.2 s).
        The raw paths remain as fallbacks."""
        t0 = time.perf_counter()
        img = None
        errors = []
        for name, fn in (("exec-out-png", lambda: self._adb(["exec-out", "screencap", "-p"])),
                         ("exec-out-raw", lambda: self._adb(["exec-out", "screencap"])),
                         ("adbutils-raw", self._adbutils_screencap)):
            try:
                img = decode_screencap(fn())
                self.capture_path = name
                break
            except Exception as ex:
                errors.append(f"{name}: {ex}")
        if img is None:
            raise DeviceError("screencap failed: " + "; ".join(errors))
        self.last_capture_ms = (time.perf_counter() - t0) * 1000
        return img

    def _adbutils_screencap(self) -> bytes:
        if self._d is None:
            raise DeviceError("adbutils not available")
        return self._d.shell("screencap", encoding=None, timeout=self.timeout, rstrip=False)

    # -- input --------------------------------------------------------------
    def drag(self, x1: float, y1: float, x2: float, y2: float, duration_ms: int = 300):
        x1, y1, x2, y2 = (int(round(v)) for v in (x1, y1, x2, y2))
        if self.drag_method == "draganddrop":
            self.shell(f"input draganddrop {x1} {y1} {x2} {y2} {duration_ms}")
        elif self.drag_method == "motionevent":
            # Android 12+: explicit DOWN / MOVE steps / UP with a short hold.
            steps = 8
            parts = [f"input motionevent DOWN {x1} {y1}", "sleep 0.08"]
            for i in range(1, steps + 1):
                xi = x1 + (x2 - x1) * i / steps
                yi = y1 + (y2 - y1) * i / steps
                parts.append(f"input motionevent MOVE {int(xi)} {int(yi)}")
            parts += ["sleep 0.08", f"input motionevent UP {x2} {y2}"]
            self.shell(" ; ".join(parts), timeout=self.timeout + duration_ms / 1000)
        else:
            self.shell(f"input swipe {x1} {y1} {x2} {y2} {duration_ms}", timeout=self.timeout + duration_ms / 1000)

    def tap(self, x: float, y: float):
        self.shell(f"input tap {int(round(x))} {int(round(y))}")

    def keyevent(self, code: int):
        self.shell(f"input keyevent {int(code)}")

    def back(self):
        self.keyevent(KEY_BACK)

    def home(self):
        self.keyevent(KEY_HOME)

    def launch(self, package: str):
        """Bring the game to the foreground via its launcher intent (recovery)."""
        self.shell(f"monkey -p {package} -c android.intent.category.LAUNCHER 1")

    # -- info ---------------------------------------------------------------
    def focus(self) -> FocusInfo:
        out = self.shell("dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'")
        if "mCurrentFocus" not in out and "mFocusedApp" not in out:
            out = self.shell("dumpsys window windows | grep -E 'mCurrentFocus|mFocusedApp'")
        return parse_focus(out)

    def battery(self) -> BatteryInfo:
        return parse_battery(self.shell("dumpsys battery"))

    def wm_size(self) -> tuple:
        out = self.shell("wm size")
        m = re.findall(r"(\d+)x(\d+)", out)
        if not m:
            raise DeviceError(f"cannot parse wm size: {out}")
        w, h = m[-1]  # override size if present (listed last)
        return int(w), int(h)

    def metadata(self) -> dict:
        def gp(k):
            try:
                return self.shell(f"getprop {k}").strip()
            except Exception:
                return None
        md = {"serial": self.serial, "model": gp("ro.product.model"), "manufacturer": gp("ro.product.manufacturer"),
              "android": gp("ro.build.version.release"), "sdk": gp("ro.build.version.sdk")}
        try:
            md["wm_size"] = self.shell("wm size").strip()
            md["wm_density"] = self.shell("wm density").strip()
        except Exception:
            pass
        return md

    def get_brightness(self) -> dict:
        """Current brightness settings (value 0-255 and auto mode 0/1)."""
        def gs(k):
            try:
                v = self.shell(f"settings get system {k}").strip()
                return int(v) if v.lstrip("-").isdigit() else None
            except Exception:
                return None
        return {"screen_brightness": gs("screen_brightness"), "screen_brightness_mode": gs("screen_brightness_mode")}

    def set_brightness(self, value: Optional[int] = None, mode: Optional[int] = None):
        """Set manual brightness (OLED burn-in protection during long runs)."""
        if mode is not None:
            self.shell(f"settings put system screen_brightness_mode {int(mode)}")
        if value is not None:
            self.shell(f"settings put system screen_brightness {max(0, min(255, int(value)))}")

    def package_version(self, package: str) -> Optional[str]:
        try:
            out = self.shell(f"dumpsys package {package} | grep versionName")
            m = re.search(r"versionName=(\S+)", out)
            return m.group(1) if m else None
        except Exception:
            return None

    def find_packages(self, pattern: str = "blast") -> list:
        out = self.shell("pm list packages")
        return [ln.split(":", 1)[1].strip() for ln in out.splitlines() if pattern.lower() in ln.lower() and ":" in ln]


class FolderDevice:
    """Replays screenshots from a folder (parse-only dry run). Input calls are recorded, not sent."""

    def __init__(self, folder, package: str = "folder.game", loop: bool = False):
        from pathlib import Path
        self.files = sorted([p for p in Path(folder).iterdir() if p.suffix.lower() in (".png", ".jpg", ".jpeg")])
        if not self.files:
            raise DeviceError(f"no images in {folder}")
        self.i = 0
        self.loop = loop
        self.package = package
        self.actions: list = []
        self.last_capture_ms = 0.0
        self.reconnects = 0
        self.exhausted = False

    def screencap(self) -> np.ndarray:
        from PIL import Image
        if self.i >= len(self.files):
            if not self.loop:
                self.exhausted = True
                self.i = len(self.files) - 1
            else:
                self.i = 0
        img = np.asarray(Image.open(self.files[self.i]).convert("RGB"))
        self.current_file = str(self.files[self.i])
        return img

    def advance(self):
        self.i += 1

    def drag(self, *a, **k):
        self.actions.append(("drag", a))
        self.advance()

    def tap(self, *a):
        self.actions.append(("tap", a))

    def keyevent(self, code):
        self.actions.append(("key", code))

    def back(self):
        self.keyevent(KEY_BACK)

    def home(self):
        self.keyevent(KEY_HOME)

    def launch(self, package):
        self.actions.append(("launch", package))

    def focus(self) -> FocusInfo:
        return FocusInfo(self.package, "Folder", "folder")

    def battery(self) -> BatteryInfo:
        return BatteryInfo(100, 30.0, True, 2)

    def is_connected(self) -> bool:
        return True

    def reconnect(self, *a, **k) -> bool:
        return True

    def metadata(self) -> dict:
        return {"device": "folder", "files": len(self.files)}

    def package_version(self, package):
        return None

    def get_brightness(self) -> dict:
        return {"screen_brightness": None, "screen_brightness_mode": None}

    def set_brightness(self, value=None, mode=None):
        self.actions.append(("brightness", value, mode))


def main(argv=None) -> int:
    """python -m bot.capture {info,grab,focus,battery,packages,time} (needs a connected phone)."""
    import argparse
    import json
    ap = argparse.ArgumentParser(prog="python -m bot.capture", description="ADB helpers")
    ap.add_argument("--serial")
    ap.add_argument("--adb-path")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info")
    g = sub.add_parser("grab")
    g.add_argument("out")
    sub.add_parser("focus")
    sub.add_parser("battery")
    p = sub.add_parser("packages")
    p.add_argument("--pattern", default="juggle", help="substring of the package name (Block Blast: com.block.juggle)")
    t = sub.add_parser("time", help="measure the time of 10 screenshots")
    t.add_argument("--n", type=int, default=10)
    a = ap.parse_args(argv)
    dev = AdbDevice(a.serial, a.adb_path)
    if a.cmd == "info":
        md = dev.metadata()
        md["focus"] = dev.focus().__dict__
        md["battery"] = dev.battery().to_dict()
        md["brightness"] = dev.get_brightness()
        print(json.dumps(md, indent=1, ensure_ascii=False))
    elif a.cmd == "grab":
        from PIL import Image
        img = dev.screencap()
        Image.fromarray(img).save(a.out)
        print(f"{a.out}: {img.shape[1]}x{img.shape[0]}, {dev.last_capture_ms:.0f} ms")
    elif a.cmd == "focus":
        print(dev.focus())
    elif a.cmd == "battery":
        print(dev.battery().to_dict())
    elif a.cmd == "packages":
        print("\n".join(dev.find_packages(a.pattern)))
    elif a.cmd == "time":
        ts = []
        for _ in range(a.n):
            dev.screencap()
            ts.append(dev.last_capture_ms)
        ts.sort()
        print(f"median {ts[len(ts) // 2]:.0f} ms, min {ts[0]:.0f}, max {ts[-1]:.0f}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
