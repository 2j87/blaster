"""Benchmark: `adb exec-out screencap` vs the scrcpy stream (read-only, no input events).

python -m bot.bench_capture --seconds 30 --max-size 0 --out research\bench_stream
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np

from .capture import AdbDevice
from .stream_capture import StreamCapture


def pct(values, q):
    return float(np.percentile(values, q)) if len(values) else None


def summarize(lat_ms, wall, cpu):
    return {"n": len(lat_ms), "median_ms": round(pct(lat_ms, 50), 2), "p95_ms": round(pct(lat_ms, 95), 2),
            "max_ms": round(max(lat_ms), 2), "calls_per_s": round(len(lat_ms) / wall, 2),
            "py_cpu_pct": round(100 * cpu / wall, 1)}


def bench_screencap(dev, seconds):
    lat = []
    t_end = time.monotonic() + seconds
    w0, c0 = time.monotonic(), time.process_time()
    while time.monotonic() < t_end:
        t = time.perf_counter()
        dev.screencap()
        lat.append((time.perf_counter() - t) * 1000)
    return summarize(lat, time.monotonic() - w0, time.process_time() - c0)


def bench_stream(dev, seconds, max_size, poll_hz, log):
    t0 = time.perf_counter()
    cap = StreamCapture(device=dev, max_size=max_size, log=log)
    ok = cap.start()
    startup_ms = (time.perf_counter() - t0) * 1000
    if not ok:
        return {"started": False, "error": cap.error}, None
    try:
        lat, ages, gaps = [], [], []
        seq0 = cap.seq
        last_seq, last_change = cap.seq, time.monotonic()
        w0, c0 = time.monotonic(), time.process_time()
        t_end = w0 + seconds
        img = None
        while time.monotonic() < t_end:
            t = time.perf_counter()
            img = cap.screencap()
            lat.append((time.perf_counter() - t) * 1000)
            ages.append(cap.last_frame_age_ms)
            now = time.monotonic()
            if cap.seq != last_seq:
                gaps.append((now - last_change) * 1000)
                last_seq, last_change = cap.seq, now
            time.sleep(max(0.0, 1.0 / poll_hz - (time.perf_counter() - t)))
        wall, cpu = time.monotonic() - w0, time.process_time() - c0
        res = summarize(lat, wall, cpu)
        res.update({"started": True, "startup_ms": round(startup_ms), "stream_size": cap.stream_size,
                    "frame_shape": list(img.shape) if img is not None else None,
                    "decoded_fps": round((cap.seq - seq0) / wall, 2),
                    "age_median_ms": round(pct(ages, 50), 1), "age_p95_ms": round(pct(ages, 95), 1),
                    "age_max_ms": round(max(ages), 1),
                    "max_gap_between_new_frames_ms": round(max(gaps), 1) if gaps else None,
                    "fallbacks": cap.fallback_count, "decode_errors": cap.decode_errors,
                    "error": cap.error, "running_at_end": cap.running})
        # color / size comparison against a screencap taken right after the stream frame
        # (two stream frames around it tell whether the screen was static meanwhile)
        st_before = cap.screencap().copy()
        sc_img = dev.screencap()
        st_img = cap.screencap()
        res["compare"] = compare(sc_img, st_img)
        res["compare"]["stream_before_vs_after_mean_abs"] = round(float(
            np.abs(st_before.astype(np.int16) - st_img.astype(np.int16)).mean()), 3)
        return res, (sc_img, st_img)
    finally:
        cap.stop()


def compare(sc_img, st_img):
    import cv2
    out = {"screencap_shape": list(sc_img.shape), "stream_shape": list(st_img.shape)}
    if sc_img.shape != st_img.shape:
        st_img = cv2.resize(st_img, (sc_img.shape[1], sc_img.shape[0]), interpolation=cv2.INTER_LINEAR)
    d = np.abs(sc_img.astype(np.int16) - st_img.astype(np.int16))
    out.update({"mean_abs_diff_rgb": [round(float(v), 2) for v in d.reshape(-1, 3).mean(0)],
                "p99_abs_diff": float(np.percentile(d, 99)), "max_abs_diff": int(d.max()),
                "mean_signed_diff_rgb(stream-screencap)":
                    [round(float(v), 2) for v in (st_img.astype(np.int16) - sc_img.astype(np.int16))
                     .reshape(-1, 3).mean(0)]})
    # 9x9 blur removes chroma-subsampling / block edges: what remains is a color shift
    b = np.abs(cv2.blur(sc_img, (9, 9)).astype(np.int16) - cv2.blur(st_img, (9, 9)).astype(np.int16))
    out["blurred_mean_abs_diff"] = round(float(b.mean()), 2)
    out["blurred_p99_abs_diff"] = float(np.percentile(b, 99))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bot.bench_capture")
    ap.add_argument("--serial")
    ap.add_argument("--adb-path")
    ap.add_argument("--seconds", type=float, default=30)
    ap.add_argument("--max-size", type=int, nargs="+", default=[0])
    ap.add_argument("--poll-hz", type=float, default=20)
    ap.add_argument("--skip-screencap", action="store_true")
    ap.add_argument("--out", help="folder for results.json and comparison images")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args(argv)
    dev = AdbDevice(a.serial, a.adb_path)
    log = print if a.verbose else None
    results = {"device": dev.serial}
    if not a.skip_screencap:
        results["screencap"] = bench_screencap(dev, a.seconds)
        print("screencap", json.dumps(results["screencap"]))
    for ms in a.max_size:
        r, imgs = bench_stream(dev, a.seconds, ms, a.poll_hz, log)
        results[f"stream_max_size_{ms}"] = r
        print(f"stream max_size={ms}", json.dumps(r))
        if a.out and imgs:
            from PIL import Image
            out = Path(a.out)
            out.mkdir(parents=True, exist_ok=True)
            Image.fromarray(imgs[0]).save(out / f"cmp_screencap_{ms}.png")
            Image.fromarray(imgs[1]).save(out / f"cmp_stream_{ms}.png")
    if a.out:
        Path(a.out).mkdir(parents=True, exist_ok=True)
        (Path(a.out) / "results.json").write_text(json.dumps(results, indent=1))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
