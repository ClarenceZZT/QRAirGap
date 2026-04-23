#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pack files needed on the remote sender host into one base64 text file.

Includes:
  - sender.py
  - protocol.py
  - visual_transport.py  (V3 gray4 visual frame protocol)
  - qrcode_vendor/   (qrcode + typing_extensions + png; no pip on server)

Requires Pillow on the server (tkinter is already there):
    pip install 'Pillow>=7.0'

Build locally:
    python3 encode_server_bundle.py

Output:
    server_bundle.b64

If qrcode_vendor/ is missing: copy it from this repo, or run python3 build_qrcode_zip.py
and unzip qrcode_vendor.zip so that qrcode/, png.py, typing_extensions.py live under ./qrcode_vendor/
(matching sender.py's expected layout).

Transfer to Python 3.7.6 server:
  1. decode_server_bundle.py (small; paste once)
  2. server_bundle.b64      (paste / upload the text file)

On server:
    python3 decode_server_bundle.py
    python3 sender.py --file ...
"""
from __future__ import print_function

import base64
import io
import os
import sys
import tarfile

_HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT = os.path.join(_HERE, "server_bundle.b64")

ROOT_FILES = (
    ("sender.py", "sender.py"),
    ("protocol.py", "protocol.py"),
    ("visual_transport.py", "visual_transport.py"),
)

VENDOR_DIR = os.path.join(_HERE, "qrcode_vendor")

# Match encode_vendor.py exclusions
_EXCLUDE = {".DS_Store", "__pycache__", ".pyc"}


def _should_exclude(name):
    return any(ex in name for ex in _EXCLUDE)


def _add_vendor_tree(tar):
    if not os.path.isdir(VENDOR_DIR):
        print("Error: {} not found.".format(VENDOR_DIR), file=sys.stderr)
        print("  Clone this repo or unpack build_qrcode_zip.py output into qrcode_vendor/", file=sys.stderr)
        sys.exit(1)
    parent = os.path.dirname(VENDOR_DIR)
    for root, dirs, files in os.walk(VENDOR_DIR):
        dirs[:] = [d for d in dirs if not _should_exclude(d)]
        for fn in sorted(files):
            if _should_exclude(fn):
                continue
            full = os.path.join(root, fn)
            arcname = os.path.relpath(full, parent)
            tar.add(full, arcname=arcname)


def main():
    for _, rel in ROOT_FILES:
        path = os.path.join(_HERE, rel)
        if not os.path.isfile(path):
            print("Error: missing {}".format(path), file=sys.stderr)
            sys.exit(1)

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for arcname, rel in ROOT_FILES:
            tar.add(os.path.join(_HERE, rel), arcname=arcname)
        # _add_vendor_tree(tar)

    raw = buf.getvalue()
    b64 = base64.b64encode(raw).decode("ascii")

    with open(OUTPUT, "w") as out:
        for i in range(0, len(b64), 76):
            out.write(b64[i : i + 76] + "\n")

    nlines = (len(b64) + 75) // 76
    print("Wrote {}  (tar.gz {} bytes, base64 {} chars, ~{} lines)".format(
        OUTPUT, len(raw), len(b64), nlines))
    print("Copy decode_server_bundle.py + this file to the server, then:")
    print("  python3 decode_server_bundle.py")


if __name__ == "__main__":
    main()
