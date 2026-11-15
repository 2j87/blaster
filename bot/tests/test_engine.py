import random

from bot import engine as E


def test_library_size_and_uniqueness():
    assert len(E.PIECES) == 41
    assert len(E.CORE_IDS) == 37
    masks = [p.mask for p in E.PIECE_LIST]
    assert len(set(masks)) == len(masks)
    for p in E.PIECE_LIST:
        assert min(r for r, _ in p.cells) == 0 and min(c for _, c in p.cells) == 0
        assert p.n == bin(p.mask).count("1")
        assert E.piece_from_cells(p.cells) is p


def test_piece_sizes():
    assert E.PIECES["1x1"].n == 1
    assert E.PIECES["sq3"].n == 9
    assert E.PIECES["BL_a"].n == 5
    assert E.PIECES["h5"].w == 5 and E.PIECES["v5"].h == 5
    assert E.PIECES["T_u"].art() == ".#./###"


def test_unknown_shape_dynamic_id():
    p = E.piece_from_cells([(0, 0), (0, 1), (1, 1), (2, 1), (2, 2)])
    assert p.id.startswith("X_") and not p.core
    assert E.get_piece(p.id) is p


def test_anchors_and_legal_placements_on_empty():
    assert len(E.legal_placements(0, E.PIECES["1x1"])) == 64
    assert len(E.legal_placements(0, E.PIECES["sq3"])) == 36
    assert len(E.legal_placements(0, E.PIECES["h5"])) == 32
    assert len(E.legal_placements(E.FULL, E.PIECES["1x1"])) == 0


def test_place_and_overlap():
    b = E.place(0, E.PIECES["sq2"], 0, 0)
    assert b == E.bit(0, 0) | E.bit(0, 1) | E.bit(1, 0) | E.bit(1, 1)
    assert not E.can_place(b, E.PIECES["1x1"], 1, 1)
    assert not E.can_place(0, E.PIECES["h5"], 0, 4)
    try:
        E.place(b, E.PIECES["1x1"], 0, 0)
        assert False
    except ValueError:
        pass


def test_simultaneous_row_and_column_clear():
    board = E.from_art("""
        #######.
        .......#
        .......#
        .......#
        .......#
        .......#
        .......#
        .......#""")
    mr = E.apply_move(board, E.PIECES["1x1"], 0, 7)
    assert mr.rows == (0,) and mr.cols == (7,)
    assert mr.n_lines == 2
    assert mr.board_after == 0
    assert mr.board_cleared


def test_double_row_clear_keeps_rest():
    board = E.ROW_MASKS[3] & ~E.bit(3, 0) | E.ROW_MASKS[4] & ~E.bit(4, 0) | E.bit(0, 5)
    mr = E.apply_move(board, E.PIECES["v2"], 3, 0)
    assert mr.rows == (3, 4) and mr.cols == ()
    assert mr.board_after == E.bit(0, 5)


def test_game_over_and_solvable():
    board = E.FULL & ~E.bit(0, 0)
    assert E.is_game_over(board, [E.PIECES["h2"], E.PIECES["sq2"]])
    assert not E.is_game_over(board, [E.PIECES["1x1"], None])
    # 1x1 fills (0,0) -> clears row 0 and col 0, then the sq2 fits
    assert E.hand_solvable(board, [E.PIECES["1x1"], E.PIECES["sq2"], E.PIECES["sq2"]])
    assert not E.hand_solvable(board, [E.PIECES["h2"], E.PIECES["sq2"]])


def test_hand_solvable_requires_order():
    # the h5 does not fit at first; the 1x1 at (7,7) clears row 7 + col 7 and makes room
    board = E.from_art("""
        #....###
        ########
        ########
        ########
        ########
        ########
        ########
        #######.""")
    h5 = E.PIECES["h5"]
    assert not E.fit_anchors(board, h5)
    assert E.hand_solvable(board, [h5, E.PIECES["1x1"]])
    assert not E.hand_solvable(board, [E.PIECES["sq2"], E.PIECES["sq2"]])


def test_find_move_and_hex():
    rng = random.Random(1)
    for _ in range(200):
        b = rng.getrandbits(64) & rng.getrandbits(64)
        b, _, _ = E.clear_lines(b)
        p = E.PIECES[rng.choice(E.CORE_IDS)]
        legal = E.legal_placements(b, p)
        if not legal:
            continue
        r, c = rng.choice(legal)
        mr = E.apply_move(b, p, r, c)
        found = E.find_move(b, mr.board_after, p)
        assert found is not None and found.board_after == mr.board_after
        assert E.from_hex(E.to_hex(b)) == b
        assert E.from_bits_str(E.to_bits_str(b)) == b
        assert E.from_grid(E.to_grid(b)) == b


def test_piece_enables_clear():
    board = E.ROW_MASKS[2] & ~E.bit(2, 3)
    assert E.piece_enables_clear(board, E.PIECES["1x1"])
    assert not E.piece_enables_clear(0, E.PIECES["sq3"])


def test_game_simulation_runs():
    g = E.Game(seed=3)
    moves = 0
    while not g.over and moves < 60:
        for s, p in enumerate(g.hand):
            if p is not None:
                legal = E.legal_placements(g.board, p)
                if legal:
                    g.play(s, *legal[0])
                    moves += 1
                    break
        else:
            break
    assert g.score >= moves
