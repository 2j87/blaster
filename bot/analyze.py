"""Analysis of session logs: scoring model fit, combo window, piece distribution,
adaptivity tests (fullness / score-vs-PB / speed / phase), lethal-hand and
"helpful hand" rates against random baselines on the same boards, transitions.

    python -m bot.analyze --logs research/logs --out research/analysis

Writes a Turkish Markdown report plus CSV tables.
"""
from __future__ import annotations

import argparse
import math
import random
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional, Sequence

import numpy as np
import pandas as pd

from . import engine, scoring
from .logger import DEFAULT_LOG_DIR, read_jsonl

# ----------------------------------------------------------------------------
# Statistics helpers (scipy optional)
# ----------------------------------------------------------------------------


def _gammq(a: float, x: float) -> float:
    """Regularised upper incomplete gamma Q(a, x) (Numerical Recipes)."""
    if x < 0 or a <= 0:
        return float("nan")
    if x == 0:
        return 1.0
    gln = math.lgamma(a)
    if x < a + 1:
        ap, s, d = a, 1.0 / a, 1.0 / a
        for _ in range(1000):
            ap += 1
            d *= x / ap
            s += d
            if abs(d) < abs(s) * 1e-14:
                break
        return max(0.0, 1.0 - s * math.exp(-x + a * math.log(x) - gln))
    b = x + 1 - a
    c = 1.0 / 1e-300
    d = 1.0 / b
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = 1e-300 if abs(d) < 1e-300 else d
        c = b + an / c
        c = 1e-300 if abs(c) < 1e-300 else c
        d = 1.0 / d
        de = d * c
        h *= de
        if abs(de - 1) < 1e-14:
            break
    return min(1.0, math.exp(-x + a * math.log(x) - gln) * h)


def chi2_sf(x: float, k: int) -> float:
    try:
        from scipy.stats import chi2
        return float(chi2.sf(x, k))
    except ImportError:
        return _gammq(k / 2.0, x / 2.0)


def norm_sf(z: float) -> float:
    return 0.5 * math.erfc(z / math.sqrt(2))


def chi2_gof(counts: Sequence, probs: Optional[Sequence] = None) -> dict:
    obs = np.asarray(counts, float)
    n = obs.sum()
    k = len(obs)
    p = np.full(k, 1.0 / k) if probs is None else np.asarray(probs, float) / np.sum(probs)
    exp = n * p
    stat = float(((obs - exp) ** 2 / np.where(exp > 0, exp, 1)).sum())
    df = k - 1
    return {"chi2": stat, "df": df, "p": chi2_sf(stat, df) if n > 0 and df > 0 else float("nan"),
            "n": int(n), "min_expected": float(exp.min()) if k else 0.0}


def chi2_contingency(table: np.ndarray) -> dict:
    t = np.asarray(table, float)
    t = t[t.sum(axis=1) > 0][:, t.sum(axis=0) > 0] if t.size else t
    if t.ndim != 2 or t.shape[0] < 2 or t.shape[1] < 2:
        return {"chi2": float("nan"), "df": 0, "p": float("nan"), "n": int(t.sum()) if t.size else 0}
    n = t.sum()
    exp = np.outer(t.sum(axis=1), t.sum(axis=0)) / n
    stat = float(((t - exp) ** 2 / exp).sum())
    df = (t.shape[0] - 1) * (t.shape[1] - 1)
    cramer = math.sqrt(stat / (n * (min(t.shape) - 1))) if n > 0 else float("nan")
    return {"chi2": stat, "df": df, "p": chi2_sf(stat, df), "n": int(n), "cramers_v": cramer,
            "min_expected": float(exp.min())}


# ----------------------------------------------------------------------------
# Loading
# ----------------------------------------------------------------------------

def load(paths) -> dict:
    recs = read_jsonl(paths)
    by = defaultdict(list)
    for r in recs:
        by[r.get("type", "?")].append(r)
    moves = pd.DataFrame(by["move"])
    hands = pd.DataFrame(by["hand"])
    if not hands.empty:
        hands["piece_ids"] = hands["pieces"].apply(lambda ps: [p["shape_id"] for p in ps])
        hands["n_pieces"] = hands["piece_ids"].apply(len)
        hands["board_int"] = hands["board"].apply(engine.from_hex)
        if "best_shown" in hands:
            hands["score_vs_pb"] = [
                (s / b) if (s is not None and b and not pd.isna(s) and not pd.isna(b) and b > 0) else np.nan
                for s, b in zip(hands["score"], hands["best_shown"])]
    return {"records": recs, "moves": moves, "hands": hands, "events": pd.DataFrame(by["event"]),
            "games": pd.DataFrame(by["game"]), "health": pd.DataFrame(by["health"]),
            "audit": pd.DataFrame(by["audit"]), "sessions": pd.DataFrame(by["session"])}


# ----------------------------------------------------------------------------
# Scoring
# ----------------------------------------------------------------------------

def _move_seq(moves: pd.DataFrame) -> list:
    if moves.empty:
        return []
    df = moves.sort_values(["session", "seq"])
    out, prev, prev_unc = [], None, False
    for r in df.itertuples(index=False):
        key = (r.session, getattr(r, "game_id", None))
        unc = bool(getattr(r, "uncertain_state", False) or False)
        nl = getattr(r, "n_lines", None)
        known = nl is not None and not (isinstance(nl, float) and math.isnan(nl))
        delta = getattr(r, "score_delta", None)
        valid = known and delta is not None and not (isinstance(delta, float) and math.isnan(delta)) \
            and bool(getattr(r, "verified", True)) and not bool(getattr(r, "uncertain_state", False) or False)
        ba = getattr(r, "board_after", None)
        out.append({"new_game": key != prev or (prev_unc and not unc), "cells": int(r.cells_placed), "n_lines": int(nl) if known else 0,
                    "pieces_left": int(getattr(r, "pieces_left", 0) or 0),
                    "board_empty": bool(getattr(r, "board_cleared", False)) or (known and int(nl) > 0 and ba == "0" * 16),
                    "delta": int(delta) if valid else None, "valid": valid,
                    "since": getattr(r, "moves_since_last_clear", None)})
        prev, prev_unc = key, unc
    return out


def fit_scoring(moves: pd.DataFrame, models: Optional[list] = None) -> pd.DataFrame:
    seq = _move_seq(moves)
    models = models or scoring.model_grid()
    rows = []
    valid_idx = [i for i, m in enumerate(seq) if m["valid"]]
    if not valid_idx:
        return pd.DataFrame(columns=["model", "n", "exact", "mae", "exact_clears", "n_clears"])
    obs = np.array([seq[i]["delta"] for i in valid_idx], float)
    clear_mask = np.array([seq[i]["n_lines"] > 0 for i in valid_idx])
    for m in models:
        pred = np.array(scoring.simulate(m, seq), float)[valid_idx]
        ok = pred == obs
        rows.append({"model": m.name, "n": len(obs), "exact": ok.mean(), "mae": np.abs(pred - obs).mean(),
                     "exact_clears": ok[clear_mask].mean() if clear_mask.any() else np.nan,
                     "n_clears": int(clear_mask.sum()), "desc": m.describe()})
    return pd.DataFrame(rows).sort_values(["exact", "exact_clears", "mae"], ascending=[False, False, True]).reset_index(drop=True)


def placement_table(moves: pd.DataFrame) -> pd.DataFrame:
    """Non-clearing moves: delta by cells placed (expect delta == cells under all candidates)."""
    seq = [m for m in _move_seq(moves) if m["valid"] and m["n_lines"] == 0]
    if not seq:
        return pd.DataFrame()
    df = pd.DataFrame(seq)
    return df.groupby(["cells", "delta"]).size().rename("n").reset_index()


def line_base_table(moves: pd.DataFrame, min_gap: int = 6) -> pd.DataFrame:
    """Clears after a long gap (combo presumably reset): residual delta - cells by n_lines."""
    seq = [m for m in _move_seq(moves) if m["valid"] and m["n_lines"] > 0
           and m["since"] is not None and not pd.isna(m["since"]) and m["since"] >= min_gap]
    if not seq:
        return pd.DataFrame()
    df = pd.DataFrame(seq)
    df["residual"] = df["delta"] - df["cells"]
    return df.groupby(["n_lines", "residual"]).size().rename("n").reset_index()


def combo_table(moves: pd.DataFrame) -> pd.DataFrame:
    """Clears: residual / base(l) by moves since the previous clear (combo window inference).
    The implied multiplier uses base 10 (l=1) / 10*l*(l-1) (candidate A) and 10*l^2 (B)."""
    seq = [m for m in _move_seq(moves) if m["valid"] and m["n_lines"] > 0]
    if not seq:
        return pd.DataFrame()
    df = pd.DataFrame(seq)
    df["residual"] = df["delta"] - df["cells"]
    df["mult_A"] = df.apply(lambda r: r.residual / scoring.CANDIDATE_A.line_base(r.n_lines), axis=1)
    df["mult_B"] = df.apply(lambda r: r.residual / scoring.CANDIDATE_B.line_base(r.n_lines), axis=1)
    df["since"] = df["since"].fillna(99).astype(int).clip(upper=10)
    g = df.groupby(["since", "n_lines"]).agg(n=("residual", "size"), residual_values=("residual", lambda s: sorted(set(s))[:8]),
                                              mult_A_median=("mult_A", "median"), mult_B_median=("mult_B", "median"))
    return g.reset_index()


# ----------------------------------------------------------------------------
# Piece distribution
# ----------------------------------------------------------------------------

def piece_table(hands: pd.DataFrame, ids: Optional[list] = None) -> tuple:
    if hands.empty:
        return pd.DataFrame(), {}
    full = hands[hands["n_pieces"] == 3] if "n_pieces" in hands else hands
    if "partial" in full:
        full = full[~full["partial"].fillna(False).astype(bool)]
    cnt = Counter(pid for ids_ in full["piece_ids"] for pid in ids_)
    ids = ids or engine.CORE_IDS
    unknown = {k: v for k, v in cnt.items() if k not in ids}
    counts = [cnt.get(i, 0) for i in ids]
    total = sum(counts)
    df = pd.DataFrame({"shape_id": ids, "family": [engine.get_piece(i).family for i in ids],
                       "cells": [engine.get_piece(i).n for i in ids], "count": counts})
    df["freq"] = df["count"] / total if total else 0.0
    df["expected_uniform"] = total / len(ids) if ids else 0
    gof = chi2_gof(counts)
    fam = df.groupby("family")["count"].sum()
    fam_sizes = df.groupby("family").size()
    gof_family = chi2_gof(fam.values, fam_sizes.values)  # uniform over poses -> family weight = #poses
    obs_ids = [i for i in ids if cnt.get(i, 0) > 0]
    gof_obs = chi2_gof([cnt[i] for i in obs_ids]) if len(obs_ids) > 1 else {}
    return df.sort_values("count", ascending=False), {"uniform_core": gof, "family_vs_pose_uniform": gof_family,
                                                      "uniform_observed_set": gof_obs, "unknown_shapes": unknown,
                                                      "n_hands": int(len(full)), "n_pieces": int(total)}


def _bin_fill(f: float) -> str:
    return "0-20" if f < 0.2 else "20-35" if f < 0.35 else "35-50" if f < 0.5 else "50+"


def _bin_pb(x) -> str:
    if x is None or pd.isna(x):
        return "pb_none"
    return "<25%" if x < 0.25 else "25-50%" if x < 0.5 else "50-90%" if x < 0.9 else "90-110%" if x < 1.1 else ">110%"


def conditional_tests(hands: pd.DataFrame) -> dict:
    """Contingency tests of size class (S/M/L) and family against conditions."""
    if hands.empty:
        return {}
    h = hands[hands["n_pieces"] == 3].copy()
    h["fill_bin"] = h["fill"].apply(_bin_fill)
    h["pb_bin"] = h["score_vs_pb"].apply(_bin_pb) if "score_vs_pb" in h else "pb_none"
    h["early"] = np.where(h.get("hand_in_game", pd.Series(99, index=h.index)).fillna(99) <= 10, "first10", "later")
    conds = {"fill_bin": "fullness", "pb_bin": "score/PB", "block_label": "speed block", "phase": "phase", "early": "game start"}
    out = {}
    for col, label in conds.items():
        if col not in h or h[col].isna().all():
            continue
        rows = []
        for val, grp in h.groupby(h[col].astype(str)):
            c = Counter(engine.size_class(engine.get_piece(p).n) for ids_ in grp["piece_ids"] for p in ids_)
            rows.append((val, c.get("S", 0), c.get("M", 0), c.get("L", 0)))
        if len(rows) < 2:
            continue
        tab = pd.DataFrame(rows, columns=[col, "S", "M", "L"]).set_index(col)
        tab["n"] = tab.sum(axis=1)
        tab["mean_cells"] = [np.mean([engine.get_piece(p).n for ids_ in grp["piece_ids"] for p in ids_])
                             for _, grp in h.groupby(h[col].astype(str))]
        out[col] = {"label": label, "table": tab, "test": chi2_contingency(tab[["S", "M", "L"]].values)}
    return out


# ----------------------------------------------------------------------------
# Baselines on the same boards
# ----------------------------------------------------------------------------

def hand_features(board: int, ids: Sequence) -> dict:
    ps = [engine.get_piece(i) for i in ids]
    return {"solvable": engine.hand_solvable(board, ps),
            "all_fit": all(engine.fit_anchors(board, p) for p in ps),
            "any_fit": any(engine.fit_anchors(board, p) for p in ps),
            "enables_clear": any(engine.piece_enables_clear(board, p) for p in ps)}


def baseline_comparison(hands: pd.DataFrame, k: int = 40, seed: int = 0, ids: Optional[list] = None,
                        weights: Optional[dict] = None, max_hands: Optional[int] = None) -> dict:
    """Observed rates of solvable / all-fit / enables-clear hands vs i.i.d. random triples
    drawn on the SAME boards (uniform over `ids`, or `weights` = observed marginal)."""
    if hands.empty:
        return {}
    h = hands[hands["n_pieces"] == 3]
    if max_hands and len(h) > max_hands:
        h = h.sample(max_hands, random_state=seed)
    rng = random.Random(seed)
    ids = list(ids or engine.CORE_IDS)
    w = [weights.get(i, 0.0) for i in ids] if weights else None
    cache: dict = {}
    feats = ["solvable", "all_fit", "any_fit", "enables_clear"]
    obs = {f: 0 for f in feats}
    exp = {f: 0.0 for f in feats}
    var = {f: 0.0 for f in feats}
    per_fill = defaultdict(lambda: {"n": 0, "obs_unsolvable": 0, "exp_unsolvable": 0.0})
    for row in h.itertuples(index=False):
        b = row.board_int
        key = (b, tuple(sorted(row.piece_ids)))
        if key not in cache:
            cache[key] = hand_features(b, row.piece_ids)
        fo = cache[key]
        acc = {f: 0 for f in feats}
        for _ in range(k):
            tri = rng.choices(ids, weights=w, k=3) if w else [rng.choice(ids) for _ in range(3)]
            kk = (b, tuple(sorted(tri)))
            if kk not in cache:
                cache[kk] = hand_features(b, tri)
            for f in feats:
                acc[f] += cache[kk][f]
        fb = _bin_fill(engine.fill_fraction(b))
        per_fill[fb]["n"] += 1
        per_fill[fb]["obs_unsolvable"] += int(not fo["solvable"])
        per_fill[fb]["exp_unsolvable"] += 1 - acc["solvable"] / k
        for f in feats:
            p = acc[f] / k
            obs[f] += int(fo[f])
            exp[f] += p
            var[f] += p * (1 - p)
    n = len(h)
    res = {"n_hands": n, "k": k, "features": {}}
    for f in feats:
        z = (obs[f] - exp[f]) / math.sqrt(var[f]) if var[f] > 0 else float("nan")
        res["features"][f] = {"observed": obs[f] / n if n else float("nan"), "baseline": exp[f] / n if n else float("nan"),
                              "z": z, "p_two_sided": 2 * norm_sf(abs(z)) if not math.isnan(z) else float("nan")}
    res["unsolvable_by_fill"] = pd.DataFrame([{"fill_bin": k_, **v} for k_, v in sorted(per_fill.items())])
    return res


# ----------------------------------------------------------------------------
# Within-hand structure and transitions
# ----------------------------------------------------------------------------

def within_hand(hands: pd.DataFrame) -> dict:
    h = hands[hands["n_pieces"] == 3] if not hands.empty else hands
    if h.empty:
        return {}
    cnt = Counter(p for ids_ in h["piece_ids"] for p in ids_)
    tot = sum(cnt.values())
    p = {k: v / tot for k, v in cnt.items()}
    # P(at least one duplicate) under i.i.d. marginal
    s2 = sum(v * v for v in p.values())
    s3 = sum(v ** 3 for v in p.values())
    p_all_distinct = 1 - 3 * s2 + 2 * s3
    dup_obs = float(np.mean([len(set(ids_)) < 3 for ids_ in h["piece_ids"]]))
    # slot position effect: size class by slot
    tab = np.zeros((3, 3))
    for ids_ in h["piece_ids"]:
        for s, pid in enumerate(ids_):
            tab[s, "SML".index(engine.size_class(engine.get_piece(pid).n))] += 1
    return {"dup_rate_observed": dup_obs, "dup_rate_iid": 1 - p_all_distinct, "n": int(len(h)),
            "slot_by_size": pd.DataFrame(tab, index=["slot0", "slot1", "slot2"], columns=["S", "M", "L"]),
            "slot_test": chi2_contingency(tab)}


def transitions(hands: pd.DataFrame) -> dict:
    """First-order transitions of size class: piece-to-piece (in slot order, across hands)
    and hand-to-hand (dominant class = class of the hand's mean cell count)."""
    if hands.empty:
        return {}
    h = hands[hands["n_pieces"] == 3].sort_values(["session", "seq"])
    piece_tab = np.zeros((3, 3))
    hand_tab = np.zeros((3, 3))
    cells_pairs = []
    for (_, gid), grp in h.groupby(["session", h.get("game_id", pd.Series(0, index=h.index))]):
        seq = [engine.size_class(engine.get_piece(p).n) for ids_ in grp["piece_ids"] for p in ids_]
        for a, b in zip(seq, seq[1:]):
            piece_tab["SML".index(a), "SML".index(b)] += 1
        hc = [sum(engine.get_piece(p).n for p in ids_) for ids_ in grp["piece_ids"]]
        hcls = ["S" if c <= 7 else "M" if c <= 11 else "L" for c in hc]
        for a, b in zip(hcls, hcls[1:]):
            hand_tab["SML".index(a), "SML".index(b)] += 1
        cells_pairs += list(zip(hc, hc[1:]))
    corr = float(np.corrcoef(np.array(cells_pairs).T)[0, 1]) if len(cells_pairs) > 2 else float("nan")
    return {"piece": pd.DataFrame(piece_tab, index=list("SML"), columns=list("SML")),
            "piece_test": chi2_contingency(piece_tab),
            "hand": pd.DataFrame(hand_tab, index=["az(<=7)", "orta(8-11)", "cok(12+)"],
                                 columns=["az(<=7)", "orta(8-11)", "cok(12+)"]),
            "hand_test": chi2_contingency(hand_tab), "hand_cells_lag1_corr": corr, "n_pairs": len(cells_pairs)}


def speed_summary(moves: pd.DataFrame) -> pd.DataFrame:
    if moves.empty or "block_label" not in moves:
        return pd.DataFrame()
    g = moves.groupby(moves["block_label"].astype(str)).agg(
        moves=("think_ms", "size"), think_ms_median=("think_ms", "median"),
        lines_mean=("n_lines", "mean"), cells_mean=("cells_placed", "mean"))
    return g.reset_index()


# ----------------------------------------------------------------------------
# Report
# ----------------------------------------------------------------------------

def _fmt_test(t: dict) -> str:
    if not t:
        return "-"
    s = f"chi2={t.get('chi2', float('nan')):.2f}, sd={t.get('df')}, p={t.get('p', float('nan')):.4g}, n={t.get('n')}"
    if "cramers_v" in t:
        s += f", Cramer V={t['cramers_v']:.3f}"
    if t.get("min_expected", 99) < 5:
        s += " (WARNING: expected frequency < 5, result unreliable)"
    return s


def _md(df: pd.DataFrame, max_rows: int = 50) -> str:
    if df is None or len(df) == 0:
        return "_no data_\n"
    d = df.head(max_rows)
    cols = [str(c) for c in d.columns]
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for row in d.itertuples(index=False):
        vals = []
        for v in row:
            if isinstance(v, float):
                vals.append(f"{v:.4g}")
            else:
                vals.append(str(v))
        lines.append("| " + " | ".join(vals) + " |")
    return "\n".join(lines) + "\n"


def report(data: dict, baseline_k: int = 40, max_hands: Optional[int] = None) -> tuple:
    moves, hands = data["moves"], data["hands"]
    out = ["# Block Blast analysis report", "",
           f"Generated: {time.strftime('%Y-%m-%d %H:%M')}. Sessions: {len(data['sessions'])}, "
           f"moves: {len(moves)}, hands: {len(hands)}, game records: {len(data['games'])}.", ""]
    tables = {}
    # scoring
    out += ["## 1. Scoring model fit", "",
            "Each candidate model was simulated over the recorded move sequence (combo state reset at the start of each game); "
            "`exact` = share of moves where the prediction equals the observed score delta exactly.", ""]
    fit = fit_scoring(moves) if not moves.empty else pd.DataFrame()
    tables["scoring_fit"] = fit
    out.append(_md(fit[["model", "n", "exact", "exact_clears", "n_clears", "mae"]] if len(fit) else fit, 15))
    if len(fit):
        best = fit.iloc[0]
        out += [f"Best model: **{best['model']}** ({best['desc']}), exact match %{100 * best['exact']:.1f}.", ""]
        named = fit[fit["model"].isin(["A", "B", "C"])]
        out.append("Literature candidates: " + ", ".join(f"{r.model}: %{100 * r.exact:.1f}" for r in named.itertuples()) + "\n")
    out += ["### Placement (no clear): score delta / cell count", ""]
    pt = placement_table(moves) if not moves.empty else pd.DataFrame()
    tables["placement"] = pt
    out.append(_md(pt))
    out += ["### Clears after a long gap (combo assumed reset): residual = delta - cells", ""]
    lb = line_base_table(moves) if not moves.empty else pd.DataFrame()
    tables["line_base"] = lb
    out.append(_md(lb))
    out += ["### Combo window: multiplier by number of moves since the previous clear", "",
            "`mult_A` = residual / base_A(l), `mult_B` = residual / (10 l^2). The `since` value at which the multiplier drops to 1 "
            "shows the combo window.", ""]
    ct = combo_table(moves) if not moves.empty else pd.DataFrame()
    tables["combo"] = ct
    out.append(_md(ct))
    # pieces
    out += ["## 2. Piece distribution", ""]
    pt2, gof = piece_table(hands)
    tables["pieces"] = pt2
    if gof:
        out += [f"Full hands: {gof['n_hands']}, pieces: {gof['n_pieces']}.",
                f"- Uniform distribution test (37 base poses): {_fmt_test(gof['uniform_core'])}",
                f"- Family weights (uniform-per-pose assumption): {_fmt_test(gof['family_vs_pose_uniform'])}",
                f"- Uniformity over observed poses only: {_fmt_test(gof['uniform_observed_set'])}",
                f"- Shapes not in the library: {gof['unknown_shapes'] or 'none'}", ""]
    out.append(_md(pt2, 45))
    # conditional
    out += ["## 3. Conditional tests (size class S=1-2, M=3-4, L=5+ cells)", ""]
    cond = conditional_tests(hands)
    for col, d in cond.items():
        out += [f"### {d['label']} (`{col}`)", "", _fmt_test(d["test"]), "", _md(d["table"].reset_index())]
        tables[f"cond_{col}"] = d["table"].reset_index()
    # baselines
    out += ["## 4. Comparison with random hands on the same boards", "",
            "For each observed hand, random triples (uniform, 37 poses) were drawn on the same board. "
            "`solvable` = all three pieces can be placed in some order; `enables_clear` = at least one piece completes a line immediately. "
            "If the observed rate differs significantly from the random baseline, the generator looks at the board (adaptive).", ""]
    base = baseline_comparison(hands, k=baseline_k, max_hands=max_hands) if not hands.empty else {}
    if base:
        bt = pd.DataFrame([{"ozellik": f, **v} for f, v in base["features"].items()])
        tables["baseline_uniform"] = bt
        out += [f"Hands: {base['n_hands']}, random samples per hand: {base['k']}.", "", _md(bt),
                "Unsolvable (lethal) hands by fullness:", "", _md(base["unsolvable_by_fill"])]
        _, gof2 = piece_table(hands)
        cnt = Counter(p for ids_ in hands[hands["n_pieces"] == 3]["piece_ids"] for p in ids_)
        if cnt:
            ids = sorted(cnt)
            base2 = baseline_comparison(hands, k=baseline_k, ids=ids, weights=dict(cnt), max_hands=max_hands, seed=1)
            bt2 = pd.DataFrame([{"ozellik": f, **v} for f, v in base2["features"].items()])
            tables["baseline_marginal"] = bt2
            out += ["Against the baseline drawn with observed marginal frequencies (independent):", "", _md(bt2),
                    "Difference between the two baselines: effect of the marginal distribution; deviation from the marginal baseline as well: board-conditional generation.", ""]
    # within-hand / transitions
    out += ["## 5. Within-hand structure and transitions", ""]
    wh = within_hand(hands)
    if wh:
        out += [f"Rate of the same piece repeating within a hand: observed %{100 * wh['dup_rate_observed']:.1f}, "
                f"expected under independence %{100 * wh['dup_rate_iid']:.1f} (n={wh['n']}).",
                f"Slot vs size relation: {_fmt_test(wh['slot_test'])}", "", _md(wh["slot_by_size"].reset_index())]
    tr = transitions(hands)
    if tr:
        out += ["Piece-to-piece size class transitions (first order):", "", _fmt_test(tr["piece_test"]), "",
                _md(tr["piece"].reset_index()), "Hand-to-hand total cell class transitions:", "", _fmt_test(tr["hand_test"]), "",
                _md(tr["hand"].reset_index()),
                f"Correlation of total cells between consecutive hands: {tr['hand_cells_lag1_corr']:.3f} (pairs: {tr['n_pairs']}).", ""]
    # speed / events / health
    out += ["## 6. Speed blocks, events, health", ""]
    sp = speed_summary(moves)
    out.append(_md(sp))
    ev = data["events"]
    if not ev.empty:
        evc = ev["kind"].value_counts().rename_axis("olay").reset_index(name="n")
        out += ["Event counts:", "", _md(evc)]
    hl = data["health"]
    if not hl.empty and "battery" in hl:
        temps = [b.get("temperature_c") for b in hl["battery"] if isinstance(b, dict) and b.get("temperature_c") is not None]
        if temps:
            out.append(f"Battery temperature: min {min(temps):.1f} C, max {max(temps):.1f} C, mean {np.mean(temps):.1f} C.\n")
    if not moves.empty and "verified" in moves:
        out.append(f"Verified move rate: %{100 * moves['verified'].mean():.1f}.\n")
    out += ["", "_Note: p-values are not corrected for multiple comparisons; this analysis is exploratory._"]
    return "\n".join(out), tables


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")  # Windows consoles without UTF-8
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(prog="python -m bot.analyze", description="Log analysis (report)")
    ap.add_argument("--logs", nargs="+", default=[str(DEFAULT_LOG_DIR)])
    ap.add_argument("--out", default=str(Path(DEFAULT_LOG_DIR).parent / "analysis"))
    ap.add_argument("--baseline-k", type=int, default=40)
    ap.add_argument("--max-hands", type=int, default=None, help="cap on hand samples for the baseline comparison")
    a = ap.parse_args(argv)
    data = load(a.logs)
    if not data["records"]:
        print("no records found")
        return 1
    md, tables = report(data, a.baseline_k, a.max_hands)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    (out / f"report_{stamp}.md").write_text(md, encoding="utf-8")
    for name, df in tables.items():
        if df is not None and len(df):
            df.to_csv(out / f"{name}_{stamp}.csv", index=False)
    print(md[:3000])
    print(f"\nreport: {out / f'report_{stamp}.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
