#!/usr/bin/env python3
"""
Build a vendored zip of the qrcode library (+ typing_extensions)
that can be imported directly on the remote server.

Run on local machine:
    python build_qrcode_zip.py

Output:
    qrcode_vendor.zip  (~80-120KB)

Remote usage:
    Place qrcode_vendor.zip alongside sender.py. It auto-loads on import.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile


OUTPUT = "qrcode_vendor.zip"

PACKAGES_TO_VENDOR = [
    "qrcode",             # directory
    "typing_extensions",  # may be .py or directory
    "png",                # pypng — required by qrcode.image.pure
]


def main():
    tmpdir = tempfile.mkdtemp(prefix="qrcode_vendor_")
    print("Installing qrcode==7.4.2 + typing_extensions into temp dir...")

    try:
        subprocess.check_call([
            sys.executable, "-m", "pip", "install",
            "--target", tmpdir,
            "--no-deps",
            "qrcode==7.4.2",
        ])
        subprocess.check_call([
            sys.executable, "-m", "pip", "install",
            "--target", tmpdir,
            "--no-deps",
            "typing_extensions",
        ])
        subprocess.check_call([
            sys.executable, "-m", "pip", "install",
            "--target", tmpdir,
            "--no-deps",
            "pypng",
        ])
    except subprocess.CalledProcessError as e:
        print("pip install failed: {}".format(e))
        shutil.rmtree(tmpdir)
        sys.exit(1)

    print("Packaging into {}...".format(OUTPUT))

    with zipfile.ZipFile(OUTPUT, "w", zipfile.ZIP_DEFLATED) as zf:
        for pkg_name in PACKAGES_TO_VENDOR:
            pkg_dir = os.path.join(tmpdir, pkg_name)
            pkg_file = os.path.join(tmpdir, pkg_name + ".py")

            if os.path.isdir(pkg_dir):
                skip_dirs = {"__pycache__", "tests", "test"}
                for dirpath, dirnames, filenames in os.walk(pkg_dir):
                    dirnames[:] = [
                        d for d in dirnames
                        if d not in skip_dirs and not d.endswith(".dist-info")
                    ]
                    for fn in filenames:
                        if fn.endswith((".pyc", ".pyo")):
                            continue
                        full_path = os.path.join(dirpath, fn)
                        arcname = os.path.relpath(full_path, tmpdir)
                        zf.write(full_path, arcname)
            elif os.path.isfile(pkg_file):
                zf.write(pkg_file, pkg_name + ".py")
            else:
                print("WARNING: {} not found, skipping".format(pkg_name))

    shutil.rmtree(tmpdir)

    size = os.path.getsize(OUTPUT)
    print("Done! {} ({:.1f} KB)".format(OUTPUT, size / 1024))
    print("Transfer this file to the remote server alongside sender.py and protocol.py.")


if __name__ == "__main__":
    main()
