# 04 Install log (Installer agent, 2026-11-16)

All installs on E:. No admin/UAC needed. No PATH or system settings changed. Python 3.14 untouched.

| Component | Version | Source | SHA-256 / verification | Path |
|---|---|---|---|---|
| Android platform-tools (adb) | adb 1.0.41, build 37.0.1-15733141 | https://dl.google.com/android/repository/platform-tools-latest-windows.zip | zip SHA-256 45F4D63113E895EBDE0C90F194099A4676B6AC653BD28D54314A9E022BBC1A99 (Google publishes no checksum; recorded only) | E:\tools\platform-tools |
| scrcpy | 5.0.1 (win64) | https://github.com/Genymobile/scrcpy/releases/download/v5.0.1/scrcpy-win64-v5.0.1.zip | SHA-256 B12A2C4EE8BE317422451FC7DCF8EE20A71B5EA7EF9AD73DDD825A25316227A5 matches GitHub release digest | E:\tools\scrcpy |
| Python | 3.12.10 (per-user, no launcher, no PATH, no file assoc) | https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe | SHA-256 67B5635E80EA51072B87941312D00EC8927C4DB9BA18938F7AD2D27B328B95FB; Authenticode valid, signer Python Software Foundation (python.org MD5 could not be matched from the page) | E:\tools\python312 |
| venv | Python 3.12.10 | created from above | | E:\Belgeler\blaster\.venv |

Venv packages: opencv-python 5.0.0.93, numpy 2.5.5, pillow 13.0.0, adbutils 2.12.0, scipy 1.18.1, pandas 3.1.1, pytest 9.2.0 (plus deps). easyocr skipped.

Note: Python 3.12.10 is the last 3.12 release with a Windows binary installer.

## Verification
- `adb version`: OK
- `scrcpy --version`: scrcpy 5.0.1
- venv python: 3.12.10
- `import cv2, numpy, adbutils, scipy, pandas, PIL, pytest`: OK, cv2.__version__ = 5.0.0
- `adb devices`: "List of devices attached" with no devices (phone not connected). The adb daemon was started by this command.

## Env scripts
- E:\Belgeler\blaster\tools.env.ps1 (dot-source in PowerShell)
- E:\Belgeler\blaster\tools.env.sh (source in Git Bash)
Both prepend platform-tools, scrcpy and venv Scripts to the session PATH and set BLASTER_ADB, BLASTER_SCRCPY, BLASTER_PYTHON, and ADB (so scrcpy uses the standalone adb 37.0.1).

## Problems
- None blocking. pip install took >2 min (backgrounded); harmless "cache entry deserialization" warnings.
- E:\tools\_downloads still holds the downloaded zips/installer (can be deleted).

## Addendum: Phone preparation (Samsung driver, pyyaml, ADB server)

- Samsung USB driver: checked with `pnputil /enum-drivers` (read-only). Drivers from Samsung Electronics Co., Ltd. are ALREADY installed: ssudbus.inf (oem137), ssudadb.inf (oem159, AndroidUsbDeviceClass), ssudmdm.inf (oem134), ssudrnds.inf (oem158); all version 2.21.4.0 (dated 04/08/2026). Therefore nothing was downloaded or installed; no UAC prompt or wizard needed. `E:	ools\_downloads\samsung_driver\` was not created.
- pyyaml: installed into the venv, `import yaml` succeeds (version 6.0.3).
- ADB server: ran `adb start-server`; `adb devices` returned an empty list (phone not connected, as expected).
- `bot	ests` was not touched. No PATH/system settings were changed.
