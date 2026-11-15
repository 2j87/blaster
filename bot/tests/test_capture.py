import io
import struct

import numpy as np

from bot import capture as C


def test_decode_raw_and_png():
    img = (np.arange(4 * 3 * 4) % 256).astype(np.uint8).reshape(3, 4, 4)
    for hdr in (struct.pack("<III", 4, 3, 1), struct.pack("<IIII", 4, 3, 1, 0)):
        out = C.decode_screencap(hdr + img.tobytes())
        assert out.shape == (3, 4, 3) and (out == img[:, :, :3]).all()
    from PIL import Image
    buf = io.BytesIO()
    Image.fromarray(img[:, :, :3]).save(buf, format="PNG")
    assert (C.decode_screencap(buf.getvalue()) == img[:, :, :3]).all()


def test_parse_focus_variants():
    f = C.parse_focus("  mCurrentFocus=Window{1a2b3c u0 com.block.juggle/com.unity3d.player.UnityPlayerActivity}")
    assert f.package == "com.block.juggle" and f.activity.endswith("UnityPlayerActivity")
    f = C.parse_focus("mFocusedApp=ActivityRecord{abc u0 com.ads.x/.AdActivity t12}")
    assert f.package == "com.ads.x"
    f = C.parse_focus("mCurrentFocus=Window{9f u0 NotificationShade}")
    assert f.package == "NotificationShade" and f.activity is None
    assert C.parse_focus("nothing").package is None


def test_parse_battery():
    txt = """Current Battery Service state:
  AC powered: false
  USB powered: true
  Wireless powered: false
  status: 2
  level: 87
  temperature: 365
"""
    b = C.parse_battery(txt)
    assert b.level == 87 and b.temperature_c == 36.5 and b.plugged is True and b.status == 2
