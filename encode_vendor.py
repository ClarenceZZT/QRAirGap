#!/usr/bin/env python3
"""
Encode qrcode_vendor/ directory into a single base64 text file.
Run locally, then send the output .txt and decode_vendor.py to the server.

Usage:
    python encode_vendor.py
    # produces: qrcode_vendor.b64
"""
import base64
import io
import os
import tarfile

VENDOR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qrcode_vendor")
OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qrcode_vendor.b64")

EXCLUDE = {".DS_Store", "__pycache__", ".pyc"}


def should_exclude(name):
    return any(ex in name for ex in EXCLUDE)


def main():
    if not os.path.isdir(VENDOR_DIR):
        print("Error: {} not found. Run build_qrcode_zip.py first.".format(VENDOR_DIR))
        raise SystemExit(1)

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for root, dirs, files in os.walk(VENDOR_DIR):
            dirs[:] = [d for d in dirs if not should_exclude(d)]
            for f in sorted(files):
                if should_exclude(f):
                    continue
                full = os.path.join(root, f)
                arcname = os.path.relpath(full, os.path.dirname(VENDOR_DIR))
                tar.add(full, arcname=arcname)

    raw = buf.getvalue()
    b64 = base64.b64encode(raw).decode("ascii")

    with open(OUTPUT, "w") as f:
        for i in range(0, len(b64), 76):
            f.write(b64[i:i+76] + "\n")

    print("Encoded: {} -> {}".format(VENDOR_DIR, OUTPUT))
    print("  tar.gz: {} bytes".format(len(raw)))
    print("  base64: {} bytes ({} lines)".format(os.path.getsize(OUTPUT), len(b64) // 76 + 1))
    print()
    print("Transfer to server:")
    print("  1. qrcode_vendor.b64  ({} bytes)".format(os.path.getsize(OUTPUT)))
    print("  2. decode_vendor.py   (tiny decoder script)")
    print()
    print("On server run:")
    print("  python3 decode_vendor.py")


if __name__ == "__main__":
    main()
