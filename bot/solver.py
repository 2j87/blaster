"""Hand solver: beam search over piece orders x placements with a bitboard heuristic.

Usage:
    plan = Solver().solve(board, [piece0, piece1, piece2])      # None for used slots
    plan.steps[0] -> Step(slot, piece_id, row, col, ...)

An `Objective` (probe / regime spec) can bias the search for experiments:
scoring probes (forbid clears for N moves, want exactly k lines), target board
fullness bands, "keep score low" or "end the game early".
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional, Sequence

from .engine import (FULL, NOT_COL0, NOT_COL7, PIECES, Piece, ROW_MASKS, COL_MASKS,
                     clear_lines, fit_anchors, iter_bits)

# Pieces used for the "room" / coverage features (harder pieces weigh more).
_COVER = [(PIECES[i], w) for i, w in [
    ("sq3", 4.0), ("h5", 2.5), ("v5", 2.5), ("BL_a", 1.5), ("BL_b", 1.5), ("BL_c", 1.5),
    ("BL_d", 1.5), ("r2x3", 1.5), ("r3x2", 1.5), ("h4", 1.0), ("v4", 1.0), ("sq2", 1.0),
    ("L4_a", 0.5), ("L4_b", 0.5), ("L4_c", 0.5), ("L4_d", 0.5), ("J4_a", 0.5), ("J4_b", 0.5),
    ("J4_c", 0.5), ("J4_d", 0.5), ("T_u", 0.5), ("T_d", 0.5), ("T_l", 0.5), ("T_r", 0.5),
    ("S_h", 0.5), ("S_v", 0.5), ("Z_h", 0.5), ("Z_v", 0.5), ("h3", 0.5), ("v3", 0.5)]]
_COVER_TOTAL = sum(w for _, w in _COVER)


@dataclass
class Weights:
    clear_move: float = 30.0  # per placement that clears >= 1 line
    per_line: float = 18.0  # per line cleared
    multi_bonus: float = 10.0  # * l*(l-1) for simultaneous lines
    combo_chain: float = 12.0  # clear while a combo is alive (since_clear <= 2)
    filled: float = -0.8  # per filled cell at the end
    rough: float = -1.2  # per empty/filled edge transition
    iso: float = -9.0  # per isolated single empty cell
    regions: float = -4.0  # per extra empty region
    small_region: float = -5.0  # per empty region of size 2..3 cells
    cover: float = 40.0  # * weighted fraction of cover pieces that fit
    near_line: float = 1.5  # per row/col with 6-7 filled (setup potential)
    incomplete: float = -5000.0  # per piece that could not be placed


@dataclass
class Objective:
    """Experiment/regime spec. Default = normal play."""
    name: str = "play"
    forbid_clear_steps: int = 0  # first n placements of this plan should not clear
    want_lines: Optional[int] = None  # reward a placement clearing exactly k lines
    want_clear_from_step: Optional[int] = None  # reward any clear at step index >= this
    penalize_other_clears: bool = False  # with want_lines: penalise clears of other sizes
    target_fill: Optional[tuple] = None  # (lo, hi) fraction band for the final board
    avoid_clears: float = 0.0  # penalty per clearing placement (keep score low)
    suicide: bool = False  # try to end the game quickly (still legal moves)
    strength: float = 1.0

    def move_value(self, step: int, n_lines: int) -> float:
        v = 0.0
        s = self.strength
        if n_lines > 0:
            if step < self.forbid_clear_steps:
                v -= 400 * s
            if self.want_lines is not None:
                if n_lines == self.want_lines:
                    v += 300 * s
                elif self.penalize_other_clears:
                    v -= 120 * s
            if self.want_clear_from_step is not None and step >= self.want_clear_from_step:
                v += 200 * s
            v -= self.avoid_clears * s
            if self.suicide:
                v -= 150 * s
        return v

    def board_value(self, board: int) -> float:
        v = 0.0
        if self.target_fill is not None:
            f = board.bit_count() / 64.0
            lo, hi = self.target_fill
            if f < lo:
                v -= (lo - f) * 400 * self.strength
            elif f > hi:
                v -= (f - hi) * 400 * self.strength
        return v

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


PLAY = Objective()


@dataclass(frozen=True)
class Step:
    slot: int
    piece_id: str
    row: int
    col: int
    rows: tuple
    cols: tuple
    board_before: int
    board_after: int

    @property
    def n_lines(self) -> int:
        return len(self.rows) + len(self.cols)


@dataclass
class Plan:
    steps: list
    value: float
    complete: bool
    n_states: int
    elapsed_ms: float
    final_board: int
    features: dict = field(default_factory=dict)


# ----------------------------------------------------------------------------
# Board features (all bitwise)
# ----------------------------------------------------------------------------

def roughness(b: int) -> int:
    return ((b ^ (b >> 1)) & NOT_COL7).bit_count() + ((b ^ (b >> 8)) & (FULL >> 8)).bit_count()


def _neighbours(x: int) -> int:
    return ((x >> 1) & NOT_COL7) | ((x << 1) & NOT_COL0) | (x >> 8) | ((x << 8) & FULL)


def isolated_empty(b: int) -> int:
    e = ~b & FULL
    return (e & ~_neighbours(e)).bit_count()


def empty_regions(b: int) -> list:
    e = ~b & FULL
    sizes = []
    while e:
        reg = e & -e
        while True:
            g = (reg | _neighbours(reg)) & e
            if g == reg:
                break
            reg = g
        e &= ~reg
        sizes.append(reg.bit_count())
    return sizes


def near_lines(b: int) -> int:
    n = 0
    for m in ROW_MASKS:
        k = (b & m).bit_count()
        if 6 <= k <= 7:
            n += 1
    for m in COL_MASKS:
        k = (b & m).bit_count()
        if 6 <= k <= 7:
            n += 1
    return n


def coverage(b: int) -> float:
    return sum(w for p, w in _COVER if fit_anchors(b, p)) / _COVER_TOTAL


def quick_eval(b: int, w: Weights) -> float:
    return w.filled * b.bit_count() + w.rough * roughness(b) + w.iso * isolated_empty(b)


def full_eval(b: int, w: Weights) -> tuple:
    regs = empty_regions(b)
    feats = {
        "filled": b.bit_count(),
        "rough": roughness(b),
        "iso": isolated_empty(b),
        "regions": len(regs),
        "small_regions": sum(1 for s in regs if 2 <= s <= 3),
        "cover": coverage(b),
        "near": near_lines(b),
    }
    v = (w.filled * feats["filled"] + w.rough * feats["rough"] + w.iso * feats["iso"]
         + w.regions * max(0, feats["regions"] - 1) + w.small_region * feats["small_regions"]
         + w.cover * feats["cover"] + w.near_line * feats["near"])
    return v, feats


# ----------------------------------------------------------------------------
# Solver
# ----------------------------------------------------------------------------

class Solver:
    def __init__(self, weights: Optional[Weights] = None, beam: Sequence = (64, 160), final_k: int = 48):
        self.w = weights or Weights()
        self.beam = tuple(beam)
        self.final_k = final_k

    def _move_value(self, step: int, n_lines: int, since_clear: int, obj: Objective) -> float:
        w = self.w
        v = 0.0
        if n_lines and not obj.suicide:
            v += w.clear_move + w.per_line * n_lines + w.multi_bonus * n_lines * (n_lines - 1)
            if since_clear <= 2:
                v += w.combo_chain
        return v + obj.move_value(step, n_lines)

    def solve(self, board: int, pieces: Sequence, objective: Optional[Objective] = None,
              since_clear: int = 99) -> Plan:
        t0 = time.perf_counter()
        obj = objective or PLAY
        slots = tuple(i for i, p in enumerate(pieces) if p is not None)
        if not slots:
            return Plan([], 0.0, True, 0, 0.0, board)
        # state: key -> (acc, board, remaining_slots, path, since_clear)
        frontier = [(0.0, board, slots, (), since_clear)]
        n_states = 0
        best_partial = frontier
        depth = 0
        while frontier and frontier[0][2]:
            children = {}
            for acc, b, rem, path, sc in frontier:
                seen_ids = set()
                for s in rem:
                    p: Piece = pieces[s]
                    if p.id in seen_ids:
                        continue
                    seen_ids.add(p.id)
                    rest = tuple(x for x in rem if x != s)
                    rest_key = tuple(sorted(pieces[x].id for x in rest))
                    for a in iter_bits(fit_anchors(b, p)):
                        nb, rows, cols = clear_lines(b | (p.mask << a))
                        nl = len(rows) + len(cols)
                        nacc = acc + self._move_value(depth, nl, sc, obj)
                        key = (nb, rest_key)
                        old = children.get(key)
                        if old is None or old[0] < nacc:
                            st = Step(s, p.id, a >> 3, a & 7, tuple(rows), tuple(cols), b, nb)
                            children[key] = (nacc, nb, rest, path + (st,), 0 if nl else sc + 1)
            n_states += len(children)
            if not children:
                break
            depth += 1
            frontier = list(children.values())
            if frontier[0][2]:  # not final depth: beam prune
                width = self.beam[min(depth - 1, len(self.beam) - 1)]
                if len(frontier) > width:
                    frontier.sort(key=lambda s: s[0] + quick_eval(s[1], self.w), reverse=True)
                    frontier = frontier[:width]
            best_partial = frontier
        complete = bool(frontier) and not frontier[0][2]
        cands = frontier if complete else best_partial
        if len(cands) > self.final_k:
            cands = sorted(cands, key=lambda s: s[0] + quick_eval(s[1], self.w), reverse=True)[:self.final_k]
        best, best_v, best_f = None, float("-inf"), {}
        for acc, b, rem, path, sc in cands:
            bv, feats = full_eval(b, self.w)
            if obj.suicide:
                bv = -bv
            v = acc + bv + obj.board_value(b) + self.w.incomplete * len(rem)
            if v > best_v:
                best, best_v, best_f = (acc, b, rem, path, sc), v, feats
        steps = list(best[3]) if best else []
        return Plan(steps, best_v, complete and best is not None, n_states,
                    (time.perf_counter() - t0) * 1000, best[1] if best else board, best_f)
