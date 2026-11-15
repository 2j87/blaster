import math

from bot import analyze as A
from bot import engine as E
from bot import scoring as S
from bot.fakedevice import VirtualClock
from bot.logger import SessionLogger
from bot.solver import Solver


def _synthetic_logs(tmp_path, model, n_games=3, seed=0, weights=None, sid="syn"):
    """Play simulated games and log them exactly like run_session does."""
    clock = VirtualClock()
    lg = SessionLogger(tmp_path, sid, clock=clock)
    solver = Solver(beam=(24, 48), final_k=16)
    hand_id = 0
    for gid in range(1, n_games + 1):
        gen = E.UniformGenerator(seed=seed * 100 + gid, weights=weights)
        g = E.Game(scoring=model, generator=gen)
        lg.game("start", gid)
        since = 99
        while not g.over and g.moves < 90:
            if all(p is not None for p in g.hand):
                hand_id += 1
                move_idx = 0
                lg.hand(hand_id, g.hand, g.board, g.score, game_id=gid, hand_in_game=hand_id,
                        best_shown=1000, block_label="fast" if gid % 2 else "slow", phase="p")
            plan = solver.solve(g.board, g.hand, since_clear=since)
            if not plan.steps:
                break
            st = plan.steps[0]
            before, sb = g.board, g.score
            left = sum(1 for i, p in enumerate(g.hand) if p is not None and i != st.slot)
            mr, delta = g.play(st.slot, st.row, st.col)
            lg.move(game_id=gid, hand_id=hand_id, move_idx=move_idx, slot=st.slot, shape_id=mr.piece_id,
                    anchor=list(mr.anchor), board_before=before, board_after=mr.board_after,
                    lines_cleared={"rows": list(mr.rows), "cols": list(mr.cols)}, n_lines=mr.n_lines,
                    cells_placed=mr.cells, score_before=sb, score_after=g.score, score_delta=delta,
                    moves_since_last_clear=since, pieces_left=left, verified=True,
                    board_cleared=mr.board_cleared, block_label="fast" if gid % 2 else "slow", think_ms=100)
            since = 0 if mr.n_lines else min(99, since + 1)
            move_idx += 1
            clock.sleep(2)
        lg.game("end", gid, final_score=g.score)
    lg.close()
    return tmp_path / sid


def test_chi2_fallback_values():
    assert abs(A._gammq(1.0, 1.0) - math.exp(-1)) < 1e-9
    assert abs(A._gammq(5 / 2, 11.07 / 2) - 0.05) < 2e-3  # chi2(5) 95% quantile
    assert abs(A._gammq(10.0, 10.0) - 0.4579297) < 1e-6


def test_fit_recovers_model_a(tmp_path):
    d = _synthetic_logs(tmp_path, S.CANDIDATE_A)
    data = A.load(d)
    fit = A.fit_scoring(data["moves"])
    row_a = fit[fit["model"] == "A"].iloc[0]
    assert row_a["exact"] == 1.0
    assert fit.iloc[0]["exact"] == 1.0
    assert fit[fit["model"] == "B"].iloc[0]["exact"] < 1.0


def test_fit_recovers_model_b(tmp_path):
    d = _synthetic_logs(tmp_path, S.CANDIDATE_B, seed=1)
    fit = A.fit_scoring(A.load(d)["moves"])
    assert fit[fit["model"] == "B"].iloc[0]["exact"] == 1.0
    assert fit[fit["model"] == "A"].iloc[0]["exact"] < 1.0


def test_tables_and_piece_tests(tmp_path):
    d = _synthetic_logs(tmp_path, S.CANDIDATE_A, n_games=4, seed=2)
    data = A.load(d)
    pt = A.placement_table(data["moves"])
    assert (pt["cells"] == pt["delta"]).all()
    df, gof = A.piece_table(data["hands"])
    assert gof["n_pieces"] == df["count"].sum()
    assert gof["uniform_core"]["p"] > 1e-4  # uniform generator should not be rejected strongly
    cond = A.conditional_tests(data["hands"])
    assert "fill_bin" in cond and "block_label" in cond
    tr = A.transitions(data["hands"])
    assert tr["piece"].values.sum() > 0
    wh = A.within_hand(data["hands"])
    assert 0 <= wh["dup_rate_observed"] <= 1


def test_biased_generator_detected(tmp_path):
    w = {i: 1.0 for i in E.CORE_IDS}
    w["1x1"] = 15.0
    w["sq3"] = 15.0
    d = _synthetic_logs(tmp_path, S.CANDIDATE_A, n_games=4, seed=3, weights=w, sid="bias")
    _, gof = A.piece_table(A.load(d)["hands"])
    assert gof["uniform_core"]["p"] < 1e-6


def test_baseline_comparison_runs(tmp_path):
    d = _synthetic_logs(tmp_path, S.CANDIDATE_A, n_games=2, seed=4)
    res = A.baseline_comparison(A.load(d)["hands"], k=10)
    f = res["features"]
    assert set(f) == {"solvable", "all_fit", "any_fit", "enables_clear"}
    # the generator is i.i.d. uniform, so the observed solvable rate is close to the baseline
    assert abs(f["solvable"]["observed"] - f["solvable"]["baseline"]) < 0.2


def test_report_renders(tmp_path):
    d = _synthetic_logs(tmp_path, S.CANDIDATE_A, n_games=2, seed=5)
    md, tables = A.report(A.load(d), baseline_k=5)
    assert "Scoring model fit" in md and "Piece distribution" in md
    assert "scoring_fit" in tables
    assert A.main(["--logs", str(d), "--out", str(tmp_path / "out"), "--baseline-k", "3"]) == 0
    assert list((tmp_path / "out").glob("report_*.md"))
