"""JSONL session logger (schema from research/03_android_plan.md section 5, extended).

Record types (one JSON object per line, flushed + fsynced per record):
  session  run metadata (device, resolution, app version, config, calibration)
  phase    a scheduler block started (phase name, kind, label, think range)
  game     game boundary: kind=start|end, game_id, final_score, best_shown
  hand     a new 3-piece hand: pieces (slot, shape_id, cells), board, score, fill
  move     one placement (board before/after as 16-hex bitboards, lines, score delta ...)
  event    ad_interrupt, game_over, revive_declined, placement_mismatch, drop_failed,
           offset_corrected, ocr_uncertain, stuck, disconnect, reconnect, thermal_pause,
           brightness, error ...
  health   battery level / temperature, capture latency, reconnect count
  audit    raw screenshot saved for parser audit (path + what the parser read)

Files: <out>/<session>/<session>_partNN.jsonl, rotated every `rotate_minutes`.
Images: <out>/<session>/img/...
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

from . import engine

DEFAULT_LOG_DIR = Path(__file__).resolve().parent.parent / "research" / "logs"

MOVE_FIELDS = ("hand_id", "move_idx", "slot", "shape_id", "anchor", "board_before", "board_after",
               "lines_cleared", "n_lines", "cells_placed", "score_before", "score_after", "score_delta")


def _default(o):
    try:
        import numpy as np
        if isinstance(o, np.integer):
            return int(o)
        if isinstance(o, np.floating):
            return float(o)
        if isinstance(o, np.ndarray):
            return o.tolist()
    except ImportError:  # pragma: no cover
        pass
    if isinstance(o, (set, tuple)):
        return list(o)
    if isinstance(o, Path):
        return str(o)
    raise TypeError(f"not JSON serialisable: {type(o)}")


class SessionLogger:
    def __init__(self, out_dir=DEFAULT_LOG_DIR, session_id: Optional[str] = None, rotate_minutes: float = 60.0,
                 clock=time, fsync: bool = True, part: int = 0):
        self.clock = clock
        self.session = session_id or time.strftime("s%Y%m%d_%H%M%S")
        self.dir = Path(out_dir) / self.session
        self.dir.mkdir(parents=True, exist_ok=True)
        self.img_dir = self.dir / "img"
        self.rotate_s = rotate_minutes * 60.0
        self.fsync = fsync
        self.part = part
        self.seq = 0
        self._f = None
        self._opened_at = 0.0
        self._open(part)

    # -- files ----------------------------------------------------------------
    def _open(self, part: int):
        if self._f:
            self._f.close()
        self.part = part
        self.path = self.dir / f"{self.session}_part{part:02d}.jsonl"
        self._f = open(self.path, "a", encoding="utf-8")
        self._opened_at = self.clock.time()

    def _maybe_rotate(self):
        if self.rotate_s > 0 and self.clock.time() - self._opened_at >= self.rotate_s:
            self._open(self.part + 1)

    def write(self, rec: dict) -> dict:
        self._maybe_rotate()
        rec = dict(rec)
        rec.setdefault("t", self.clock.time())
        rec["session"] = self.session
        rec["seq"] = self.seq
        self.seq += 1
        self._f.write(json.dumps(rec, default=_default, ensure_ascii=False) + "\n")
        self._f.flush()
        if self.fsync:
            try:
                os.fsync(self._f.fileno())
            except OSError:
                pass
        return rec

    def close(self):
        if self._f:
            self._f.close()
            self._f = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- typed records ----------------------------------------------------------
    def session_meta(self, **kw) -> dict:
        return self.write({"type": "session", **kw})

    def phase(self, **kw) -> dict:
        return self.write({"type": "phase", **kw})

    def game(self, kind: str, game_id: int, **kw) -> dict:
        return self.write({"type": "game", "kind": kind, "game_id": game_id, **kw})

    def hand(self, hand_id: int, pieces: list, board: int, score: Optional[int], **kw) -> dict:
        """pieces: list indexed by slot of engine.Piece or None."""
        ps = [{"slot": i, "shape_id": p.id, "cells": [list(c) for c in p.cells]}
              for i, p in enumerate(pieces) if p is not None]
        return self.write({"type": "hand", "hand_id": hand_id, "pieces": ps, "board": engine.to_hex(board),
                           "fill": round(engine.fill_fraction(board), 4), "score": score, **kw})

    def move(self, **kw) -> dict:
        missing = [k for k in MOVE_FIELDS if k not in kw]
        if missing:
            raise ValueError(f"move record missing fields: {missing}")
        for k in ("board_before", "board_after", "predicted_board_after"):
            if isinstance(kw.get(k), int):
                kw[k] = engine.to_hex(kw[k])
        return self.write({"type": "move", **kw})

    def event(self, kind: str, detail: str = "", **kw) -> dict:
        return self.write({"type": "event", "kind": kind, "detail": detail, **kw})

    def health(self, **kw) -> dict:
        return self.write({"type": "health", **kw})

    def audit(self, **kw) -> dict:
        return self.write({"type": "audit", **kw})

    # -- images -------------------------------------------------------------------
    def save_image(self, img, name: str, quality: int = 90, box: Optional[tuple] = None) -> str:
        """Save (optionally cropped) image; returns the path relative to the session dir."""
        from PIL import Image
        if box is not None:
            x0, y0, x1, y1 = box
            img = img[max(0, y0):y1, max(0, x0):x1]
        p = self.img_dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        im = Image.fromarray(img)
        if p.suffix.lower() in (".jpg", ".jpeg"):
            im.save(p, quality=quality)
        else:
            im.save(p, optimize=False, compress_level=1)
        return str(p.relative_to(self.dir)).replace("\\", "/")


# ----------------------------------------------------------------------------
# Checkpoint (crash-safe resume)
# ----------------------------------------------------------------------------

def save_checkpoint(path, state: dict):
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(state, default=_default, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def load_checkpoint(path) -> Optional[dict]:
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def read_jsonl(paths) -> list:
    """Read records from files/dirs (dirs are searched recursively for *.jsonl).
    Truncated last lines (crash) are skipped."""
    if isinstance(paths, (str, Path)):
        paths = [paths]
    files = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            files += sorted(p.rglob("*.jsonl"))
        elif p.exists():
            files.append(p)
    out = []
    for f in files:
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    out.sort(key=lambda r: (r.get("session", ""), r.get("seq", 0)))
    return out
