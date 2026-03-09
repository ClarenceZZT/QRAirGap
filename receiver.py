# -*- coding: utf-8 -*-
"""
QR Air Gap Receiver — runs on the local machine (macOS).
Captures a screen region, decodes QR codes, and reassembles files.

Supports multi-file reception: when all chunks for one session are received,
the file is saved and the receiver automatically starts listening for the next.

Usage:
    python receiver.py --outdir ./received/ [--fps 5]
    python receiver.py --region 100,200,600,650 --outdir ./received/
"""
import argparse
import ctypes
import ctypes.util
import os
import platform
import queue
import select
import subprocess
import sys
import termios
import threading
import time
import tty
import tkinter as tk

if platform.system() == "Darwin":
    _orig_find_library = ctypes.util.find_library
    def _patched_find_library(name):
        result = _orig_find_library(name)
        if result is None and name == "zbar":
            for p in ["/opt/homebrew/lib/libzbar.dylib", "/usr/local/lib/libzbar.dylib"]:
                if os.path.exists(p):
                    return p
        return result
    ctypes.util.find_library = _patched_find_library

import cv2
import mss
import numpy as np
from pyzbar.pyzbar import decode as _pyzbar_decode_raw, ZBarSymbol

from protocol import decode_chunk, is_end_signal

_thread_local = threading.local()
_receiver_verbose = False


def _get_cv2_qr_detector():
    if not hasattr(_thread_local, 'detector'):
        _thread_local.detector = cv2.QRCodeDetector()
    return _thread_local.detector


def _log_v(msg):
    if _receiver_verbose:
        print(msg)


def pyzbar_decode(image):
    return _pyzbar_decode_raw(image, symbols=[ZBarSymbol.QRCODE])


class RegionSelector(object):
    """Transparent overlay for selecting a capture region.
    Uses a normal Tkinter window so it participates in standard window ordering
    — the user can Cmd+Tab or click other windows to bring them in front.
    """

    HANDLE_SIZE = 8

    def __init__(self):
        self.region = None
        self._start_x = 0
        self._start_y = 0
        self._drag_mode = None
        self._move_offset = (0, 0)

        self.root = tk.Tk()
        self.root.title("Select QR region — drag to select, Enter to confirm, Esc to cancel")
        self.root.attributes("-alpha", 0.3)
        self.root.configure(bg="gray")

        scr_w = self.root.winfo_screenwidth()
        scr_h = self.root.winfo_screenheight()
        self.root.geometry("{}x{}+0+0".format(scr_w, scr_h))

        self.canvas = tk.Canvas(self.root, cursor="cross", bg="gray", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)

        self.rect = None
        self.handles = []
        self._rect_coords = None

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.root.bind("<Return>", self._confirm)
        self.root.bind("<Escape>", lambda e: self.root.destroy())

        self.info_text = self.canvas.create_text(
            scr_w // 2, 40,
            text="Drag to select region. Drag edges/corners to resize, drag inside to move.\n"
                 "Press Enter to confirm, Esc to cancel.\n"
                 "You can Cmd+Tab to switch windows and arrange your desktop.",
            font=("Helvetica", 16), fill="white", justify=tk.CENTER,
        )
        self.size_text = None

    def run(self):
        self.root.mainloop()
        return self.region

    def _update_size_label(self):
        if self._rect_coords is None:
            return
        x1, y1, x2, y2 = self._rect_coords
        w, h = int(x2 - x1), int(y2 - y1)
        cx, cy = (x1 + x2) / 2, y2 + 20
        label = "{}x{} at ({},{})".format(w, h, int(x1), int(y1))
        if self.size_text:
            self.canvas.coords(self.size_text, cx, cy)
            self.canvas.itemconfig(self.size_text, text=label)
        else:
            self.size_text = self.canvas.create_text(
                cx, cy, text=label, font=("Courier", 14, "bold"), fill="yellow",
            )

    def _draw_handles(self):
        for h in self.handles:
            self.canvas.delete(h)
        self.handles.clear()
        if self._rect_coords is None:
            return
        x1, y1, x2, y2 = self._rect_coords
        hs = self.HANDLE_SIZE
        corners = {
            "nw": (x1, y1), "ne": (x2, y1), "sw": (x1, y2), "se": (x2, y2),
            "n": ((x1+x2)/2, y1), "s": ((x1+x2)/2, y2),
            "w": (x1, (y1+y2)/2), "e": (x2, (y1+y2)/2),
        }
        for name, (cx, cy) in corners.items():
            h = self.canvas.create_rectangle(
                cx - hs, cy - hs, cx + hs, cy + hs,
                fill="yellow", outline="red", width=1, tags="handle_" + name,
            )
            self.handles.append(h)

    def _hit_test(self, x, y):
        if self._rect_coords is None:
            return None
        x1, y1, x2, y2 = self._rect_coords
        hs = self.HANDLE_SIZE + 4
        corners = {
            "nw": (x1, y1), "ne": (x2, y1), "sw": (x1, y2), "se": (x2, y2),
            "n": ((x1+x2)/2, y1), "s": ((x1+x2)/2, y2),
            "w": (x1, (y1+y2)/2), "e": (x2, (y1+y2)/2),
        }
        for name, (cx, cy) in corners.items():
            if abs(x - cx) <= hs and abs(y - cy) <= hs:
                return name
        if x1 <= x <= x2 and y1 <= y <= y2:
            return "move"
        return None

    def _on_press(self, event):
        x, y = event.x, event.y
        hit = self._hit_test(x, y)
        if hit == "move":
            self._drag_mode = "move"
            x1, y1 = self._rect_coords[0], self._rect_coords[1]
            self._move_offset = (x - x1, y - y1)
        elif hit is not None:
            self._drag_mode = hit
        else:
            self._drag_mode = "new"
            self._start_x = x
            self._start_y = y
            if self.rect:
                self.canvas.delete(self.rect)
            for h in self.handles:
                self.canvas.delete(h)
            self.handles.clear()
            self.rect = self.canvas.create_rectangle(
                x, y, x, y, outline="red", width=2,
            )
            self._rect_coords = None

    def _on_drag(self, event):
        x, y = event.x, event.y
        if self._drag_mode == "new":
            self.canvas.coords(self.rect, self._start_x, self._start_y, x, y)
            self._rect_coords = (
                min(self._start_x, x), min(self._start_y, y),
                max(self._start_x, x), max(self._start_y, y),
            )
        elif self._drag_mode == "move" and self._rect_coords:
            ox, oy = self._move_offset
            x1, y1, x2, y2 = self._rect_coords
            w, h = x2 - x1, y2 - y1
            nx1, ny1 = x - ox, y - oy
            self._rect_coords = (nx1, ny1, nx1 + w, ny1 + h)
            self.canvas.coords(self.rect, nx1, ny1, nx1 + w, ny1 + h)
        elif self._rect_coords:
            x1, y1, x2, y2 = self._rect_coords
            if "n" in self._drag_mode:
                y1 = y
            if "s" in self._drag_mode:
                y2 = y
            if "w" in self._drag_mode:
                x1 = x
            if "e" in self._drag_mode:
                x2 = x
            self._rect_coords = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
            self.canvas.coords(self.rect, *self._rect_coords)

        self._draw_handles()
        self._update_size_label()

    def _on_release(self, event):
        if self._drag_mode == "new":
            x, y = event.x, event.y
            self._rect_coords = (
                min(self._start_x, x), min(self._start_y, y),
                max(self._start_x, x), max(self._start_y, y),
            )
        self._drag_mode = None
        if self._rect_coords:
            self._draw_handles()
            self._update_size_label()

    def _confirm(self, event=None):
        if self._rect_coords is None:
            return
        x1, y1, x2, y2 = self._rect_coords
        w, h = x2 - x1, y2 - y1
        if w > 20 and h > 20:
            win_x = self.root.winfo_rootx()
            win_y = self.root.winfo_rooty()
            self.region = {
                "left": int(x1) + win_x,
                "top": int(y1) + win_y,
                "width": int(w),
                "height": int(h),
            }
        self.root.destroy()


_decode_stats = {"total": 0, "fast_ok": 0}


def try_decode_qr(frame_bgr):
    """Try pyzbar + cv2 QRCodeDetector with multiple preprocessing strategies.
    Skips expensive upscale strategy when fast strategies have high success rate."""
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)

    _decode_stats["total"] += 1

    results = pyzbar_decode(gray)
    if results:
        _decode_stats["fast_ok"] += 1
        return results[0].data.decode("utf-8", errors="replace")

    text, _, _ = _get_cv2_qr_detector().detectAndDecode(gray)
    if text:
        _decode_stats["fast_ok"] += 1
        return text

    thresh = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 51, 11
    )
    results = pyzbar_decode(thresh)
    if results:
        _decode_stats["fast_ok"] += 1
        return results[0].data.decode("utf-8", errors="replace")

    text, _, _ = _get_cv2_qr_detector().detectAndDecode(thresh)
    if text:
        _decode_stats["fast_ok"] += 1
        return text

    stats = _decode_stats
    if stats["total"] > 30 and stats["fast_ok"] / stats["total"] > 0.9:
        return None

    h, w = gray.shape
    upscaled = cv2.resize(gray, (w * 2, h * 2), interpolation=cv2.INTER_CUBIC)
    results = pyzbar_decode(upscaled)
    if results:
        return results[0].data.decode("utf-8", errors="replace")
    text, _, _ = _get_cv2_qr_detector().detectAndDecode(upscaled)
    if text:
        return text

    return None


def print_progress(sid, received_count, total, fps, filename=""):
    bar_len = 30
    filled = int(bar_len * received_count / total) if total else 0
    bar = "#" * filled + "-" * (bar_len - filled)
    pct = 100.0 * received_count / total if total else 0
    try:
        term_width = os.get_terminal_size().columns
    except (AttributeError, ValueError, OSError):
        term_width = 120
    content = "[{}] [{}] {}/{} ({:.0f}%) @{}fps".format(
        sid, bar, received_count, total, pct, int(fps)
    )
    if filename:
        max_name = term_width - len(content) - 4
        if max_name > 5:
            name = filename if len(filename) <= max_name else "..." + filename[-(max_name - 3):]
            content += "  " + name
    sys.stdout.write("\r" + content.ljust(term_width - 1))
    sys.stdout.flush()


def _startup_check():
    """Verify pyzbar can actually decode a QR code."""
    try:
        import qrcode
    except ImportError:
        _vendor_zip = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'qrcode_vendor.zip')
        if os.path.isfile(_vendor_zip):
            sys.path.insert(0, _vendor_zip)
            import qrcode
        else:
            print("[check] qrcode not available, skipping self-test")
            return True

    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=6, border=2)
    qr.add_data('selftest')
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").get_image().convert("L")
    arr = np.array(img)
    results = pyzbar_decode(arr)
    if results and results[0].data == b'selftest':
        print("[check] pyzbar self-test PASSED")
        return True
    else:
        print("[check] pyzbar self-test FAILED — decode returned: {}".format(results))
        return False


def _check_key_pressed():
    """Non-blocking check for key press (Unix only)."""
    if select.select([sys.stdin], [], [], 0)[0]:
        ch = sys.stdin.read(1)
        return ch
    return None


_ACTIVATE_SENDER_SCRIPT = '''
tell application "System Events"
    repeat with p in (every process whose background only is false)
        try
            repeat with w in every window of p
                if name of w contains "QR Air Gap" then
                    set frontmost of p to true
                    return "ok"
                end if
            end repeat
        end try
    end repeat
    return "not found"
end tell
'''


def _activate_sender_window():
    """Bring the sender's Tk window to the front before sending keystrokes."""
    try:
        result = subprocess.run(
            ['osascript', '-e', _ACTIVATE_SENDER_SCRIPT],
            timeout=5, capture_output=True,
        )
        status = result.stdout.decode().strip()
        if status == "not found":
            print("[auto] Warning: 'QR Air Gap' window not found, sending to frontmost")
            return False
        return True
    except Exception:
        return False


def _send_keystroke_async(key, delay=0.5):
    """Simulate a keystroke in a background thread (non-blocking)."""
    def _do():
        time.sleep(delay)
        try:
            _activate_sender_window()
            time.sleep(0.2)
            subprocess.run(
                ['osascript', '-e',
                 'tell application "System Events" to keystroke "{}"'.format(key)],
                timeout=5, capture_output=True,
            )
            print("[auto] Sent '{}' keystroke".format(key))
        except Exception as e:
            print("[auto] Failed to send keystroke: {}".format(e))
    threading.Thread(target=_do, daemon=True).start()


_sending_missing = False


def _encode_ranges(indices):
    """Encode sorted indices as compact ranges: [1,2,3,5,6,8] -> '1-3,5-6,8'"""
    if not indices:
        return ""
    s = sorted(indices)
    ranges = []
    start = end = s[0]
    for i in s[1:]:
        if i == end + 1:
            end = i
        else:
            ranges.append("{}-{}".format(start, end) if start != end else str(start))
            start = end = i
    ranges.append("{}-{}".format(start, end) if start != end else str(start))
    return ",".join(ranges)


def _send_missing_frames_async(missing_indices, delay=0.3, max_csv_len=200):
    """Send missing frame indices to sender via keystroke with range encoding.

    If the range-encoded CSV exceeds max_csv_len, it is split into multiple
    batches sent sequentially. The sender accumulates frames from each batch.
    """
    global _sending_missing
    if _sending_missing:
        return
    _sending_missing = True

    def _do():
        global _sending_missing
        try:
            time.sleep(delay)
            range_csv = _encode_ranges(missing_indices)

            if len(range_csv) <= max_csv_len:
                batches = [range_csv]
            else:
                parts = range_csv.split(',')
                batches = []
                cur = []
                cur_len = 0
                for part in parts:
                    added = len(part) + (1 if cur else 0)
                    if cur_len + added > max_csv_len and cur:
                        batches.append(",".join(cur))
                        cur = [part]
                        cur_len = len(part)
                    else:
                        cur.append(part)
                        cur_len += added
                if cur:
                    batches.append(",".join(cur))

            _activate_sender_window()
            time.sleep(0.3)
            for i, batch_csv in enumerate(batches):
                prefix = "m" if i == 0 else "a"
                payload = "{p}{r},{r}.".format(p=prefix, r=batch_csv)
                subprocess.run(
                    ['osascript', '-e',
                     'tell application "System Events" to keystroke "{}"'.format(payload)],
                    timeout=10, capture_output=True,
                )
                if len(batches) > 1:
                    print("[auto] Batch {}/{}: {} chars".format(
                        i + 1, len(batches), len(payload)))
                if i < len(batches) - 1:
                    time.sleep(0.5)
            print("[auto] Sent missing frames: {} frames ({} batch{}, {} total chars)".format(
                len(missing_indices), len(batches),
                "es" if len(batches) > 1 else "",
                sum(len("m{r},{r}.".format(r=b)) for b in batches)))
        except Exception as e:
            print("[auto] Failed to send missing frames: {}".format(e))
        finally:
            _sending_missing = False
    threading.Thread(target=_do, daemon=True).start()


def _safe_filename(filename):
    """Strip path traversal components to keep files inside outdir."""
    parts = filename.replace("\\", "/").split("/")
    safe = [p for p in parts if p and p not in ("..", ".")]
    return os.path.join(*safe) if safe else None


def _save_file(received, total, filename, outdir):
    # type: (dict, int, str, str) -> str
    """Assemble chunks and save to outdir. Returns the output path."""
    if not filename:
        filename = "received_{}.bin".format(int(time.time()))
    filename = _safe_filename(filename) or "received_{}.bin".format(int(time.time()))
    outpath = os.path.join(outdir, filename)
    os.makedirs(os.path.dirname(outpath), exist_ok=True)

    missing = set(range(total)) - set(received.keys())
    parts = []
    for i in range(total):
        if i in received:
            parts.append(received[i])
        else:
            parts.append(b"[MISSING CHUNK %d]" % i)
    output_data = b"".join(parts)

    with open(outpath, "wb") as f:
        f.write(output_data)

    if missing:
        print("\nSaved (incomplete): {} ({} B, {} missing chunks)".format(
            outpath, len(output_data), len(missing)
        ))
    else:
        print("\nSaved: {} ({} B, all {} chunks OK)".format(
            outpath, len(output_data), total
        ))
    return outpath


def _capture_thread(region, frame_queue, stop_event, interval, debug):
    """Producer: captures screen frames at the given rate."""
    frame_count = 0
    with mss.mss() as sct:
        while not stop_event.is_set():
            t0 = time.time()
            screenshot = sct.grab(region)
            frame = np.array(screenshot)
            frame_bgr = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
            frame_count += 1

            if debug and frame_count <= 20:
                path = "debug_frames/frame_{:04d}.png".format(frame_count)
                cv2.imwrite(path, frame_bgr)

            try:
                frame_queue.put_nowait((frame_count, frame_bgr))
            except queue.Full:
                pass

            elapsed = time.time() - t0
            sleep_time = interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)


def _decode_thread(frame_queue, result_queue, stop_event):
    """Consumer: decodes QR codes from captured frames."""
    while not stop_event.is_set():
        try:
            item = frame_queue.get(timeout=0.1)
        except queue.Empty:
            continue
        frame_num, frame_bgr = item
        raw_text = try_decode_qr(frame_bgr)
        result_queue.put((frame_num, frame_bgr.shape[:2], raw_text))


def main():
    parser = argparse.ArgumentParser(description="QR Air Gap Receiver")
    parser.add_argument("--outdir", "-o", default="received",
                        help="Output directory (default: received/)")
    parser.add_argument("--fps", type=float, default=5,
                        help="Capture frames per second (default: 5)")
    parser.add_argument("--timeout", type=float, default=15,
                        help="Seconds of no new chunks before warning (default: 15)")
    parser.add_argument("--debug", action="store_true",
                        help="Save captured frames to debug_frames/")
    parser.add_argument("--region", type=str, default=None,
                        help="Skip GUI selector, use LEFT,TOP,WIDTH,HEIGHT")
    parser.add_argument("--auto-next", action="store_true", default=True,
                        help="Auto-send 'n' keystroke after file complete (default: on)")
    parser.add_argument("--no-auto-next", dest="auto_next", action="store_false",
                        help="Disable auto-sending 'n' keystroke")
    parser.add_argument("--decode-workers", type=int, default=2,
                        help="Number of decode threads (default: 2)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show detailed processing steps")
    args = parser.parse_args()

    global _receiver_verbose
    _receiver_verbose = args.verbose

    print("=== QR Air Gap Receiver ===")

    if not _startup_check():
        print("ERROR: pyzbar cannot decode QR codes. Check zbar installation.")
        print("  macOS: brew install zbar")
        sys.exit(1)

    if args.region:
        parts = [int(x) for x in args.region.split(",")]
        region = {"left": parts[0], "top": parts[1], "width": parts[2], "height": parts[3]}
        print("Using manual region: {}".format(region))
    else:
        print("Select the screen region containing the QR code...")
        selector = RegionSelector()
        region = selector.run()

    if region is None:
        print("No region selected. Exiting.")
        sys.exit(1)

    print("Capture region: {}x{} at ({}, {})".format(
        region["width"], region["height"], region["left"], region["top"]
    ))

    os.makedirs(args.outdir, exist_ok=True)
    if args.debug:
        os.makedirs("debug_frames", exist_ok=True)

    interval = 1.0 / args.fps
    frame_queue = queue.Queue(maxsize=8)
    result_queue = queue.Queue()
    stop_event = threading.Event()

    cap_thread = threading.Thread(
        target=_capture_thread,
        args=(region, frame_queue, stop_event, interval, args.debug),
        daemon=True,
    )
    decode_threads = []
    for _ in range(args.decode_workers):
        t = threading.Thread(
            target=_decode_thread,
            args=(frame_queue, result_queue, stop_event),
            daemon=True,
        )
        decode_threads.append(t)

    print("Capturing at {:.1f} fps, {} decode workers. Press q to stop.\n".format(
        args.fps, args.decode_workers
    ))

    files_saved = []
    received = {}
    session_id = None
    total = None
    current_filename = ""
    last_new_time = time.time()
    warned = False
    decode_fail_count = 0
    end_received = False
    completed_sids = set()
    last_decoded_idx = -1

    old_settings = termios.tcgetattr(sys.stdin)
    try:
        tty.setcbreak(sys.stdin.fileno())

        cap_thread.start()
        for t in decode_threads:
            t.start()

        while True:
            ch = _check_key_pressed()
            if ch == 'q':
                print("\n\nStopped by user (q).")
                break

            batch_count = 0
            while batch_count < 20:
                try:
                    frame_num, shape, raw_text = result_queue.get_nowait()
                except queue.Empty:
                    break
                batch_count += 1

                if raw_text is None:
                    decode_fail_count += 1
                    if decode_fail_count <= 3 or decode_fail_count % 50 == 0:
                        h, w = shape
                        try:
                            tw = os.get_terminal_size().columns
                        except (AttributeError, ValueError, OSError):
                            tw = 120
                        msg = "[scan] frame #{} ({}x{}) — no QR ({} fails)".format(
                            frame_num, w, h, decode_fail_count
                        )
                        sys.stdout.write("\r" + msg.ljust(tw - 1))
                        sys.stdout.flush()
                    continue

                if is_end_signal(raw_text):
                    print("\n\nEND signal received — sender has finished all files.")
                    end_received = True
                    break

                chunk_info = decode_chunk(raw_text)
                if chunk_info is None:
                    continue

                sid = chunk_info["sid"]
                idx = chunk_info["idx"]
                fname = chunk_info.get("filename", "")

                if sid in completed_sids:
                    continue

                if session_id is None:
                    session_id = sid
                    total = chunk_info["total"]
                    current_filename = fname
                    last_decoded_idx = -1
                    sys.stdout.write("\r\033[2K")
                    print("Session detected: {}  File: {}  Chunks: {}".format(
                        sid, fname or "(unnamed)", total
                    ))

                if sid != session_id:
                    received = {}
                    session_id = sid
                    total = chunk_info["total"]
                    current_filename = fname
                    warned = False
                    decode_fail_count = 0
                    last_decoded_idx = -1
                    sys.stdout.write("\r\033[2K")
                    print("\nNew session detected: {}  File: {}  Chunks: {}".format(
                        sid, fname or "(unnamed)", total
                    ))

                is_new = idx not in received
                if is_new:
                    received[idx] = chunk_info["data"]
                    last_new_time = time.time()
                    warned = False
                    if total is not None:
                        print_progress(session_id, len(received), total, args.fps, current_filename)

                same_as_last = (idx == last_decoded_idx)
                last_decoded_idx = idx

                if not is_new and not same_as_last and total is not None and len(received) < total:
                    if not _sending_missing:
                        missing = sorted(set(range(total)) - set(received.keys()))
                        if missing:
                            missing_show = missing[:20]
                            suffix = " ..." if len(missing) > 20 else ""
                            print("\n[resend] Duplicate frame {} detected, missing {} frames [{}{}]".format(
                                idx, len(missing),
                                ", ".join(str(x) for x in missing_show), suffix))
                            _send_missing_frames_async(missing)

                if total is not None and len(received) >= total:
                    _save_file(received, total, current_filename, args.outdir)
                    files_saved.append(current_filename or session_id)
                    completed_sids.add(session_id)

                    received = {}
                    session_id = None
                    total = None
                    current_filename = ""
                    warned = False
                    decode_fail_count = 0
                    last_decoded_idx = -1

                    if args.auto_next:
                        print("Auto-sending 'n' to switch to next file...")
                        _send_keystroke_async("n")
                    print("Waiting for next file... (q to quit)\n")

            if end_received:
                break

            if session_id is not None and not warned:
                if time.time() - last_new_time > args.timeout:
                    remaining = set(range(total)) - set(received.keys())
                    missing_str = ", ".join(str(x) for x in sorted(remaining)[:20])
                    if len(remaining) > 20:
                        missing_str += "..."
                    print("\n[WARN] No new chunks for {:.0f}s. Missing: [{}]".format(
                        args.timeout, missing_str
                    ))
                    warned = True

            time.sleep(0.02)

    except KeyboardInterrupt:
        print("\n\nInterrupted.")
    finally:
        stop_event.set()
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
        cap_thread.join(timeout=2)
        for t in decode_threads:
            t.join(timeout=2)

    if received and total:
        _save_file(received, total, current_filename, args.outdir)
        files_saved.append(current_filename or session_id or "unknown")

    print("\n=== Summary ===")
    if files_saved:
        print("Files received: {}".format(len(files_saved)))
        for f in files_saved:
            print("  - {}".format(f))
    else:
        print("No files received.")
        print("Check debug_frames/frame_0001.png to verify the capture region.")


if __name__ == "__main__":
    main()
