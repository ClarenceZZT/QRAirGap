#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Decode server_bundle.b64 (gzip tar from encode_server_bundle.py) into cwd.

Python 3.7+ — stdlib only (no pip packages required for this script).

Usage:
    python3 decode_server_bundle.py [server_bundle.b64]

Produces:
    ./sender.py
    ./protocol.py
    ./visual_transport.py
    ./qrcode_vendor/...

Then install Pillow on the server if needed:
    pip install 'Pillow>=7.0'
For --protocol 3 (gray4), also install numpy:
    pip install numpy
"""
from __future__ import print_function

import base64
import io
import os
import sys
import tarfile


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "server_bundle.b64"
    if not os.path.isfile(path):
        print("Error: file not found: {}".format(path), file=sys.stderr)
        sys.exit(1)

    with open(path, "r") as f:
        text = f.read()
    # Wrapped base64 lines / spaces — normalize before decode
    b64 = "".join(text.split())
    raw = base64.b64decode(b64)

    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        tar.extractall(".")

    print("OK: extracted sender.py, protocol.py, visual_transport.py, qrcode_vendor/")
    print("Run: pip install 'Pillow>=7.0'  (if Pillow not already installed)")
    print("For --protocol 3 (gray4): pip install numpy")


if __name__ == "__main__":
    main()
