import random
import time

from bot import engine as E
from bot.solver import Objective, Solver, coverage, empty_regions, isolated_empty, roughness


def _random_board(rng, fill):
    b = 0
    for i in range(64):
        if rng.random() < fill:
            b |= 1 << i
    return E.clear_lines(b)[0]


def test_features():
    assert roughness(0) == 0
    assert isolated_empty(E.FULL & ~E.bit(3, 3)) == 1
    assert empty_regions(0) == [64]
    assert sorted(empty_regions(E.ROW_MASKS[4])) == [24, 32]
    assert coverage(0) == 1.0


def test_solver_takes_the_clear():
    board = E.ROW_MASKS[7] & ~(E.bit(7, 3) | E.bit(7, 4))
    plan = Solver().solve(board, [E.PIECES["h2"], E.PIECES["sq3"], E.PIECES["1x1"]])
    assert plan.complete and len(plan.steps) == 3
    assert any(s.n_lines >= 1 for s in plan.steps)
    # the plan replays exactly with the engine
    b = board
    for s in plan.steps:
        mr = E.apply_move(b, E.get_piece(s.piece_id), s.row, s.col)
        assert mr.board_after == s.board_after
        b = mr.board_after


def test_solver_handles_used_slots_and_no_fit():
    plan = Solver().solve(0, [None, E.PIECES["sq2"], None])
    assert len(plan.steps) == 1 and plan.steps[0].slot == 1
    full = E.FULL & ~E.bit(0, 0)
    plan = Solver().solve(full, [E.PIECES["sq3"], E.PIECES["h5"], E.PIECES["v5"]])
    assert not plan.complete and plan.steps == []


def test_solver_speed_typical():
    rng = random.Random(7)
    s = Solver()
    times = []
    for _ in range(40):
        b = _random_board(rng, rng.uniform(0.0, 0.5))
        hand = [E.PIECES[rng.choice(E.CORE_IDS)] for _ in range(3)]
        t = time.perf_counter()
        s.solve(b, hand)
        times.append((time.perf_counter() - t) * 1000)
    times.sort()
    assert times[len(times) // 2] < 200, times
    assert times[-1] < 1500, times


def test_probe_forbid_clear():
    board = E.ROW_MASKS[7] & ~E.bit(7, 0)
    hand = [E.PIECES["1x1"], None, None]
    plan = Solver().solve(board, hand, Objective(forbid_clear_steps=1))
    assert plan.steps[0].n_lines == 0
    plan = Solver().solve(board, hand)
    assert plan.steps[0].n_lines == 1


def test_probe_want_lines_two():
    # a v2 at col 0 rows 6-7 clears two rows; a 1x1 alone could clear one
    board = (E.ROW_MASKS[6] | E.ROW_MASKS[7]) & ~(E.bit(6, 0) | E.bit(7, 0))
    plan = Solver().solve(board, [E.PIECES["v2"], E.PIECES["1x1"], None],
                          Objective(want_lines=2, penalize_other_clears=True))
    assert any(s.n_lines == 2 for s in plan.steps)


def test_suicide_mode_avoids_clears():
    board = E.ROW_MASKS[7] & ~E.bit(7, 0)
    plan = Solver().solve(board, [E.PIECES["1x1"], E.PIECES["sq2"], E.PIECES["h3"]], Objective(suicide=True))
    assert all(s.n_lines == 0 for s in plan.steps)


def test_survival_against_uniform_generator():
    s = Solver()
    total = 0
    for seed in range(3):
        g = E.Game(seed=seed)
        while not g.over and g.moves < 150:
            p = s.solve(g.board, g.hand)
            if not p.steps:
                break
            st = p.steps[0]
            g.play(st.slot, st.row, st.col)
        total += g.moves
    assert total / 3 > 40
