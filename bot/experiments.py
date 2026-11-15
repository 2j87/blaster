"""Experiment control: phase scheduler (JSON/YAML config), pacing, scoring-probe controller.

A phase config chains blocks inside one long run (pilot or overnight):

{
  "name": "overnight",
  "loop": true,                      # repeat the phase list until --minutes runs out
  "phases": [
    {"name": "probe",  "kind": "probe-scoring", "minutes": 30, "think_s": [0.5, 1.0]},
    {"name": "abba",   "kind": "abba", "minutes": 80, "block_minutes": 10,
       "a": {"label": "fast", "think_s": [0.2, 0.6]}, "b": {"label": "slow", "think_s": [8, 10]}},
    {"name": "early",  "kind": "regime", "regime": "end_early", "minutes": 20},
    {"name": "low",    "kind": "regime", "regime": "low_score", "minutes": 20},
    {"name": "pb",     "kind": "regime", "regime": "push_pb", "minutes": 40},
    {"name": "fill",   "kind": "regime", "regime": "fill_band", "fill": [0.35, 0.55], "minutes": 30},
    {"name": "play",   "kind": "play", "minutes": 60}
  ]
}

kinds: play | probe-scoring | mixed | regime | abba. Optional per-phase keys:
think_s [lo, hi] (seconds, uniform per move), block_minutes, seed_order (abba: random
start), max_games, label.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .solver import Objective, PLAY

THINK_FAST = (0.2, 0.6)
THINK_SLOW = (8.0, 10.0)
THINK_DEFAULT = (0.5, 1.2)


@dataclass
class Block:
    phase: str
    kind: str
    start_min: float
    end_min: float
    label: str
    think_s: tuple
    params: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"phase": self.phase, "kind": self.kind, "start_min": round(self.start_min, 3),
                "end_min": round(self.end_min, 3), "label": self.label, "think_s": list(self.think_s),
                "params": self.params}


def load_config(path) -> dict:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml  # optional dependency
        except ImportError as ex:  # pragma: no cover
            raise RuntimeError("PyYAML not installed; use a .json phase file") from ex
        return yaml.safe_load(text)
    return json.loads(text)


def preset_config(mode: str = "play", speed: str = "fast", block_minutes: float = 10.0,
                  minutes: float = 60.0) -> dict:
    """Config equivalent to the simple CLI flags --mode/--speed."""
    think = {"fast": THINK_FAST, "slow": THINK_SLOW}.get(speed, THINK_DEFAULT)
    if speed == "abba":
        return {"name": f"{mode}-abba", "loop": True, "phases": [
            {"name": f"{mode}-abba", "kind": "abba", "inner": mode, "minutes": minutes,
             "block_minutes": block_minutes, "seed_order": True,
             "a": {"label": "fast", "think_s": list(THINK_FAST)},
             "b": {"label": "slow", "think_s": list(THINK_SLOW)}}]}
    return {"name": f"{mode}-{speed}", "loop": True,
            "phases": [{"name": mode, "kind": mode, "minutes": minutes, "think_s": list(think),
                        "label": speed}]}


def expand(config: dict, total_minutes: float, seed: int = 0) -> list:
    """Flatten a config into a timeline of Blocks covering total_minutes."""
    rng = random.Random(seed)
    phases = config.get("phases") or []
    if not phases:
        raise ValueError("config has no phases")
    blocks: list = []
    t = 0.0
    loop = bool(config.get("loop", True))
    guard = 0
    while t < total_minutes - 1e-9 and guard < 10000:
        guard += 1
        for ph in phases:
            if t >= total_minutes - 1e-9:
                break
            dur = float(ph.get("minutes", 10))
            end = min(t + dur, total_minutes)
            kind = ph.get("kind", "play")
            name = ph.get("name", kind)
            if kind == "abba":
                bm = float(ph.get("block_minutes", 10))
                a, b = ph.get("a", {}), ph.get("b", {})
                pair = [a, b]
                if ph.get("seed_order", True) and rng.random() < 0.5:
                    pair = [b, a]
                order = [pair[0], pair[1], pair[1], pair[0]]
                inner = ph.get("inner", "play")
                i, s = 0, t
                while s < end - 1e-9:
                    spec = order[i % 4]
                    e = min(s + bm, end)
                    blocks.append(Block(name, inner, s, e, spec.get("label", "abba"),
                                        tuple(spec.get("think_s", THINK_DEFAULT)),
                                        {k: v for k, v in ph.items() if k not in ("a", "b")}))
                    s, i = e, i + 1
            else:
                blocks.append(Block(name, kind, t, end, ph.get("label", ph.get("regime", kind)),
                                    tuple(ph.get("think_s", THINK_DEFAULT)), dict(ph)))
            t = end
        if not loop:
            break
    return blocks


class PhaseScheduler:
    def __init__(self, config: dict, total_minutes: float, seed: int = 0):
        self.config = config
        self.total_minutes = total_minutes
        self.blocks = expand(config, total_minutes, seed)
        self.rng = random.Random(seed + 1)

    def block_at(self, elapsed_min: float) -> Optional[Block]:
        for b in self.blocks:
            if b.start_min <= elapsed_min < b.end_min:
                return b
        return None

    def block_index(self, elapsed_min: float) -> int:
        for i, b in enumerate(self.blocks):
            if b.start_min <= elapsed_min < b.end_min:
                return i
        return -1

    def think_time(self, block: Block) -> float:
        lo, hi = block.think_s
        return self.rng.uniform(lo, hi) if hi > lo else float(lo)


# ----------------------------------------------------------------------------
# Regimes and scoring probes
# ----------------------------------------------------------------------------

def regime_objective(block: Block, ctx: dict) -> Objective:
    reg = block.params.get("regime", "play")
    if reg == "end_early":
        return Objective(name="end_early", suicide=True)
    if reg == "low_score":
        return Objective(name="low_score", avoid_clears=40.0)
    if reg == "fill_band":
        lo, hi = block.params.get("fill", [0.35, 0.55])
        return Objective(name="fill_band", target_fill=(float(lo), float(hi)))
    if reg == "push_pb":
        return Objective(name="push_pb")
    return Objective(name=reg)


@dataclass
class ProbeTargets:
    gaps: tuple = (0, 1, 2, 3, 4, 5)  # non-clearing placements between two clears
    gap_n: int = 8
    multi: tuple = (2, 3, 4)
    multi_n: int = 10
    max_fill: float = 0.6  # above this fullness probes yield to safe play


class ProbeController:
    """Chooses Objectives that steer play into under-sampled scoring-experiment cells."""

    def __init__(self, targets: Optional[ProbeTargets] = None):
        self.t = targets or ProbeTargets()
        self.gap_counts = {k: 0 for k in self.t.gaps}
        self.multi_counts = {l: 0 for l in self.t.multi}
        self.single = 0
        self.baseline = 0

    def state(self) -> dict:
        return {"gap_counts": self.gap_counts, "multi_counts": self.multi_counts,
                "single": self.single, "baseline": self.baseline}

    def load_state(self, st: dict):
        if not st:
            return
        self.gap_counts.update({int(k): v for k, v in st.get("gap_counts", {}).items()})
        self.multi_counts.update({int(k): v for k, v in st.get("multi_counts", {}).items()})
        self.single = st.get("single", 0)
        self.baseline = st.get("baseline", 0)

    def done(self) -> bool:
        return (all(v >= self.t.gap_n for v in self.gap_counts.values())
                and all(v >= self.t.multi_n for v in self.multi_counts.values()))

    def next_objective(self, since_clear: int, fill: float) -> Objective:
        if fill > self.t.max_fill:
            return Objective(name="probe_safety")
        # gap experiments: a clear happened recently
        if since_clear <= max(self.t.gaps):
            open_k = [k for k in self.t.gaps if k >= since_clear and self.gap_counts[k] < self.t.gap_n]
            if open_k:
                k = min(open_k, key=lambda k: (self.gap_counts[k] / self.t.gap_n, k))
                d = k - since_clear
                return Objective(name=f"probe_gap{k}", forbid_clear_steps=d, want_clear_from_step=d,
                                 want_lines=1)
        open_l = [l for l in self.t.multi if self.multi_counts[l] < self.t.multi_n]
        if open_l and fill < 0.5:
            l = min(open_l, key=lambda l: (self.multi_counts[l] / self.t.multi_n, l))
            return Objective(name=f"probe_multi{l}", want_lines=l, penalize_other_clears=True)
        return Objective(name="probe_any", want_clear_from_step=0)

    def record(self, n_lines: int, since_clear_before: int):
        if n_lines == 0:
            self.baseline += 1
            return
        if n_lines == 1:
            self.single += 1
        if n_lines in self.multi_counts:
            self.multi_counts[n_lines] += 1
        if since_clear_before in self.gap_counts:
            self.gap_counts[since_clear_before] += 1


def objective_for(block: Optional[Block], ctx: dict, probes: ProbeController) -> Objective:
    """ctx: since_clear, fill. Returns the Objective for the next move."""
    if block is None:
        return PLAY
    kind = block.kind
    if kind == "probe-scoring":
        return probes.next_objective(ctx.get("since_clear", 99), ctx.get("fill", 0.0))
    if kind == "mixed":
        if not probes.done() and ctx.get("fill", 0.0) < 0.45:
            return probes.next_objective(ctx.get("since_clear", 99), ctx.get("fill", 0.0))
        return PLAY
    if kind == "regime":
        return regime_objective(block, ctx)
    return PLAY
