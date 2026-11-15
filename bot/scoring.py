"""Candidate scoring models from the literature review (01_literature.md).

All models share the shape:
    delta = per_cell * cells + [line clear] line_base(l) * combo_multiplier + board_clear_bonus
and differ in the line base, combo multiplier and combo survival ("grace") rule.

Candidate A (snickrscodes): base 10 (l=1) / 10*l*(l-1) (l>=2), multiplier = combo after
    increment, survival via a hand counter (3 + pieces left after a clear, -1 per
    non-clearing placement, reset at 0), +300 full-board clear.
Candidate B (unsupo): base 10*l^2, multiplier = combo after increment, reset on any miss.
Candidate C: base 10*l, multiplier = 1 + combo before increment (= combo after), reset on miss.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, replace
from typing import Union


@dataclass(frozen=True)
class ScoringModel:
    name: str
    per_cell: int = 1
    line_form: str = "A"  # "A": 10 / 10*l*(l-1); "B": u*l^2; "C": u*l; "T": u*l*(l+1)/2
    line_unit: int = 10
    combo_form: str = "after"  # "after": x c'; "none": x1; "half": x(1 + 0.5*(c'-1))
    grace: Union[str, int] = "hand"  # "hand" (A's counter) or int k misses tolerated
    board_clear_bonus: int = 0

    def line_base(self, l: int) -> float:
        u = self.line_unit
        if l <= 0:
            return 0
        if self.line_form == "A":
            return u if l == 1 else u * l * (l - 1)
        if self.line_form == "B":
            return u * l * l
        if self.line_form == "C":
            return u * l
        if self.line_form == "T":
            return u * l * (l + 1) // 2
        raise ValueError(self.line_form)

    def multiplier(self, combo_after: int) -> float:
        if self.combo_form == "after":
            return combo_after
        if self.combo_form == "none":
            return 1
        if self.combo_form == "half":
            return 1 + 0.5 * (combo_after - 1)
        raise ValueError(self.combo_form)

    def describe(self) -> str:
        return (f"{self.name}: cell={self.per_cell} line={self.line_form}/{self.line_unit} "
                f"combo={self.combo_form} grace={self.grace} board={self.board_clear_bonus}")


@dataclass(frozen=True)
class ComboState:
    combo: int = 0
    counter: int = 3  # for grace == "hand"
    misses: int = 0  # consecutive non-clearing placements since the last clear


def step(model: ScoringModel, st: ComboState, cells: int, n_lines: int, pieces_left_after: int,
         board_empty_after: bool = False) -> tuple:
    """Score one placement. Returns (delta, new_state). Deltas are ints when possible."""
    delta = model.per_cell * cells
    if n_lines > 0:
        c = st.combo + 1
        delta += model.line_base(n_lines) * model.multiplier(c)
        if board_empty_after:
            delta += model.board_clear_bonus
        counter = 3 + pieces_left_after
        new = ComboState(c, counter, 0)
    else:
        misses = st.misses + 1
        if model.grace == "hand":
            counter = st.counter - 1
            if counter <= 0:
                new = ComboState(0, 3, misses)
            else:
                new = ComboState(st.combo, counter, misses)
        else:
            k = int(model.grace)
            if misses > k:
                new = ComboState(0, st.counter, misses)
            else:
                new = ComboState(st.combo, st.counter, misses)
    if isinstance(delta, float) and delta.is_integer():
        delta = int(delta)
    return delta, new


CANDIDATE_A = ScoringModel("A", line_form="A", combo_form="after", grace="hand", board_clear_bonus=300)
CANDIDATE_B = ScoringModel("B", line_form="B", combo_form="after", grace=0)
CANDIDATE_C = ScoringModel("C", line_form="C", combo_form="after", grace=0)
CANDIDATES = [CANDIDATE_A, CANDIDATE_B, CANDIDATE_C]


def model_grid() -> list:
    """Parametric family used for fitting (includes A, B and C)."""
    out = list(CANDIDATES)
    for lf, cf, gr, bb, unit in itertools.product(
            ["A", "B", "C", "T"], ["after", "none", "half"], ["hand", 0, 1, 2, 3, 4, 5],
            [0, 300], [10, 20]):
        m = ScoringModel(f"{lf}/{unit}/{cf}/g{gr}/b{bb}", 1, lf, unit, cf, gr, bb)
        out.append(m)
    # dedupe by parameters (keep named candidates first)
    seen, res = set(), []
    for m in out:
        key = replace(m, name="")
        if key not in seen:
            seen.add(key)
            res.append(m)
    return res


def simulate(model: ScoringModel, moves) -> list:
    """Predicted deltas for a sequence of dicts with keys cells, n_lines, pieces_left,
    board_empty, new_game (bool, resets the combo state before the move)."""
    st = ComboState()
    out = []
    for m in moves:
        if m.get("new_game"):
            st = ComboState()
        d, st = step(model, st, int(m["cells"]), int(m["n_lines"]), int(m.get("pieces_left", 0)),
                     bool(m.get("board_empty", False)))
        out.append(d)
    return out
