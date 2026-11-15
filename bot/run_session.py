"""Main loop: capture -> parse -> solve -> drag -> verify -> log, for pilot and overnight runs.

Examples (run from E:\\Belgeler\\blaster):
    python -m bot.run_session --device fake --minutes 30 --fast-clock          # dry run, no phone
    python -m bot.run_session --minutes 60 --mode mixed --speed abba             # pilot on the phone
    python -m bot.run_session --minutes 600 --phases bot/phases/overnight.json --min-brightness 10
    python -m bot.run_session --resume-last                                      # continue after a crash

Stop safely: Ctrl+C (finishes the current move) or create the file research/logs/STOP.
Safety: the bot only drags pieces on the board and taps buttons whose stored template
matches (decline revive, restart, play). It never taps ads, purchase or install buttons.
"""
from __future__ import annotations

import argparse
import json
import random
import signal
import sys
import time
import traceback
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from . import engine, experiments, vision
from .capture import DeviceError, FocusInfo, FolderDevice, KEY_WAKEUP
from .fakedevice import FakeDevice, RealClock, VirtualClock
from .logger import DEFAULT_LOG_DIR, SessionLogger, load_checkpoint, save_checkpoint
from .solver import Objective, Solver

OFFSET_CANDIDATES_DY = [-2.0, -1.0, -3.0, 0.0, -1.5, -2.5, -4.0, -0.5]
SAFE_BUTTONS = ("decline", "restart", "play", "home_play", "close")


class Session:
    def __init__(self, device, cal: vision.Calibration, args, logger: SessionLogger, clock,
                 scheduler: experiments.PhaseScheduler, cal_path: Optional[Path] = None,
                 checkpoint: Optional[dict] = None):
        self.dev = device
        self.cal = cal
        self.cal_path = cal_path
        self.a = args
        self.log = logger
        self.clock = clock
        self.sched = scheduler
        self.solver = Solver()
        self.probes = experiments.ProbeController()
        self.reader = vision.DigitReader(cal.tdir)
        self.stop_requested = False
        self.stop_file = Path(args.stop_file) if args.stop_file else None
        # run state
        self.t_start = clock.time()
        self.elapsed_offset_min = 0.0
        self.game_id = 0
        self.game_active = False
        self.game_moves = 0
        self.hand_id = 0
        self.hand_in_game = 0
        self.hand_slots: list = [None, None, None]
        self.expect_new_hand = True
        self.move_idx = 0
        self.moves_total = 0
        self.score: Optional[int] = None
        self.best_shown: Optional[int] = None
        self.since_clear = 99
        self.awaiting_new_game = False
        self.game_end_logged = False
        self.last_change_t = clock.time()
        self.nongame_since: Optional[float] = None
        self.foreign_since: Optional[float] = None
        self.foreign_backs = 0
        self.recovery_level = 0
        self.last_health_t = 0.0
        self.last_ckpt_t = clock.time()
        self.block_idx = -1
        self.block: Optional[experiments.Block] = None
        self.drop_failures = 0
        self.offset_try = 0
        self.game_activity: Optional[str] = None
        self.brightness_orig: Optional[dict] = None
        self.resumed = False
        self.consecutive_errors = 0
        self.pending_move = None  # set while a drag is in flight (for disconnect bookkeeping)
        self.interrupted = None  # pending_move of a drag cut off by a disconnect
        self.game_uncertain = False  # a move of this game may be missing from the log
        self.uncertain_misses = 0
        if checkpoint:
            self._restore(checkpoint)

    # ------------------------------------------------------------------ utils
    def now(self) -> float:
        return self.clock.time()

    def elapsed_min(self) -> float:
        return (self.now() - self.t_start) / 60.0 + self.elapsed_offset_min

    def status(self, text: str):
        try:
            (self.log.dir / "STATUS.txt").write_text(
                f"{time.strftime('%Y-%m-%d %H:%M:%S')}  minutes={self.elapsed_min():.1f}  game={self.game_id} "
                f"el={self.hand_id} moves={self.moves_total}\n{text}\n", encoding="utf-8")
        except OSError:
            pass

    def _should_stop(self) -> bool:
        if self.stop_requested:
            return True
        if self.stop_file and self.stop_file.exists():
            self.log.event("stop_file", str(self.stop_file))
            return True
        if isinstance(self.dev, FolderDevice) and self.dev.exhausted:
            return True
        return self.elapsed_min() >= self.a.minutes

    def is_game_focus(self, f: FocusInfo) -> bool:
        pkg = self.cal.game_package
        if not pkg:
            return True
        if f.package != pkg:
            return False
        # in-app ad activities (e.g. Unity/AppLovin ad units) run inside the game package
        if self.game_activity and f.activity and f.activity != self.game_activity:
            low = f.activity.lower()
            if any(k in low for k in ("ad", "applovin", "unity3d.ads", "mbridge", "vungle", "ironsource", "fyber")):
                return False
        return True

    # ------------------------------------------------------------ checkpoint
    def checkpoint_state(self) -> dict:
        return {"session": self.log.session, "part": self.log.part, "elapsed_min": self.elapsed_min(),
                "game_id": self.game_id, "hand_id": self.hand_id, "moves_total": self.moves_total,
                "probes": self.probes.state(), "finger_offset_cells": self.cal.finger_offset_cells,
                "offset_verified": self.cal.offset_verified, "best_shown": self.best_shown,
                "game_activity": self.game_activity, "brightness_orig": self.brightness_orig,
                "t_wall": time.time()}

    def _restore(self, ck: dict):
        self.elapsed_offset_min = float(ck.get("elapsed_min", 0.0))
        self.game_id = int(ck.get("game_id", 0))
        self.hand_id = int(ck.get("hand_id", 0))
        self.moves_total = int(ck.get("moves_total", 0))
        self.probes.load_state(ck.get("probes", {}))
        if ck.get("finger_offset_cells"):
            self.cal.finger_offset_cells = ck["finger_offset_cells"]
            self.cal.offset_verified = bool(ck.get("offset_verified", False))
        self.best_shown = ck.get("best_shown")
        self.game_activity = ck.get("game_activity")
        self.brightness_orig = ck.get("brightness_orig")
        self.resumed = True

    def maybe_checkpoint(self, force: bool = False):
        if force or self.now() - self.last_ckpt_t >= self.a.checkpoint_s:
            save_checkpoint(self.log.dir / "checkpoint.json", self.checkpoint_state())
            self.last_ckpt_t = self.now()

    # ------------------------------------------------------------------ setup
    def setup(self):
        md = {}
        try:
            md = self.dev.metadata()
        except Exception as ex:
            md = {"metadata_error": str(ex)}
        if not self.cal.game_package:
            try:
                f = self.dev.focus()
                self.cal.game_package = f.package
                self.log.event("package_detected", f"{f.package}/{f.activity}")
            except Exception:
                pass
        version = None
        try:
            version = self.dev.package_version(self.cal.game_package) if self.cal.game_package else None
        except Exception:
            pass
        if self.a.min_brightness is not None:
            try:
                if self.brightness_orig is None:
                    self.brightness_orig = self.dev.get_brightness()
                self.dev.set_brightness(self.a.min_brightness, mode=0)
                self.log.event("brightness", "set", original=self.brightness_orig, new=self.a.min_brightness)
            except Exception as ex:
                self.log.event("brightness", f"failed: {ex}")
        self.log.session_meta(
            device=md, app_version=version, package=self.cal.game_package, args=vars(self.a),
            calibration=asdict(self.cal), phases=[b.to_dict() for b in self.sched.blocks],
            network=self.a.network, profile=self.a.profile, resumed=self.resumed,
            resumed_elapsed_min=self.elapsed_offset_min, bot_version="0.1.0")

    def teardown(self):
        if self.brightness_orig and self.a.min_brightness is not None:
            try:
                ob = self.brightness_orig
                self.dev.set_brightness(ob.get("screen_brightness"), mode=ob.get("screen_brightness_mode"))
                self.log.event("brightness", "restored", original=ob)
            except Exception as ex:
                self.log.event("brightness", f"restore failed: {ex}")
        if self.game_active and not self.game_end_logged:
            self.log.game("end", self.game_id, final_score=self.score, best_shown=self.best_shown,
                          moves=self.game_moves, reason="session_end")
        self.log.event("session_end", f"moves={self.moves_total}", elapsed_min=round(self.elapsed_min(), 2),
                       probes=self.probes.state())
        self.maybe_checkpoint(force=True)
        self.status("session finished")

    # ------------------------------------------------------------------- loop
    def run(self):
        self.setup()
        try:
            while not self._should_stop():
                b_idx = self.sched.block_index(self.elapsed_min())
                if b_idx != self.block_idx:
                    self.block_idx = b_idx
                    self.block = self.sched.blocks[b_idx] if b_idx >= 0 else None
                    if self.block:
                        self.log.phase(**self.block.to_dict(), elapsed_min=round(self.elapsed_min(), 3))
                try:
                    self.maybe_health()
                    self.tick()
                    self.consecutive_errors = 0
                except DeviceError as ex:
                    self.handle_disconnect(ex)
                except KeyboardInterrupt:
                    raise
                except Exception as ex:  # keep overnight runs alive; log and continue
                    self.consecutive_errors += 1
                    self.log.event("error", f"{type(ex).__name__}: {ex}", trace=traceback.format_exc()[-2000:])
                    self.clock.sleep(min(30, 2 * self.consecutive_errors))
                    if self.consecutive_errors > 50:
                        self.log.event("abort", "too many consecutive errors")
                        break
                self.maybe_checkpoint()
        except KeyboardInterrupt:
            self.log.event("stop", "keyboard interrupt")
        finally:
            self.teardown()

    def handle_disconnect(self, ex):
        self.log.event("disconnect", str(ex))
        if self.pending_move is not None:
            pub = {k: v for k, v in self.pending_move.items() if not k.startswith("_")}
            self.log.event("move_interrupted", "drag sent but not logged; will reconstruct from the next frame", **pub)
            self.interrupted = self.pending_move
            self.pending_move = None
        self.status("ADB connection lost, reconnecting...")
        ok = self.dev.reconnect(max_wait_s=self.a.reconnect_wait_s,
                                log=lambda m: self.log.event("reconnect_attempt", m))
        if ok:
            self.log.event("reconnect", "ok", reconnects=getattr(self.dev, "reconnects", None))
            self.expect_resync()
        else:
            self.log.event("abort", "device did not come back")
            self.stop_requested = True

    def expect_resync(self):
        self.last_change_t = self.now()

    # ----------------------------------------------------------------- health
    def maybe_health(self):
        if self.now() - self.last_health_t < self.a.health_s:
            return
        self.last_health_t = self.now()
        try:
            bat = self.dev.battery()
        except DeviceError:
            raise
        except Exception as ex:
            self.log.event("health_error", str(ex))
            return
        self.log.health(battery=bat.to_dict(), capture_ms=getattr(self.dev, "last_capture_ms", None),
                        reconnects=getattr(self.dev, "reconnects", None), elapsed_min=round(self.elapsed_min(), 2))
        if bat.temperature_c is not None and bat.temperature_c >= self.a.max_temp:
            self.thermal_pause(bat.temperature_c)
        if bat.level is not None and bat.level <= self.a.min_battery and not bat.plugged:
            self.log.event("battery_low", f"level={bat.level}")
            self.status("Battery low and not charging: paused")
            self.stop_requested = True

    def thermal_pause(self, temp: float):
        self.log.event("thermal_pause", f"temp={temp}", threshold=self.a.max_temp)
        resume_at = self.a.max_temp - self.a.temp_hysteresis
        t0 = self.now()
        while not self.stop_requested:
            self.status(f"Heat break: battery {temp:.1f} C (resume threshold {resume_at:.1f} C)")
            self.clock.sleep(60)
            try:
                temp = self.dev.battery().temperature_c or 0.0
            except Exception:
                continue
            self.log.health(battery={"temperature_c": temp}, note="thermal_pause")
            if temp <= resume_at or (self.stop_file and self.stop_file.exists()):
                break
            if self.elapsed_min() >= self.a.minutes:
                break
        self.log.event("thermal_resume", f"temp={temp}", paused_s=round(self.now() - t0, 1))
        self.last_change_t = self.now()

    # ------------------------------------------------------------------- tick
    def tick(self):
        f = self.dev.focus()
        if not self.is_game_focus(f):
            self.handle_foreign(f)
            return
        if self.foreign_since is not None:
            self.log.event("ad_end", f"{f.package}", duration_s=round(self.now() - self.foreign_since, 1))
            self.foreign_since = None
            self.foreign_backs = 0
        t_obs = self.now()
        img = self.dev.screencap()
        cap_ms = getattr(self.dev, "last_capture_ms", None)
        sr = vision.read_screen(img, self.cal, self.reader)
        if sr.kind != "game":
            self.handle_nongame(img)
            return
        if self.game_activity is None and f.activity:
            self.game_activity = f.activity
        self.nongame_since = None
        self.recovery_level = 0
        board = sr.board.board
        if sr.best is not None:
            self.best_shown = sr.best
        self.detect_new_game(board, sr)
        if self.interrupted is not None:
            self.recover_interrupted(board, sr)
        pieces = sr.pieces
        if all(p is None for p in pieces):
            self.clock.sleep(0.3)
            self.check_stuck(img, "empty_tray")
            return
        ids = [p.id if p else None for p in pieces]
        if self.expect_new_hand:
            self.new_hand(pieces, board, sr)
        elif ids != self.hand_slots:
            # parse glitch or missed hand: adopt the observed tray, log it
            self.log.event("tray_mismatch", f"expected {self.hand_slots} saw {ids}")
            self.game_uncertain = True
            self.uncertain_misses = 0
            if sum(1 for i in ids if i) == 3 and sum(1 for i in self.hand_slots if i) < 3:
                self.new_hand(pieces, board, sr, note="late_detect")
            else:
                self.hand_slots = ids
        if sr.score is not None and sr.score_conf >= self.a.min_score_conf:
            self.score = sr.score
        if engine.is_game_over(board, pieces):
            self.log.event("no_fit", "no piece fits", board=engine.to_hex(board), pieces=ids)
            self.clock.sleep(1.0)
            self.check_stuck(img, "no_fit")
            return
        ctx = {"since_clear": self.since_clear, "fill": engine.fill_fraction(board)}
        obj = experiments.objective_for(self.block, ctx, self.probes)
        plan = self.solver.solve(board, pieces, obj, since_clear=self.since_clear)
        if not plan.steps:
            self.clock.sleep(0.5)
            return
        think_target = self.sched.think_time(self.block) if self.block else 0.0
        wait = t_obs + think_target - self.now()
        if wait > 0:
            self.clock.sleep(wait)
        self.execute(img, sr, plan, obj, t_obs, think_target, cap_ms)

    # ------------------------------------------------------------ game flow
    def detect_new_game(self, board: int, sr: vision.ScreenRead):
        start = False
        reason = ""
        if self.awaiting_new_game:
            if engine.popcount(board) <= 4:
                start, reason = True, "restart"
        elif not self.game_active:
            start, reason = True, "first_observation"
        elif (self.score is not None and sr.score is not None and sr.score_conf >= self.a.min_score_conf
              and sr.score + 20 < self.score and engine.popcount(board) <= 6):
            if not self.game_end_logged:
                self.log.game("end", self.game_id, final_score=self.score, best_shown=self.best_shown,
                              moves=self.game_moves, reason="score_reset_detected")
            start, reason = True, "score_reset"
        if start:
            self.game_id += 1
            self.game_active = True
            self.game_end_logged = False
            self.awaiting_new_game = False
            self.game_moves = 0
            self.hand_in_game = 0
            self.since_clear = 99
            self.game_uncertain = False
            self.score = sr.score if sr.score_conf >= self.a.min_score_conf else None
            self.expect_new_hand = True
            self.log.game("start", self.game_id, reason=reason, score=sr.score, best_shown=self.best_shown,
                          board=engine.to_hex(board), resumed=self.resumed and self.game_id == 1,
                          phase=self.block.phase if self.block else None,
                          block_label=self.block.label if self.block else None)

    def new_hand(self, pieces: list, board: int, sr: vision.ScreenRead, note: str = ""):
        self.hand_id += 1
        self.hand_in_game += 1
        self.hand_slots = [p.id if p else None for p in pieces]
        self.expect_new_hand = False
        self.move_idx = 0
        n = sum(1 for p in pieces if p)
        self.log.hand(self.hand_id, pieces, board, sr.score, game_id=self.game_id, hand_in_game=self.hand_in_game,
                      best_shown=self.best_shown, partial=n < 3, note=note,
                      slot_conf=[round(s.confidence, 3) for s in sr.slots],
                      board_conf=round(sr.board.confidence, 3),
                      phase=self.block.phase if self.block else None,
                      block_label=self.block.label if self.block else None,
                      block_kind=self.block.kind if self.block else None,
                      elapsed_min=round(self.elapsed_min(), 3), since_clear=self.since_clear,
                      game_moves=self.game_moves)
        if self.a.save_images != "none" and self.hand_id % max(1, self.a.audit_every_hands) == 0:
            self.save_audit(sr, "hand")

    def end_game(self, reason: str):
        if self.game_active and not self.game_end_logged:
            self.log.game("end", self.game_id, final_score=self.score, best_shown=self.best_shown,
                          moves=self.game_moves, hands=self.hand_in_game, reason=reason)
            self.game_end_logged = True
        self.game_active = False
        self.awaiting_new_game = True
        self.expect_new_hand = True

    def handle_nongame(self, img):
        if self.nongame_since is None:
            self.nongame_since = self.now()
            self.last_nongame_img = img
        for name in SAFE_BUTTONS:
            m = vision.match_button(img, self.cal, name, self.a.button_min_score)
            if not m:
                continue
            x, y, score = m
            if name == "decline":
                self.log.event("revive_declined", f"score={score:.2f}", game_id=self.game_id)
            elif name in ("restart", "play", "home_play"):
                self.end_game(f"button_{name}")
                self.log.event("restart_tap", name, score=round(score, 3), game_id=self.game_id)
            else:
                self.log.event("button_tap", name, score=round(score, 3))
            self.save_audit(None, f"button_{name}", img=img)
            self.dev.tap(x, y)
            self.clock.sleep(self.a.after_tap_s)
            self.nongame_since = None
            return
        dt = self.now() - self.nongame_since
        if dt > self.a.stuck_s:
            self.recover(img, "nongame_screen", dt)
        else:
            self.status("Non-game screen (game over / pop-up?), waiting")
            self.clock.sleep(1.0)

    def handle_foreign(self, f: FocusInfo):
        if self.foreign_since is None:
            self.foreign_since = self.now()
            self.foreign_backs = 0
            self.log.event("ad_interrupt", f"{f.package}/{f.activity}", raw=f.raw[:200])
            try:
                self.save_audit(None, "foreign", img=self.dev.screencap())
            except DeviceError:
                raise
            except Exception:
                pass
        dt = self.now() - self.foreign_since
        self.status(f"Focus is not on the game ({f.package}); {dt:.0f} s. Ads are never tapped, waiting.")
        if f.package is None:
            # screen off / lock screen: wake once in a while (no taps)
            if dt > 10 and self.foreign_backs == 0:
                self.dev.keyevent(KEY_WAKEUP)
                self.foreign_backs += 1
        elif dt > self.a.ad_back_after_s and self.foreign_backs < self.a.ad_max_backs:
            if dt > self.a.ad_back_after_s + self.foreign_backs * 10:
                self.dev.back()
                self.foreign_backs += 1
                self.log.event("ad_back", f"BACK #{self.foreign_backs}")
        elif dt > self.a.ad_timeout_s and self.cal.game_package:
            self.log.event("ad_timeout", "relaunching game", duration_s=round(dt, 1))
            self.dev.launch(self.cal.game_package)
            self.foreign_since = self.now()  # restart the timer
            self.foreign_backs = 0
        self.clock.sleep(2.0)

    def recover(self, img, why: str, dt: float):
        """Escalating recovery: screenshot -> BACK -> relaunch -> ask for a human."""
        lvl = self.recovery_level
        if lvl == 0:
            self.log.event("stuck", why, seconds=round(dt, 1))
            self.save_audit(None, "stuck", img=img)
            self.dev.back()
            self.log.event("recover", "BACK")
        elif lvl == 1 and self.cal.game_package:
            self.dev.launch(self.cal.game_package)
            self.log.event("recover", "relaunch")
        else:
            self.status("STUCK: human intervention needed (screen not recognised). Bot is waiting.")
            if lvl == 2:
                self.log.event("needs_human", why)
            self.clock.sleep(30)
        self.recovery_level = min(lvl + 1, 3)
        self.nongame_since = self.now() if lvl < 2 else self.nongame_since
        self.clock.sleep(3.0)

    def check_stuck(self, img, why: str):
        dt = self.now() - self.last_change_t
        if dt > self.a.stuck_s:
            self.recover(img, why, dt)
            self.last_change_t = self.now()

    # --------------------------------------------------------------- moves
    def execute(self, img, sr: vision.ScreenRead, plan, obj: Objective, t_obs: float, think_target: float, cap_ms):
        step = plan.steps[0]
        board = sr.board.board
        piece = engine.get_piece(step.piece_id)
        slot = sr.slots[step.slot]
        start, end = self.cal.drag_points(piece, step.row, step.col, img.shape, slot.center)
        score_before = self.score
        since_before = self.since_clear
        t_start = self.now()
        self.pending_move = {"game_id": self.game_id, "hand_id": self.hand_id, "board_before": engine.to_hex(board),
                             "shape_id": piece.id, "intended_anchor": [step.row, step.col],
                             "_board": board, "_slot": step.slot, "_score_before": score_before,
                             "_since_before": since_before, "_obj": obj.name, "_t_obs": t_obs, "_t_start": t_start,
                             "_think_target": think_target, "_move_idx": self.move_idx}
        self.dev.drag(start[0], start[1], end[0], end[1], self.a.swipe_ms)
        t_drop = self.now()
        img2, frames, settled = vision.wait_settle(self.dev.screencap, self.cal, timeout=self.a.settle_timeout,
                                                   interval=self.a.settle_interval,
                                                   min_stable_s=self.a.settle_min_stable, clock=self.clock)
        t_settled = self.now()
        sr2 = vision.read_screen(img2, self.cal, self.reader)
        predicted = engine.apply_move(board, piece, step.row, step.col)
        if sr2.kind == "game" and sr2.board.board != predicted.board_after and sr2.board.board != board:
            # one re-check: a late banner/animation can still cover the board
            self.clock.sleep(self.a.recheck_s)
            img_b = self.dev.screencap()
            sr_b = vision.read_screen(img_b, self.cal, self.reader)
            frames.append(img_b)
            if sr_b.kind == "game":
                self.log.event("recheck", "board re-read after mismatch",
                               fixed=sr_b.board.board == predicted.board_after)
                img2, sr2 = img_b, sr_b
        # stable score: two equal confident reads
        score_after, score_conf = self.read_stable_score(sr2)
        verified, kind = False, None
        actual_mr = None
        after_board = sr2.board.board if sr2.kind == "game" else None
        if after_board is None:
            kind = "post_move_not_game"
            actual_mr = predicted  # most likely the last move before game over
        elif after_board == predicted.board_after:
            verified, actual_mr = True, predicted
        elif after_board == board and sr2.pieces[step.slot] is not None:
            kind = "drop_failed"
        else:
            actual_mr = engine.find_move(board, after_board, piece)
            kind = "offset_shift" if actual_mr else "placement_mismatch"
        self.handle_verification(kind, verified, step, actual_mr, board, after_board, img2)
        if kind == "drop_failed":
            self.pending_move = None
            return
        mr = actual_mr or predicted
        n_lines = mr.n_lines if actual_mr else None
        delta = None
        if score_after is not None and score_before is not None:
            delta = score_after - score_before
        banner, banner_label, banner_path = False, None, None
        if sr2.kind == "game":
            banner, bcrop, _ = vision.detect_banner(frames, img2, self.cal)
            if banner and bcrop is not None:
                banner_label = vision.classify_banner(bcrop, self.cal.tdir)
                if self.a.save_images != "none":
                    banner_path = self.log.save_image(bcrop, f"banner/{self.hand_id:06d}_{self.move_idx}.jpg", 85)
        shot = None
        if self.a.save_images in ("crops", "all"):
            x0, y0, x1, y1 = self.cal.board_px(img2.shape)
            side = x1 - x0
            box = (0, max(0, int(y0 - 0.5 * side)), img2.shape[1], min(img2.shape[0], int(y1 + 0.5 * side)))
            shot = self.log.save_image(img2, f"moves/{self.hand_id:06d}_{self.move_idx}.jpg", 80, box=box)
        pieces_left = sum(1 for i, s in enumerate(self.hand_slots) if s is not None and i != step.slot)
        self.log.move(
            game_id=self.game_id, hand_id=self.hand_id, move_idx=self.move_idx, slot=step.slot,
            shape_id=piece.id, anchor=list(mr.anchor), intended_anchor=[step.row, step.col],
            board_before=board, board_after=after_board if after_board is not None else predicted.board_after,
            predicted_board_after=predicted.board_after,
            lines_cleared={"rows": list(mr.rows), "cols": list(mr.cols)} if n_lines is not None else None,
            n_lines=n_lines, cells_placed=piece.n, board_cleared=bool(n_lines) and mr.board_after == 0,
            score_before=score_before, score_after=score_after, score_delta=delta, score_conf=round(score_conf, 3),
            banner=banner_label, banner_detected=banner, banner_crop=banner_path, combo_counter=None,
            moves_since_last_clear=since_before, pieces_left=pieces_left, verified=verified, mismatch=kind,
            settled=settled, settle_frames=len(frames), t_obs=t_obs, t_start=t_start, t_drop=t_drop,
            t_settled=t_settled, think_target_ms=round(think_target * 1000), think_ms=round((t_start - t_obs) * 1000),
            decision_ms=round(plan.elapsed_ms, 2), capture_ms=cap_ms, phase=self.block.phase if self.block else None,
            block_label=self.block.label if self.block else None, block_kind=self.block.kind if self.block else None,
            objective=obj.name, plan_value=round(plan.value, 2), fill_before=round(engine.fill_fraction(board), 4),
            elapsed_min=round(self.elapsed_min(), 3), source="agent", screenshot=shot, best_shown=self.best_shown,
            uncertain_state=self.game_uncertain)
        self.pending_move = None
        self._bookkeep(step.slot, n_lines, verified, since_before, obj.name, score_after)
        if (not verified) or (self.a.audit_every and self.moves_total % self.a.audit_every == 0):
            self.save_audit(sr2 if sr2.kind == "game" else None, "mismatch" if not verified else "periodic", img=img2)
        if sr2.kind != "game":
            self.end_game_pending()
        self.status(f"phase={self.block.phase if self.block else '-'} objective={obj.name} score={self.score} "
                    f"last move {piece.id}@{mr.anchor} lines={n_lines} delta={delta}")

    def recover_interrupted(self, board_now: int, sr: vision.ScreenRead):
        """Reconstruct a move whose drag was sent right before a disconnect."""
        it, self.interrupted = self.interrupted, None
        if it["game_id"] != self.game_id:
            return
        b0 = it["_board"]
        piece = engine.get_piece(it["shape_id"])
        if board_now == b0:
            self.log.event("move_interrupted_no_effect", it["shape_id"])
            return
        mr = engine.find_move(b0, board_now, piece)
        if mr is None:
            self.log.event("move_interrupted_unresolved", "board changed but not by that piece")
            self.game_uncertain = True
            self.uncertain_misses = 0
            return
        score_after = sr.score if (sr.score is not None and sr.score_conf >= self.a.min_score_conf) else None
        sb = it["_score_before"]
        delta = score_after - sb if (score_after is not None and sb is not None) else None
        slot = it["_slot"]
        pieces_left = sum(1 for i, s in enumerate(self.hand_slots) if s is not None and i != slot)
        self.log.move(
            game_id=self.game_id, hand_id=self.hand_id, move_idx=it["_move_idx"], slot=slot, shape_id=piece.id,
            anchor=list(mr.anchor), intended_anchor=it["intended_anchor"], board_before=b0, board_after=board_now,
            predicted_board_after=None, lines_cleared={"rows": list(mr.rows), "cols": list(mr.cols)},
            n_lines=mr.n_lines, cells_placed=piece.n, board_cleared=mr.board_after == 0 and mr.n_lines > 0,
            score_before=sb, score_after=score_after, score_delta=delta, score_conf=round(sr.score_conf, 3),
            banner=None, banner_detected=None, banner_crop=None, combo_counter=None,
            moves_since_last_clear=it["_since_before"], pieces_left=pieces_left, verified=True,
            mismatch="recovered_after_disconnect", recovered=True, t_obs=it["_t_obs"], t_start=it["_t_start"],
            think_target_ms=round(it["_think_target"] * 1000), think_ms=round((it["_t_start"] - it["_t_obs"]) * 1000),
            phase=self.block.phase if self.block else None, block_label=self.block.label if self.block else None,
            block_kind=self.block.kind if self.block else None, objective=it["_obj"],
            fill_before=round(engine.fill_fraction(b0), 4), elapsed_min=round(self.elapsed_min(), 3),
            source="agent", best_shown=self.best_shown, uncertain_state=self.game_uncertain)
        self._bookkeep(slot, mr.n_lines, True, it["_since_before"], it["_obj"], score_after)

    def _bookkeep(self, slot: int, n_lines, verified: bool, since_before: int, obj_name: str, score_after):
        if self.game_uncertain:
            # every candidate model has reset its combo after 6 consecutive misses
            self.uncertain_misses = self.uncertain_misses + 1 if (n_lines == 0 and verified) else 0
            if self.uncertain_misses >= 6:
                self.game_uncertain = False
                self.log.event("state_resynced", "6 non-clearing moves after an uncertainty")
        if n_lines:
            self.since_clear = 0
        elif n_lines == 0:
            self.since_clear = min(99, self.since_clear + 1)
        if obj_name.startswith("probe") and n_lines is not None:
            self.probes.record(n_lines, since_before)
        if score_after is not None:
            self.score = score_after
        self.hand_slots[slot] = None
        self.move_idx += 1
        self.moves_total += 1
        self.game_moves += 1
        self.last_change_t = self.now()
        if all(s is None for s in self.hand_slots):
            self.expect_new_hand = True

    def end_game_pending(self):
        # the game-over / revive screen will be handled by handle_nongame on the next tick
        self.nongame_since = None

    def read_stable_score(self, sr2: vision.ScreenRead) -> tuple:
        if sr2.kind != "game":
            return None, 0.0
        last = sr2.score if sr2.score_conf >= self.a.min_score_conf else None
        conf = sr2.score_conf
        for _ in range(self.a.score_reads):
            self.clock.sleep(self.a.score_interval)
            img = self.dev.screencap()
            v, c, _ = vision.read_number(img, self.cal, self.reader, "score")
            if v is not None and c >= self.a.min_score_conf and v == last:
                return v, min(conf, c)
            last, conf = (v if c >= self.a.min_score_conf else None), c
        if last is None:
            self.log.event("ocr_uncertain", "score not stable/confident", conf=round(conf, 3))
        return last, conf

    def handle_verification(self, kind, verified, step, actual_mr, board, after_board, img2):
        if verified:
            self.drop_failures = 0
            if not self.cal.offset_verified:
                self.cal.offset_verified = True
                self.log.event("offset_verified", f"finger_offset_cells={self.cal.finger_offset_cells}")
                self.persist_cal()
            return
        if kind == "drop_failed":
            self.drop_failures += 1
            self.log.event("drop_failed", f"{step.piece_id}@{(step.row, step.col)}", count=self.drop_failures)
            if not self.cal.offset_verified or self.drop_failures >= 3:
                self.offset_try = (self.offset_try + 1) % len(OFFSET_CANDIDATES_DY)
                self.cal.finger_offset_cells = [0.0, OFFSET_CANDIDATES_DY[self.offset_try]]
                self.cal.offset_verified = False
                self.log.event("offset_try", f"finger_offset_cells={self.cal.finger_offset_cells}")
            return
        if kind == "offset_shift" and actual_mr is not None:
            dr = actual_mr.anchor[0] - step.row
            dc = actual_mr.anchor[1] - step.col
            old = list(self.cal.finger_offset_cells)
            self.cal.finger_offset_cells = [old[0] + dc, old[1] + dr]
            self.cal.offset_verified = False
            self.log.event("offset_corrected", f"shift=({dr},{dc})", old=old, new=self.cal.finger_offset_cells)
            self.persist_cal()
            return
        if kind == "placement_mismatch":
            self.log.event("placement_mismatch", "board after move matches no placement of the piece",
                           board_before=engine.to_hex(board),
                           board_after=engine.to_hex(after_board) if after_board is not None else None,
                           piece=step.piece_id, intended=[step.row, step.col])

    def persist_cal(self):
        if self.cal_path:
            try:
                self.cal.save(self.cal_path)
            except OSError:
                pass

    def save_audit(self, sr: Optional[vision.ScreenRead], why: str, img=None):
        if self.a.save_images == "none":
            return
        if img is None:
            try:
                img = self.dev.screencap()
            except Exception:
                return
            sr = vision.read_screen(img, self.cal, self.reader)
        name = f"audit/{self.moves_total:06d}_{why}.png"
        path = self.log.save_image(img, name)
        rec = {"why": why, "path": path, "game_id": self.game_id, "hand_id": self.hand_id}
        if sr is not None and sr.kind == "game":
            rec.update(parsed_board=engine.to_hex(sr.board.board), board_conf=round(sr.board.confidence, 3),
                       parsed_pieces=[p.id if p else None for p in sr.pieces], parsed_score=sr.score,
                       score_conf=round(sr.score_conf, 3), parsed_best=sr.best)
        else:
            rec["parsed_kind"] = sr.kind if sr is not None else None
        self.log.audit(**rec)


# ----------------------------------------------------------------------------
# Setup helpers
# ----------------------------------------------------------------------------

def setup_fake_calibration(dev: FakeDevice, out_dir: Path, offset_error: float = 0.0) -> vision.Calibration:
    """Calibrate on the fake device exactly like a user would on the phone."""
    tdir = out_dir / "fake_templates"
    img = dev.screencap()
    cal = vision.auto_calibrate(img, best_px=(60, 200, 340, 320), base=vision.Calibration(template_dir=str(tdir)))
    cal.game_package = dev.package
    reader = vision.DigitReader(cal.tdir)
    box = cal.px_box(cal.score, img.shape)
    for v in (12345, 67890):
        reader.learn(vision.crop(dev.render_score_sample(v), box), v)
    for screen, name in (("revive", "decline"), ("gameover", "restart")):
        vision.add_button(dev.render_screen(screen), cal, name, dev.button_boxes[name])
    cal.finger_offset_cells = [dev.true_offset[0], dev.true_offset[1] + offset_error]
    cal.offset_verified = offset_error == 0.0
    return cal


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m bot.run_session", description="Block Blast data collection bot")
    ap.add_argument("--minutes", type=float, default=60.0)
    ap.add_argument("--mode", choices=["play", "probe-scoring", "mixed"], default="play")
    ap.add_argument("--speed", choices=["fast", "slow", "abba", "normal"], default="normal")
    ap.add_argument("--phases", help="phase scheduler JSON/YAML file (replaces mode/speed)")
    ap.add_argument("--block-minutes", type=float, default=10.0)
    ap.add_argument("--think", nargs=2, type=float, metavar=("LO", "HI"),
                    help="per-move thinking time range in seconds, for all phases")
    ap.add_argument("--device", choices=["adb", "fake", "folder"], default="adb")
    ap.add_argument("--serial")
    ap.add_argument("--adb-path")
    ap.add_argument("--drag-method", choices=["swipe", "motionevent", "draganddrop"], default="swipe")
    ap.add_argument("--folder", help="screenshot folder for --device folder")
    ap.add_argument("--calibration", default=str(vision.DEFAULT_CAL))
    ap.add_argument("--logs", default=str(DEFAULT_LOG_DIR))
    ap.add_argument("--session-id")
    ap.add_argument("--resume", metavar="SESSION_ID")
    ap.add_argument("--resume-last", action="store_true")
    ap.add_argument("--stop-file", default=None, help="default: <logs>/STOP")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--save-images", choices=["none", "crops", "all"], default="crops")
    ap.add_argument("--audit-every", type=int, default=50, help="save a raw screenshot every N moves")
    ap.add_argument("--audit-every-hands", type=int, default=100)
    ap.add_argument("--rotate-minutes", type=float, default=60.0)
    ap.add_argument("--checkpoint-s", type=float, default=60.0)
    ap.add_argument("--health-s", type=float, default=180.0)
    ap.add_argument("--max-temp", type=float, default=42.0)
    ap.add_argument("--temp-hysteresis", type=float, default=3.0)
    ap.add_argument("--min-battery", type=int, default=15)
    ap.add_argument("--min-brightness", type=int, default=None, help="0-255; the previous value is restored on exit")
    ap.add_argument("--swipe-ms", type=int, default=300)
    ap.add_argument("--settle-timeout", type=float, default=4.0)
    ap.add_argument("--settle-interval", type=float, default=0.15)
    ap.add_argument("--settle-min-stable", type=float, default=0.4)
    ap.add_argument("--recheck-s", type=float, default=0.8)
    ap.add_argument("--score-reads", type=int, default=3)
    ap.add_argument("--score-interval", type=float, default=0.2)
    ap.add_argument("--min-score-conf", type=float, default=0.6)
    ap.add_argument("--button-min-score", type=float, default=0.85)
    ap.add_argument("--after-tap-s", type=float, default=2.0)
    ap.add_argument("--stuck-s", type=float, default=60.0)
    ap.add_argument("--ad-back-after-s", type=float, default=35.0)
    ap.add_argument("--ad-max-backs", type=int, default=3)
    ap.add_argument("--ad-timeout-s", type=float, default=150.0)
    ap.add_argument("--reconnect-wait-s", type=float, default=600.0)
    ap.add_argument("--network", choices=["online", "offline", "unknown"], default="unknown")
    ap.add_argument("--profile", choices=["real", "fresh", "unknown"], default="unknown")
    # fake device options
    ap.add_argument("--fast-clock", action="store_true", help="fake: virtual clock (runs instantly)")
    ap.add_argument("--fake-ads", type=float, default=0.0)
    ap.add_argument("--fake-disconnects", type=float, default=0.0)
    ap.add_argument("--fake-offset-error", type=float, default=0.0)
    ap.add_argument("--fake-edge-overlay", action="store_true")
    return ap


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")  # Windows consoles without UTF-8
    except (AttributeError, ValueError):
        pass
    a = build_parser().parse_args(argv)
    logs = Path(a.logs)
    logs.mkdir(parents=True, exist_ok=True)
    if a.stop_file is None:
        a.stop_file = str(logs / "STOP")
    if Path(a.stop_file).exists():
        Path(a.stop_file).unlink()  # stale flag from a previous run
    checkpoint = None
    session_id = a.session_id
    if a.resume_last and not a.resume:
        cks = sorted(logs.glob("*/checkpoint.json"), key=lambda p: p.stat().st_mtime)
        if cks:
            a.resume = cks[-1].parent.name
    if a.resume:
        checkpoint = load_checkpoint(logs / a.resume / "checkpoint.json")
        if not checkpoint:
            print(f"checkpoint not found: {a.resume}")
            return 2
        session_id = a.resume
    seed = a.seed if a.seed is not None else random.randrange(1 << 30)
    if checkpoint and "seed" in (checkpoint.get("args") or {}):
        seed = checkpoint["args"]["seed"]
    clock = VirtualClock() if (a.device == "fake" and a.fast_clock) else RealClock()
    part = (checkpoint.get("part", 0) + 1) if checkpoint else 0
    logger = SessionLogger(logs, session_id, a.rotate_minutes, clock=clock, part=part,
                           fsync=not (a.device == "fake" and a.fast_clock))
    cal_path: Optional[Path] = Path(a.calibration)
    if a.device == "fake":
        dev = FakeDevice(seed=seed, clock=clock, ad_prob=a.fake_ads, disconnect_prob=a.fake_disconnects,
                         edge_overlay=a.fake_edge_overlay)
        cal = setup_fake_calibration(dev, logger.dir, a.fake_offset_error)
        cal_path = logger.dir / "calibration_fake.json"
        cal.save(cal_path)
    else:
        if not cal_path.exists():
            print(f"calibration file missing: {cal_path}. Follow the calibration steps in the README first.")
            return 2
        cal = vision.Calibration.load(cal_path)
        if a.device == "folder":
            dev = FolderDevice(a.folder, package=cal.game_package or "folder.game")
            cal_path = None  # never write calibration in parse-only runs
        else:
            from .capture import AdbDevice
            dev = AdbDevice(a.serial, a.adb_path, drag_method=a.drag_method)
    if a.phases:
        config = experiments.load_config(a.phases)
    else:
        config = experiments.preset_config(a.mode, a.speed, a.block_minutes, a.minutes)
    if a.think:
        for ph in config.get("phases", []):
            ph["think_s"] = list(a.think)
    a.seed = seed
    sched = experiments.PhaseScheduler(config, a.minutes, seed)
    sess = Session(dev, cal, a, logger, clock, sched, cal_path, checkpoint)

    def _sig(signum, frame):
        sess.stop_requested = True
        print("\nstopping (after the current move)...")

    try:
        signal.signal(signal.SIGINT, _sig)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, _sig)
    except ValueError:
        pass  # not in main thread (tests)
    print(f"session {logger.session} -> {logger.dir}")
    sess.run()
    logger.close()
    print(f"done: {sess.moves_total} moves, {sess.game_id} games. Logs: {logger.dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
