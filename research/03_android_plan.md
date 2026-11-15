# Block Blast! Android fallback: capture and reverse-engineering plan

Status: planning only. Nothing was installed or changed. Date of check: 2026-11-16.
Evidence tags: [V] verified locally or from a fetched source, [K] general knowledge, not re-verified here, [U] unverified or uncertain.

## 1. Local readiness check (read-only)

| Item | Result |
|---|---|
| OS | Windows 11 Home Single Language 10.0.26300 |
| CPU / RAM | i7-13650HX (13th gen), 16 GB RAM |
| GPU | Intel UHD + NVIDIA RTX 4050 Laptop |
| Free disk | C: 25 GB, E: 185 GB (F/G/H: 23-34 GB). Put SDK, AVD and logs on E:. |
| adb | NOT FOUND |
| scrcpy | NOT FOUND |
| ffmpeg | NOT FOUND |
| tesseract | NOT FOUND |
| Android Studio / SDK / .android | None of these folders exist |
| BlueStacks / Genymotion folders | Absent |
| Java | Only Java 8 (Oracle java8path). Not needed unless Android Studio is used, which bundles its own JBR. |
| Python | 3.14.5 at C:\Python314 |
| Python packages | numpy 2.4.6, pandas 3.0.3, pillow 12.3.0. No opencv, pytesseract, easyocr, adbutils, scipy or torch. |
| Virtualization | `VirtualizationFirmwareEnabled=False` but `HypervisorPresent=True`; systeminfo says "A hypervisor has been detected" and VBS (virtualization-based security) is Running. |

Interpretation of the virtualization result:
- A running hypervisor means VT-x is already enabled in firmware. The `False` flag from WMI is the usual artefact when Windows already owns the hypervisor. [K]
- Hyper-V Requirements lines are hidden because of this. The optional-feature query (HypervisorPlatform / VirtualMachinePlatform) needs admin, so it could not be read. [V]
- Windows 11 Home has no full Hyper-V role but does offer the "Windows Hypervisor Platform" and "Virtual Machine Platform" optional features. [K]
- The Android emulator can use WHPX through Windows Hypervisor Platform. A Google issue tracker and Microsoft MAUI docs confirm the mechanism; Home-edition reliability is reported inconsistently. [V/U]
- The definitive test is `emulator -accel-check` after the SDK is installed (read-only).

Python 3.14 is very new. opencv-python, numpy and Pillow have 3.14 wheels or will soon. easyocr/torch are risky on 3.14. Recommendation: create a separate venv with Python 3.12 (user installs via python.org or `py` launcher) if any package fails. Prefer template-matched digits, which need only numpy and opencv. [U]

## 2. Device options compared

| Option | ADB | Win11 Home | Hyper-V/WHPX conflict | Verdict |
|---|---|---|---|---|
| (a) Physical Android phone, USB ADB | Native, most reliable. Wireless debugging (Android 11+) is also possible. | Fine | None | **Primary** if user has a phone. Real device means real touch, real ads, no emulator detection, native frame rate. |
| (b) Android Studio AVD, `google_apis_playstore` image | Native adb | Works via WHPX if the feature is enabled (may need a reboot and admin by the user) | Uses WHPX, and Google notes WHPX can break other emulators | **Backup** if no phone. Official and no third-party installers. Heavy (about 10-15 GB). |
| (c) BlueStacks 5, LDPlayer 9, MuMu Player | All expose ADB (enable in settings; BlueStacks 127.0.0.1:5555 and following ports, LDPlayer ships its own adb, MuMu 127.0.0.1:16384 or 7555). [K/U] | Works | BlueStacks and LDPlayer can use Hyper-V mode or their own; coexistence with VBS or WHPX is finicky | Third-party installers carry bloatware and ads. Ads in the emulator frontends and emulator-detection are likelier. Use only if (a) and (b) fail. |
| (c') Google Play Games on PC | Developer emulator has ADB (`adb connect localhost:6520`, bundled adb). [V] | Needs Hyper-V-class features and a supported setup. [U] | Yes | Only titles in the PC catalogue run; whether Block Blast is listed is not confirmed. Not recommended. |
| (d) Windows Subsystem for Android | Had ADB | n/a | n/a | **Dead.** Support ended 2025-03-05, and the Amazon Appstore is gone. [V] Do not plan on it. |

Recommended (CONFIRMED, user has a phone; see Section 11): (a) physical phone with USB ADB. Backup (brief): (b) AVD with Play image, 1080x1920-ish portrait, API 34 or 35, x86_64, 4 GB RAM, running on E:.

Constraint stated in the brief: the app must come from Google Play and be installed by the user, who also does the Play sign-in. Throwaway Google account recommended for an AVD.

## 3. Data capture

Capture options:
- `adb exec-out screencap -p`: PNG over the pipe, no scrcpy needed. Typically about 0.3-1.5 s per frame on a phone (the PNG encode on-device dominates). Third-party docs claim 500-1500 ms. [V/U] Fine for a turn-based puzzle: the game is static between moves.
- Raw variant: `adb exec-out screencap` (no `-p`) returns raw RGBA with a 12-byte header. This is faster because there is no PNG encode/decode. Preferred for the pipeline. [K]
- scrcpy (user downloads it from the official Genymobile GitHub release): H.264 stream, 30-60 fps, <50 ms latency. `--record=file.mkv` gives video for post-hoc timing analysis. scrcpy also offers `--no-control`, so recording a human's play is safe. [V/K]
- Recommended mix: scrcpy running in a window and recording to .mkv for the human-plays case (timing ground truth, 60 fps), plus periodic raw screencap for state. For the agent-plays case use raw screencap only.
- Windows desktop computer-use on the scrcpy or emulator window is possible but slower and noisier than ADB (screenshot to vision-LLM round trips of 3-10 s each). It makes sense only as a last resort when ADB input is blocked.

Input injection:
- `adb shell input swipe x1 y1 x2 y2 <duration_ms>` works for most drag-and-drop puzzles. Block Blast picks the piece on touch-down and places it on release, with the piece drawn above the finger. Use durations of 150-400 ms. Very short swipes sometimes register as a tap. [K/U]
- If swipe is unreliable: `adb shell input draganddrop x1 y1 x2 y2 <ms>` (Android 7+), or `sendevent` to /dev/input/eventN (needs per-device parsing but works without root on many phones; usually shell-readable), or `input motionevent DOWN/MOVE/UP x y` (Android 12+). [K]
- Each `adb shell` call costs about 50-150 ms start-up. For lower overhead keep one persistent `adb shell` via adbutils or `adb -s ... shell` over a pipe. [K]
- Also read `wm size` and `wm density` once. Screen resolution defines all pixel calibration.

## 4. State extraction pipeline (Python)

Libraries: `opencv-python`, `numpy`, `adbutils` (pure Python, maintained, supports raw screencap and shell), plus `pytesseract` only if the user installs Tesseract. Preferred: template-matched digits for the score (no heavy deps), with `easyocr` as an offline fallback. `pandas` for analysis.

Pipeline per frame:
1. **Calibration (once)**: take a screenshot of the empty board. Detect the board square by finding the largest near-square region with the grid colour, or have the user confirm four corner points. Store `board_x0, board_y0, cell` (cell size is about board_width/8). Calibrate the tray slot centres (three slots) the same way.
2. **Board**: for each of 64 cells, sample the median colour of the central 40% patch. Classify empty versus filled by distance to the empty-cell colour (background is a dark blue-grey, filled cells are saturated block colours). Output an 8x8 binary mask plus optional colour index. Debug: save overlay images when ambiguous.
3. **Tray**: for each of three slots, crop the slot region and threshold on saturation to get the piece mask, find the connected components, and compute the bounding box. Divide the bounding box by the tray cell size (the tray cell is smaller than the board cell, scale about 0.5-0.6 and must be calibrated from a known 1x1 piece). Produce a minimal 2D boolean matrix, then map to the canonical shape id by lookup (about 40 poses including 1x1, 1x2..1x5, 2x2, 3x3, 2x3, 3x2, L/J variants of 2x2 and 3x3, S/Z, T, plus-less variants). A piece slot may be empty after being placed: detect it when saturation is below threshold.
4. **Score**: crop the score region (top centre, large digits). Use template matching on digit glyphs collected from the first few screenshots (user confirms), or Tesseract with `--psm 7 -c tessedit_char_whitelist=0123456789`. The score animates (count-up), so read only after it is stable (two consecutive reads equal, with 150-300 ms spacing).
5. **Banners**: combo/praise banners ("Good", "Great", "Excellent", "Amazing", "Unbelievable", "Perfect", with a combo counter) are large centred text that appears for about 1 s. Detect by a frame-difference in the central band between the board and the tray, then capture the crop and classify by template or OCR. Because the banner is transient, use scrcpy video for timing and pull the first 1-2 s after each placement via fast raw screencap bursts (about 3-4 captures at 300 ms).
6. **Game over / ads**: detect the game-over panel and the revive prompt via a colour/template signature. Detect interstitials by the absence of the board (the board-calibration fit fails), then pause the agent, log an event `ad_interrupt`, and wait or alert the user. Never auto-click ads.

Placement verification: after each drag, re-capture and verify that exactly the expected cells changed (piece cells added, full lines cleared). If the delta disagrees, log `placement_mismatch` and re-sync rather than assuming.

## 5. JSONL log schema

One line per **placement event** (plus separate lines per hand and per event):

```json
{"type":"hand","session":"s01","hand_id":42,"t":1731700000.123,
 "pieces":[{"slot":0,"shape_id":"L3_a","cells":[[0,0],[1,0],[2,0],[2,1]]},{"slot":1,"shape_id":"1x1","cells":[[0,0]]},{"slot":2,"shape_id":"sq2","cells":[[0,0],[0,1],[1,0],[1,1]]}],
 "board":"0000000000100000...(64 chars)","score":1840,"pb_known":9050,"mode":"classic"}

{"type":"move","session":"s01","hand_id":42,"move_idx":0,"t_start":1731700001.0,"t_drop":1731700001.6,"t_settled":1731700003.1,
 "slot":0,"shape_id":"L3_a","anchor":[5,2],
 "board_before":"...64...","board_after":"...64...","lines_cleared":{"rows":[3],"cols":[]},"n_lines":1,
 "cells_placed":4,"score_before":1840,"score_after":1864,"score_delta":24,
 "banner":"Great","combo_counter":2,"moves_since_last_clear":0,
 "think_ms":900,"capture_ms":320,"source":"agent|human","screenshot":"img/s01_0042_0.png"}

{"type":"event","session":"s01","t":..., "kind":"ad_interrupt|game_over|revive_prompt|placement_mismatch|app_restart|ocr_uncertain","detail":"..."}
```

Also store per-session metadata: device model, resolution, app version, Android version, whether the personal best is known/visible, and the time since session start. Keep the PNG of every post-move frame (a few hundred KB each) so any OCR error can be corrected offline; about 1.5 hands/min x 3 x 0.3 MB is roughly 5-8 GB/hour for PNG, so store JPEG q90 crops (board, score, tray) instead, around 100 KB per move.

## 6. Non-invasive extra signals

- `adb logcat -v threadtime`: Unity-based games typically log little. Expect ad SDK chatter (Applovin, Unity ads, Google Mobile Ads), `ActivityManager` lifecycle lines, and `Unity` lines. Do not expect scoring or piece-generation data. Capture it to file (`logcat -b all`) during the pilot session and grep for anything useful; costs nothing. [K/U]
- `dumpsys activity activities | grep -i resumed` and `dumpsys gfxinfo <pkg> framestats` give foreground-app confirmation and frame timing, useful for detecting ads and app switches. `dumpsys window | grep mCurrentFocus` detects ad activities (a different focused activity or package) which is a cleaner ad detector than image analysis. [K]
- `dumpsys netstats` or `dumpsys connectivity` only show traffic volume, which is enough to note when the game is talking to a server around hand generation.
- **Out of scope (not to be done):** certificate-pinning bypass, traffic MITM, APK modification or repackaging, root, Frida or Xposed hooking, memory reading. These bypass protections and are excluded.

## 7. Experiment protocol

Priority questions: (A) the scoring formula, (B) the piece distribution, (C) adaptivity.

### A. Scoring hypotheses
Always record `score_delta` after the score has stopped animating.

1. **Placement-only baseline**: place a piece that clears nothing. Expect delta = cells placed (1 pt per cell) if that hypothesis holds. Repeat 20 times over several piece sizes (1, 2, 3, 4, 5, 9 cells).
2. **Single-line clear**: set up a row with 8 minus n filled cells, place the matching piece. Compare delta against (n) + 10 vs other candidates. Repeat 10 times for rows and 10 for columns.
3. **Multi-line clears (2, 3, 4 lines at once)**: use rows, columns and a mixed row+column cross. Required to separate 10·l·(l-1) from 10·l² from linear. Needs about 8-10 samples per l value (l=2,3,4, maybe 5-6 if lucky).
4. **Combo survival**: after a clear, make k = 0, 1, 2, 3, 4 non-clearing moves, then clear again, and record the combo counter and delta. The test shows when the combo counter resets. At least 6 samples per k.
5. **Board clear**: usually rare; take opportunistic samples and log them. Do not engineer more than a few attempts.
6. Controlled setup is feasible by the agent or human choosing placements that build lines; the hand is random, so the pipeline should simply label every clear event from natural play, then fit a regression on features (`cells_placed`, `n_lines`, `combo_counter`, `moves_since_clear`). The designed experiments above guide play when a cell is under-sampled. Fit by exact-integer enumeration, since scores are integers.

### B. Piece distribution
- About 40 poses, so 39 degrees of freedom for a chi-square goodness-of-fit against uniform. Rule of thumb: expected count >= 5 per cell, so at least 200 pieces; for power to detect a 30-50% deviation from uniform on a single pose, about 1000+ pieces. Target: **1000-1500 pieces = 350-500 hands**.
- If the generator is conditional on the board (adaptive), compare marginals unconditioned versus conditioned on board fill % and on "a piece fits" features; this needs more data (about 3000+ pieces) and logistic or multinomial models.
- Also test within-hand structure: independence of the three pieces (a 3-way contingency table is sparse, so use pairwise chi-square or a permutation test), duplicates within a hand, and whether at least one piece is always placeable (solvability guarantee), measured from boards.
- The game's initial hand and early hands are likely scripted. Log the first 10 hands of each fresh run separately.

### C. Adaptivity and speed
- Fast block (decide < 2 s per move) versus slow block (> 8 s per move), alternating in 10-minute segments in randomised order (ABBA design) to control for time-since-start drift.
- Compare piece-size distribution, proportion of "helpful" pieces (a piece that completes a line), and solvability between blocks, using a permutation test.
- Personal-best proximity: record the score relative to PB. If the PB is visible on screen, store it. Compare hand statistics when score < 50% PB versus > 90% PB. Fresh installs have PB = 0, which is a confound (everything counts as near best), so note that the user's own profile and a fresh profile differ.
- An A/B-test trait possibly served by the server cannot be seen locally. Record device id hash and install date only in metadata, and note that results may not generalise.

### Who plays: agent versus human

| Mode | Hands/hour | Notes |
|---|---|---|
| AI agent loop (screencap -> parse -> solver -> 3 swipes) | Raw screencap about 0.5-1 s and parse about 0.1 s; 3 placements each requiring an animation settle of 1-2 s. Roughly 12-20 s per hand if the decision is a local solver (no LLM), so **180-300 hands/h**. If an LLM decides every move through screenshot vision, 20-40 s per move, 60-120 s per hand, **30-60 hands/h**. | Use a local heuristic solver, not a vision LLM, for the data run. The agent also gives precise, controllable speed (needed for experiment C). Games end: log and restart takes extra time and may involve ads. |
| Human plays, pipeline records (scrcpy + screencap burst) | 1 hand takes 10-25 s for a casual player, **150-300 hands/h** | More natural piece generation and no automation concern. But the human attention is expensive and the pipeline must handle fast play. |

Hence one hour of agent play with a local solver yields about 200-300 hands, which falls just short of the 350-500 hand target; plan for **about 2 hours total** or accept lower power for the conditional analyses. Even the pessimistic LLM-driven mode (about 50 hands/h) gives too little data and is only suitable for the scoring probes in A.

## 8. Setup steps

### User must do
1. Choose the device: phone (recommended) or enable the AVD route.
2. Phone path: enable Developer options, USB debugging; connect the cable; accept the RSA prompt; keep the screen on (`svc power stayon usb` is an adb command the agent can run, or the "Stay awake" developer setting).
3. Install Block Blast! from Google Play **themselves**, signed in with their own account (throwaway recommended). Complete first-run prompts and any consent or privacy dialogs, and decline personalised ads if possible.
4. Download and unzip official tools from their official pages when approved: Android platform-tools (adb) and scrcpy (Genymobile GitHub releases). Optionally Tesseract. The agent must not download them without explicit approval.
5. AVD path only: install Android Studio, turn on Windows Hypervisor Platform and Virtual Machine Platform (admin, reboot), create an AVD with a Play Store image, sign into Google Play inside it, install the game.
6. Decide whether to play with the real profile (existing PB) or a fresh one, and consider turning off sound and haptics.
7. Disable auto-rotate, set the screen timeout to maximum, enable Do Not Disturb to avoid notifications that cover the board.

### Agent can do (after the user approves the downloads)
1. `adb devices`, `adb shell wm size`, `getprop` to record device metadata.
2. Create a Python 3.12 venv on E:, install `opencv-python numpy adbutils pandas` (and optionally pytesseract or easyocr).
3. Write `capture.py` (raw screencap), `calibrate.py`, `extract.py` (board/tray/score/banner), `solver.py` (simple greedy line-clearing heuristic with a speed parameter), `driver.py` (swipe injection and verification), `log.py` (JSONL writer), and analysis notebooks/scripts.
4. Run the swipe reliability test (50 placements) and the OCR accuracy audit against manually labelled frames.
5. Run the pilot session (10 minutes) and review logs including `logcat` for useful lines.
6. Run the scoring probes and the main data session, then do the statistics.

## 9. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Ads and interstitials (shown between games, banner ads, rewarded revive prompts) | Interrupt the loop, can block clicks, and can shift the layout | Detect via `mCurrentFocus`/the board-fit check, pause and notify; never auto-click ad content; the user can close ads. Airplane mode often suppresses ads but may disable features (the game works offline) and could change generation if it is server-driven, so treat as a separate condition. |
| Terms of Service | Automation or botting may breach the game's ToS and Google Play rules; account ban risk | Use a throwaway account; keep the session short; no cheating, no leaderboards, no purchases; the user decides. Human-plays mode is lowest risk. |
| Play Store sign-in | Must be done by the user; the agent never handles credentials | User-only step above. |
| Account and progress | The PB state affects the adaptive hypothesis; real progress is changed by the sessions | Choose fresh versus real profile deliberately, log it. |
| Emulator detection | Some games degrade or show different ads on emulators | The physical phone avoids this. |
| Hypervisor conflicts | Enabling WHPX/Hyper-V can break BlueStacks/LDPlayer and, with VBS already running, performance may drop | Avoid mixing emulators; test with `emulator -accel-check`. |
| Unreliable swipe | Pieces dropped in the wrong cell, because of finger offset | Calibrate the finger-offset (the piece renders above the touch point); verify each placement, log mismatches. |
| OCR errors | Corrupted scoring fit | Store crops, double-read, and audit with a labelled set; use integer-consistency checks. |
| Data volume and disk | C: has only 25 GB free | Keep everything on E:. |
| Python 3.14 package compatibility | Install failures | Use a 3.12 venv. |
| Adaptive generation confounds | Speed, PB and time-of-day can all change the distribution | Randomised block design, metadata logging, a larger sample. |
| Phone battery and thermal | Long sessions throttle the phone | Keep it charging, with the screen at low brightness. |

## 10. Time and cost estimate

Costs are zero money: all tools are free (Android Studio, platform-tools, scrcpy and Tesseract are free); the only cost is the phone and optionally a cable.

| Phase | Effort |
|---|---|
| User setup (phone path) | 20-40 min (AVD path: 1.5-3 h including downloads and reboot) |
| Agent: tooling and venv | 20 min |
| Agent: calibration, extractor, driver, solver coding and debugging | 2-4 h |
| Pilot and fixes | 30-60 min |
| Scoring probes (A) | 30-45 min |
| Main data collection | 60-120 min (200-300 hands/h with the local solver) |
| Analysis | 1-2 h |
| **Total** | **about 6-10 h of work, 1.5-3 h of wall time of actual play** |

## Sources

- Windows Subsystem for Android end of support: https://www.windowscentral.com/software-apps/windows-11/microsoft-is-killing-support-for-running-android-apps-on-windows-11 and https://www.theregister.com/2024/03/05/windows_subsystem_android/
- WHPX and Android emulator acceleration: https://learn.microsoft.com/dotnet/maui/android/emulator/hardware-acceleration and the Google issue tracker threads such as https://issuetracker.google.com/issues/189877657
- Google Play Games on PC developer emulator and ADB: https://developer.android.com/games/playgames/pg-emulator
- scrcpy behaviour and screencap latency (third-party figures, rough): https://glama.ai/mcp/servers/@jduartedj/android-mcp-server/blob/56a9fde42b0fc8aa19d52feaf3b762c12861657b/docs/SCRCPY_STREAMING.md and https://man.archlinux.org/man/scrcpy.1.en
- BlueStacks, LDPlayer and MuMu ADB ports, Block Blast availability on Google Play Games PC, and the logcat content of the app were not confirmed by search and are tagged [K]/[U].

## 11. Addendum: physical phone is CONFIRMED as the primary path

The user has a physical Android phone and can connect it over USB. This supersedes the emulator-first wording above: **primary = phone over USB ADB** (Wi-Fi ADB optional). AVD, BlueStacks and the other emulators stay as a brief backup only.

### 11.1 User steps on the phone
1. Settings > About phone > tap "Build number" 7 times (enter the PIN). Developer options then appears (under Settings > System, or Additional settings, depending on brand).
2. In Developer options turn on **USB debugging**. Optionally turn on **Stay awake** (screen stays on while charging). On Xiaomi/HyperOS, Oppo, Vivo and some Samsung builds also enable "USB debugging (Security settings)" if `adb shell input` later fails with an INJECT_EVENTS permission error. [K]
3. Set USB mode to "File transfer / MTP" (some phones expose ADB only in that mode). Use a data-capable cable in a direct port, not a hub.
4. Connect to the PC. When "Allow USB debugging?" shows the PC's RSA fingerprint, tick "Always allow from this computer" and tap Allow. Only the user does this authorisation.
5. Install **Block Blast!** (Hungry Studio) from Google Play, signed in by the user. Open it once, accept the consent prompts, finish any tutorial, and reach the Classic-mode board. Tell the agent the app version, whether a personal best is displayed, and whether the profile is fresh or real.
6. Recommended phone settings during sessions:
   - Screen timeout at the longest value (or Stay awake while charging); brightness low to medium; phone plugged in.
   - Do Not Disturb on, notifications silenced, auto-rotate off (portrait), no floating overlays (screen recorders, chat heads, battery savers), battery saver off.
   - Developer options "Show taps" and "Pointer location" off (they would corrupt screenshots). Keep display size and font size at default and do not change them mid-study.
   - Screen unlocked, no app lock on the game.
7. Airplane mode versus ads and server traits: ads and any server-driven A/B traits need the network. Offline (airplane mode, Wi-Fi off) probably removes ads and freezes server-side configuration. That makes it useful as a **separate controlled condition**, but it may also change generation. Plan: run the main data online (the real condition) with ads handled by pause-and-notify, then run a smaller offline block (about 100 hands) for comparison. Log `network: online|offline` in the metadata. The user should check once that the game starts offline. [U]
8. Tell the agent the phone model, the Android version, and whether Wi-Fi ADB is wanted (Android 11+, Developer options > Wireless debugging). USB is the default; Wi-Fi ADB is slower and flakier for screencap.

### 11.2 What the agent must install on the PC (list only; nothing installed yet; each needs user approval)
- **Android platform-tools** (adb) from Google's official download page, unzipped to e.g. E:\tools\platform-tools, called by full path or added to PATH for the session. An OEM USB driver is needed only if `adb devices` shows nothing (Windows 11 usually uses its generic driver).
- **scrcpy** (official Genymobile GitHub release; it bundles its own adb, so keep the versions consistent). Used for video recording and live view with `--no-control` during human play.
- **Python 3.12 venv** (the system has 3.14.5 with numpy, pandas and pillow). Packages: `opencv-python`, `adbutils`, `numpy`, `pandas`, `pillow`, `scipy`, `matplotlib`/`statsmodels` for analysis. Optional OCR: `pytesseract` plus the Tesseract-OCR binary, or `easyocr` (pulls torch; avoid unless template-matched digits fail).
- Optional: ffmpeg to split scrcpy .mkv recordings into frames.
- Not needed: Android Studio, Java, any emulator.

### 11.3 Handling resolution and DPI differences
- Read `adb shell wm size` (physical and override) and `adb shell wm density`, and log both in the session metadata. Never hard-code pixel coordinates.
- Use **relative geometry** from a one-time calibration capture of the empty board. Calibration finds the board's bounding square (colour-segmented, refined by the grid lines), giving `board_x0, board_y0, board_size`, and `cell = board_size / 8`. Store these as fractions of screen width and height. Tray slot centres are also stored as fractions; the tray cell size comes from a known 1x1 piece or a measured scale factor relative to the board cell.
- Taller screens (20:9 and up) and display cutouts shift vertical offsets. Because calibration is taken from a real screenshot, only the calibrated numbers matter.
- Swipe targets use the board cell centres plus a calibrated **drag offset**. The piece renders above the finger, so the touch end point is offset by (dx, dy) from the target cell. Calibrate by dropping a 1x1 at three known cells and solving for the offset, then verify with a 3x3.
- Self-check after every placement: re-parse the board and assert the changed cells equal the intended pose. A constant mismatch means the offset is updated automatically; anything else is logged as `placement_mismatch`.
- If density or font scaling changes between sessions, re-run calibration (about 10 seconds).

### 11.4 Connectivity smoke test (agent runs it once the phone is plugged in and authorised)
Read-only except one screenshot and a harmless tap outside the game:
1. `adb version` and `adb devices -l`. Expect one line ending in `device`. If it says `unauthorized`, ask the user to re-accept the dialog. If the device is missing, check the cable, USB mode and driver.
2. `adb shell getprop ro.product.model`, `ro.build.version.release`, `ro.build.version.sdk`, then `adb shell wm size` and `adb shell wm density`.
3. `adb shell pm list packages | findstr /i blast` to find the real package id (do not assume it).
4. `adb exec-out screencap -p > smoke.png`; check that the dimensions match `wm size`; time 10 captures and record the median latency; repeat with raw `screencap` (no `-p`) and compare.
5. `adb shell dumpsys window | findstr mCurrentFocus` to confirm the game is in the foreground.
6. Input test outside the game (home screen or a blank spot): `adb shell input tap` and `adb shell input swipe x1 y1 x2 y2 200`, confirmed on a screenshot. If the phone ignores input, report the OEM security toggle from step 11.1.2.
7. `adb logcat -d -t 200`, to see whether anything is logged under the game's package.
8. scrcpy smoke test: `scrcpy --no-control --max-fps 30 --record=smoke.mkv` for 10 seconds; confirm the file plays and note the real fps.
9. Report a pass/fail table to the user before any game input is sent.
