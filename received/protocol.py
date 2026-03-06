# -*- coding: utf-8 -*-
"""
Shared protocol for QR Air Gap data transfer.
Compatible with Python 3.7+.
"""
import base64
import binascii
import json
import random
import string
from typing import Dict, List, Optional


def generate_session_id():
    # type: () -> str
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=4))


def chunk_data(data, chunk_size=400):
    # type: (bytes, int) -> List[bytes]
    chunks = []
    for i in range(0, len(data), chunk_size):
        chunks.append(data[i:i + chunk_size])
    return chunks


def encode_chunk(sid, idx, total, data, filename=""):
    # type: (str, int, int, bytes, str) -> str
    encoded = base64.b64encode(data).decode('ascii')
    crc = binascii.crc32(data) & 0xFFFFFFFF
    payload = {
        "s": sid,
        "i": idx,
        "t": total,
        "d": encoded,
        "c": crc,
    }
    if filename:
        payload["f"] = filename
    return json.dumps(payload, separators=(',', ':'))


def decode_chunk(payload):
    # type: (str) -> Optional[Dict]
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
        "sid": obj["s"],
        "idx": obj["i"],
        "total": obj["t"],
        "data": data,
        "filename": obj.get("f", ""),
    }


def encode_end_signal():
    # type: () -> str
    return json.dumps({"END": True}, separators=(',', ':'))


def is_end_signal(payload):
    # type: (str) -> bool
    try:
        obj = json.loads(payload)
        return obj.get("END", False) is True
    except (json.JSONDecodeError, ValueError):
        return False
