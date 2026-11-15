import random

import numpy as np

from bot import engine as E
from bot import vision as V
from bot.fakedevice import FakeDevice


def _calibrated(tmp_path, dev=None):
    dev = dev or FakeDevice(seed=1)
    img = dev.screencap()
    cal = V.auto_calibrate(img, base=V.Calibration(template_dir=str(tmp_path / "tpl")))
    reader = V.DigitReader(cal.tdir)
    box = cal.px_box(cal.score, img.shape)
    for v in (12345, 67890):
        reader.learn(V.crop(dev.render_score_sample(v), box), v)
    return dev, cal, reader


def test_board_detection_exact(tmp_path):
    dev, cal, _ = _calibrated(tmp_path)
    x0, y0, x1, y1 = cal.board_px((dev.H, dev.W))
    tx0, ty0, tx1, ty1 = dev.board_box()
    assert max(abs(x0 - tx0), abs(y0 - ty0), abs(x1 - tx1), abs(y1 - ty1)) <= 3


def test_board_parse_random_states(tmp_path):
    dev, cal, _ = _calibrated(tmp_path)
    rng = random.Random(5)
    for _ in range(15):
        b = 0
        for i in range(64):
            if rng.random() < 0.45:
                b |= 1 << i
        dev.game.board = b
        dev.colors = {i: rng.randrange(7) for i in range(64)}
        br = V.parse_board(dev.screencap(), cal)
        assert br.board == b
        assert br.confidence > 0.95


def test_tray_parses_every_core_piece(tmp_path):
    dev, cal, _ = _calibrated(tmp_path)
    ids = list(E.CORE_IDS)
    for i in range(0, len(ids), 3):
        trio = ids[i:i + 3]
        dev.game.hand = [E.PIECES[x] for x in trio] + [None] * (3 - len(trio))
        slots = V.parse_tray(dev.screencap(), cal)
        got = [s.piece.id if s.piece else None for s in slots]
        assert got[:len(trio)] == trio, (trio, got)


def test_empty_slot_detected(tmp_path):
    dev, cal, _ = _calibrated(tmp_path)
    dev.game.hand = [None, E.PIECES["sq3"], None]
    got = [s.piece.id if s.piece else None for s in V.parse_tray(dev.screencap(), cal)]
    assert got == [None, "sq3", None]


def test_score_reading(tmp_path):
    dev, cal, reader = _calibrated(tmp_path)
    assert set(reader.known_digits()) == set("0123456789")
    for v in (0, 7, 42, 908, 13579, 246801):
        img = dev.render_score_sample(v)
        val, conf, _ = V.read_number(img, cal, reader)
        assert val == v and conf > 0.6


def test_learn_rejects_wrong_digit_count(tmp_path):
    dev, cal, reader = _calibrated(tmp_path)
    img = dev.render_score_sample(123)
    try:
        reader.learn(V.crop(img, cal.px_box(cal.score, img.shape)), 12)
        assert False
    except ValueError:
        pass


def test_nongame_screens_classified(tmp_path):
    dev, cal, reader = _calibrated(tmp_path)
    for screen in ("revive", "gameover"):
        sr = V.read_screen(dev.render_screen(screen), cal, reader)
        assert sr.kind == "unknown"
    assert V.read_screen(dev.screencap(), cal, reader).kind == "game"


def test_button_templates_match_only_their_screen(tmp_path):
    dev, cal, _ = _calibrated(tmp_path)
    V.add_button(dev.render_screen("revive"), cal, "decline", dev.button_boxes["decline"])
    V.add_button(dev.render_screen("gameover"), cal, "restart", dev.button_boxes["restart"])
    assert V.match_button(dev.render_screen("revive"), cal, "decline")
    assert V.match_button(dev.render_screen("gameover"), cal, "restart")
    assert V.match_button(dev.render_screen("gameover"), cal, "decline") is None
    assert V.match_button(dev.render_screen("revive"), cal, "restart") is None
    assert V.match_button(dev.screencap(), cal, "restart") is None


def test_edge_overlay_tolerated(tmp_path):
    dev = FakeDevice(seed=2, edge_overlay=True)
    dev, cal, reader = _calibrated(tmp_path, dev)
    sr = V.read_screen(dev.screencap(), cal, reader)
    assert sr.kind == "game"
    assert [s.piece.id for s in sr.slots] == [p.id for p in dev.game.hand]


def test_other_resolution(tmp_path):
    from PIL import Image
    dev = FakeDevice(seed=4)
    small = np.asarray(Image.fromarray(dev.screencap()).resize((720, 1560), Image.NEAREST))
    cal = V.auto_calibrate(small, base=V.Calibration(template_dir=str(tmp_path / "t")))
    sr = V.read_screen(small, cal)
    assert sr.kind == "game" and sr.board.board == 0
    assert [s.piece.id for s in sr.slots] == [p.id for p in dev.game.hand]
    # fractions transfer back to full resolution
    big = dev.screencap()
    assert V.read_screen(big, cal).kind == "game"


def test_banner_detection_and_settle(tmp_path):
    dev, cal, _ = _calibrated(tmp_path)
    plain = dev.screencap()
    dev.banner_until = dev.clock.time() + 10
    with_banner = dev.screencap()
    found, crop, d = V.detect_banner([with_banner, plain], plain, cal)
    assert found and crop is not None
    found2, _, _ = V.detect_banner([plain, plain], plain, cal)
    assert not found2
    dev.banner_until = dev.clock.time() + 0.5
    img, frames, settled = V.wait_settle(dev.screencap, cal, clock=dev.clock)
    assert settled and V.parse_board(img, cal).board == dev.game.board


def test_calibration_roundtrip(tmp_path):
    dev, cal, _ = _calibrated(tmp_path)
    p = tmp_path / "cal.json"
    cal.save(p)
    cal2 = V.Calibration.load(p)
    assert cal2.board == cal.board and cal2.empty_rgb == cal.empty_rgb
    over = V.draw_overlay(dev.screencap(), cal2)
    assert over.shape == (dev.H, dev.W, 3)


def test_drag_points_hit_target(tmp_path):
    dev, cal, _ = _calibrated(tmp_path)
    cal.finger_offset_cells = list(dev.true_offset)
    p = dev.game.hand[0]
    slots = V.parse_tray(dev.screencap(), cal)
    legal = E.legal_placements(dev.game.board, p)
    r, c = legal[len(legal) // 2]
    start, end = cal.drag_points(p, r, c, (dev.H, dev.W), slots[0].center)
    dev.drag(*start, *end)
    assert dev.game.board == E.place(0, p, r, c)
