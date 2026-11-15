from bot import scoring as S


def test_line_bases():
    A, B, C = S.CANDIDATE_A, S.CANDIDATE_B, S.CANDIDATE_C
    assert [A.line_base(l) for l in range(5)] == [0, 10, 20, 60, 120]
    assert [B.line_base(l) for l in range(5)] == [0, 10, 40, 90, 160]
    assert [C.line_base(l) for l in range(5)] == [0, 10, 20, 30, 40]


def test_candidate_a_combo_and_window():
    A = S.CANDIDATE_A
    st = S.ComboState()
    d, st = S.step(A, st, 4, 0, 2)
    assert d == 4 and st.combo == 0
    d, st = S.step(A, st, 3, 1, 1)  # first clear: combo 1, counter 3+1
    assert d == 3 + 10 and st.combo == 1 and st.counter == 4
    d, st = S.step(A, st, 2, 2, 0)  # second clear: 20 * 2
    assert d == 2 + 40 and st.combo == 2 and st.counter == 3
    # three misses: counter 3 -> 2 -> 1 -> 0 resets on the third
    for k in range(2):
        d, st = S.step(A, st, 1, 0, 2 - k)
        assert st.combo == 2
    d, st = S.step(A, st, 1, 0, 0)
    assert st.combo == 0


def test_candidate_a_board_clear_bonus():
    d, _ = S.step(S.CANDIDATE_A, S.ComboState(), 1, 2, 0, board_empty_after=True)
    assert d == 1 + 20 + 300


def test_candidate_b_resets_on_any_miss():
    B = S.CANDIDATE_B
    st = S.ComboState()
    d, st = S.step(B, st, 1, 1, 2)
    assert d == 11
    d, st = S.step(B, st, 1, 1, 1)
    assert d == 1 + 10 * 2
    d, st = S.step(B, st, 1, 0, 0)
    assert st.combo == 0
    d, st = S.step(B, st, 1, 2, 2)
    assert d == 1 + 40


def test_grace_k():
    m = S.ScoringModel("g2", grace=2, line_form="C")
    st = S.ComboState()
    _, st = S.step(m, st, 1, 1, 2)
    _, st = S.step(m, st, 1, 0, 1)
    _, st = S.step(m, st, 1, 0, 0)
    assert st.combo == 1
    _, st = S.step(m, st, 1, 0, 2)
    assert st.combo == 0


def test_simulate_and_grid():
    seq = [{"cells": 3, "n_lines": 1, "pieces_left": 2, "new_game": True},
           {"cells": 2, "n_lines": 1, "pieces_left": 1}]
    assert S.simulate(S.CANDIDATE_A, seq) == [13, 22]
    grid = S.model_grid()
    assert len(grid) > 100
    assert {"A", "B", "C"} <= {m.name for m in grid}
