"""Screen parsing for Block Blast!: calibration, board, tray, score, banners, buttons.

Everything is stored as screen FRACTIONS in calibration.json so the same file works
after resolution changes (the default target is a 1080x2340 portrait Samsung A24).
Only numpy + Pillow are required (scipy is used for labelling when available).

Calibration flow (single screenshot + assisted step):
    1. python -m bot.capture grab shot_empty.png            (empty board, Classic)
    2. python -m bot.vision calibrate shot_empty.png --score-value 0 --overlay cal.png
       -> look at cal.png; if a rectangle is off, re-run with --board/--tray/--score px boxes
    3. python -m bot.vision label-score shot_x.png 1234      (repeat until digits 0-9 learnt)
    4. python -m bot.vision tray-scale shot_hand.png          (estimates tray cell size)
    5. python -m bot.vision add-button gameover.png --name restart --box x0 y0 x1 y1
       python -m bot.vision add-button revive.png --name decline --box x0 y0 x1 y1
    6. python -m bot.vision parse shot.png                     (sanity check)
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from . import engine

BOT_DIR = Path(__file__).resolve().parent
DEFAULT_CAL = BOT_DIR / "calibration.json"
TEMPLATE_DIR = BOT_DIR / "templates"
GLYPH_W, GLYPH_H = 20, 30
BUTTON_KINDS = ("restart", "decline", "play", "close", "home_play", "continue_free")

# Default geometry for a 1080x2340 portrait screen (fractions). Only a starting guess;
# real values come from `calibrate`.
DEFAULT_GEOMETRY = {
    "board": [0.046, 0.305, 0.954, 0.724],
    "tray": [0.03, 0.75, 0.97, 0.92],
    "score": [0.2, 0.17, 0.8, 0.27],
    "best": [0.02, 0.10, 0.45, 0.16],
}


# ----------------------------------------------------------------------------
# Calibration
# ----------------------------------------------------------------------------

@dataclass
class Calibration:
    screen: list = field(default_factory=lambda: [1080, 2340])
    board: list = field(default_factory=lambda: list(DEFAULT_GEOMETRY["board"]))
    tray: list = field(default_factory=lambda: list(DEFAULT_GEOMETRY["tray"]))
    score: list = field(default_factory=lambda: list(DEFAULT_GEOMETRY["score"]))
    best: Optional[list] = None  # personal-best region (optional)
    empty_rgb: list = field(default_factory=lambda: [30, 40, 90])
    fill_thresh: float = 55.0
    tray_cell_ratio: float = 0.5  # tray cell pitch / board cell pitch
    finger_offset_cells: list = field(default_factory=lambda: [0.0, -2.0])  # piece centre - finger
    offset_verified: bool = False
    game_package: Optional[str] = None
    buttons: dict = field(default_factory=dict)  # name -> {"box": frac box, "template": rel path}
    template_dir: str = "templates"
    version: int = 1

    # -- persistence --------------------------------------------------------
    def save(self, path=DEFAULT_CAL):
        Path(path).write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path=DEFAULT_CAL) -> "Calibration":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    @property
    def tdir(self) -> Path:
        p = Path(self.template_dir)
        return p if p.is_absolute() else BOT_DIR / p

    # -- geometry -----------------------------------------------------------
    def px_box(self, box, shape) -> tuple:
        h, w = shape[:2]
        x0, y0, x1, y1 = box
        return (int(round(x0 * w)), int(round(y0 * h)), int(round(x1 * w)), int(round(y1 * h)))

    def board_px(self, shape) -> tuple:
        return self.px_box(self.board, shape)

    def cell_px(self, shape) -> tuple:
        x0, y0, x1, y1 = self.board_px(shape)
        return (x1 - x0) / 8.0, (y1 - y0) / 8.0

    def cell_center(self, r: float, c: float, shape) -> tuple:
        x0, y0, x1, y1 = self.board_px(shape)
        cw, ch = (x1 - x0) / 8.0, (y1 - y0) / 8.0
        return x0 + (c + 0.5) * cw, y0 + (r + 0.5) * ch

    def slot_boxes(self, shape) -> list:
        x0, y0, x1, y1 = self.px_box(self.tray, shape)
        w = (x1 - x0) / 3.0
        return [(int(x0 + i * w), y0, int(x0 + (i + 1) * w), y1) for i in range(3)]

    def drag_points(self, piece: engine.Piece, r: int, c: int, shape, slot_center: tuple) -> tuple:
        """(start, end) screen points to drop `piece` with top-left at (r, c)."""
        x0, y0, x1, y1 = self.board_px(shape)
        cw, ch = (x1 - x0) / 8.0, (y1 - y0) / 8.0
        tx = x0 + (c + piece.w / 2.0) * cw
        ty = y0 + (r + piece.h / 2.0) * ch
        dx, dy = self.finger_offset_cells
        return slot_center, (tx - dx * cw, ty - dy * ch)


def crop(img: np.ndarray, box) -> np.ndarray:
    x0, y0, x1, y1 = box
    h, w = img.shape[:2]
    return img[max(0, y0):min(h, y1), max(0, x0):min(w, x1)]


def _dist(a: np.ndarray, ref) -> np.ndarray:
    return np.sqrt(((a.astype(np.float32) - np.asarray(ref, np.float32)) ** 2).sum(axis=-1))


def _label(mask: np.ndarray):
    """Connected components (4-neighbour). Returns (labels, n)."""
    try:
        from scipy import ndimage
        return ndimage.label(mask)
    except ImportError:
        pass
    h, w = mask.shape
    lab = np.zeros((h, w), np.int32)
    n = 0
    ys, xs = np.nonzero(mask)
    for y, x in zip(ys, xs):
        if lab[y, x]:
            continue
        n += 1
        stack = [(y, x)]
        lab[y, x] = n
        while stack:
            cy, cx = stack.pop()
            for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not lab[ny, nx]:
                    lab[ny, nx] = n
                    stack.append((ny, nx))
    return lab, n


def _dilate(mask: np.ndarray, k: int) -> np.ndarray:
    out = mask.copy()
    for _ in range(k):
        m = out.copy()
        m[1:] |= out[:-1]
        m[:-1] |= out[1:]
        m[:, 1:] |= out[:, :-1]
        m[:, :-1] |= out[:, 1:]
        out = m
    return out


def detect_board(img: np.ndarray, step: int = 4) -> tuple:
    """Find the board as the largest region with the dominant central colour.
    Returns ((x0, y0, x1, y1) px, empty_rgb)."""
    h, w = img.shape[:2]
    small = img[::step, ::step].astype(np.int32)
    sh, sw = small.shape[:2]
    cen = small[int(sh * 0.32):int(sh * 0.62), int(sw * 0.2):int(sw * 0.8)].reshape(-1, 3)
    q = (cen // 24)
    keys = q[:, 0] * 10000 + q[:, 1] * 100 + q[:, 2]
    vals, counts = np.unique(keys, return_counts=True)
    mode = vals[np.argmax(counts)]
    ref = cen[keys == mode].mean(axis=0)
    mask = _dist(small, ref) < 38
    mask = _dilate(mask, 2)
    lab, n = _label(mask)
    if n == 0:
        raise ValueError("board not found")
    sizes = np.bincount(lab.ravel())
    sizes[0] = 0
    k = int(np.argmax(sizes))
    ys, xs = np.nonzero(lab == k)
    x0, x1 = (xs.min() + 2) * step, (xs.max() - 1) * step + step
    y0, y1 = (ys.min() + 2) * step, (ys.max() - 1) * step + step
    rough = (int(x0), int(y0), int(x1), int(y1))
    refined = refine_board(img, rough, ref)
    x0, y0, x1, y1 = refined or rough
    # square it up (the board is square; trust the width, centre vertically)
    side = x1 - x0
    if abs((y1 - y0) - side) > 0.08 * side:
        cy = (y0 + y1) / 2.0
        y0, y1 = int(cy - side / 2), int(cy + side / 2)
    return (int(x0), int(y0), int(x1), int(y1)), [float(v) for v in ref]


def _runs(flags: np.ndarray) -> list:
    runs, start = [], None
    for i, v in enumerate(flags):
        if v and start is None:
            start = i
        elif not v and start is not None:
            runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, len(flags)))
    return runs


def refine_board(img: np.ndarray, rough: tuple, ref, tight: float = 16.0) -> Optional[tuple]:
    """Refine a rough board box using the periodic pattern of empty-cell interiors
    (frame and grid lines have a slightly different colour). Needs a mostly empty board."""
    x0, y0, x1, y1 = rough
    pad = int(0.03 * (x1 - x0))
    h, w = img.shape[:2]
    X0, Y0, X1, Y1 = max(0, x0 - pad), max(0, y0 - pad), min(w, x1 + pad), min(h, y1 + pad)
    sub = img[Y0:Y1, X0:X1]
    m = _dist(sub, ref) < tight
    out = []
    for axis, off in ((0, X0), (1, Y0)):
        prof = m.mean(axis=axis)
        if prof.max() < 0.3:
            return None
        runs = [r for r in _runs(prof > 0.5 * prof.max()) if r[1] - r[0] > 3]
        if len(runs) < 8:
            return None
        widths = np.array([b - a for a, b in runs])
        cw = np.median(widths)
        runs = [r for r in runs if 0.6 * cw <= r[1] - r[0] <= 1.4 * cw]
        if len(runs) < 8:
            return None
        starts = np.array([a for a, _ in runs], np.float32)
        # pitch from neighbouring starts; keep the 8 runs best matching a regular grid
        pitch = float(np.median(np.diff(starts)))
        if len(runs) > 8:
            best, best_e = 0, 1e9
            for k in range(len(runs) - 7):
                e = float(np.abs(np.diff(starts[k:k + 8]) - pitch).sum())
                if e < best_e:
                    best, best_e = k, e
            runs = runs[best:best + 8]
            starts = starts[best:best + 8]
        pitch = (runs[-1][0] - runs[0][0]) / 7.0
        gap = pitch - float(np.median([b - a for a, b in runs]))
        lo = runs[0][0] - gap / 2.0
        out.append((off + lo, off + lo + 8 * pitch))
    (bx0, bx1), (by0, by1) = out
    return int(round(bx0)), int(round(by0)), int(round(bx1)), int(round(by1))


def cell_colors(img: np.ndarray, cal: Calibration, patch: float = 0.4) -> np.ndarray:
    """Median RGB of the central patch of each cell -> (8, 8, 3) float."""
    x0, y0, x1, y1 = cal.board_px(img.shape)
    cw, ch = (x1 - x0) / 8.0, (y1 - y0) / 8.0
    out = np.zeros((8, 8, 3), np.float32)
    m = (1 - patch) / 2
    for r in range(8):
        for c in range(8):
            ya, yb = int(y0 + (r + m) * ch), int(y0 + (r + 1 - m) * ch)
            xa, xb = int(x0 + (c + m) * cw), int(x0 + (c + 1 - m) * cw)
            out[r, c] = np.median(img[ya:max(yb, ya + 1), xa:max(xb, xa + 1)].reshape(-1, 3), axis=0)
    return out


@dataclass
class BoardRead:
    board: int
    dist: np.ndarray  # (8, 8) distance to the empty colour
    ambiguous: int  # cells close to the threshold
    confidence: float  # fraction of clearly classified cells
    colors: np.ndarray


def parse_board(img: np.ndarray, cal: Calibration) -> BoardRead:
    cols = cell_colors(img, cal)
    d = _dist(cols, cal.empty_rgb)
    t = cal.fill_thresh
    filled = d > t
    bright_ref = max(cal.empty_rgb)
    # a "filled" cell must also be brighter or more saturated than the empty colour,
    # otherwise it is probably a dimming overlay (game over / pop-up)
    vmax = cols.max(axis=-1)
    sat = vmax - cols.min(axis=-1)
    plausible = (vmax > bright_ref + 25) | (sat > 70)
    clear_empty = d < 0.6 * t
    clear_filled = (d > 1.4 * t) & plausible
    amb = int((~(clear_empty | clear_filled)).sum())
    b = 0
    for r in range(8):
        for c in range(8):
            if filled[r, c]:
                b |= engine.bit(r, c)
    return BoardRead(b, d, amb, float((clear_empty | clear_filled).mean()), cols)


# ----------------------------------------------------------------------------
# Tray
# ----------------------------------------------------------------------------

@dataclass
class SlotRead:
    piece: Optional[engine.Piece]
    bbox: Optional[tuple]  # px (x0, y0, x1, y1) in screen coordinates
    center: tuple  # px drag start point
    confidence: float
    dims: tuple = (0, 0)


def _slot_mask(sub: np.ndarray, thresh: float = 45.0) -> np.ndarray:
    border = np.concatenate([sub[:3].reshape(-1, 3), sub[-3:].reshape(-1, 3),
                             sub[:, :3].reshape(-1, 3), sub[:, -3:].reshape(-1, 3)])
    bg = np.median(border, axis=0)
    m = _dist(sub, bg) > thresh
    # light opening against single-pixel noise
    er = m.copy()
    er[1:-1, 1:-1] = m[1:-1, 1:-1] & m[:-2, 1:-1] & m[2:, 1:-1] & m[1:-1, :-2] & m[1:-1, 2:]
    return _dilate(er, 1) & m


def _mask_bbox(m: np.ndarray, min_count: int) -> Optional[tuple]:
    rows = np.nonzero(m.sum(axis=1) >= min_count)[0]
    cols = np.nonzero(m.sum(axis=0) >= min_count)[0]
    if len(rows) == 0 or len(cols) == 0:
        return None
    return int(cols[0]), int(rows[0]), int(cols[-1]) + 1, int(rows[-1]) + 1


def _occupancy(m: np.ndarray, nh: int, nw: int) -> np.ndarray:
    h, w = m.shape
    occ = np.zeros((nh, nw), np.float32)
    for r in range(nh):
        for c in range(nw):
            ya, yb = int((r + 0.25) * h / nh), int((r + 0.75) * h / nh)
            xa, xb = int((c + 0.25) * w / nw), int((c + 0.75) * w / nw)
            patch = m[ya:max(yb, ya + 1), xa:max(xb, xa + 1)]
            occ[r, c] = patch.mean() if patch.size else 0
    return occ


def shape_from_mask(m: np.ndarray, tray_cell: float) -> tuple:
    """Map a cropped piece mask to (Piece, confidence, dims)."""
    h, w = m.shape
    nh0 = int(np.clip(round(h / tray_cell), 1, 5))
    nw0 = int(np.clip(round(w / tray_cell), 1, 5))
    best = (None, -1.0, (nh0, nw0))
    for nh in sorted({nh0, max(1, nh0 - 1), min(5, nh0 + 1)}, key=lambda v: abs(v - nh0)):
        for nw in sorted({nw0, max(1, nw0 - 1), min(5, nw0 + 1)}, key=lambda v: abs(v - nw0)):
            occ = _occupancy(m, nh, nw)
            cells = [(r, c) for r in range(nh) for c in range(nw) if occ[r, c] > 0.5]
            if not cells:
                continue
            p = engine.piece_from_cells(cells)
            if p.h != nh or p.w != nw:
                continue  # empty border rows/cols -> wrong grid
            margin = float(np.abs(occ - 0.5).mean() * 2)  # 1 = crisp
            dim_err = abs(h / nh - tray_cell) / tray_cell + abs(w / nw - tray_cell) / tray_cell
            conf = margin - 0.8 * dim_err - (0.3 if not p.core else 0.0)
            if conf > best[1]:
                best = (p, conf, (nh, nw))
    return best


def parse_tray(img: np.ndarray, cal: Calibration, thresh: float = 45.0) -> list:
    bw = cal.cell_px(img.shape)[0]
    tc = bw * cal.tray_cell_ratio
    out = []
    for (x0, y0, x1, y1) in cal.slot_boxes(img.shape):
        sub = img[y0:y1, x0:x1]
        m = _slot_mask(sub, thresh)
        bb = _mask_bbox(m, max(2, int(tc * 0.25)))
        if bb is None or (bb[2] - bb[0]) < tc * 0.5 or (bb[3] - bb[1]) < tc * 0.5:
            out.append(SlotRead(None, None, ((x0 + x1) / 2, (y0 + y1) / 2), 1.0))
            continue
        bx0, by0, bx1, by1 = bb
        p, conf, dims = shape_from_mask(m[by0:by1, bx0:bx1], tc)
        box = (x0 + bx0, y0 + by0, x0 + bx1, y0 + by1)
        out.append(SlotRead(p, box, ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2), conf, dims))
    return out


def tray_bboxes(img: np.ndarray, cal: Calibration, thresh: float = 45.0) -> list:
    res = []
    for (x0, y0, x1, y1) in cal.slot_boxes(img.shape):
        m = _slot_mask(img[y0:y1, x0:x1], thresh)
        bb = _mask_bbox(m, 3)
        if bb:
            res.append((bb[2] - bb[0], bb[3] - bb[1]))
    return res


def estimate_tray_ratio(dims: list, board_cell: float, lo: float = 0.3, hi: float = 0.85) -> float:
    """Pick the tray-cell / board-cell ratio that makes piece bboxes integer multiples."""
    best_r, best_e = 0.5, float("inf")
    for r in np.arange(lo, hi, 0.0025):
        tc = r * board_cell
        e = 0.0
        for d in [v for wh in dims for v in wh]:
            k = d / tc
            kr = min(5, max(1, round(k)))
            e += (k - kr) ** 2 + (4.0 if k > 5.5 else 0.0)
        e += 0.02 * (r - 0.55) ** 2
        if e < best_e:
            best_r, best_e = float(r), e
    return best_r


# ----------------------------------------------------------------------------
# Score digits
# ----------------------------------------------------------------------------

def _resize_bin(g: np.ndarray, w: int = GLYPH_W, h: int = GLYPH_H) -> np.ndarray:
    from PIL import Image
    im = Image.fromarray((g * 255).astype(np.uint8)).resize((w, h), Image.BILINEAR)
    return (np.asarray(im) > 127)


def segment_glyphs(sub: np.ndarray, min_rel_h: float = 0.55) -> list:
    """Split white-ish text into glyph masks (left to right)."""
    if sub.size == 0:
        return []
    mn = sub.min(axis=-1).astype(np.float32)
    top = np.percentile(mn, 99.5)
    if top < 120:
        return []
    ink = mn > max(120, 0.72 * top)
    colsum = ink.sum(axis=0)
    runs, start = [], None
    for x, v in enumerate(colsum):
        if v > 0 and start is None:
            start = x
        elif v == 0 and start is not None:
            runs.append((start, x))
            start = None
    if start is not None:
        runs.append((start, len(colsum)))
    glyphs = []
    for a, b in runs:
        g = ink[:, a:b]
        rows = np.nonzero(g.any(axis=1))[0]
        if len(rows) == 0:
            continue
        glyphs.append((a, b, rows[0], rows[-1] + 1, g[rows[0]:rows[-1] + 1]))
    if not glyphs:
        return []
    hmax = max(g[3] - g[2] for g in glyphs)
    return [g[4] for g in glyphs if (g[3] - g[2]) >= min_rel_h * hmax and (g[1] - g[0]) >= 1]


class DigitReader:
    def __init__(self, tdir: Path):
        self.dir = Path(tdir) / "digits"
        self.templates: dict = {}
        self.reload()

    def reload(self):
        from PIL import Image
        self.templates = {}
        if self.dir.exists():
            for p in sorted(self.dir.glob("*.png")):
                d = p.stem.split("_")[0]
                if d.isdigit() and len(d) == 1:
                    self.templates.setdefault(d, []).append(np.asarray(Image.open(p).convert("L")) > 127)

    def known_digits(self) -> list:
        return sorted(self.templates)

    def learn(self, sub: np.ndarray, value: int) -> int:
        from PIL import Image
        glyphs = segment_glyphs(sub)
        s = str(int(value))
        if len(glyphs) != len(s):
            raise ValueError(f"found {len(glyphs)} glyphs but value has {len(s)} digits")
        self.dir.mkdir(parents=True, exist_ok=True)
        n = 0
        for ch, g in zip(s, glyphs):
            k = len(list(self.dir.glob(f"{ch}_*.png")))
            if k >= 6:
                continue
            Image.fromarray((_resize_bin(g) * 255).astype(np.uint8)).save(self.dir / f"{ch}_{k}.png")
            n += 1
        self.reload()
        return n

    def read(self, sub: np.ndarray) -> tuple:
        """Returns (value or None, confidence 0..1, n_glyphs)."""
        glyphs = segment_glyphs(sub)
        if not glyphs or not self.templates:
            return None, 0.0, len(glyphs)
        digits, confs = [], []
        for g in glyphs:
            gb = _resize_bin(g)
            scores = []
            for d, ts in self.templates.items():
                s = max(float((gb & t).sum()) / max(1.0, float((gb | t).sum())) for t in ts)
                scores.append((s, d))
            scores.sort(reverse=True)
            best = scores[0]
            second = scores[1][0] if len(scores) > 1 else 0.0
            digits.append(best[1])
            confs.append(min(best[0], 1.0 - 0.5 * max(0.0, second - best[0] + 0.15)))
        return int("".join(digits)), float(min(confs)), len(glyphs)


def read_number(img: np.ndarray, cal: Calibration, reader: DigitReader, region: str = "score") -> tuple:
    box = getattr(cal, region)
    if not box:
        return None, 0.0, 0
    return reader.read(crop(img, cal.px_box(box, img.shape)))


# ----------------------------------------------------------------------------
# Screen state, settle, banners, buttons
# ----------------------------------------------------------------------------

@dataclass
class ScreenRead:
    kind: str  # "game" | "unknown"
    board: Optional[BoardRead]
    slots: list
    score: Optional[int]
    score_conf: float
    best: Optional[int] = None

    @property
    def pieces(self) -> list:
        return [s.piece for s in self.slots]


def read_screen(img: np.ndarray, cal: Calibration, reader: Optional[DigitReader] = None,
                min_board_conf: float = 0.88) -> ScreenRead:
    br = parse_board(img, cal)
    if br.confidence < min_board_conf:
        return ScreenRead("unknown", br, [], None, 0.0)
    slots = parse_tray(img, cal)
    score, conf, best = None, 0.0, None
    if reader is not None:
        score, conf, _ = read_number(img, cal, reader, "score")
        if cal.best:
            best, _, _ = read_number(img, cal, reader, "best")
    return ScreenRead("game", br, slots, score, conf, best)


def _gray_small(img: np.ndarray, box, step: int = 4) -> np.ndarray:
    return crop(img, box)[::step, ::step].astype(np.float32).mean(axis=-1)


def frame_diff(a: np.ndarray, b: np.ndarray, cal: Calibration) -> float:
    """Mean abs grey difference over board + tray + score regions."""
    tot = 0.0
    for box in (cal.board, cal.tray, cal.score):
        pa, pb = _gray_small(a, cal.px_box(box, a.shape)), _gray_small(b, cal.px_box(box, b.shape))
        if pa.shape == pb.shape and pa.size:
            tot = max(tot, float(np.abs(pa - pb).mean()))
    return tot


def wait_settle(capture: Callable, cal: Calibration, timeout: float = 4.0, interval: float = 0.15,
                eps: float = 1.5, min_stable_s: float = 0.4, clock=None) -> tuple:
    """Capture until the screen has been unchanged for at least `min_stable_s`
    (banners and count-ups can look static for a couple of frames).
    Returns (img, frames, settled). `clock` provides .time() and .sleep()."""
    import time as _t
    clock = clock or _t
    t0 = clock.time()
    frames = [capture()]
    stable_since = None
    while clock.time() - t0 < timeout:
        clock.sleep(interval)
        f = capture()
        frames.append(f)
        if frame_diff(frames[-2], f, cal) < eps:
            if stable_since is None:
                stable_since = clock.time() - interval
            if clock.time() - stable_since >= min_stable_s:
                return f, frames, True
        else:
            stable_since = None
    return frames[-1], frames, False


def banner_region(cal: Calibration, shape) -> tuple:
    bx0, by0, bx1, by1 = cal.board_px(shape)
    side = by1 - by0
    return (bx0, int(by0 - 0.15 * side), bx1, int(by0 + 0.75 * side))


def detect_banner(frames: list, settled: np.ndarray, cal: Calibration, thresh: float = 18.0) -> tuple:
    """Returns (detected, crop_of_strongest_frame or None, max_diff)."""
    box = banner_region(cal, settled.shape)
    ref = _gray_small(settled, box)
    best, best_d = None, 0.0
    for f in frames[:-1]:
        g = _gray_small(f, box)
        if g.shape != ref.shape:
            continue
        # banners are text: look at the strongest horizontal band, not the whole region
        rowdiff = np.abs(g - ref).mean(axis=1)
        k = max(1, len(rowdiff) // 8)
        band = np.convolve(rowdiff, np.ones(k) / k, mode="valid").max() if len(rowdiff) >= k else 0
        if band > best_d:
            best, best_d = f, float(band)
    if best is not None and best_d > thresh:
        return True, crop(best, box), best_d
    return False, None, best_d


def classify_banner(crop_img: np.ndarray, tdir: Path) -> Optional[str]:
    """Match a banner crop against labelled templates in templates/banners/<label>_*.png."""
    from PIL import Image
    d = Path(tdir) / "banners"
    if crop_img is None or not d.exists():
        return None
    g = np.asarray(Image.fromarray(crop_img).convert("L").resize((96, 48))).astype(np.float32)
    g = (g - g.mean()) / (g.std() + 1e-6)
    best, best_s = None, 0.6
    for p in d.glob("*.png"):
        t = np.asarray(Image.open(p).convert("L").resize((96, 48))).astype(np.float32)
        t = (t - t.mean()) / (t.std() + 1e-6)
        s = float((g * t).mean())
        if s > best_s:
            best, best_s = p.stem.split("_")[0], s
    return best


def _ncc(a: np.ndarray, b: np.ndarray) -> float:
    a = a.astype(np.float32)
    b = b.astype(np.float32)
    a = (a - a.mean()) / (a.std() + 1e-6)
    b = (b - b.mean()) / (b.std() + 1e-6)
    return float((a * b).mean())


def match_button(img: np.ndarray, cal: Calibration, name: str, min_score: float = 0.85) -> Optional[tuple]:
    """If the registered button template matches at its stored place, return its centre px.
    Only template-verified buttons are ever tapped (never ads/purchases)."""
    from PIL import Image
    info = cal.buttons.get(name)
    if not info:
        return None
    tp = cal.tdir / info["template"]
    if not tp.exists():
        return None
    box = cal.px_box(info["box"], img.shape)
    sub = crop(img, box)
    if sub.size == 0:
        return None
    t = np.asarray(Image.open(tp).convert("L").resize((64, 32))).astype(np.float32)
    s = np.asarray(Image.fromarray(sub).convert("L").resize((64, 32))).astype(np.float32)
    score = _ncc(s, t)
    # also require similar mean colour (guards against a different button in the same place)
    if score >= min_score:
        return ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0, score)
    return None


def add_button(img: np.ndarray, cal: Calibration, name: str, box_px: tuple) -> str:
    from PIL import Image
    h, w = img.shape[:2]
    x0, y0, x1, y1 = box_px
    d = cal.tdir / "buttons"
    d.mkdir(parents=True, exist_ok=True)
    rel = f"buttons/{name}.png"
    Image.fromarray(crop(img, box_px)).save(cal.tdir / rel)
    cal.buttons[name] = {"box": [x0 / w, y0 / h, x1 / w, y1 / h], "template": rel}
    return rel


# ----------------------------------------------------------------------------
# Calibration helpers
# ----------------------------------------------------------------------------

def auto_calibrate(img: np.ndarray, board_px: Optional[tuple] = None, tray_px: Optional[tuple] = None,
                   score_px: Optional[tuple] = None, best_px: Optional[tuple] = None,
                   base: Optional[Calibration] = None) -> Calibration:
    h, w = img.shape[:2]
    cal = base or Calibration()
    cal.screen = [w, h]
    if board_px is None:
        board_px, ref = detect_board(img)
    else:
        ref = None
    x0, y0, x1, y1 = board_px
    side = x1 - x0
    cal.board = [x0 / w, y0 / h, x1 / w, y1 / h]
    cols = cell_colors(img, cal)
    flat = cols.reshape(-1, 3)
    # empty colour: the most common cell colour cluster (calibrate on a near-empty board)
    med = np.median(flat, axis=0) if ref is None else np.asarray(ref)
    close = flat[_dist(flat, med) < 40]
    cal.empty_rgb = [float(v) for v in (np.median(close, axis=0) if len(close) else med)]
    if tray_px is None:
        tray_px = (max(0, int(x0 - 0.03 * side)), int(y1 + 0.05 * side),
                   min(w, int(x1 + 0.03 * side)), min(h, int(y1 + 0.45 * side)))
    cal.tray = [tray_px[0] / w, tray_px[1] / h, tray_px[2] / w, tray_px[3] / h]
    if score_px is None:
        score_px = (int(w * 0.2), max(0, int(y0 - 0.40 * side)), int(w * 0.8), int(y0 - 0.04 * side))
    cal.score = [score_px[0] / w, score_px[1] / h, score_px[2] / w, score_px[3] / h]
    if best_px is not None:
        cal.best = [best_px[0] / w, best_px[1] / h, best_px[2] / w, best_px[3] / h]
    dims = tray_bboxes(img, cal)
    if len(dims) >= 2:
        cal.tray_cell_ratio = estimate_tray_ratio(dims, side / 8.0)
    return cal


def draw_overlay(img: np.ndarray, cal: Calibration, sr: Optional[ScreenRead] = None) -> np.ndarray:
    out = img.copy()

    def rect(box, color, t=3):
        x0, y0, x1, y1 = [int(v) for v in box]
        out[max(0, y0):y0 + t, x0:x1] = color
        out[max(0, y1 - t):y1, x0:x1] = color
        out[y0:y1, max(0, x0):x0 + t] = color
        out[y0:y1, max(0, x1 - t):x1] = color

    bx0, by0, bx1, by1 = cal.board_px(img.shape)
    cw, ch = (bx1 - bx0) / 8, (by1 - by0) / 8
    for i in range(9):
        out[int(by0 + i * ch):int(by0 + i * ch) + 2, bx0:bx1] = (255, 0, 255)
        out[by0:by1, int(bx0 + i * cw):int(bx0 + i * cw) + 2] = (255, 0, 255)
    for sb in cal.slot_boxes(img.shape):
        rect(sb, (0, 255, 0))
    rect(cal.px_box(cal.score, img.shape), (255, 255, 0))
    if cal.best:
        rect(cal.px_box(cal.best, img.shape), (255, 128, 0))
    if sr is not None and sr.board is not None:
        for r in range(8):
            for c in range(8):
                if sr.board.board >> (r * 8 + c) & 1:
                    cx, cy = cal.cell_center(r, c, img.shape)
                    out[int(cy) - 6:int(cy) + 6, int(cx) - 6:int(cx) + 6] = (255, 0, 0)
        for s in sr.slots:
            if s.bbox:
                rect(s.bbox, (255, 0, 0), 2)
    for name, info in cal.buttons.items():
        rect(cal.px_box(info["box"], img.shape), (0, 255, 255))
    return out


def load_image(path) -> np.ndarray:
    from PIL import Image
    return np.asarray(Image.open(path).convert("RGB"))


def save_image(img: np.ndarray, path, quality: int = 90):
    from PIL import Image
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    im = Image.fromarray(img)
    if p.suffix.lower() in (".jpg", ".jpeg"):
        im.save(p, quality=quality)
    else:
        im.save(p)


def describe(sr: ScreenRead) -> str:
    lines = [f"screen: {sr.kind}"]
    if sr.board is not None:
        lines.append(f"board confidence: {sr.board.confidence:.2f}, ambiguous cells: {sr.board.ambiguous}")
        lines.append(engine.render(sr.board.board))
    for i, s in enumerate(sr.slots):
        lines.append(f"slot {i}: {s.piece.id if s.piece else '-'} "
                     f"({s.piece.art() if s.piece else ''}) conf={s.confidence:.2f} bbox={s.bbox}")
    lines.append(f"score: {sr.score} (conf {sr.score_conf:.2f}); best: {sr.best}")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def _box(v):
    return tuple(int(x) for x in v) if v else None


def main(argv=None):
    try:
        sys.stdout.reconfigure(errors="replace")  # Windows consoles without UTF-8
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(prog="python -m bot.vision", description="Calibration and screen parsing")
    ap.add_argument("--cal", default=str(DEFAULT_CAL))
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("calibrate")
    c.add_argument("image")
    c.add_argument("--board", nargs=4, type=int)
    c.add_argument("--tray", nargs=4, type=int)
    c.add_argument("--score", nargs=4, type=int)
    c.add_argument("--best", nargs=4, type=int)
    c.add_argument("--score-value", type=int)
    c.add_argument("--package")
    c.add_argument("--overlay", default=str(BOT_DIR / "calibration_overlay.png"))
    c.add_argument("--fresh", action="store_true", help="ignore the old calibration")
    l = sub.add_parser("label-score")
    l.add_argument("image")
    l.add_argument("value", type=int)
    l.add_argument("--region", default="score")
    t = sub.add_parser("tray-scale")
    t.add_argument("image")
    b = sub.add_parser("add-button")
    b.add_argument("image")
    b.add_argument("--name", required=True, choices=BUTTON_KINDS)
    b.add_argument("--box", nargs=4, type=int, required=True)
    p = sub.add_parser("parse")
    p.add_argument("image")
    p.add_argument("--overlay")
    o = sub.add_parser("set-offset")
    o.add_argument("dx", type=float)
    o.add_argument("dy", type=float)
    a = ap.parse_args(argv)

    calp = Path(a.cal)
    if a.cmd == "calibrate":
        img = load_image(a.image)
        base = None if (a.fresh or not calp.exists()) else Calibration.load(calp)
        cal = auto_calibrate(img, _box(a.board), _box(a.tray), _box(a.score), _box(a.best), base)
        if a.package:
            cal.game_package = a.package
        cal.save(calp)
        reader = DigitReader(cal.tdir)
        if a.score_value is not None:
            try:
                n = reader.learn(crop(img, cal.px_box(cal.score, img.shape)), a.score_value)
                print(f"{n} digit templates saved; known digits: {reader.known_digits()}")
            except ValueError as ex:
                print(f"WARNING: could not learn score template: {ex}")
        sr = read_screen(img, cal, reader)
        save_image(draw_overlay(img, cal, sr), a.overlay)
        print(f"calibration saved: {calp}\noverlay image: {a.overlay}")
        print(describe(sr))
        return 0
    cal = Calibration.load(calp)
    if a.cmd == "label-score":
        img = load_image(a.image)
        reader = DigitReader(cal.tdir)
        n = reader.learn(crop(img, cal.px_box(getattr(cal, a.region), img.shape)), a.value)
        print(f"{n} new templates; known digits: {reader.known_digits()}")
        missing = sorted(set("0123456789") - set(reader.known_digits()))
        if missing:
            print(f"missing digits: {missing}")
    elif a.cmd == "tray-scale":
        img = load_image(a.image)
        dims = tray_bboxes(img, cal)
        cal.tray_cell_ratio = estimate_tray_ratio(dims, cal.cell_px(img.shape)[0])
        cal.save(calp)
        print(f"tray cell ratio = {cal.tray_cell_ratio:.4f} (bbox: {dims})")
        print(describe(read_screen(img, cal, DigitReader(cal.tdir))))
    elif a.cmd == "add-button":
        img = load_image(a.image)
        rel = add_button(img, cal, a.name, tuple(a.box))
        cal.save(calp)
        print(f"button '{a.name}' saved -> {rel}")
    elif a.cmd == "parse":
        img = load_image(a.image)
        sr = read_screen(img, cal, DigitReader(cal.tdir))
        print(describe(sr))
        for name in cal.buttons:
            print(f"button {name}: {match_button(img, cal, name)}")
        if a.overlay:
            save_image(draw_overlay(img, cal, sr), a.overlay)
    elif a.cmd == "set-offset":
        cal.finger_offset_cells = [a.dx, a.dy]
        cal.save(calp)
        print(f"finger offset = {cal.finger_offset_cells} cells")
    return 0


if __name__ == "__main__":
    sys.exit(main())
