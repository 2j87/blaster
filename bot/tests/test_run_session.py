"""Full-loop dry runs on the fake device (virtual clock: minutes of play in seconds)."""
import json

from bot import analyze, audit, run_session
from bot.fakedevice import FakeDevice, VirtualClock
from bot.logger import SessionLogger, read_jsonl
from bot import experiments, vision


def _run(tmp_path, *extra, sid="t"):
    args = ["--device", "fake", "--fast-clock", "--logs", str(tmp_path), "--session-id", sid,
            "--seed", "3", "--save-images", "none"] + list(extra)
    assert run_session.main(args) == 0
    return read_jsonl(tmp_path / sid)


def test_dry_run_mixed_abba(tmp_path):
    recs = _run(tmp_path, "--minutes", "12", "--mode", "mixed", "--speed", "abba", "--block-minutes", "3")
    moves = [r for r in recs if r["type"] == "move"]
    assert len(moves) > 100
    ok = sum(1 for m in moves if m["verified"]) / len(moves)
    assert ok > 0.95
    assert {m["block_label"] for m in moves} == {"fast", "slow"}
    assert any(m["objective"].startswith("probe") for m in moves)
    slow = [m["think_ms"] for m in moves if m["block_label"] == "slow"]
    assert min(slow) >= 7900
    phases = [r for r in recs if r["type"] == "phase"]
    assert len(phases) == 4
    fit = analyze.fit_scoring(analyze.load(tmp_path / "t")["moves"])
    assert fit[fit["model"] == "A"].iloc[0]["exact"] == 1.0  # the fake scores with candidate A


def test_game_over_restart_and_safety(tmp_path):
    cfg = tmp_path / "early.json"
    cfg.write_text(json.dumps(EARLY), encoding="utf-8")
    recs = _run(tmp_path, "--minutes", "6", "--phases", str(cfg), sid="g")
    games = [r for r in recs if r["type"] == "game"]
    starts = [g for g in games if g["kind"] == "start"]
    assert len(starts) >= 2
    ev = [r["kind"] for r in recs if r["type"] == "event"]
    assert "revive_declined" in ev and "restart_tap" in ev
    ends = [g for g in games if g["kind"] == "end"]
    assert all(e.get("final_score") is not None for e in ends)


EARLY = {"name": "early", "loop": True,
         "phases": [{"name": "early", "kind": "regime", "regime": "end_early", "minutes": 10, "think_s": [0.2, 0.4]}]}


def test_robustness_ads_disconnect_offset(tmp_path):
    recs = _run(tmp_path, "--minutes", "10", "--fake-ads", "0.03", "--fake-disconnects", "0.004",
                "--fake-offset-error", "1.0", "--fake-edge-overlay", sid="r")
    ev = [r["kind"] for r in recs if r["type"] == "event"]
    assert "ad_interrupt" in ev and "ad_end" in ev
    assert "disconnect" in ev and "reconnect" in ev
    assert "offset_verified" in ev
    moves = [r for r in recs if r["type"] == "move"]
    assert sum(1 for m in moves if m["verified"]) / len(moves) > 0.9
    fit = analyze.fit_scoring(analyze.load(tmp_path / "r")["moves"])
    assert fit[fit["model"] == "A"].iloc[0]["exact"] == 1.0


def _session(tmp_path, sid, minutes, dev_kw=None, extra=(), config=None):
    a = run_session.build_parser().parse_args(["--device", "fake", "--fast-clock", "--minutes", str(minutes),
                                               "--logs", str(tmp_path), "--save-images", "none", *extra])
    a.stop_file = str(tmp_path / "STOP")
    clock = VirtualClock()
    dev = FakeDevice(seed=1, clock=clock, **(dev_kw or {}))
    lg = SessionLogger(tmp_path, sid, clock=clock, fsync=False)
    cal = run_session.setup_fake_calibration(dev, lg.dir)
    sched = experiments.PhaseScheduler(config or experiments.preset_config("play", "fast"), minutes, 1)
    return run_session.Session(dev, cal, a, lg, clock, sched), dev, lg


def test_thermal_pause_and_brightness(tmp_path):
    hot = lambda t: 45.0 if 100 < t < 400 else 35.0
    sess, dev, lg = _session(tmp_path, "h", 9, {"temp_profile": hot}, ["--min-brightness", "5", "--health-s", "60"])
    sess.run()
    lg.close()
    recs = read_jsonl(tmp_path / "h")
    ev = [r["kind"] for r in recs if r["type"] == "event"]
    assert "thermal_pause" in ev and "thermal_resume" in ev
    assert dev.brightness == {"screen_brightness": 128, "screen_brightness_mode": 1}  # restored
    b = [r for r in recs if r["type"] == "event" and r["kind"] == "brightness"]
    assert b[0]["detail"] == "set" and b[-1]["detail"] == "restored"


def test_stop_file_and_checkpoint_resume(tmp_path):
    sess, dev, lg = _session(tmp_path, "c", 60)
    (tmp_path / "STOP").write_text("x")
    sess.run()
    lg.close()
    ck = json.loads((tmp_path / "c" / "checkpoint.json").read_text())
    assert ck["session"] == "c"
    # resume continues the same session with the next log part
    assert run_session.main(["--device", "fake", "--fast-clock", "--logs", str(tmp_path), "--resume", "c",
                             "--minutes", "3", "--save-images", "none"]) == 0
    parts = sorted((tmp_path / "c").glob("c_part*.jsonl"))
    assert len(parts) >= 2
    metas = [r for r in read_jsonl(tmp_path / "c") if r["type"] == "session"]
    assert metas[-1]["resumed"] is True


def test_never_taps_ad_continue(tmp_path):
    sess, dev, lg = _session(tmp_path, "s", 8, config=EARLY)
    sess.run()
    lg.close()
    assert dev.unknown_taps == 0  # every tap hit a verified button (decline / restart)
    assert dev.games >= 3


def test_audit_report(tmp_path):
    _run(tmp_path, "--minutes", "4", "--save-images", "all", "--audit-every", "5", sid="a")
    out = tmp_path / "labels.json"
    n = audit.make_labels(tmp_path / "a", out, 5)
    assert n > 0
    labels = json.loads(out.read_text(encoding="utf-8"))
    res = audit.labelled_accuracy(tmp_path / "a", labels)
    assert res["cell_acc"] == 1.0 and res["piece_acc"] == 1.0
    c = audit.consistency(read_jsonl(tmp_path / "a"))
    assert c["noclear_delta_eq_cells"] == 1.0
    assert audit.main(["report", "--logs", str(tmp_path / "a")]) == 0


def test_folder_device_parse_only(tmp_path):
    dev = FakeDevice(seed=8)
    folder = tmp_path / "shots"
    folder.mkdir()
    for i in range(3):
        vision.save_image(dev.screencap(), folder / f"{i:03d}.png")
    cal_dir = tmp_path / "cal"
    cal_dir.mkdir()
    cal = run_session.setup_fake_calibration(dev, cal_dir)
    cal.save(cal_dir / "cal.json")
    assert run_session.main(["--device", "folder", "--folder", str(folder), "--calibration", str(cal_dir / "cal.json"),
                             "--logs", str(tmp_path), "--session-id", "f", "--minutes", "1",
                             "--save-images", "none"]) == 0
    recs = read_jsonl(tmp_path / "f")
    assert any(r["type"] == "hand" for r in recs)
