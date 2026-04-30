# -*- coding: utf-8 -*-
"""
GrayN visual frame protocol for QR Air Gap — with optional color channels.

Encoding (sender, Python 3.7+): numpy + Pillow
Decoding (receiver, Python 3.9+): numpy + OpenCV

Parameters:
  n_levels (4 or 8): gray intensity levels per channel
  n_colors (1, 2, or 4): number of independent color channels

Modes:
  n_colors=1: Grayscale only (Gray4: 2 bpm, Gray8: 3 bpm)
  n_colors=2: Gray + one RGB channel (Gray4: 3 bpm, Gray8: 4 bpm)
  n_colors=4: Gray + R + G + B     (Gray4: 4 bpm, Gray8: 5 bpm)

Symbol layout: sym = palette_idx * n_levels + level
  palette_idx 0 = gray  (R=G=B)
  palette_idx 1 = 1st color channel (R, G, or B)
  palette_idx 2 = green (n_colors=4 only)
  palette_idx 3 = blue  (n_colors=4 only)

XOR mask: sym ^= (n_levels - 1) flips only the level bits, preserving color.

Frame layout (in modules):
  quiet(4) + finder(7) + guard(1) + DATA(WxH) + guard(1) + finder(7) + quiet(4)
  Total: (W+24) x (H+24) modules, rendered at module_size pixels per module.

V3 binary packet (23 bytes fixed header):
  [0]      version  encodes (n_colors, n_levels)
  [1:5]    SID      4 bytes ASCII
  [5:9]    IDX      uint32 BE
  [9:13]   TOTAL    uint32 BE
  [13:17]  CRC32    uint32 BE (payload only)
  [17:19]  FNLEN    uint16 BE
  [19:23]  DATALEN  uint32 BE
  [23:23+FNLEN]              filename (UTF-8)
  [23+FNLEN:23+FNLEN+DLEN]   payload

Calibration frame:
  Data grid divided into n_levels rows x n_colors columns.
  Row i = level i, Column j = color j. NO XOR mask applied.
  Receiver samples each (color, level) cell to calibrate thresholds.
"""

import binascii
import math
import struct

import numpy as np

# ---------------------------------------------------------------------------
# Layout constants
# ---------------------------------------------------------------------------

QUIET = 4
FINDER_SIZE = 7
GUARD = 1
_SIDE = QUIET + FINDER_SIZE + GUARD   # 12
FRAME_OVERHEAD = 2 * _SIDE            # 24
_WARP_SCALE = 8

# ---------------------------------------------------------------------------
# Version byte mapping — (n_colors, n_levels) ↔ version byte
# ---------------------------------------------------------------------------

_VER_MAP = {
    (1, 4): 0x03,   # legacy gray4
    (1, 8): 0x08,   # legacy gray8
    (2, 4): 0x24,
    (2, 8): 0x28,
    (4, 4): 0x44,
    (4, 8): 0x48,
}
_VER_INV = {v: k for k, v in _VER_MAP.items()}

_VALID_LEVELS = {4, 8}
_VALID_COLORS = {1, 2, 4}

_CALIB_MARKER = b"__CALIB__"


class CalibResult:
    """Returned by FrameDecoder when a calibration frame is detected."""
    __slots__ = ("n_colors", "n_levels")

    def __init__(self, n_colors, n_levels):
        self.n_colors = n_colors
        self.n_levels = n_levels

    def __eq__(self, other):
        if isinstance(other, bytes):
            return other == _CALIB_MARKER
        return NotImplemented

    def __ne__(self, other):
        result = self.__eq__(other)
        return not result if result is not NotImplemented else NotImplemented

# Packing groups: bpm → (symbols_per_group, bytes_per_group)
_PACK_GROUPS = {
    2: (4, 1),
    3: (8, 3),
    4: (2, 1),
    5: (8, 5),
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compute_bpm(n_levels, n_colors=1):
    total = n_colors * n_levels
    bpm = int(math.log2(total))
    assert 1 << bpm == total, "n_colors*n_levels must be a power of 2"
    return bpm


def _color_palette(n_colors, color_ch=1):
    """Active color indices: 0=gray, 1=R, 2=G, 3=B."""
    if n_colors == 1:
        return [0]
    if n_colors == 2:
        return [0, color_ch]
    return [0, 1, 2, 3]


def _make_gray_lut(n_levels):
    return np.linspace(0, 255, n_levels).astype(np.uint8)


_COLOR_MIN = 48   # minimum pixel value for color channels (avoid confusion with black)


def _make_color_lut_rgb(n_levels, n_colors, color_ch=1):
    """Build RGB LUT: symbol → (R, G, B), shape (n_colors*n_levels, 3).

    For gray (palette 0):  R=G=B = linspace(0, 255, n_levels)
    For color channels:    dominant = linspace(_COLOR_MIN, 255, n_levels), others = 0
    """
    palette = _color_palette(n_colors, color_ch)
    total = n_levels * n_colors
    lut = np.zeros((total, 3), dtype=np.uint8)
    gray_vals = np.linspace(0, 255, n_levels).astype(np.uint8)
    color_vals = np.linspace(_COLOR_MIN, 255, n_levels).astype(np.uint8)
    for pi, ci in enumerate(palette):
        base = pi * n_levels
        if ci == 0:
            for lv in range(n_levels):
                v = int(gray_vals[lv])
                lut[base + lv] = [v, v, v]
        else:
            for lv in range(n_levels):
                v = int(color_vals[lv])
                rgb = [0, 0, 0]
                rgb[ci - 1] = v
                lut[base + lv] = rgb
    return lut


# ---------------------------------------------------------------------------
# V3 packet format
# ---------------------------------------------------------------------------

_V3_FMT = struct.Struct('>B4sIIIHI')   # 1+4+4+4+4+2+4 = 23
V3_HEADER_SIZE = _V3_FMT.size          # 23
_V3_END_SID = b'END0'


def frame_capacity_bytes(grid_w, grid_h, n_levels=4, n_colors=1):
    bpm = _compute_bpm(n_levels, n_colors)
    total_mods = grid_w * grid_h
    spg, bpg = _PACK_GROUPS[bpm]
    return (total_mods // spg) * bpg


def chunk_capacity_bytes(grid_w, grid_h, filename="", n_levels=4, n_colors=1):
    fname_len = len(filename.encode('utf-8')) if filename else 0
    return frame_capacity_bytes(grid_w, grid_h, n_levels, n_colors) - V3_HEADER_SIZE - fname_len


# -- encode / decode --------------------------------------------------------

def encode_v3_packet(sid, idx, total, data, filename="", n_levels=4, n_colors=1):
    ver = _VER_MAP[(n_colors, n_levels)]
    crc = binascii.crc32(data) & 0xFFFFFFFF
    fname_b = filename.encode('utf-8') if filename else b''
    sid_b = sid.encode('ascii')[:4].ljust(4, b'0')
    hdr = _V3_FMT.pack(ver, sid_b, idx, total, crc, len(fname_b), len(data))
    return hdr + fname_b + data


def decode_v3_packet(raw):
    if not raw or len(raw) < V3_HEADER_SIZE:
        return None
    try:
        ver, sid_b, idx, total, exp_crc, fnlen, dlen = _V3_FMT.unpack(
            raw[:V3_HEADER_SIZE])
    except struct.error:
        return None
    if ver not in _VER_INV:
        return None
    n_colors, n_levels = _VER_INV[ver]
    fn_end = V3_HEADER_SIZE + fnlen
    d_end = fn_end + dlen
    if d_end > len(raw):
        return None
    sid = sid_b.decode('ascii', errors='replace')
    fname = (raw[V3_HEADER_SIZE:fn_end].decode('utf-8', errors='replace')
             if fnlen else "")
    payload = raw[fn_end:d_end]
    if binascii.crc32(payload) & 0xFFFFFFFF != exp_crc:
        return None
    return {"sid": sid, "idx": idx, "total": total,
            "data": payload, "filename": fname,
            "n_levels": n_levels, "n_colors": n_colors}


def diagnose_v3_packet(raw):
    if not raw:
        return "empty"
    if len(raw) < V3_HEADER_SIZE:
        return "too short ({} < {})".format(len(raw), V3_HEADER_SIZE)
    hdr_hex = raw[:V3_HEADER_SIZE].hex()
    try:
        ver, sid_b, idx, total, exp_crc, fnlen, dlen = _V3_FMT.unpack(
            raw[:V3_HEADER_SIZE])
    except struct.error:
        return "unpack fail, hdr={}".format(hdr_hex)
    if ver not in _VER_INV:
        return "bad ver=0x{:02x} (expect {}), hdr={}".format(
            ver, "/".join("0x{:02x}".format(v) for v in sorted(_VER_INV)), hdr_hex)
    fn_end = V3_HEADER_SIZE + fnlen
    d_end = fn_end + dlen
    if d_end > len(raw):
        return ("overflow: fnlen={} dlen={} need {} have {}, "
                "sid={} idx={} total={}").format(
                    fnlen, dlen, d_end, len(raw),
                    sid_b.decode('ascii', errors='replace'), idx, total)
    payload = raw[fn_end:d_end]
    act_crc = binascii.crc32(payload) & 0xFFFFFFFF
    if act_crc != exp_crc:
        return ("CRC mismatch: expect={:08x} actual={:08x}, "
                "sid={} idx={} total={} fnlen={} dlen={}").format(
                    exp_crc, act_crc,
                    sid_b.decode('ascii', errors='replace'),
                    idx, total, fnlen, dlen)
    return "OK (should not reach here)"


def encode_v3_end_packet(n_levels=4, n_colors=1):
    ver = _VER_MAP.get((n_colors, n_levels), 0x03)
    return _V3_FMT.pack(ver, _V3_END_SID, 0xFFFFFFFF, 0, 0, 0, 0)


def is_v3_end_packet(raw):
    if not raw or len(raw) < V3_HEADER_SIZE:
        return False
    try:
        ver, sid_b, _, total, _, _, _ = _V3_FMT.unpack(raw[:V3_HEADER_SIZE])
        return ver in _VER_INV and sid_b == _V3_END_SID and total == 0
    except struct.error:
        return False


# ---------------------------------------------------------------------------
# Symbol ↔ bytes — parameterized by bits_per_module
# ---------------------------------------------------------------------------

def _bytes_to_symbols(data, bpm):
    arr = np.frombuffer(data, dtype=np.uint8)
    if len(arr) == 0:
        return np.array([], dtype=np.uint8)
    if bpm == 2:
        return np.column_stack([
            (arr >> 6) & 3, (arr >> 4) & 3, (arr >> 2) & 3, arr & 3
        ]).ravel().astype(np.uint8)
    if bpm == 3:
        pad = (3 - len(arr) % 3) % 3
        if pad:
            arr = np.concatenate([arr, np.zeros(pad, dtype=np.uint8)])
        groups = arr.reshape(-1, 3).astype(np.uint32)
        bits24 = (groups[:, 0] << 16) | (groups[:, 1] << 8) | groups[:, 2]
        return np.column_stack([
            (bits24 >> 21) & 7, (bits24 >> 18) & 7, (bits24 >> 15) & 7,
            (bits24 >> 12) & 7, (bits24 >> 9) & 7, (bits24 >> 6) & 7,
            (bits24 >> 3) & 7, bits24 & 7,
        ]).ravel().astype(np.uint8)
    if bpm == 4:
        return np.column_stack([
            (arr >> 4) & 0xF, arr & 0xF
        ]).ravel().astype(np.uint8)
    if bpm == 5:
        pad = (5 - len(arr) % 5) % 5
        if pad:
            arr = np.concatenate([arr, np.zeros(pad, dtype=np.uint8)])
        groups = arr.reshape(-1, 5).astype(np.uint64)
        bits40 = ((groups[:, 0] << 32) | (groups[:, 1] << 24) |
                  (groups[:, 2] << 16) | (groups[:, 3] << 8) | groups[:, 4])
        return np.column_stack([
            (bits40 >> 35) & 0x1F, (bits40 >> 30) & 0x1F,
            (bits40 >> 25) & 0x1F, (bits40 >> 20) & 0x1F,
            (bits40 >> 15) & 0x1F, (bits40 >> 10) & 0x1F,
            (bits40 >> 5) & 0x1F, bits40 & 0x1F,
        ]).ravel().astype(np.uint8)
    raise ValueError("unsupported bpm={}".format(bpm))


def _symbols_to_bytes(syms, bpm):
    if bpm == 2:
        n = (len(syms) // 4) * 4
        if n == 0:
            return b''
        s = syms[:n].reshape(-1, 4).astype(np.uint16)
        return bytes(
            (s[:, 0] << 6 | s[:, 1] << 4 | s[:, 2] << 2 | s[:, 3]).astype(np.uint8))
    if bpm == 3:
        n = (len(syms) // 8) * 8
        if n == 0:
            return b''
        s = syms[:n].reshape(-1, 8).astype(np.uint32)
        bits24 = (s[:, 0] << 21 | s[:, 1] << 18 | s[:, 2] << 15 |
                  s[:, 3] << 12 | s[:, 4] << 9 | s[:, 5] << 6 |
                  s[:, 6] << 3 | s[:, 7])
        out = np.column_stack([
            (bits24 >> 16) & 0xFF, (bits24 >> 8) & 0xFF, bits24 & 0xFF,
        ]).astype(np.uint8).ravel()
        return bytes(out)
    if bpm == 4:
        n = (len(syms) // 2) * 2
        if n == 0:
            return b''
        s = syms[:n].reshape(-1, 2).astype(np.uint16)
        return bytes((s[:, 0] << 4 | s[:, 1]).astype(np.uint8))
    if bpm == 5:
        n = (len(syms) // 8) * 8
        if n == 0:
            return b''
        s = syms[:n].reshape(-1, 8).astype(np.uint64)
        bits40 = (s[:, 0] << 35 | s[:, 1] << 30 | s[:, 2] << 25 |
                  s[:, 3] << 20 | s[:, 4] << 15 | s[:, 5] << 10 |
                  s[:, 6] << 5 | s[:, 7])
        out = np.column_stack([
            (bits40 >> 32) & 0xFF, (bits40 >> 24) & 0xFF,
            (bits40 >> 16) & 0xFF, (bits40 >> 8) & 0xFF, bits40 & 0xFF,
        ]).astype(np.uint8).ravel()
        return bytes(out)
    raise ValueError("unsupported bpm={}".format(bpm))


def _build_mask(grid_w, grid_h, x0, y0):
    """XOR mask: True where (x+y)%3==0."""
    ys = np.arange(y0, y0 + grid_h).reshape(-1, 1)
    xs = np.arange(x0, x0 + grid_w).reshape(1, -1)
    return ((xs + ys) % 3 == 0)


# ---------------------------------------------------------------------------
# Frame encoding (sender side)
# ---------------------------------------------------------------------------

def _draw_finder_rgb(img, left, top, module_size):
    """Draw a 7x7 finder pattern in RGB image (pixel coordinates)."""
    s = FINDER_SIZE
    ms = module_size
    x0, y0 = left * ms, top * ms
    black = np.array([0, 0, 0], dtype=np.uint8)
    white = np.array([255, 255, 255], dtype=np.uint8)
    img[y0:y0 + s * ms, x0:x0 + s * ms] = black
    img[y0 + ms:y0 + (s - 1) * ms, x0 + ms:x0 + (s - 1) * ms] = white
    img[y0 + 2 * ms:y0 + (s - 2) * ms, x0 + 2 * ms:x0 + (s - 2) * ms] = black


def encode_frame(packet_bytes, grid_w=160, grid_h=96, module_size=4,
                 n_levels=4, n_colors=1, color_ch=1):
    """Encode V3 packet bytes into an RGB image (numpy uint8 H,W,3)."""
    bpm = _compute_bpm(n_levels, n_colors)
    fw = grid_w + FRAME_OVERHEAD
    fh = grid_h + FRAME_OVERHEAD
    ms = module_size

    rgb_lut = _make_color_lut_rgb(n_levels, n_colors, color_ch)
    total_mods = grid_w * grid_h

    syms = _bytes_to_symbols(packet_bytes, bpm)
    pad = total_mods - len(syms)
    if pad > 0:
        syms = np.concatenate([syms, np.zeros(pad, dtype=np.uint8)])
    elif pad < 0:
        raise ValueError(
            "V3 packet ({} B) exceeds frame capacity ({} B, grid {}x{}, bpm={})".format(
                len(packet_bytes),
                frame_capacity_bytes(grid_w, grid_h, n_levels, n_colors),
                grid_w, grid_h, bpm))

    x0, y0 = _SIDE, _SIDE
    grid = syms.reshape(grid_h, grid_w).copy()
    grid[_build_mask(grid_w, grid_h, x0, y0)] ^= (n_levels - 1)

    # Build module-level RGB matrix for data region
    data_rgb = rgb_lut[grid]  # (grid_h, grid_w, 3)

    # Full frame: white background
    img = np.full((fh * ms, fw * ms, 3), 255, dtype=np.uint8)

    # Place data region
    dy, dx = y0 * ms, x0 * ms
    img[dy:dy + grid_h * ms, dx:dx + grid_w * ms] = np.repeat(
        np.repeat(data_rgb, ms, axis=0), ms, axis=1)

    # Draw finders
    q, f = QUIET, FINDER_SIZE
    _draw_finder_rgb(img, q, q, ms)
    _draw_finder_rgb(img, fw - q - f, q, ms)
    _draw_finder_rgb(img, q, fh - q - f, ms)
    _draw_finder_rgb(img, fw - q - f, fh - q - f, ms)

    return img


def frame_to_pil(frame_rgb):
    """Convert numpy RGB array to a PIL Image."""
    from PIL import Image
    return Image.fromarray(frame_rgb, 'RGB')


# ---------------------------------------------------------------------------
# Calibration frame
# ---------------------------------------------------------------------------

def encode_calibration_frame(grid_w=160, grid_h=96, module_size=4,
                             n_levels=4, n_colors=1, color_ch=1):
    """Calibration frame: n_levels rows x n_colors columns, no XOR mask."""
    fw = grid_w + FRAME_OVERHEAD
    fh = grid_h + FRAME_OVERHEAD
    ms = module_size

    rgb_lut = _make_color_lut_rgb(n_levels, n_colors, color_ch)

    band_h = grid_h // n_levels
    sect_w = grid_w // n_colors

    # Build data region RGB
    data_rgb = np.full((grid_h, grid_w, 3), 255, dtype=np.uint8)
    palette = _color_palette(n_colors, color_ch)
    for ci in range(n_colors):
        for lv in range(n_levels):
            sym = ci * n_levels + lv
            r0 = lv * band_h
            r1 = (lv + 1) * band_h if lv < n_levels - 1 else grid_h
            c0 = ci * sect_w
            c1 = (ci + 1) * sect_w if ci < n_colors - 1 else grid_w
            data_rgb[r0:r1, c0:c1] = rgb_lut[sym]

    x0, y0 = _SIDE, _SIDE
    img = np.full((fh * ms, fw * ms, 3), 255, dtype=np.uint8)

    dy, dx = y0 * ms, x0 * ms
    img[dy:dy + grid_h * ms, dx:dx + grid_w * ms] = np.repeat(
        np.repeat(data_rgb, ms, axis=0), ms, axis=1)

    q, f = QUIET, FINDER_SIZE
    _draw_finder_rgb(img, q, q, ms)
    _draw_finder_rgb(img, fw - q - f, q, ms)
    _draw_finder_rgb(img, q, fh - q - f, ms)
    _draw_finder_rgb(img, fw - q - f, fh - q - f, ms)

    return img


def calibration_frame_to_pil(grid_w=160, grid_h=96, module_size=4,
                             n_levels=4, n_colors=1, color_ch=1):
    return frame_to_pil(
        encode_calibration_frame(grid_w, grid_h, module_size,
                                 n_levels, n_colors, color_ch))


# ---------------------------------------------------------------------------
# Frame decoding (receiver side — requires OpenCV)
# ---------------------------------------------------------------------------

def _find_nested_squares(binary):
    import cv2
    contours, hierarchy = cv2.findContours(
        binary, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    if hierarchy is None:
        return []
    h = hierarchy[0]
    out = []
    for i in range(len(contours)):
        ci = h[i][2]
        if ci < 0:
            continue
        gi = h[ci][2]
        if gi < 0:
            continue
        a0 = cv2.contourArea(contours[i])
        if a0 < 49:
            continue
        a1 = cv2.contourArea(contours[ci])
        a2 = cv2.contourArea(contours[gi])
        r1 = a1 / a0 if a0 > 0 else 0
        r2 = a2 / a0 if a0 > 0 else 0
        if not (0.20 < r1 < 0.78 and 0.02 < r2 < 0.45):
            continue
        rect = cv2.minAreaRect(contours[i])
        w_, h_ = rect[1]
        if min(w_, h_) < 5 or max(w_, h_) / min(w_, h_) > 1.8:
            continue
        M = cv2.moments(contours[i])
        if M["m00"] < 1:
            continue
        cx = M["m10"] / M["m00"]
        cy = M["m01"] / M["m00"]
        out.append((float(cx), float(cy), float(max(w_, h_))))
    return out


def _dedup_finders(cands, min_dist=20.0):
    out = []
    for c in cands:
        if not any((c[0] - o[0]) ** 2 + (c[1] - o[1]) ** 2 < min_dist ** 2
                   for o in out):
            out.append(c)
    return out


def _order_quad(cands):
    if len(cands) < 4:
        return None
    pts = np.array([(c[0], c[1]) for c in cands[:4]], dtype=np.float32)
    by_y = pts[np.argsort(pts[:, 1])]
    top = by_y[:2]
    bot = by_y[2:]
    tl, tr = top[np.argsort(top[:, 0])]
    bl, br = bot[np.argsort(bot[:, 0])]
    quad = np.array([tl, tr, br, bl], dtype=np.float32)
    d_top = np.linalg.norm(quad[1] - quad[0])
    d_bot = np.linalg.norm(quad[2] - quad[3])
    d_lft = np.linalg.norm(quad[3] - quad[0])
    d_rgt = np.linalg.norm(quad[2] - quad[1])
    for a, b in [(d_top, d_bot), (d_lft, d_rgt)]:
        if min(a, b) < 1 or max(a, b) / min(a, b) > 1.5:
            return None
    return quad


def _detect_finders(gray):
    import cv2
    _, bw = cv2.threshold(gray, 0, 255,
                          cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    cands = _find_nested_squares(bw)
    if len(cands) < 4:
        bw2 = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY, 51, 11)
        cands.extend(_find_nested_squares(bw2))
        cands = _dedup_finders(cands)
    if len(cands) < 4:
        cands.extend(_find_nested_squares(255 - bw))
        cands = _dedup_finders(cands)
    if len(cands) < 4:
        return None
    cands.sort(key=lambda c: c[2], reverse=True)
    cands = cands[:4]
    sizes = [c[2] for c in cands]
    if max(sizes) / max(min(sizes), 1) > 2.5:
        return None
    return _order_quad(cands)


# ---------------------------------------------------------------------------
# Warp + sample (supports both gray-only and RGB)
# ---------------------------------------------------------------------------

def _warp_and_sample_rgb(bgr, H_mat, grid_w, grid_h):
    """Warp BGR frame, sample data grid as RGB.

    Returns (vals_rgb, lo, hi) or None.
    vals_rgb: float32 array (grid_h, grid_w, 3) — R, G, B values.
    lo, hi: finder dark/bright medians (from grayscale).
    """
    import cv2
    fw = grid_w + FRAME_OVERHEAD
    fh = grid_h + FRAME_OVERHEAD
    scale = _WARP_SCALE
    ww, wh = fw * scale, fh * scale

    warped = cv2.warpPerspective(bgr, H_mat, (ww, wh), flags=cv2.INTER_LINEAR)

    # Finder calibration via grayscale
    gray_w = cv2.cvtColor(warped, cv2.COLOR_BGR2GRAY)
    dark_vals = []
    bright_vals = []
    corners = [
        (QUIET, QUIET),
        (fw - QUIET - FINDER_SIZE, QUIET),
        (QUIET, fh - QUIET - FINDER_SIZE),
        (fw - QUIET - FINDER_SIZE, fh - QUIET - FINDER_SIZE),
    ]
    for fx, fy in corners:
        for dy in range(FINDER_SIZE):
            for dx in range(FINDER_SIZE):
                is_outer = (dx == 0 or dx == FINDER_SIZE - 1 or
                            dy == 0 or dy == FINDER_SIZE - 1)
                is_center = (2 <= dx <= FINDER_SIZE - 3 and
                             2 <= dy <= FINDER_SIZE - 3)
                px = int((fx + dx) * scale + scale // 2)
                py = int((fy + dy) * scale + scale // 2)
                if 0 <= px < ww and 0 <= py < wh:
                    v = float(gray_w[py, px])
                    if is_outer or is_center:
                        dark_vals.append(v)
                    else:
                        bright_vals.append(v)
    if not dark_vals or not bright_vals:
        return None
    lo = float(np.median(dark_vals))
    hi = float(np.median(bright_vals))
    if hi - lo < 20:
        return None

    x0, y0 = _SIDE, _SIDE
    mx = (np.arange(grid_w) + x0).astype(np.float64)
    my = (np.arange(grid_h) + y0).astype(np.float64)
    mxx, myy = np.meshgrid(mx, my)
    px_arr = np.clip((mxx * scale + scale // 2).astype(int), 0, ww - 1)
    py_arr = np.clip((myy * scale + scale // 2).astype(int), 0, wh - 1)

    # Sample RGB (warped is BGR)
    vals_b = warped[py_arr, px_arr, 0].astype(np.float32)
    vals_g = warped[py_arr, px_arr, 1].astype(np.float32)
    vals_r = warped[py_arr, px_arr, 2].astype(np.float32)
    vals_rgb = np.stack([vals_r, vals_g, vals_b], axis=-1)

    return vals_rgb, lo, hi


# ---------------------------------------------------------------------------
# Classification and decoding
# ---------------------------------------------------------------------------

def _classify_gray_only(vals_gray, grid_w, grid_h, n_levels, calibration):
    """Classify grayscale values → symbols → bytes (n_colors=1 path)."""
    x0, y0 = _SIDE, _SIDE
    max_sym = n_levels - 1

    if calibration is not None and len(calibration.get("gray", [])) == n_levels:
        centers = np.array(calibration["gray"], dtype=np.float64)
        thresholds = [(centers[i] + centers[i + 1]) / 2.0
                      for i in range(n_levels - 1)]
    else:
        lo_v = float(np.min(vals_gray))
        hi_v = float(np.max(vals_gray))
        rng = hi_v - lo_v if hi_v - lo_v > 1 else 1.0
        thresholds = [lo_v + rng * (2 * i + 1) / (2 * n_levels)
                      for i in range(n_levels - 1)]

    syms = np.zeros_like(vals_gray, dtype=np.uint8)
    for i in range(1, n_levels):
        syms[vals_gray >= thresholds[i - 1]] = i

    syms[_build_mask(grid_w, grid_h, x0, y0)] ^= max_sym
    bpm = _compute_bpm(n_levels, 1)
    return _symbols_to_bytes(syms.ravel(), bpm)


def _classify_color(vals_rgb, grid_w, grid_h, n_levels, n_colors, calibration):
    """Classify RGB values → symbols → bytes (n_colors>1 path).

    Two-step classification:
      1. Determine color category (gray / R / G / B) from channel dominance
      2. Determine intensity level within that color using calibrated thresholds
    """
    x0, y0 = _SIDE, _SIDE
    r = vals_rgb[:, :, 0]
    g = vals_rgb[:, :, 1]
    b = vals_rgb[:, :, 2]

    max_ch = np.maximum(np.maximum(r, g), b)
    min_ch = np.minimum(np.minimum(r, g), b)
    sat = max_ch - min_ch

    # Step 1: color classification
    # Derive saturation threshold from calibration if available
    sat_thresh = 25.0
    if calibration is not None and "sat_thresh" in calibration:
        sat_thresh = calibration["sat_thresh"]

    palette_idx = np.zeros(r.shape, dtype=np.uint8)  # 0=gray
    is_color = sat > sat_thresh

    if n_colors == 2:
        palette_idx[is_color] = 1
    else:
        r_dom = is_color & (r >= g) & (r >= b)
        g_dom = is_color & (g > r) & (g >= b)
        b_dom = is_color & ~r_dom & ~g_dom & is_color
        # Map to palette: R=1, G=2, B=3
        palette_idx[r_dom] = 1
        palette_idx[g_dom] = 2
        palette_idx[b_dom] = 3

    # Step 2: level classification per color
    gray_val = (r + g + b) / 3.0
    # Dominant channel value for color classification
    if n_colors == 2:
        # Auto-detect which channel the single color uses
        if calibration is not None and "color_channels" in calibration:
            ch_list = calibration["color_channels"]
            color_ch_idx = ch_list[1] - 1 if len(ch_list) > 1 else 0
        else:
            color_ch_idx = 0
        dom_val = vals_rgb[:, :, color_ch_idx]
    else:
        dom_val = np.where(palette_idx == 1, r,
                  np.where(palette_idx == 2, g, b))

    # Choose value source: gray_val for gray modules, dom_val for color
    level_val = np.where(palette_idx == 0, gray_val, dom_val)

    # Per-color level thresholds from calibration
    levels = np.zeros(r.shape, dtype=np.uint8)

    if calibration is not None:
        for pi in range(n_colors):
            key = "c{}".format(pi)
            if key not in calibration:
                continue
            centers = calibration[key]
            if len(centers) != n_levels:
                continue
            thresholds = [(centers[i] + centers[i + 1]) / 2.0
                          for i in range(n_levels - 1)]
            mask_pi = (palette_idx == pi)
            lv_pi = np.zeros(r.shape, dtype=np.uint8)
            for i in range(1, n_levels):
                lv_pi[level_val >= thresholds[i - 1]] = i
            levels[mask_pi] = lv_pi[mask_pi]
    else:
        # Fallback: linear thresholds across full range
        lo_v = float(np.min(level_val))
        hi_v = float(np.max(level_val))
        rng = hi_v - lo_v if hi_v - lo_v > 1 else 1.0
        thresholds = [lo_v + rng * (2 * i + 1) / (2 * n_levels)
                      for i in range(n_levels - 1)]
        for i in range(1, n_levels):
            levels[level_val >= thresholds[i - 1]] = i

    syms = palette_idx * n_levels + levels
    syms[_build_mask(grid_w, grid_h, x0, y0)] ^= (n_levels - 1)

    bpm = _compute_bpm(n_levels, n_colors)
    return _symbols_to_bytes(syms.ravel(), bpm)


# ---------------------------------------------------------------------------
# Calibration detection
# ---------------------------------------------------------------------------

def _try_detect_calibration_gray(vals_gray, grid_h, n_levels):
    """Detect gray-only calibration frame. Returns dict or None."""
    band_h = grid_h // n_levels
    if band_h < 4:
        return None
    margin_rows = max(1, band_h // 5)
    centers = []
    for lv in range(n_levels):
        r0 = lv * band_h + margin_rows
        r1 = (lv + 1) * band_h - margin_rows if lv < n_levels - 1 else grid_h - margin_rows
        if r1 <= r0:
            r0 = lv * band_h
            r1 = (lv + 1) * band_h if lv < n_levels - 1 else grid_h
        band = vals_gray[r0:r1, :]
        margin_cols = max(1, band.shape[1] // 10)
        inner = band[:, margin_cols:-margin_cols] if margin_cols < band.shape[1] // 2 else band
        centers.append(float(np.median(inner)))
    for i in range(len(centers) - 1):
        if centers[i + 1] - centers[i] < 3:
            return None
    if centers[-1] < 1 or centers[0] / centers[-1] > 0.30:
        return None
    return {"gray": centers, "n_colors": 1, "n_levels": n_levels}


def _try_detect_calibration_color(vals_rgb, grid_w, grid_h, n_levels, n_colors):
    """Detect multi-color calibration frame.

    Returns calibration dict with per-color centers, or None.
    """
    band_h = grid_h // n_levels
    sect_w = grid_w // n_colors
    if band_h < 4 or sect_w < 4:
        return None

    margin_r = max(1, band_h // 5)
    margin_c = max(1, sect_w // 5)

    cal = {"n_colors": n_colors, "n_levels": n_levels}
    color_channels = [0]  # gray

    for ci in range(n_colors):
        centers = []
        for lv in range(n_levels):
            r0 = lv * band_h + margin_r
            r1 = (lv + 1) * band_h - margin_r if lv < n_levels - 1 else grid_h - margin_r
            c0 = ci * sect_w + margin_c
            c1 = (ci + 1) * sect_w - margin_c if ci < n_colors - 1 else grid_w - margin_c
            if r1 <= r0 or c1 <= c0:
                return None
            patch = vals_rgb[r0:r1, c0:c1, :]  # (rows, cols, 3)
            if ci == 0:
                val = float(np.median((patch[:, :, 0] + patch[:, :, 1] + patch[:, :, 2]) / 3.0))
            else:
                med_rgb = np.median(patch, axis=(0, 1))  # (3,)
                dom_ch = int(np.argmax(med_rgb))
                val = float(np.median(patch[:, :, dom_ch]))
                if lv == 0:
                    color_channels.append(dom_ch + 1)  # 1=R, 2=G, 3=B
            centers.append(val)

        # Verify monotonically increasing
        for i in range(len(centers) - 1):
            if centers[i + 1] - centers[i] < 3:
                return None
        cal["c{}".format(ci)] = centers

    # Verify gray section looks gray (low saturation)
    gray_sect = vals_rgb[:, :sect_w, :]
    gray_max = np.max(gray_sect, axis=-1)
    gray_min = np.min(gray_sect, axis=-1)
    med_sat = float(np.median(gray_max - gray_min))
    if med_sat > 30:
        return None

    # Verify color sections have high saturation (at higher levels)
    if n_colors > 1:
        for ci in range(1, n_colors):
            top_lv = n_levels - 1
            r0 = top_lv * band_h + margin_r
            r1 = grid_h - margin_r
            c0 = ci * sect_w + margin_c
            c1 = (ci + 1) * sect_w - margin_c if ci < n_colors - 1 else grid_w - margin_c
            patch = vals_rgb[r0:r1, c0:c1, :]
            p_max = np.max(patch, axis=-1)
            p_min = np.min(patch, axis=-1)
            color_sat = float(np.median(p_max - p_min))
            if color_sat < 30:
                return None

    # Compute saturation threshold
    if n_colors > 1:
        # Max sat in gray section
        gray_sats = gray_max - gray_min
        max_gray_sat = float(np.percentile(gray_sats, 95))
        # Min sat in color sections at lowest level
        min_color_sat = 255.0
        for ci in range(1, n_colors):
            lv = 0
            r0 = lv * band_h + margin_r
            r1 = (lv + 1) * band_h - margin_r
            c0 = ci * sect_w + margin_c
            c1 = (ci + 1) * sect_w - margin_c if ci < n_colors - 1 else grid_w - margin_c
            if r1 <= r0 or c1 <= c0:
                continue
            patch = vals_rgb[r0:r1, c0:c1, :]
            p_sat = np.max(patch, axis=-1) - np.min(patch, axis=-1)
            min_color_sat = min(min_color_sat, float(np.percentile(p_sat, 5)))
        cal["sat_thresh"] = (max_gray_sat + min_color_sat) / 2.0
    else:
        cal["sat_thresh"] = 25.0

    cal["color_channels"] = color_channels
    return cal


# ---------------------------------------------------------------------------
# Shared calibration cache across all FrameDecoder instances (all threads)
# ---------------------------------------------------------------------------

import threading as _threading

_shared_calib_lock = _threading.Lock()
_shared_calib_cache = {}  # {(n_colors, n_levels): calibration_dict}


def _shared_calib_store(nc, nl, cal):
    with _shared_calib_lock:
        _shared_calib_cache[(nc, nl)] = cal


def _shared_calib_snapshot():
    with _shared_calib_lock:
        return dict(_shared_calib_cache)


def clear_shared_calib_cache():
    """Clear the shared calibration cache (e.g. for manual re-calibration)."""
    with _shared_calib_lock:
        _shared_calib_cache.clear()


# ---------------------------------------------------------------------------
# Stateful decoder with homography caching + calibration
# ---------------------------------------------------------------------------

class FrameDecoder:
    """Stateful frame decoder with homography caching and calibration.

    Supports gray-only (n_colors=1) and color (n_colors=2/4) modes.
    Calibration data is shared across all instances via a module-level cache.
    """

    _RELOCATE_AFTER = 3

    def __init__(self, grid_w=160, grid_h=96, n_levels=4, n_colors=1):
        self.grid_w = grid_w
        self.grid_h = grid_h
        self.n_levels = n_levels
        self.n_colors = n_colors
        self._H_mat = None
        self._consec_fail = 0
        self._calibration = None
        self._dst = None
        self._build_dst()

    def _build_dst(self):
        fw = self.grid_w + FRAME_OVERHEAD
        fh = self.grid_h + FRAME_OVERHEAD
        fc = QUIET + FINDER_SIZE // 2
        scale = _WARP_SCALE
        self._dst = np.array([
            [(fc + 0.5) * scale, (fc + 0.5) * scale],
            [(fw - fc - 0.5) * scale, (fc + 0.5) * scale],
            [(fw - fc - 0.5) * scale, (fh - fc - 0.5) * scale],
            [(fc + 0.5) * scale, (fh - fc - 0.5) * scale],
        ], dtype=np.float32)

    def _locate(self, gray):
        import cv2
        quad = _detect_finders(gray)
        if quad is None:
            return None
        H_mat = cv2.getPerspectiveTransform(quad, self._dst)
        self._H_mat = H_mat
        self._consec_fail = 0
        return H_mat

    def _classify_with_params(self, vals_rgb, nc, nl, cal):
        """Classify pixels using specific (n_colors, n_levels, calibration)."""
        if nc == 1:
            vals_gray = (vals_rgb[:, :, 0] + vals_rgb[:, :, 1] + vals_rgb[:, :, 2]) / 3.0
            return _classify_gray_only(
                vals_gray, self.grid_w, self.grid_h, nl, cal)
        return _classify_color(
            vals_rgb, self.grid_w, self.grid_h, nl, nc, cal)

    @staticmethod
    def _crc_ok(raw):
        """Check if raw bytes form a valid V3 packet (CRC matches)."""
        if not raw or len(raw) < V3_HEADER_SIZE:
            return False
        try:
            ver, sid_b, idx, total, exp_crc, fnlen, dlen = _V3_FMT.unpack(
                raw[:V3_HEADER_SIZE])
        except struct.error:
            return False
        if ver not in _VER_INV:
            return False
        fn_end = V3_HEADER_SIZE + fnlen
        d_end = fn_end + dlen
        if d_end > len(raw):
            return False
        payload = raw[fn_end:d_end]
        return binascii.crc32(payload) & 0xFFFFFFFF == exp_crc

    def _try_decode_with_H(self, bgr, gray, H_mat):
        """Warp, sample, then try calibration detection or data decode.

        When a data decode CRC fails, cached calibrations for other
        protocols are tried before giving up.
        """
        result = _warp_and_sample_rgb(bgr, H_mat, self.grid_w, self.grid_h)
        if result is None:
            return None
        vals_rgb, lo, hi = result

        # Try calibration detection with fallbacks for both n_colors and n_levels
        color_candidates = [self.n_colors]
        if self.n_colors > 1:
            color_candidates.append(1)
        level_candidates = [self.n_levels]
        if self.n_levels != 4:
            level_candidates.append(4)
        if self.n_levels != 8:
            level_candidates.append(8)
        for nc in color_candidates:
            for nl in level_candidates:
                if nc == 1:
                    vals_gray = (vals_rgb[:, :, 0] + vals_rgb[:, :, 1] + vals_rgb[:, :, 2]) / 3.0
                    cal = _try_detect_calibration_gray(vals_gray, self.grid_h, nl)
                else:
                    cal = _try_detect_calibration_color(
                        vals_rgb, self.grid_w, self.grid_h, nl, nc)
                if cal is not None:
                    self._calibration = cal
                    _shared_calib_store(cal["n_colors"], cal["n_levels"], cal)
                    self.n_colors = cal["n_colors"]
                    self.n_levels = cal["n_levels"]
                    return CalibResult(cal["n_colors"], cal["n_levels"])

        # Data decode with current params
        raw = self._classify_with_params(
            vals_rgb, self.n_colors, self.n_levels, self._calibration)

        # Check END frame fallback for color modes
        if raw is not None and self.n_colors > 1 and not is_v3_end_packet(raw):
            gray_cal = None
            if self._calibration is not None and "c0" in self._calibration:
                gray_cal = {"gray": self._calibration["c0"],
                            "n_colors": 1, "n_levels": self.n_levels}
            raw_end = self._classify_with_params(vals_rgb, 1, self.n_levels, gray_cal)
            if is_v3_end_packet(raw_end):
                return raw_end

        # If CRC passes with current params, return immediately
        if raw is not None and self._crc_ok(raw):
            return raw

        # CRC failed — try other cached calibrations (shared across threads)
        for (cc_nc, cc_nl), cc_cal in _shared_calib_snapshot().items():
            if (cc_nc, cc_nl) == (self.n_colors, self.n_levels):
                continue
            alt_raw = self._classify_with_params(vals_rgb, cc_nc, cc_nl, cc_cal)
            if alt_raw is not None and self._crc_ok(alt_raw):
                self.n_colors = cc_nc
                self.n_levels = cc_nl
                self._calibration = cc_cal
                return alt_raw

            # Also try END packet decode for color alternatives
            if cc_nc > 1 and alt_raw is not None and not is_v3_end_packet(alt_raw):
                alt_gray_cal = None
                if cc_cal is not None and "c0" in cc_cal:
                    alt_gray_cal = {"gray": cc_cal["c0"],
                                    "n_colors": 1, "n_levels": cc_nl}
                alt_end = self._classify_with_params(vals_rgb, 1, cc_nl, alt_gray_cal)
                if is_v3_end_packet(alt_end):
                    self.n_colors = cc_nc
                    self.n_levels = cc_nl
                    self._calibration = cc_cal
                    return alt_end

        return raw

    def decode(self, frame_bgr):
        """Decode a visual frame.

        Returns:
          - raw V3 packet bytes on success
          - _CALIB_MARKER if a calibration frame was detected
          - None on failure
        """
        import cv2
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)

        if self._H_mat is not None:
            raw = self._try_decode_with_H(frame_bgr, gray, self._H_mat)
            if raw is not None:
                self._consec_fail = 0
                return raw
            self._consec_fail += 1
            if self._consec_fail < self._RELOCATE_AFTER:
                return None

        H_mat = self._locate(gray)
        if H_mat is None:
            return None
        return self._try_decode_with_H(frame_bgr, gray, H_mat)


def is_calib_marker(raw):
    return isinstance(raw, CalibResult) or raw == _CALIB_MARKER


def decode_frame(frame_bgr, grid_w=160, grid_h=96, n_levels=4, n_colors=1):
    """Stateless wrapper — uses a per-thread cached FrameDecoder."""
    import threading
    _tls = getattr(decode_frame, '_tls', None)
    if _tls is None:
        _tls = threading.local()
        decode_frame._tls = _tls

    key = (grid_w, grid_h, n_levels, n_colors)
    decoder = getattr(_tls, 'decoder', None)
    cur_key = getattr(_tls, 'key', None)
    if decoder is None or cur_key != key:
        decoder = FrameDecoder(grid_w, grid_h, n_levels, n_colors)
        _tls.decoder = decoder
        _tls.key = key

    return decoder.decode(frame_bgr)
