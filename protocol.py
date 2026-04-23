# -*- coding: utf-8 -*-
"""
Shared protocol for QR Air Gap data transfer.
Compatible with Python 3.7+.

Protocol v1: JSON + base64, QR byte mode (original)
Protocol v2: base45, QR alphanumeric mode (~25% more data per QR frame)
Protocol v3: gray4 visual frame (see visual_transport.py)

The receiver auto-detects the version -- no configuration needed.
V1/V2 are text-based (QR payload strings), V3 is binary (handled by
visual_transport.encode_v3_packet / decode_v3_packet).
"""
import base64
import binascii
import json
import os
import random
import string
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# Base45 codec (RFC 9285)
# Charset matches QR alphanumeric mode exactly:
#   0-9  A-Z  SP $ % * + - . / :
# ---------------------------------------------------------------------------

_B45_CHARSET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ $%*+-./:"
_B45_DECODE_MAP = {c: i for i, c in enumerate(_B45_CHARSET)}


def b45encode(data):
    # type: (bytes) -> str
    out = []
    for i in range(0, len(data) - 1, 2):
        n = data[i] * 256 + data[i + 1]
        c, n = n % 45, n // 45
        d, e = n % 45, n // 45
        out.append(_B45_CHARSET[c])
        out.append(_B45_CHARSET[d])
        out.append(_B45_CHARSET[e])
    if len(data) % 2:
        n = data[-1]
        out.append(_B45_CHARSET[n % 45])
        out.append(_B45_CHARSET[n // 45])
    return ''.join(out)


def b45decode(s):
    # type: (str) -> Optional[bytes]
    out = []
    i = 0
    while i + 2 < len(s):
        try:
            c = _B45_DECODE_MAP[s[i]]
            d = _B45_DECODE_MAP[s[i + 1]]
            e = _B45_DECODE_MAP[s[i + 2]]
        except KeyError:
            return None
        n = c + d * 45 + e * 2025
        if n > 65535:
            return None
        out.append(n >> 8)
        out.append(n & 0xFF)
        i += 3
    if i + 1 < len(s):
        try:
            c = _B45_DECODE_MAP[s[i]]
            d = _B45_DECODE_MAP[s[i + 1]]
        except KeyError:
            return None
        n = c + d * 45
        if n > 255:
            return None
        out.append(n)
    elif i < len(s):
        return None  # dangling single char is invalid
    return bytes(out)


# ---------------------------------------------------------------------------
# Session / chunking (shared)
# ---------------------------------------------------------------------------

def generate_session_id(uppercase=False):
    # type: (bool) -> str
    chars = (string.ascii_uppercase if uppercase else string.ascii_lowercase) + string.digits
    return ''.join(random.choices(chars, k=4))


def normalize_transfer_filename(filepath, base_dir=None):
    # type: (str, Optional[str]) -> str
    """Relative path or basename, always with '/' separators (cross-platform)."""
    if base_dir:
        name = os.path.relpath(filepath, base_dir)
    else:
        name = os.path.basename(filepath)
    return os.path.normpath(name).replace("\\", "/")


def parse_session_id(sid, protocol):
    # type: (Optional[str], int) -> str
    """Four-character session id: random if sid is None/empty, else validated."""
    if not sid or not str(sid).strip():
        return generate_session_id(uppercase=(protocol == 2))
    s = str(sid).strip()
    if len(s) != 4:
        raise ValueError("session id must be exactly 4 alphanumeric characters")
    allowed = string.ascii_letters + string.digits
    for c in s:
        if c not in allowed:
            raise ValueError("session id must be alphanumeric (A-Z, a-z, 0-9)")
    return s.upper() if protocol == 2 else s.lower()


def chunk_data(data, chunk_size=400):
    # type: (bytes, int) -> List[bytes]
    chunks = []
    for i in range(0, len(data), chunk_size):
        chunks.append(data[i:i + chunk_size])
    return chunks


# ---------------------------------------------------------------------------
# V1 protocol -- JSON + base64, QR byte mode
# ---------------------------------------------------------------------------

def _encode_chunk_v1(sid, idx, total, data, filename=""):
    encoded = base64.b64encode(data).decode('ascii')
    crc = binascii.crc32(data) & 0xFFFFFFFF
    payload = {"s": sid, "i": idx, "t": total, "d": encoded, "c": crc}
    if filename:
        payload["f"] = filename
    return json.dumps(payload, separators=(',', ':'))


def _decode_chunk_v1(payload):
    try:
        obj = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return None
    required = {"s", "i", "t", "d", "c"}
    if not required.issubset(obj.keys()):
        return None
    try:
        data = base64.b64decode(obj["d"])
    except Exception:
        return None
    crc = binascii.crc32(data) & 0xFFFFFFFF
    if crc != obj["c"]:
        return None
    return {
        "sid": obj["s"], "idx": obj["i"], "total": obj["t"],
        "data": data, "filename": obj.get("f", ""),
    }


# ---------------------------------------------------------------------------
# V2 protocol -- base45, QR alphanumeric mode
#
# Fixed header (32 chars):
#   [0]       "2"           version marker
#   [1:5]     SID           4-char uppercase alphanumeric
#   [5:13]    IDX           8-digit 0-padded decimal  (max 99999999)
#   [13:21]   TOTAL         8-digit 0-padded decimal  (max 99999999)
#   [21:29]   CRC32         8-char uppercase hex
#   [29:32]   FNLEN         3-digit 0-padded decimal  (base45 fname length)
# Variable:
#   [28:28+FNLEN]   filename in base45
#   [28+FNLEN:]     data in base45
#
# All characters stay within QR alphanumeric charset, so the qrcode library
# automatically selects alphanumeric mode.  Net effect vs v1 (JSON+base64):
# ~50% more useful data per QR frame at the same QR version.
#
# NOTE: v2.0 used 4-digit idx/total (header=24), overflowed at >9999 chunks.
# v2.1 uses 8-digit fields (header=32). Decoder tries 32-char first, then
# falls back to 24-char for backward compatibility.
# ---------------------------------------------------------------------------

_V2_HEADER_LEN = 32
_V2_HEADER_LEN_LEGACY = 24


def _encode_chunk_v2(sid, idx, total, data, filename=""):
    if total > 99999999:
        raise ValueError(
            "V2 protocol supports max 99999999 chunks (got {}). "
            "Use --protocol 1 or increase --chunk-size.".format(total))
    crc = binascii.crc32(data) & 0xFFFFFFFF
    fname_b45 = b45encode(filename.encode('utf-8')) if filename else ""
    header = "2{sid}{idx:08d}{total:08d}{crc:08X}{fnlen:03d}".format(
        sid=sid[:4].upper().ljust(4, '0'),
        idx=idx, total=total, crc=crc, fnlen=len(fname_b45))
    return header + fname_b45 + b45encode(data)


def _decode_chunk_v2(payload):
    result = _decode_chunk_v2_inner(payload, _V2_HEADER_LEN, 8)
    if result is not None:
        return result
    return _decode_chunk_v2_inner(payload, _V2_HEADER_LEN_LEGACY, 4)


def _decode_chunk_v2_inner(payload, hdr_len, num_width):
    # type: (str, int, int) -> Optional[Dict]
    if len(payload) < hdr_len:
        return None
    try:
        sid = payload[1:5]
        idx_end = 5 + num_width
        total_end = idx_end + num_width
        idx = int(payload[5:idx_end])
        total = int(payload[idx_end:total_end])
        expected_crc = int(payload[total_end:total_end + 8], 16)
        fnlen = int(payload[total_end + 8:total_end + 11])
    except (ValueError, IndexError):
        return None
    data_start = total_end + 11
    fname_b45 = payload[data_start:data_start + fnlen]
    data_b45 = payload[data_start + fnlen:]
    filename = ""
    if fname_b45:
        fname_bytes = b45decode(fname_b45)
        if fname_bytes is None:
            return None
        filename = fname_bytes.decode('utf-8', errors='replace')
    data = b45decode(data_b45)
    if data is None:
        return None
    actual_crc = binascii.crc32(data) & 0xFFFFFFFF
    if actual_crc != expected_crc:
        return None
    return {"sid": sid, "idx": idx, "total": total, "data": data, "filename": filename}


# ---------------------------------------------------------------------------
# Public API -- encode dispatches on protocol, decode auto-detects
# ---------------------------------------------------------------------------

def encode_chunk(sid, idx, total, data, filename="", protocol=1):
    # type: (str, int, int, bytes, str, int) -> str
    if protocol == 2:
        return _encode_chunk_v2(sid, idx, total, data, filename)
    return _encode_chunk_v1(sid, idx, total, data, filename)


def decode_chunk(payload):
    # type: (str) -> Optional[Dict]
    if payload and len(payload) >= _V2_HEADER_LEN_LEGACY and payload[0] == '2':
        result = _decode_chunk_v2(payload)
        if result is not None:
            return result
    return _decode_chunk_v1(payload)


def decode_chunk_verbose(payload):
    # type: (str) -> (Optional[Dict], str)
    """Like decode_chunk but returns (result, reason) for diagnostics."""
    if not payload:
        return None, "empty payload"

    if len(payload) >= _V2_HEADER_LEN_LEGACY and payload[0] == '2':
        result, reason = _decode_chunk_v2_verbose(payload)
        if result is not None:
            return result, "v2 ok"
        v2_reason = reason
    else:
        v2_reason = None

    result, reason = _decode_chunk_v1_verbose(payload)
    if result is not None:
        return result, "v1 ok"

    if v2_reason:
        return None, "v2: {} / v1: {}".format(v2_reason, reason)
    return None, "v1: {}".format(reason)


def _decode_chunk_v1_verbose(payload):
    # type: (str) -> (Optional[Dict], str)
    try:
        obj = json.loads(payload)
    except (json.JSONDecodeError, ValueError) as e:
        return None, "json parse: {}".format(e)
    if not isinstance(obj, dict):
        return None, "not a dict: {}".format(type(obj).__name__)
    required = {"s", "i", "t", "d", "c"}
    missing = required - set(obj.keys())
    if missing:
        return None, "missing keys: {}".format(missing)
    try:
        data = base64.b64decode(obj["d"])
    except Exception as e:
        return None, "base64: {}".format(e)
    crc = binascii.crc32(data) & 0xFFFFFFFF
    if crc != obj["c"]:
        return None, "crc mismatch: computed={:#010x} expected={:#010x}".format(crc, obj["c"])
    return {
        "sid": obj["s"], "idx": obj["i"], "total": obj["t"],
        "data": data, "filename": obj.get("f", ""),
    }, "ok"


def _decode_chunk_v2_verbose(payload):
    # type: (str) -> (Optional[Dict], str)
    for hdr_len, nw, label in [(_V2_HEADER_LEN, 8, "v2.1"), (_V2_HEADER_LEN_LEGACY, 4, "v2.0")]:
        if len(payload) < hdr_len:
            continue
        try:
            sid = payload[1:5]
            idx_end = 5 + nw
            total_end = idx_end + nw
            idx = int(payload[5:idx_end])
            total = int(payload[idx_end:total_end])
            expected_crc = int(payload[total_end:total_end + 8], 16)
            fnlen = int(payload[total_end + 8:total_end + 11])
        except (ValueError, IndexError) as e:
            continue
        data_start = total_end + 11
        fname_b45 = payload[data_start:data_start + fnlen]
        data_b45 = payload[data_start + fnlen:]
        filename = ""
        if fname_b45:
            fname_bytes = b45decode(fname_b45)
            if fname_bytes is None:
                return None, "{} fname b45 decode failed: {!r}".format(label, fname_b45[:50])
            filename = fname_bytes.decode('utf-8', errors='replace')
        data = b45decode(data_b45)
        if data is None:
            return None, "{} data b45 decode failed (len={})".format(label, len(data_b45))
        actual_crc = binascii.crc32(data) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            return None, "{} crc mismatch: computed={:#010x} expected={:#010x}".format(
                label, actual_crc, expected_crc)
        return {"sid": sid, "idx": idx, "total": total, "data": data, "filename": filename}, "ok"
    return None, "header parse failed for both v2.1 and v2.0 (header={!r})".format(payload[:35])


def encode_end_signal(protocol=1):
    # type: (int) -> str
    """V1/V2 text END signal.  V3 uses visual_transport.encode_v3_end_packet()."""
    if protocol == 2:
        return "2END"
    return json.dumps({"END": True}, separators=(',', ':'))


def is_end_signal(payload):
    # type: (str) -> bool
    if payload == "2END":
        return True
    try:
        obj = json.loads(payload)
        return obj.get("END", False) is True
    except (json.JSONDecodeError, ValueError):
        return False
