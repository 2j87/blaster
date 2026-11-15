import json

import numpy as np

from bot import engine as E
from bot.fakedevice import VirtualClock
from bot.logger import SessionLogger, load_checkpoint, read_jsonl, save_checkpoint


def _move(**kw):
    base = dict(hand_id=1, move_idx=0, slot=0, shape_id="sq2", anchor=[0, 0], board_before=0,
                board_after=E.place(0, E.PIECES["sq2"], 0, 0), lines_cleared={"rows": [], "cols": []},
                n_lines=0, cells_placed=4, score_before=0, score_after=4, score_delta=4)
    base.update(kw)
    return base


def test_records_and_hex(tmp_path):
    clock = VirtualClock()
    with SessionLogger(tmp_path, "s1", clock=clock) as lg:
        lg.session_meta(device={"model": "x"})
        lg.hand(1, [E.PIECES["sq2"], None, E.PIECES["h3"]], 0, 0, game_id=1)
        lg.move(**_move(extra=np.int64(5)))
        lg.event("ad_interrupt", "pkg")
    recs = read_jsonl(tmp_path / "s1")
    assert [r["type"] for r in recs] == ["session", "hand", "move", "event"]
    assert [r["seq"] for r in recs] == [0, 1, 2, 3]
    mv = recs[2]
    assert mv["board_after"] == E.to_hex(E.place(0, E.PIECES["sq2"], 0, 0))
    assert mv["extra"] == 5
    assert recs[1]["pieces"][1]["shape_id"] == "h3" and recs[1]["pieces"][1]["slot"] == 2


def test_move_requires_fields(tmp_path):
    lg = SessionLogger(tmp_path, "s2")
    m = _move()
    del m["score_delta"]
    try:
        lg.move(**m)
        assert False
    except ValueError:
        pass
    lg.close()


def test_rotation(tmp_path):
    clock = VirtualClock()
    lg = SessionLogger(tmp_path, "s3", rotate_minutes=60, clock=clock)
    lg.event("a")
    clock.sleep(3601)
    lg.event("b")
    clock.sleep(3601)
    lg.event("c")
    lg.close()
    files = sorted((tmp_path / "s3").glob("*.jsonl"))
    assert len(files) == 3
    assert [r["detail"] if r["type"] == "event" else None for r in read_jsonl(tmp_path / "s3")] == ["", "", ""]
    assert [r["kind"] for r in read_jsonl(tmp_path / "s3")] == ["a", "b", "c"]


def test_truncated_line_skipped(tmp_path):
    lg = SessionLogger(tmp_path, "s4")
    lg.event("ok")
    lg.close()
    with open(lg.path, "a", encoding="utf-8") as f:
        f.write('{"type": "event", "kind": "broken"')
    recs = read_jsonl(tmp_path)
    assert [r["kind"] for r in recs] == ["ok"]


def test_checkpoint_atomic(tmp_path):
    p = tmp_path / "ck.json"
    save_checkpoint(p, {"a": 1, "t": (1, 2)})
    assert load_checkpoint(p) == {"a": 1, "t": [1, 2]}
    assert load_checkpoint(tmp_path / "missing.json") is None
    p.write_text("{broken", encoding="utf-8")
    assert load_checkpoint(p) is None


def test_save_image(tmp_path):
    lg = SessionLogger(tmp_path, "s5")
    img = np.zeros((100, 50, 3), np.uint8)
    rel = lg.save_image(img, "moves/x.jpg", box=(0, 0, 50, 40))
    assert (lg.dir / rel).exists()
    lg.close()
