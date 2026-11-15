"""Block Blast! reverse-engineering bot package.

Modules:
    engine       bitboard board model, piece library, placement/clearing
    scoring      candidate scoring models (A/B/C + parametric family)
    solver       beam search over orders x placements, probe objectives
    experiments  phase scheduler, pacing, probe controller
    capture      ADB device access (screencap, drag, focus, battery)
    fakedevice   simulated phone for dry runs (renders synthetic screens)
    vision       calibration, board/tray/score parsing, banners
    logger       JSONL logging with rotation and image saving
    run_session  main loop CLI
    analyze      statistical analysis of logs
    audit        parser accuracy audit
"""

__version__ = "0.1.0"
