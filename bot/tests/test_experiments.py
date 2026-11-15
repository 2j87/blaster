from pathlib import Path

from bot import experiments as X

PHASES = Path(__file__).resolve().parents[1] / "phases"


def test_expand_abba_and_loop():
    cfg = X.preset_config("play", "abba", block_minutes=10, minutes=40)
    blocks = X.expand(cfg, 40, seed=3)
    labels = [b.label for b in blocks]
    assert len(blocks) == 4
    assert labels[0] == labels[3] and labels[1] == labels[2] and labels[0] != labels[1]
    assert blocks[-1].end_min == 40


def test_overnight_config_covers_600_minutes():
    cfg = X.load_config(PHASES / "overnight.json")
    s = X.PhaseScheduler(cfg, 600, seed=1)
    assert abs(s.blocks[-1].end_min - 600) < 1e-6
    kinds = {b.kind for b in s.blocks}
    assert {"probe-scoring", "play", "regime"} <= kinds
    regimes = {b.params.get("regime") for b in s.blocks if b.kind == "regime"}
    assert {"end_early", "low_score", "fill_band", "push_pb"} <= regimes
    for t in (0, 59.9, 333.3, 599.9):
        assert s.block_at(t) is not None
    assert s.block_at(600) is None


def test_pilot_config_and_think_time():
    s = X.PhaseScheduler(X.load_config(PHASES / "pilot.json"), 60, seed=2)
    slow = [b for b in s.blocks if b.label == "slow"]
    assert slow
    for _ in range(50):
        v = s.think_time(slow[0])
        assert 8 <= v <= 10


def test_regime_objectives():
    b = X.Block("x", "regime", 0, 1, "fill", (0, 0), {"regime": "fill_band", "fill": [0.3, 0.5]})
    o = X.regime_objective(b, {})
    assert o.target_fill == (0.3, 0.5)
    b.params["regime"] = "end_early"
    assert X.regime_objective(b, {}).suicide


def test_probe_controller():
    pc = X.ProbeController()
    o = pc.next_objective(since_clear=0, fill=0.2)
    assert o.name == "probe_gap0" and o.forbid_clear_steps == 0
    pc.gap_counts[0] = 99
    o = pc.next_objective(since_clear=0, fill=0.2)
    assert o.name == "probe_gap1" and o.forbid_clear_steps == 1
    o = pc.next_objective(since_clear=50, fill=0.2)
    assert o.name.startswith("probe_multi")
    assert pc.next_objective(0, 0.9).name == "probe_safety"
    pc.record(2, 3)
    assert pc.multi_counts[2] == 1 and pc.gap_counts[3] == 1
    st = pc.state()
    pc2 = X.ProbeController()
    pc2.load_state(st)
    assert pc2.multi_counts[2] == 1
