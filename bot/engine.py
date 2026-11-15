"""Bitboard model of the Block Blast! 8x8 board.

Bit layout: bit index = row * 8 + col, row 0 is the TOP row, col 0 the LEFT column.
A board is a plain Python int in [0, 2**64). Pieces are fixed-orientation
polyominoes (no rotation by the player); every orientation is its own pose.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Optional, Sequence

N = 8
FULL = (1 << 64) - 1
ROW_MASKS = tuple(0xFF << (8 * r) for r in range(N))
COL_MASKS = tuple(sum(1 << (8 * r + c) for r in range(N)) for c in range(N))
NOT_COL0 = FULL & ~COL_MASKS[0]
NOT_COL7 = FULL & ~COL_MASKS[7]
NOT_ROW7 = FULL & ~ROW_MASKS[7]


def bit(r: int, c: int) -> int:
    return 1 << (r * 8 + c)


def iter_bits(x: int) -> Iterator[int]:
    while x:
        low = x & -x
        yield low.bit_length() - 1
        x ^= low


def popcount(x: int) -> int:
    return x.bit_count()


# ----------------------------------------------------------------------------
# Pieces
# ----------------------------------------------------------------------------

@dataclass(frozen=True)
class Piece:
    id: str
    family: str
    cells: tuple  # sorted ((r, c), ...) normalised to min r = min c = 0
    h: int
    w: int
    mask: int  # cells at anchor (0, 0)
    offsets: tuple  # r*8+c per cell
    anchors: int  # bitmask of anchors keeping the piece inside the board
    core: bool = True

    @property
    def n(self) -> int:
        return len(self.cells)

    def mask_at(self, r: int, c: int) -> int:
        return self.mask << (r * 8 + c)

    def grid(self) -> list:
        g = [[0] * self.w for _ in range(self.h)]
        for r, c in self.cells:
            g[r][c] = 1
        return g

    def art(self) -> str:
        return "/".join("".join("#" if v else "." for v in row) for row in self.grid())

    def size_class(self) -> str:
        return size_class(self.n)


def size_class(n: int) -> str:
    if n <= 2:
        return "S"  # 1-2 cells
    if n <= 4:
        return "M"  # 3-4 cells
    return "L"  # 5+ cells (h5/v5/BL/2x3/3x3)


def _normalise(cells: Iterable) -> tuple:
    cells = list(cells)
    if not cells:
        raise ValueError("empty piece")
    mr = min(r for r, _ in cells)
    mc = min(c for _, c in cells)
    return tuple(sorted({(r - mr, c - mc) for r, c in cells}))


def make_piece(pid: str, family: str, cells: Iterable, core: bool = True) -> Piece:
    cells = _normalise(cells)
    h = max(r for r, _ in cells) + 1
    w = max(c for _, c in cells) + 1
    if h > N or w > N:
        raise ValueError("piece larger than board")
    offsets = tuple(r * 8 + c for r, c in cells)
    mask = sum(1 << o for o in offsets)
    anchors = 0
    for r in range(N - h + 1):
        for c in range(N - w + 1):
            anchors |= bit(r, c)
    return Piece(pid, family, cells, h, w, mask, offsets, anchors, core)


def _from_art(art: str) -> list:
    return [(r, c) for r, row in enumerate(art.split("/")) for c, ch in enumerate(row) if ch == "#"]


# (id, family, art, core). Art rows separated by '/', '#' = filled.
_SHAPE_DEFS = [
    ("1x1", "mono", "#", True),
    ("h2", "bar2", "##", True), ("v2", "bar2", "#/#", True),
    ("h3", "bar3", "###", True), ("v3", "bar3", "#/#/#", True),
    ("h4", "bar4", "####", True), ("v4", "bar4", "#/#/#/#", True),
    ("h5", "bar5", "#####", True), ("v5", "bar5", "#/#/#/#/#", True),
    ("sq2", "sq2", "##/##", True),
    ("sq3", "sq3", "###/###/###", True),
    ("r2x3", "rect6", "###/###", True), ("r3x2", "rect6", "##/##/##", True),
    ("L3_a", "L3", "#./##", True), ("L3_b", "L3", "##/#.", True),
    ("L3_c", "L3", "##/.#", True), ("L3_d", "L3", ".#/##", True),
    ("L4_a", "L4", "#./#./##", True), ("L4_b", "L4", "###/#..", True),
    ("L4_c", "L4", "##/.#/.#", True), ("L4_d", "L4", "..#/###", True),
    ("J4_a", "J4", ".#/.#/##", True), ("J4_b", "J4", "#../###", True),
    ("J4_c", "J4", "##/#./#.", True), ("J4_d", "J4", "###/..#", True),
    ("T_u", "T", ".#./###", True), ("T_d", "T", "###/.#.", True),
    ("T_l", "T", ".#/##/.#", True), ("T_r", "T", "#./##/#.", True),
    ("S_h", "S", ".##/##.", True), ("S_v", "S", "#./##/.#", True),
    ("Z_h", "Z", "##./.##", True), ("Z_v", "Z", ".#/##/#.", True),
    ("BL_a", "BL", "#../#../###", True), ("BL_b", "BL", "###/#../#..", True),
    ("BL_c", "BL", "###/..#/..#", True), ("BL_d", "BL", "..#/..#/###", True),
    # Diagonal steps: listed by one community solver, not confirmed in the live game.
    ("D2_a", "diag", "#./.#", False), ("D2_b", "diag", ".#/#.", False),
    ("D3_a", "diag", "#../.#./..#", False), ("D3_b", "diag", "..#/.#./#..", False),
]

PIECES: dict = {}
for _pid, _fam, _art, _core in _SHAPE_DEFS:
    PIECES[_pid] = make_piece(_pid, _fam, _from_art(_art), _core)

PIECE_LIST = list(PIECES.values())
CORE_IDS = [p.id for p in PIECE_LIST if p.core]
ALL_IDS = [p.id for p in PIECE_LIST]
_BY_MASK = {p.mask: p for p in PIECE_LIST}
_DYNAMIC: dict = {}


def get_piece(pid: str) -> Piece:
    if pid in PIECES:
        return PIECES[pid]
    if pid in _DYNAMIC:
        return _DYNAMIC[pid]
    if pid.startswith("X_"):
        m = int(pid[2:], 16)
        cells = [(o // 8, o % 8) for o in iter_bits(m)]
        return piece_from_cells(cells)
    raise KeyError(pid)


def piece_from_cells(cells: Iterable) -> Piece:
    """Canonical piece for a set of cells; unknown shapes get a dynamic id X_<hexmask>."""
    cells = _normalise(cells)
    mask = sum(1 << (r * 8 + c) for r, c in cells)
    p = _BY_MASK.get(mask)
    if p is not None:
        return p
    pid = f"X_{mask:x}"
    if pid not in _DYNAMIC:
        _DYNAMIC[pid] = make_piece(pid, "unknown", cells, core=False)
    return _DYNAMIC[pid]


def piece_from_grid(grid: Sequence[Sequence[int]]) -> Piece:
    return piece_from_cells([(r, c) for r, row in enumerate(grid) for c, v in enumerate(row) if v])


# ----------------------------------------------------------------------------
# Board operations
# ----------------------------------------------------------------------------

def empty_mask(board: int) -> int:
    return ~board & FULL


def fit_anchors(board: int, piece: Piece) -> int:
    """Bitmask of anchor squares (top-left of the piece's bounding box) where it fits."""
    e = ~board & FULL
    f = piece.anchors
    for off in piece.offsets:
        f &= e >> off
        if not f:
            break
    return f


def legal_placements(board: int, piece: Piece) -> list:
    return [(a >> 3, a & 7) for a in iter_bits(fit_anchors(board, piece))]


def can_place(board: int, piece: Piece, r: int, c: int) -> bool:
    if r < 0 or c < 0 or r + piece.h > N or c + piece.w > N:
        return False
    return (board & piece.mask_at(r, c)) == 0


def place(board: int, piece: Piece, r: int, c: int) -> int:
    if not can_place(board, piece, r, c):
        raise ValueError(f"illegal placement of {piece.id} at {(r, c)}")
    return board | piece.mask_at(r, c)


def full_lines(board: int) -> tuple:
    rows = [r for r in range(N) if (board & ROW_MASKS[r]) == ROW_MASKS[r]]
    cols = [c for c in range(N) if (board & COL_MASKS[c]) == COL_MASKS[c]]
    return rows, cols


def clear_lines(board: int) -> tuple:
    """Clear all full rows and columns simultaneously. Returns (board, rows, cols)."""
    rows, cols = full_lines(board)
    if not rows and not cols:
        return board, rows, cols
    m = 0
    for r in rows:
        m |= ROW_MASKS[r]
    for c in cols:
        m |= COL_MASKS[c]
    return board & ~m, rows, cols


@dataclass(frozen=True)
class MoveResult:
    piece_id: str
    anchor: tuple
    board_before: int
    board_placed: int
    board_after: int
    rows: tuple
    cols: tuple
    cells: int

    @property
    def n_lines(self) -> int:
        return len(self.rows) + len(self.cols)

    @property
    def board_cleared(self) -> bool:
        return self.n_lines > 0 and self.board_after == 0


def apply_move(board: int, piece: Piece, r: int, c: int) -> MoveResult:
    placed = place(board, piece, r, c)
    after, rows, cols = clear_lines(placed)
    return MoveResult(piece.id, (r, c), board, placed, after, tuple(rows), tuple(cols), piece.n)


def find_move(before: int, after: int, piece: Piece) -> Optional[MoveResult]:
    """Find the placement of `piece` that turns `before` into `after` (None if none)."""
    for r, c in legal_placements(before, piece):
        mr = apply_move(before, piece, r, c)
        if mr.board_after == after:
            return mr
    return None


def any_fits(board: int, pieces: Iterable) -> bool:
    return any(p is not None and fit_anchors(board, p) for p in pieces)


def is_game_over(board: int, pieces: Iterable) -> bool:
    ps = [p for p in pieces if p is not None]
    return bool(ps) and not any_fits(board, ps)


def hand_solvable(board: int, pieces: Sequence) -> bool:
    """True if some order and placement places every piece of the hand (with clearing)."""
    ps = [p for p in pieces if p is not None]
    if not ps:
        return True
    seen = set()

    def rec(b: int, rem: tuple) -> bool:
        if not rem:
            return True
        key = (b, rem)
        if key in seen:
            return False
        seen.add(key)
        done = set()
        for i, p in enumerate(rem):
            if p.id in done:
                continue
            done.add(p.id)
            rest = rem[:i] + rem[i + 1:]
            for a in iter_bits(fit_anchors(b, p)):
                nb, _, _ = clear_lines(b | (p.mask << a))
                if rec(nb, rest):
                    return True
        return False

    return rec(board, tuple(sorted(ps, key=lambda p: p.id)))


def piece_enables_clear(board: int, piece: Piece) -> bool:
    """True if some placement of the piece immediately completes at least one line."""
    for a in iter_bits(fit_anchors(board, piece)):
        rows, cols = full_lines(board | (piece.mask << a))
        if rows or cols:
            return True
    return False


# ----------------------------------------------------------------------------
# Conversions
# ----------------------------------------------------------------------------

def to_hex(board: int) -> str:
    return f"{board & FULL:016x}"


def from_hex(s: str) -> int:
    return int(s, 16) & FULL


def to_bits_str(board: int) -> str:
    """64 chars '0'/'1', row-major from the top-left cell."""
    return "".join("1" if board >> i & 1 else "0" for i in range(64))


def from_bits_str(s: str) -> int:
    return sum(1 << i for i, ch in enumerate(s) if ch == "1")


def to_grid(board: int) -> list:
    return [[board >> (r * 8 + c) & 1 for c in range(N)] for r in range(N)]


def from_grid(grid: Sequence[Sequence]) -> int:
    b = 0
    for r in range(N):
        for c in range(N):
            if grid[r][c]:
                b |= bit(r, c)
    return b


def from_art(art: str) -> int:
    """Board from 8 lines of '#'/'.' (whitespace stripped, '/' or newline separated)."""
    rows = [ln.strip() for ln in art.replace("/", "\n").strip().splitlines() if ln.strip()]
    if len(rows) != N or any(len(r) != N for r in rows):
        raise ValueError("board art must be 8x8")
    return from_grid([[ch == "#" for ch in row] for row in rows])


def render(board: int) -> str:
    return "\n".join("".join("#" if board >> (r * 8 + c) & 1 else "." for c in range(N)) for r in range(N))


def fill_fraction(board: int) -> float:
    return board.bit_count() / 64.0


# ----------------------------------------------------------------------------
# Simulated game (used by the fake device, tests and baselines)
# ----------------------------------------------------------------------------

class UniformGenerator:
    """Uniform i.i.d. hand generator over a list of piece ids (simulation baseline)."""

    def __init__(self, ids: Sequence = None, seed: Optional[int] = None, weights: Optional[dict] = None):
        self.ids = list(ids or CORE_IDS)
        self.rng = random.Random(seed)
        self.weights = [weights.get(i, 1.0) for i in self.ids] if weights else None

    def hand(self, board: int = 0) -> list:
        if self.weights:
            return [get_piece(i) for i in self.rng.choices(self.ids, weights=self.weights, k=3)]
        return [get_piece(self.rng.choice(self.ids)) for _ in range(3)]


@dataclass
class Game:
    """Classic-mode simulation: 3-piece hands, refill after all three are used."""
    scoring: object = None  # scoring.ScoringModel; imported lazily to avoid a cycle
    generator: object = None
    seed: Optional[int] = None
    board: int = 0
    score: int = 0
    hand: list = field(default_factory=list)
    over: bool = False
    moves: int = 0
    hands_dealt: int = 0

    def __post_init__(self):
        from . import scoring as _sc
        if self.scoring is None:
            self.scoring = _sc.CANDIDATE_A
        if self.generator is None:
            self.generator = UniformGenerator(seed=self.seed)
        self.combo = _sc.ComboState()
        if not self.hand:
            self.deal()

    def deal(self):
        self.hand = list(self.generator.hand(self.board))
        self.hands_dealt += 1
        self.over = is_game_over(self.board, self.hand)

    def play(self, slot: int, r: int, c: int) -> tuple:
        """Place hand[slot] at (r, c). Returns (MoveResult, score_delta)."""
        from . import scoring as _sc
        p = self.hand[slot]
        if p is None:
            raise ValueError("slot empty")
        mr = apply_move(self.board, p, r, c)
        self.hand[slot] = None
        left = sum(1 for x in self.hand if x is not None)
        delta, self.combo = _sc.step(self.scoring, self.combo, mr.cells, mr.n_lines, left, mr.board_after == 0)
        self.board = mr.board_after
        self.score += delta
        self.moves += 1
        if left == 0:
            self.deal()
        else:
            self.over = is_game_over(self.board, self.hand)
        return mr, delta
