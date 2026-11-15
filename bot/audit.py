"""Parser accuracy audit.

1) Label-free consistency checks from the logs (verified-move rate, mismatch kinds,
   OCR uncertainty, "non-clearing move delta == cells" check, tray mismatches).
2) Labelled accuracy: the raw screenshots saved under img/audit/ can be labelled
   (labels JSON prefilled with the parser's own reading, a human/agent fixes errors),
   then board-cell, piece and score accuracy are reported.

    python -m bot.audit report --logs research/logs/<session>
    python -m bot.audit make-labels --logs research/logs/<session> --out labels.json --n 100
    python -m bot.audit report --logs research/logs/<session> --labels labels.json [--reparse]

Labels format: {"img/audit/000050_periodic.png": {"board": ["........", ... 8 rows],
                 "pieces": ["sq2", null, "h3"], "score": 1234}, ...}
Paths are relative to the session directory.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path
from typing import Optional

import pandas as pd

from . import engine, vision
from .logger import read_jsonl


def _session_dirs(paths) -> list:
    out = []
    for p in paths:
        p = Path(p)
        if (p / "checkpoint.json").exists() or list(p.glob("*.jsonl")):
            out.append(p)
        else:
            out += [d for d in p.iterdir() if d.is_dir() and list(d.glob("*.jsonl"))]
    return out


def consistency(recs: list) -> dict:
    moves = [r for r in recs if r.get("type") == "move"]
    events = [r for r in recs if r.get("type") == "event"]
    n = len(moves)
    res = {"moves": n}
    if not n:
        return res
    res["verified_rate"] = sum(1 for m in moves if m.get("verified")) / n
    kinds = pd.Series([m.get("mismatch") or "ok" for m in moves]).value_counts().to_dict()
    res["mismatch_kinds"] = kinds
    res["score_missing_rate"] = sum(1 for m in moves if m.get("score_delta") is None) / n
    nc = [m for m in moves if m.get("n_lines") == 0 and m.get("score_delta") is not None and m.get("verified")]
    if nc:
        bad = [m for m in nc if m["score_delta"] != m["cells_placed"]]
        res["noclear_delta_eq_cells"] = 1 - len(bad) / len(nc)
        res["noclear_n"] = len(nc)
        res["noclear_bad_examples"] = [(m.get("hand_id"), m.get("cells_placed"), m.get("score_delta")) for m in bad[:10]]
    neg = [m for m in moves if m.get("score_delta") is not None and m["score_delta"] < 0]
    res["negative_delta"] = len(neg)
    ev = pd.Series([e.get("kind") for e in events]).value_counts().to_dict() if events else {}
    res["events"] = ev
    hands = [r for r in recs if r.get("type") == "hand"]
    if hands:
        confs = [c for h in hands for c in (h.get("slot_conf") or [])]
        res["slot_conf_low_rate"] = sum(1 for c in confs if c < 0.5) / len(confs) if confs else None
        res["unknown_shapes"] = sum(1 for h in hands for p in h.get("pieces", []) if p["shape_id"].startswith("X_"))
        res["tray_mismatch_per_hand"] = ev.get("tray_mismatch", 0) / len(hands)
    return res


def make_labels(session_dir: Path, out: Path, n: int = 100, seed: int = 0):
    recs = [r for r in read_jsonl(session_dir) if r.get("type") == "audit" and r.get("parsed_board")]
    random.Random(seed).shuffle(recs)
    labels = {}
    for r in recs[:n]:
        labels[r["path"]] = {"board": engine.render(engine.from_hex(r["parsed_board"])).split("\n"),
                             "pieces": r.get("parsed_pieces"), "score": r.get("parsed_score"),
                             "_note": "parser reading; correct any wrong fields"}
    out.write_text(json.dumps(labels, indent=1, ensure_ascii=False), encoding="utf-8")
    return len(labels)


def labelled_accuracy(session_dir: Path, labels: dict, reparse: bool = False,
                      cal: Optional[vision.Calibration] = None) -> dict:
    audits = {r["path"]: r for r in read_jsonl(session_dir) if r.get("type") == "audit"}
    reader = vision.DigitReader(cal.tdir) if (reparse and cal) else None
    cells_ok = cells_tot = boards_ok = pieces_ok = pieces_tot = score_ok = score_tot = 0
    errors = []
    for path, lab in labels.items():
        if reparse and cal:
            ip = session_dir / path
            if not ip.exists():
                continue
            sr = vision.read_screen(vision.load_image(ip), cal, reader)
            if sr.kind != "game":
                parsed = {"board": None, "pieces": None, "score": None}
            else:
                parsed = {"board": sr.board.board, "pieces": [p.id if p else None for p in sr.pieces], "score": sr.score}
        else:
            a = audits.get(path)
            if not a or not a.get("parsed_board"):
                continue
            parsed = {"board": engine.from_hex(a["parsed_board"]), "pieces": a.get("parsed_pieces"),
                      "score": a.get("parsed_score")}
        if lab.get("board"):
            truth = engine.from_art("\n".join(lab["board"]))
            pb = parsed["board"]
            if pb is None:
                cells_tot += 64
            else:
                diff = (truth ^ pb).bit_count()
                cells_ok += 64 - diff
                cells_tot += 64
                boards_ok += int(diff == 0)
                if diff:
                    errors.append((path, "board", diff))
        if lab.get("pieces") is not None:
            for t, p in zip(lab["pieces"], parsed["pieces"] or [None] * 3):
                pieces_tot += 1
                pieces_ok += int(t == p)
                if t != p:
                    errors.append((path, "piece", f"{p} -> {t}"))
        if lab.get("score") is not None:
            score_tot += 1
            score_ok += int(lab["score"] == parsed["score"])
            if lab["score"] != parsed["score"]:
                errors.append((path, "score", f"{parsed['score']} -> {lab['score']}"))
    nb = sum(1 for lab in labels.values() if lab.get("board"))
    return {"n_labels": len(labels), "cell_acc": cells_ok / cells_tot if cells_tot else None,
            "board_exact": boards_ok / nb if nb else None, "piece_acc": pieces_ok / pieces_tot if pieces_tot else None,
            "score_acc": score_ok / score_tot if score_tot else None, "errors": errors[:50]}


def render_report(sections: list) -> str:
    out = ["# Parser audit report", "", f"Time: {time.strftime('%Y-%m-%d %H:%M')}", ""]
    for name, c, lab in sections:
        out += [f"## Session `{name}`", "", "### Label-free consistency checks", ""]
        if not c.get("moves"):
            out.append("_no move records_\n")
        else:
            out += [f"- Moves: {c['moves']}",
                    f"- Rate of moves verified against the board: %{100 * c['verified_rate']:.2f}",
                    f"- Mismatch kinds: {c['mismatch_kinds']}",
                    f"- Rate of moves with unreadable score delta: %{100 * c['score_missing_rate']:.2f}",
                    f"- Negative score delta (sign of OCR error): {c['negative_delta']}"]
            if "noclear_delta_eq_cells" in c:
                out.append(f"- Delta == cell count on non-clearing moves: %{100 * c['noclear_delta_eq_cells']:.2f} "
                           f"(n={c['noclear_n']}); sample deviations (hand, cells, delta): {c['noclear_bad_examples']}")
            if "slot_conf_low_rate" in c:
                out.append(f"- Rate of low-confidence tray reads: {c['slot_conf_low_rate']}")
                out.append(f"- Shapes outside the library: {c['unknown_shapes']}")
                out.append(f"- Tray mismatches per hand: {c['tray_mismatch_per_hand']:.4f}")
            out.append(f"- Events: {c['events']}")
        out.append("")
        if lab:
            out += ["### Labelled accuracy", "",
                    f"- Labelled images: {lab['n_labels']}",
                    f"- Cell accuracy: {lab['cell_acc']}",
                    f"- Rate of boards read fully correctly: {lab['board_exact']}",
                    f"- Piece accuracy: {lab['piece_acc']}",
                    f"- Score accuracy: {lab['score_acc']}",
                    f"- Errors (first 50): {lab['errors']}", ""]
    return "\n".join(out)


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")  # Windows consoles without UTF-8
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(prog="python -m bot.audit", description="Parser accuracy audit")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report")
    r.add_argument("--logs", nargs="+", required=True)
    r.add_argument("--labels")
    r.add_argument("--reparse", action="store_true", help="re-parse labelled images with the current calibration")
    r.add_argument("--calibration", default=str(vision.DEFAULT_CAL))
    r.add_argument("--out")
    m = sub.add_parser("make-labels")
    m.add_argument("--logs", required=True)
    m.add_argument("--out", required=True)
    m.add_argument("--n", type=int, default=100)
    a = ap.parse_args(argv)
    if a.cmd == "make-labels":
        k = make_labels(Path(a.logs), Path(a.out), a.n)
        print(f"{k} labels written: {a.out} (pre-filled from the parser reading; correct any errors)")
        return 0
    sections = []
    labels = json.loads(Path(a.labels).read_text(encoding="utf-8")) if a.labels else None
    cal = vision.Calibration.load(a.calibration) if (a.reparse and Path(a.calibration).exists()) else None
    for d in _session_dirs(a.logs):
        c = consistency(read_jsonl(d))
        lab = labelled_accuracy(d, labels, a.reparse, cal) if labels else None
        sections.append((d.name, c, lab))
    md = render_report(sections)
    if a.out:
        Path(a.out).write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
