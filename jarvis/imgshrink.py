"""Downscale oversized PNG screenshots so they can never blow the 1 MB JSON stream limit.

Stdlib (zlib/struct) plus numpy, which is already in the venv. No Pillow.
Supports non-interlaced 8-bit PNG (gray, gray+alpha, RGB, RGBA, palette), which is what screenshot
tools produce. Anything else returns None and the caller should drop the image instead.

CLI:  python -m jarvis.imgshrink in.png out.png [max_dim] [max_bytes]
"""

import base64
import struct
import sys
import zlib

SIG = b"\x89PNG\r\n\x1a\n"
MAX_PIXELS = 40_000_000  # refuse to decode anything bigger (pure-python unfilter is slow)
DEFAULT_MAX_DIM = 1280
DEFAULT_MAX_BYTES = 400 * 1024  # raw PNG bytes; ~540 KB as base64, well under the 1 MB stream limit


def _chunks(data: bytes):
    pos = len(SIG)
    while pos + 8 <= len(data):
        n, typ = struct.unpack(">I4s", data[pos:pos + 8])
        yield typ, data[pos + 8:pos + 8 + n]
        pos += 12 + n


def decode_png(data: bytes):
    """Returns (width, height, channels, numpy uint8 array HxWxC) or None if unsupported."""
    import numpy as np
    if not data.startswith(SIG):
        return None
    ihdr, idat, plte = None, [], None
    for typ, body in _chunks(data):
        if typ == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", body)
        elif typ == b"PLTE":
            plte = body
        elif typ == b"IDAT":
            idat.append(body)
        elif typ == b"IEND":
            break
    if not ihdr:
        return None
    w, h, depth, ctype, _comp, _filt, interlace = ihdr
    if depth != 8 or interlace != 0 or ctype not in (0, 2, 3, 4, 6) or w * h > MAX_PIXELS or not w or not h:
        return None
    ch = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    try:
        raw = zlib.decompress(b"".join(idat))
    except zlib.error:
        return None
    stride = w * ch
    if len(raw) < h * (stride + 1):
        return None
    out = np.zeros((h, stride), dtype=np.uint8)
    prev = bytearray(stride)
    pos = 0
    for y in range(h):
        ft = raw[pos]
        line = bytearray(raw[pos + 1:pos + 1 + stride])
        pos += stride + 1
        if ft == 0:
            pass
        elif ft == 1:
            for i in range(ch, stride):
                line[i] = (line[i] + line[i - ch]) & 255
        elif ft == 2:
            a = np.frombuffer(bytes(line), dtype=np.uint8) + np.frombuffer(bytes(prev), dtype=np.uint8)
            line = bytearray(a.tobytes())
        elif ft == 3:
            for i in range(stride):
                left = line[i - ch] if i >= ch else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 255
        elif ft == 4:
            for i in range(stride):
                a = line[i - ch] if i >= ch else 0
                b = prev[i]
                c = prev[i - ch] if i >= ch else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 255
        else:
            return None
        out[y] = np.frombuffer(bytes(line), dtype=np.uint8)
        prev = line
    img = out.reshape(h, w, ch)
    if ctype == 3:
        if not plte:
            return None
        pal = np.frombuffer(plte[:len(plte) // 3 * 3], dtype=np.uint8).reshape(-1, 3)
        idx = img[:, :, 0]
        if idx.max() >= len(pal):
            return None
        img = pal[idx]
        ch = 3
    return w, h, ch, img


def encode_png(img) -> bytes:
    """img: numpy uint8 HxWxC with C in 1..4 (1 gray, 2 gray+alpha, 3 RGB, 4 RGBA)."""
    import numpy as np
    h, w, ch = img.shape
    ctype = {1: 0, 2: 4, 3: 2, 4: 6}[ch]
    rows = np.concatenate([np.zeros((h, 1), dtype=np.uint8), img.reshape(h, w * ch)], axis=1)
    body = zlib.compress(rows.tobytes(), 9)

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return SIG + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, ctype, 0, 0, 0)) + chunk(b"IDAT", body) + chunk(b"IEND", b"")


def _box_downscale(img, factor_w: int, factor_h: int):
    import numpy as np
    h, w, ch = img.shape
    nh, nw = max(1, h // factor_h), max(1, w // factor_w)
    crop = img[:nh * factor_h, :nw * factor_w].astype(np.uint32)
    crop = crop.reshape(nh, factor_h, nw, factor_w, ch).mean(axis=(1, 3))
    return crop.round().astype(np.uint8)


def shrink_png(data: bytes, max_dim: int = DEFAULT_MAX_DIM, max_bytes: int = DEFAULT_MAX_BYTES):
    """Return PNG bytes no larger than max_bytes (shrinking in steps), or None if it cannot be done.
    Data already within both limits is returned unchanged."""
    dec = decode_png(data)
    if dec is None:
        return data if len(data) <= max_bytes and data.startswith(SIG) and _png_dims(data) <= (max_dim, max_dim) else None
    w, h, _ch, img = dec
    if len(data) <= max_bytes and max(w, h) <= max_dim:
        return data
    cur = img
    limit = max_dim
    for _ in range(8):
        ch_, cw_ = cur.shape[0], cur.shape[1]
        if max(ch_, cw_) > limit:
            f = -(-max(ch_, cw_) // limit)  # ceil: integer box factor
            cur = _box_downscale(cur, f, f)
        out = encode_png(cur)
        if len(out) <= max_bytes:
            return out
        limit = max(16, int(max(cur.shape[0], cur.shape[1]) * 0.7))
        if limit <= 16 and max(cur.shape[:2]) <= 16:
            break
    return None


def _png_dims(data: bytes):
    try:
        w, h = struct.unpack(">II", data[16:24])
        return (w, h)
    except struct.error:
        return (10 ** 9, 10 ** 9)


def shrink_base64_png(b64: str, max_dim: int = DEFAULT_MAX_DIM, max_bytes: int = DEFAULT_MAX_BYTES):
    """Accepts plain base64 or a data:image/png;base64, URL. Returns same form, shrunk, or None."""
    prefix = ""
    s = b64
    if s.startswith("data:"):
        prefix, _, s = s.partition(",")
        prefix += ","
    try:
        raw = base64.b64decode(s, validate=False)
    except ValueError:
        return None
    out = shrink_png(raw, max_dim, max_bytes)
    if out is None:
        return None
    return prefix + base64.b64encode(out).decode("ascii")


def shrink_file(src: str, dst: str, max_dim: int = DEFAULT_MAX_DIM, max_bytes: int = DEFAULT_MAX_BYTES) -> bool:
    with open(src, "rb") as f:
        out = shrink_png(f.read(), max_dim, max_bytes)
    if out is None:
        return False
    with open(dst, "wb") as f:
        f.write(out)
    return True


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    ok = shrink_file(sys.argv[1], sys.argv[2], *(int(a) for a in sys.argv[3:5]))
    print("ok" if ok else "could not shrink (unsupported PNG variant)")
    sys.exit(0 if ok else 1)
