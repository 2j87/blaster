"""Simulated phone for dry runs: renders synthetic 1080x2340 Block Blast-like screens,
interprets drags like the real game (piece drawn above the finger), and simulates the
annoying parts of a long run: score count-up, combo banners, revive offer, game-over
screen, ads in another package, ADB disconnects, battery heating and an edge overlay.

Uses a virtual clock so long sessions run in seconds in tests.
"""
from __future__ import annotations

import random
from typing import Optional

import numpy as np

from . import engine, scoring
from .capture import BatteryInfo, DeviceError, FocusInfo

FAKE_PACKAGE = "com.fake.blockblast"
AD_PACKAGE = "com.fake.adsdk"

_FONT = {  # 5x7 bitmap digits
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
}

PALETTE = [(230, 70, 70), (250, 170, 40), (250, 220, 60), (80, 200, 90), (60, 180, 230),
           (150, 90, 230), (240, 110, 200)]


class VirtualClock:
    def __init__(self, t0: float = 1_800_000_000.0):
        self.t = t0

    def time(self) -> float:
        return self.t

    def sleep(self, s: float):
        self.t += max(0.0, s)


class RealClock:
    def time(self) -> float:
        import time
        return time.time()

    def sleep(self, s: float):
        import time
        time.sleep(max(0.0, s))


class FakeDevice:
    W, H = 1080, 2340
    BG = (58, 88, 168)
    FRAME = (22, 30, 70)
    EMPTY = (32, 42, 92)
    GRID = (24, 33, 76)

    def __init__(self, seed: int = 0, clock=None, true_offset=(0.0, -2.0), ad_prob: float = 0.0,
                 disconnect_prob: float = 0.0, model: scoring.ScoringModel = scoring.CANDIDATE_A,
                 generator=None, edge_overlay: bool = False, temp_profile=None, banner_s: float = 0.8,
                 countup_s: float = 0.5, revive_timeout: float = 10.0):
        self.clock = clock or VirtualClock()
        self.rng = random.Random(seed)
        self.seed = seed
        self.model = model
        self.generator = generator
        self.true_offset = true_offset
        self.ad_prob = ad_prob
        self.disconnect_prob = disconnect_prob
        self.edge_overlay = edge_overlay
        self.temp_profile = temp_profile  # callable(t_elapsed) -> temp C
        self.banner_s = banner_s
        self.countup_s = countup_s
        self.revive_timeout = revive_timeout
        self.t_start = self.clock.time()
        self.package = FAKE_PACKAGE
        self.last_capture_ms = 5.0
        self.reconnects = 0
        self.connected = True
        self.best = 0
        self.games = 0
        self.screen = "game"  # game | revive | gameover | home
        self.ad_until: Optional[float] = None
        self.ad_backable_at: Optional[float] = None
        self.revive_at: Optional[float] = None
        self.brightness = {"screen_brightness": 128, "screen_brightness_mode": 1}
        self.taps: list = []
        self.drags: list = []
        self.unknown_taps = 0
        # geometry
        self.bx0, self.by0 = 50, 714
        self.side = 980
        self.cell = self.side / 8.0
        self.tray_cell = self.cell * 0.5
        self.tray_cy = self.by0 + self.side + 0.25 * self.side
        self.button_boxes = {
            "decline": (390, 1500, 690, 1600),
            "continue_ad": (290, 1250, 790, 1400),
            "restart": (340, 1600, 740, 1760),
        }
        self._new_game()

    # -- game ---------------------------------------------------------------
    def _new_game(self):
        gen = self.generator or engine.UniformGenerator(seed=self.rng.randrange(1 << 30))
        self.game = engine.Game(scoring=self.model, generator=gen)
        self.colors = {}
        self.hand_colors = [self.rng.randrange(len(PALETTE)) for _ in range(3)]
        self.score_from = 0
        self.score_t = 0.0
        self.banner_until = 0.0
        self.screen = "game"
        self.games += 1
        self.last_hand_id = self.game.hands_dealt

    def _check_over(self):
        if self.game.over and self.screen == "game":
            self.screen = "revive"
            self.revive_at = self.clock.time()
            self.best = max(self.best, self.game.score)

    # -- device API ---------------------------------------------------------
    def _maybe_fail(self):
        if not self.connected:
            raise DeviceError("device offline")
        if self.disconnect_prob and self.rng.random() < self.disconnect_prob:
            self.connected = False
            raise DeviceError("device offline")

    def is_connected(self) -> bool:
        return self.connected

    def reconnect(self, max_wait_s: float = 300, log=None) -> bool:
        self.clock.sleep(3.0)
        self.connected = True
        self.reconnects += 1
        return True

    def shell(self, cmd: str, timeout=None) -> str:
        self._maybe_fail()
        return ""

    def focus(self) -> FocusInfo:
        self._maybe_fail()
        self._tick()
        if self.ad_until is not None:
            return FocusInfo(AD_PACKAGE, "AdActivity", "ad")
        if self.screen == "home":
            return FocusInfo("com.sec.android.app.launcher", "Launcher", "home")
        return FocusInfo(FAKE_PACKAGE, "UnityPlayerActivity", "game")

    def battery(self) -> BatteryInfo:
        self._maybe_fail()
        t = self.clock.time() - self.t_start
        temp = self.temp_profile(t) if self.temp_profile else 33.0
        return BatteryInfo(90, temp, True, 2)

    def get_brightness(self) -> dict:
        return dict(self.brightness)

    def set_brightness(self, value=None, mode=None):
        if mode is not None:
            self.brightness["screen_brightness_mode"] = int(mode)
        if value is not None:
            self.brightness["screen_brightness"] = int(value)

    def metadata(self) -> dict:
        return {"device": "fake", "model": "FakePhone 1080x2340", "seed": self.seed}

    def package_version(self, package):
        return "fake-1.0"

    def _tick(self):
        now = self.clock.time()
        if self.ad_until is not None and now >= self.ad_until:
            self.ad_until = None
        if self.screen == "revive" and self.revive_at is not None and now - self.revive_at > self.revive_timeout:
            self.screen = "gameover"

    def keyevent(self, code: int):
        self._maybe_fail()
        if code == 4:  # BACK
            if self.ad_until is not None and self.ad_backable_at is not None and self.clock.time() >= self.ad_backable_at:
                self.ad_until = None
        elif code == 3:  # HOME
            self.screen = "home" if self.screen == "game" else self.screen

    def back(self):
        self.keyevent(4)

    def home(self):
        self.keyevent(3)

    def launch(self, package):
        if self.screen == "home":
            self.screen = "game"

    def tap(self, x, y):
        self._maybe_fail()
        self.taps.append((x, y))

        def inside(name):
            x0, y0, x1, y1 = self.button_boxes[name]
            return x0 <= x <= x1 and y0 <= y <= y1

        if self.screen == "revive" and inside("decline"):
            self.screen = "gameover"
        elif self.screen == "revive" and inside("continue_ad"):
            self.unknown_taps += 1  # the bot must never do this
        elif self.screen == "gameover" and inside("restart"):
            self._new_game()
        else:
            self.unknown_taps += 1

    def drag(self, x1, y1, x2, y2, duration_ms: int = 300):
        self._maybe_fail()
        self.drags.append((x1, y1, x2, y2))
        self.clock.sleep(duration_ms / 1000.0)
        if self.screen != "game" or self.ad_until is not None:
            return
        slot = self._slot_at(x1, y1)
        if slot is None or self.game.hand[slot] is None:
            return
        p = self.game.hand[slot]
        cx = x2 + self.true_offset[0] * self.cell
        cy = y2 + self.true_offset[1] * self.cell
        c = int(round((cx - self.bx0) / self.cell - p.w / 2.0))
        r = int(round((cy - self.by0) / self.cell - p.h / 2.0))
        if not engine.can_place(self.game.board, p, r, c):
            return
        color = self.hand_colors[slot]
        hand_before = self.game.hands_dealt
        mr, delta = self.game.play(slot, r, c)
        for cell in engine.iter_bits(p.mask_at(r, c)):
            self.colors[cell] = color
        self.colors = {k: v for k, v in self.colors.items() if mr.board_after >> k & 1}
        if self.game.hands_dealt != hand_before:
            self.hand_colors = [self.rng.randrange(len(PALETTE)) for _ in range(3)]
        if delta:
            self.score_from = self.game.score - delta
            self.score_t = self.clock.time()
        if mr.n_lines:
            self.banner_until = self.clock.time() + self.banner_s
        self._check_over()
        if self.ad_prob and self.rng.random() < self.ad_prob and self.screen == "game":
            self.ad_until = self.clock.time() + 25.0
            self.ad_backable_at = self.clock.time() + 5.0

    def _slot_at(self, x, y) -> Optional[int]:
        if abs(y - self.tray_cy) > 3 * self.tray_cell:
            return None
        i = int((x - self.bx0) // (self.side / 3))
        return i if 0 <= i < 3 else None

    # -- rendering ----------------------------------------------------------
    def screencap(self) -> np.ndarray:
        self._maybe_fail()
        self._tick()
        img = np.empty((self.H, self.W, 3), np.uint8)
        img[:] = self.BG
        if self.ad_until is not None:
            img[:] = (5, 5, 5)
            img[100:180, 950:1030] = (200, 200, 200)
            return img
        if self.screen == "home":
            img[:] = (20, 120, 60)
            return img
        self._draw_board(img)
        self._draw_tray(img)
        now = self.clock.time()
        frac = (now - self.score_t) / self.countup_s if self.countup_s > 0 else 1.0
        if frac < 1.0:
            shown = int(self.score_from + (self.game.score - self.score_from) * max(0.0, frac))
        else:
            shown = self.game.score
        self._draw_number(img, max(0, shown), cx=self.W // 2, cy=515, scale=11)
        self._draw_number(img, max(self.best, 0), cx=200, cy=260, scale=6)
        if now < self.banner_until:
            # animated like the real banner: it zooms/drifts while visible
            k = int(((self.banner_until - now) * 100) % 60)
            y = self.by0 + int(self.side * 0.4) - k
            img[y:y + 120, 150 + k:930 - k] = (250, 245, 210)
            img[y + 40:y + 80, 200 + k:880 - k:40] = (200, 60, 60)
        if self.screen in ("revive", "gameover"):
            img[:] = (img.astype(np.float32) * 0.35).astype(np.uint8)
            if self.screen == "revive":
                self._button(img, "continue_ad", (60, 200, 80), stripes=True)
                self._button(img, "decline", (150, 150, 160), stripes=False)
            else:
                self._button(img, "restart", (240, 160, 40), stripes=True)
        if self.edge_overlay:  # Samsung Game Booster style edge handle
            img[1000:1200, self.W - 18:self.W] = (240, 240, 240)
        return img

    def _button(self, img, name, color, stripes):
        x0, y0, x1, y1 = self.button_boxes[name]
        img[y0:y1, x0:x1] = color
        img[y0 + 10:y1 - 10, x0 + 10:x0 + 30] = (255, 255, 255)
        if stripes:
            for k in range(x0 + 60, x1 - 20, 50):
                img[y0 + 25:y1 - 25, k:k + 15] = (255, 255, 255)
        else:
            img[(y0 + y1) // 2 - 6:(y0 + y1) // 2 + 6, x0 + 60:x1 - 40] = (30, 30, 30)

    def _draw_board(self, img):
        x0, y0, s = self.bx0, self.by0, self.side
        img[y0 - 10:y0 + s + 10, x0 - 10:x0 + s + 10] = self.FRAME
        img[y0:y0 + s, x0:x0 + s] = self.GRID
        g = 3
        for r in range(8):
            for c in range(8):
                ya, xa = int(y0 + r * self.cell) + g, int(x0 + c * self.cell) + g
                yb, xb = int(y0 + (r + 1) * self.cell) - g, int(x0 + (c + 1) * self.cell) - g
                k = r * 8 + c
                if self.game.board >> k & 1:
                    col = PALETTE[self.colors.get(k, 0)]
                    img[ya:yb, xa:xb] = col
                    img[ya:ya + 8, xa:xb] = tuple(min(255, v + 40) for v in col)
                else:
                    img[ya:yb, xa:xb] = self.EMPTY

    def _draw_tray(self, img):
        tc = self.tray_cell
        for i, p in enumerate(self.game.hand):
            if p is None:
                continue
            cx = self.bx0 + (i + 0.5) * self.side / 3
            col = PALETTE[self.hand_colors[i]]
            ox, oy = cx - p.w * tc / 2, self.tray_cy - p.h * tc / 2
            for r, c in p.cells:
                ya, xa = int(oy + r * tc) + 2, int(ox + c * tc) + 2
                img[ya:int(oy + (r + 1) * tc) - 2, xa:int(ox + (c + 1) * tc) - 2] = col

    def _draw_number(self, img, value: int, cx: int, cy: int, scale: int):
        s = str(int(value))
        gw, gap = 5 * scale, scale * 2
        total = len(s) * gw + (len(s) - 1) * gap
        x = cx - total // 2
        y = cy - (7 * scale) // 2
        for ch in s:
            for r, row in enumerate(_FONT[ch]):
                for c, v in enumerate(row):
                    if v == "1":
                        img[y + r * scale:y + (r + 1) * scale, x + c * scale:x + (c + 1) * scale] = (255, 255, 255)
            x += gw + gap

    # -- helpers for tests / dry-run calibration -----------------------------
    def board_box(self) -> tuple:
        return (self.bx0, self.by0, self.bx0 + self.side, self.by0 + self.side)

    def render_score_sample(self, value: int) -> np.ndarray:
        """Screen with a given score (for digit-template learning in dry runs)."""
        img = np.empty((self.H, self.W, 3), np.uint8)
        img[:] = self.BG
        self._draw_number(img, value, cx=self.W // 2, cy=515, scale=11)
        return img

    def render_screen(self, screen: str) -> np.ndarray:
        old = self.screen
        self.screen = screen
        img = self.screencap()
        self.screen = old
        return img
