#!/usr/bin/env python3
# Decode qrcode_vendor.b64 -> qrcode_vendor/ directory
# Usage: python3 decode_vendor.py [file.b64]
import base64,io,sys,tarfile
f=sys.argv[1] if len(sys.argv)>1 else "qrcode_vendor.b64"
d=base64.b64decode(open(f).read())
tarfile.open(fileobj=io.BytesIO(d)).extractall(".")
print("OK: extracted to qrcode_vendor/")
