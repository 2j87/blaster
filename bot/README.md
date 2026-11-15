# Block Blast! bot

Plays Block Blast! on a phone connected over USB ADB and logs every move as JSONL.

## Setup

```powershell
.\.venv\Scripts\Activate.ps1
python -m pytest bot\tests -q
```

On the phone, USB debugging must be on and Auto Blocker off. If `adb` is not on PATH, pass `--adb-path <path to adb.exe>`.

## Calibration (in order)

```powershell
python -m bot.capture info
python -m bot.capture packages --pattern blast
python -m bot.capture grab cal_empty.png
python -m bot.vision calibrate cal_empty.png --score-value 0 --package <package_name> --overlay cal_overlay.png
python -m bot.vision label-score shot_1234.png 1234
python -m bot.vision tray-scale shot_hand.png
python -m bot.vision add-button revive.png --name decline --box x0 y0 x1 y1
python -m bot.vision add-button gameover.png --name restart --box x0 y0 x1 y1
python -m bot.vision parse shot.png --overlay check.png
```

Repeat `label-score` until all digits 0-9 have been learned. Only the decline and restart buttons are registered.

## Running

```powershell
python -m bot.run_session --minutes 60 --phases bot\phases\pilot.json --min-brightness 10 --network online --profile real
python -m bot.run_session --minutes 600 --phases bot\phases\overnight.json --min-brightness 10 --network online --profile real
```

Dry run without a phone: `python -m bot.run_session --device fake --fast-clock --minutes 30 --mode mixed --speed abba`

## Stopping and resuming

- Ctrl+C: exits after the current move.
- Creating the file `research\logs\STOP` also stops the run.
- Resume: `python -m bot.run_session --resume-last`

## Output

JSONL records, `checkpoint.json`, `STATUS.txt` and `img\` are written under `research\logs\<session>\`. Calibration goes to `bot\calibration.json`, templates to `bot\templates\`.

Analysis: `python -m bot.analyze --logs research\logs --out research\analysis --baseline-k 40`
