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
from collections import deque
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

from protocol import decode_chunk, decode_chunk_verbose, is_end_signal

_thread_local = threading.local()
_receiver_verbose = False


class SpeedTracker(object):
    """Rolling-window speed tracker for received data."""

    def __init__(self, window=3.0):
        self._window = window
        self._samples = deque()
        self._total_bytes = 0
        self._start_time = None

    def add(self, nbytes):
        now = time.time()
        if self._start_time is None:
            self._start_time = now
        self._samples.append((now, nbytes))
        self._total_bytes += nbytes
        cutoff = now - self._window
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def speed_kbps(self):
        if not self._samples:
            return 0.0
        now = time.time()
        window_start = self._samples[0][0]
        window_elapsed = now - window_start
        if window_elapsed < 0.1:
            if self._start_time is None:
                return 0.0
            total_elapsed = now - self._start_time
            return self._total_bytes / max(total_elapsed, 0.1) / 1024.0
        window_bytes = sum(b for _, b in self._samples)
        return window_bytes / window_elapsed / 1024.0

    def reset(self):
        self._samples.clear()
        self._total_bytes = 0
        self._start_time = None


class PipelineStats:
    """Thread-safe rolling-window FPS counters for each pipeline stage."""

    def __init__(self, window=2.0):
        self._window = window
        self._lock = threading.Lock()
        self._counters = {}  # name -> deque of timestamps

    def tick(self, name):
        now = time.time()
        with self._lock:
            if name not in self._counters:
                self._counters[name] = deque()
            dq = self._counters[name]
            dq.append(now)
            cutoff = now - self._window
            while dq and dq[0] < cutoff:
                dq.popleft()

    def fps(self, name):
        now = time.time()
        with self._lock:
            dq = self._counters.get(name)
            if not dq:
                return 0.0
            cutoff = now - self._window
            while dq and dq[0] < cutoff:
                dq.popleft()
            if not dq:
                return 0.0
            span = now - dq[0]
            if span < 0.05:
                return 0.0
            return len(dq) / span

    def summary(self):
        parts = []
        for n in ("capture", "decode_ok", "dup", "crc_fail", "decode_fail"):
            f = self.fps(n)
            if f > 0 or n == "decode_ok":
                label = n.replace("_", " ")
                parts.append("{}={:.1f}".format(label, f))
        return "  ".join(parts)


_pipeline_stats = PipelineStats()


class SessionLog:
    """Records detailed events during a transfer session for post-run analysis."""

    def __init__(self, outdir="received"):
        self._outdir = outdir
        self._global_start = time.time()
        self._sessions = []      # list of completed session reports
        self._cur = None         # current session dict

    def begin_session(self, sid, filename, total):
        if self._cur is not None and self._cur["sid"] != sid:
            self._finalize_current("interrupted")
        self._cur = {
            "sid": sid,
            "filename": filename,
            "total": total,
            "t_start": time.time(),
            "t_end": None,
            "chunks_received": {},    # idx -> (timestamp, byte_len)
            "decode_fail_frames": [], # (frame_num, timestamp)
            "crc_fail_frames": [],    # (frame_num, timestamp, detail)
            "dup_frames": [],         # (frame_num, timestamp, idx)
            "cycles": [],             # (timestamp, dup_idx, gap_s, missing_count)
            "resends": [],            # (timestamp, trigger, missing_indices)
            "status": "in_progress",
        }

    def chunk_ok(self, idx, data_len, frame_num=None):
        if self._cur is None:
            return
        self._cur["chunks_received"][idx] = (time.time(), data_len, frame_num)

    def chunk_dup(self, idx, frame_num=None):
        if self._cur is None:
            return
        self._cur["dup_frames"].append((frame_num, time.time(), idx))

    def decode_fail(self, frame_num):
        if self._cur is None:
            return
        self._cur["decode_fail_frames"].append((frame_num, time.time()))

    def crc_fail(self, frame_num, detail=""):
        if self._cur is None:
            return
        self._cur["crc_fail_frames"].append((frame_num, time.time(), detail))

    def cycle_detected(self, dup_idx, gap_s, missing_count):
        if self._cur is None:
            return
        self._cur["cycles"].append((time.time(), dup_idx, gap_s, missing_count))

    def resend_triggered(self, trigger, missing_indices):
        if self._cur is None:
            return
        self._cur["resends"].append((time.time(), trigger, list(missing_indices)))

    def end_session(self, status="complete"):
        self._finalize_current(status)

    def _finalize_current(self, status):
        if self._cur is None:
            return
        self._cur["t_end"] = time.time()
        self._cur["status"] = status
        self._sessions.append(self._cur)
        self._cur = None

    def generate_report(self):
        if self._cur is not None:
            self._finalize_current("interrupted")

        lines = []
        lines.append("=" * 72)
        lines.append("  QR Air Gap — Session Report")
        lines.append("  Generated: {}".format(
            time.strftime("%Y-%m-%d %H:%M:%S")))
        total_elapsed = time.time() - self._global_start
        lines.append("  Total run time: {:.1f}s".format(total_elapsed))
        lines.append("=" * 72)

        pipe_summary = _pipeline_stats.summary()
        if pipe_summary:
            lines.append("\nPipeline (last window): {}".format(pipe_summary))

        if not self._sessions:
            lines.append("\nNo sessions recorded.")
            return "\n".join(lines)

        for si, s in enumerate(self._sessions):
            lines.append("\n" + "-" * 60)
            lines.append("Session {}: [{}]  Status: {}".format(
                si + 1, s["sid"], s["status"]))
            lines.append("  File: {}".format(s["filename"] or "(unnamed)"))
            lines.append("  Chunks: {}/{}".format(
                len(s["chunks_received"]), s["total"]))
            duration = (s["t_end"] or time.time()) - s["t_start"]
            lines.append("  Duration: {:.1f}s".format(duration))

            total_bytes = sum(v[1] for v in s["chunks_received"].values())
            if duration > 0:
                lines.append("  Throughput: {:.1f} KB/s".format(
                    total_bytes / duration / 1024))
            else:
                lines.append("  Throughput: N/A")

            # Missing chunks
            if s["total"]:
                missing = sorted(set(range(s["total"])) -
                                 set(s["chunks_received"].keys()))
                if missing:
                    lines.append("\n  MISSING CHUNKS ({}):".format(len(missing)))
                    lines.append("    {}".format(_encode_ranges(missing)))
                else:
                    lines.append("\n  All chunks received.")

            # Decode failures
            n_dfail = len(s["decode_fail_frames"])
            if n_dfail:
                lines.append("\n  DECODE FAILURES: {} frames".format(n_dfail))
                if n_dfail <= 50:
                    frames_list = [str(f[0]) for f in s["decode_fail_frames"]]
                    lines.append("    Frames: {}".format(", ".join(frames_list)))
                else:
                    first5 = [str(f[0]) for f in s["decode_fail_frames"][:5]]
                    last5 = [str(f[0]) for f in s["decode_fail_frames"][-5:]]
                    lines.append("    First 5: {}".format(", ".join(first5)))
                    lines.append("    Last  5: {}".format(", ".join(last5)))

            # CRC failures
            n_crc = len(s["crc_fail_frames"])
            if n_crc:
                lines.append("\n  CRC FAILURES: {} frames".format(n_crc))
                for f_num, ts, detail in s["crc_fail_frames"][:20]:
                    t_rel = ts - s["t_start"]
                    lines.append("    frame #{} @ {:.1f}s  {}".format(
                        f_num, t_rel, detail))
                if n_crc > 20:
                    lines.append("    ... and {} more".format(n_crc - 20))

            # Duplicate frames
            n_dup = len(s["dup_frames"])
            if n_dup:
                lines.append("\n  DUPLICATE CHUNKS decoded: {}".format(n_dup))

            # Cycle detections
            if s["cycles"]:
                lines.append("\n  CYCLE DETECTIONS: {}".format(len(s["cycles"])))
                for ts, dup_idx, gap, miss_c in s["cycles"]:
                    t_rel = ts - s["t_start"]
                    lines.append("    @ {:.1f}s  dup chunk={} gap={:.0f}s "
                                 "missing={}".format(t_rel, dup_idx, gap, miss_c))

            # Resend requests
            if s["resends"]:
                lines.append("\n  RESEND REQUESTS: {}".format(len(s["resends"])))
                for ts, trigger, idxs in s["resends"]:
                    t_rel = ts - s["t_start"]
                    lines.append("    @ {:.1f}s  trigger={}  frames={}".format(
                        t_rel, trigger, _encode_ranges(idxs)))

            # Chunk receive timeline (first & last)
            if s["chunks_received"]:
                by_time = sorted(s["chunks_received"].items(),
                                 key=lambda kv: kv[1][0])
                first_t = by_time[0][1][0] - s["t_start"]
                last_t = by_time[-1][1][0] - s["t_start"]
                lines.append("\n  TIMELINE:")
                lines.append("    First chunk: idx={} @ {:.1f}s".format(
                    by_time[0][0], first_t))
                lines.append("    Last  chunk: idx={} @ {:.1f}s".format(
                    by_time[-1][0], last_t))

        lines.append("\n" + "=" * 72)
        return "\n".join(lines)

    def save_report(self, path=None):
        report = self.generate_report()
        if path is None:
            ts = time.strftime("%Y%m%d_%H%M%S")
            path = os.path.join(self._outdir, "session_{}.log".format(ts))
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            f.write(report)
        print(report)
        print("\nReport saved to: {}".format(path))
        return path


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

    try:
        text, _, _ = _get_cv2_qr_detector().detectAndDecode(gray)
        if text:
            _decode_stats["fast_ok"] += 1
            return text
    except cv2.error:
        pass

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


def try_decode_qr_multi(frame_bgr):
    """Decode ALL QR codes in the frame (for multi-QR mode)."""
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    seen = set()
    texts = []

    def _collect_pyzbar(img):
        for r in pyzbar_decode(img):
            t = r.data.decode("utf-8", errors="replace")
            if t not in seen:
                texts.append(t)
                seen.add(t)

    def _collect_cv2_multi(img):
        try:
            detector = _get_cv2_qr_detector()
            retval, decoded_info, *_ = detector.detectAndDecodeMulti(img)
            if retval and decoded_info is not None:
                for t in decoded_info:
                    if t and t not in seen:
                        texts.append(t)
                        seen.add(t)
        except (AttributeError, cv2.error, Exception):
            pass

    _collect_pyzbar(gray)
    _collect_cv2_multi(gray)

    if not texts:
        thresh = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 51, 11
        )
        _collect_pyzbar(thresh)
        _collect_cv2_multi(thresh)

    return texts if texts else None


def print_progress(sid, received_count, total, fps, filename="", speed_kbps=0.0):
    bar_len = 30
    filled = int(bar_len * received_count / total) if total else 0
    bar = "#" * filled + "-" * (bar_len - filled)
    pct = 100.0 * received_count / total if total else 0
    try:
        term_width = os.get_terminal_size().columns
    except (AttributeError, ValueError, OSError):
        term_width = 120
    speed_str = " {:.1f}KB/s".format(speed_kbps) if speed_kbps > 0 else ""
    pipe = _pipeline_stats.summary()
    content = "[{}] [{}] {}/{} ({:.0f}%){}  {}".format(
        sid, bar, received_count, total, pct, speed_str, pipe
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


def _is_calib_result(obj):
    from visual_transport import CalibResult
    return isinstance(obj, CalibResult)


def _send_alert_email(alert_cfg, filename, progress, total, stall_secs):
    """Send a stall alert email via SMTP in a background thread."""
    email_addr = alert_cfg.get("email", "")
    if not email_addr:
        return
    def _do():
        try:
            import smtplib
            from email.mime.text import MIMEText
            subject = "[QRAirGap] Stall alert: {} ({}/{})".format(
                filename or "unknown", progress, total or "?")
            body = (
                "QRAirGap receiver has not received new frames for {:.0f} seconds.\n\n"
                "File: {}\n"
                "Progress: {}/{} chunks\n"
                "Missing: {} chunks\n"
            ).format(stall_secs, filename or "(unnamed)", progress, total or "?",
                     (total - progress) if total else "?")
            msg = MIMEText(body)
            msg["Subject"] = subject
            msg["From"] = alert_cfg.get("smtp_user", email_addr)
            msg["To"] = email_addr

            server = alert_cfg.get("smtp_server", "")
            port = int(alert_cfg.get("smtp_port", 587))
            user = alert_cfg.get("smtp_user", "")
            password = alert_cfg.get("smtp_password", "")
            use_tls = alert_cfg.get("smtp_use_tls", True)

            if port == 465 and not use_tls:
                conn = smtplib.SMTP_SSL(server, port, timeout=30)
            else:
                conn = smtplib.SMTP(server, port, timeout=30)
                if use_tls:
                    conn.starttls()
            with conn as s:
                if user and password:
                    s.login(user, password)
                s.sendmail(msg["From"], [email_addr], msg.as_string())
            print("[ALERT] Email sent to {}".format(email_addr))
        except Exception as e:
            print("[ALERT] Failed to send email: {}".format(e))
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


def _rebuild_received_for_degradation(old_received, old_total, new_total, new_cap):
    """Rebuild received dict when degradation changes chunk boundaries.

    Maps old chunk data to new chunk indices based on byte offsets,
    preserving all previously received data.
    """
    if not old_received or new_cap <= 0:
        return {}

    old_cap = None
    for idx in sorted(old_received.keys()):
        if idx < old_total - 1:
            old_cap = len(old_received[idx])
            break
    if old_cap is None:
        first_key = next(iter(old_received))
        old_cap = len(old_received[first_key])
    if old_cap <= 0:
        return {}

    file_size = max(k * old_cap + len(old_received[k]) for k in old_received)

    new_received = {}
    for j in range(new_total):
        j_start = j * new_cap
        if j_start >= file_size:
            break
        j_end = min(j_start + new_cap, file_size)
        chunk_parts = []
        fully_covered = True
        pos = j_start

        while pos < j_end:
            old_idx = pos // old_cap
            if old_idx not in old_received:
                fully_covered = False
                break
            offset_in_old = pos - old_idx * old_cap
            avail = len(old_received[old_idx]) - offset_in_old
            if avail <= 0:
                fully_covered = False
                break
            take = min(avail, j_end - pos)
            chunk_parts.append(old_received[old_idx][offset_in_old:offset_in_old + take])
            pos += take

        if fully_covered and chunk_parts:
            new_received[j] = b''.join(chunk_parts)

    return new_received


import re as _re

_PART_RE = _re.compile(r'^(.+)\.part(\d+)of(\d+)$')
_PART_RE_LEGACY = _re.compile(r'^(.+)\.part(\d+)$')


def _try_merge_parts(outdir, filename):
    """If filename is a .partNofM, check if all M parts exist and merge them.

    New format: <base>.part01of03, .part02of03, .part03of03 (1-based).
    Legacy format: <base>.part01, .part02, ... (no total — never auto-merge).

    Returns True if merge succeeded, False otherwise.
    """
    m = _PART_RE.match(filename)
    if not m:
        ml = _PART_RE_LEGACY.match(filename)
        if ml:
            print("[merge] Legacy part format (no total) — skipping auto-merge")
        return False
    base = m.group(1)
    total_parts = int(m.group(3))
    safe_base = _safe_filename(base)
    if not safe_base:
        return False
    parent_dir = os.path.dirname(os.path.join(outdir, _safe_filename(filename) or filename))

    part_files = {}
    for entry in os.listdir(parent_dir):
        pm = _PART_RE.match(entry)
        if pm and pm.group(1) == os.path.basename(safe_base):
            part_num = int(pm.group(2))
            file_total = int(pm.group(3))
            if file_total == total_parts:
                part_files[part_num] = os.path.join(parent_dir, entry)

    if not part_files:
        return False

    expected = set(range(1, total_parts + 1))
    if set(part_files.keys()) != expected:
        have = sorted(part_files.keys())
        want = sorted(expected - set(part_files.keys()))
        print("[merge] Waiting for parts: have {}/{}, missing {}".format(
            len(have), total_parts, want))
        return False

    merged_path = os.path.join(parent_dir, os.path.basename(safe_base))
    print("[merge] All {} parts found, merging → {}".format(
        total_parts, merged_path))
    total_size = 0
    with open(merged_path, "wb") as out:
        for i in range(1, total_parts + 1):
            pf = part_files[i]
            sz = os.path.getsize(pf)
            total_size += sz
            with open(pf, "rb") as inp:
                while True:
                    chunk = inp.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
    print("[merge] Done: {} ({} B from {} parts)".format(
        merged_path, total_size, total_parts))

    for i in range(1, total_parts + 1):
        os.remove(part_files[i])
    print("[merge] Removed {} part files".format(total_parts))
    return True


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
            _pipeline_stats.tick("capture")

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


def _decode_thread(frame_queue, result_queue, stop_event, multi_qr=False,
                   grid_w=160, grid_h=96, n_levels=4, n_colors=1):
    """Consumer: decodes QR codes (or V3 visual frames) from captured frames."""
    while not stop_event.is_set():
        try:
            item = frame_queue.get(timeout=0.1)
        except queue.Empty:
            continue
        frame_num, frame_bgr = item

        try:
            if multi_qr:
                texts = try_decode_qr_multi(frame_bgr)
            else:
                text = try_decode_qr(frame_bgr)
                texts = [text] if text else None

            if texts:
                result_queue.put((frame_num, frame_bgr.shape[:2], texts))
                continue

            try:
                from visual_transport import decode_frame, is_calib_marker
                raw = decode_frame(frame_bgr, grid_w, grid_h, n_levels, n_colors)
                if raw is not None:
                    result_queue.put((frame_num, frame_bgr.shape[:2], [raw]))
                    continue
            except Exception:
                pass

            result_queue.put((frame_num, frame_bgr.shape[:2], None))
        except Exception:
            result_queue.put((frame_num, frame_bgr.shape[:2], None))


def _load_config(path):
    """Load YAML config file, return dict or empty dict if not found."""
    try:
        import yaml
    except ImportError:
        return {}
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        print("[config] Warning: failed to load {}: {}".format(path, e))
        return {}


def main():
    parser = argparse.ArgumentParser(description="QR Air Gap Receiver")
    parser.add_argument("--config", default="config.yaml",
                        help="Path to YAML config file (default: config.yaml)")
    parser.add_argument("--outdir", "-o", default=None,
                        help="Output directory (default: received/)")
    parser.add_argument("--fps", type=float, default=None,
                        help="Capture frames per second (default: 5)")
    parser.add_argument("--timeout", type=float, default=None,
                        help="Seconds of no new chunks before warning (default: 15)")
    parser.add_argument("--debug", action="store_true",
                        help="Save captured frames to debug_frames/")
    parser.add_argument("--region", type=str, default=None,
                        help="Skip GUI selector, use LEFT,TOP,WIDTH,HEIGHT")
    parser.add_argument("--auto-next", action="store_true", default=None,
                        help="Auto-send 'n' keystroke after file complete (default: on)")
    parser.add_argument("--no-auto-next", dest="auto_next", action="store_false",
                        help="Disable auto-sending 'n' keystroke")
    parser.add_argument("--decode-workers", type=int, default=None,
                        help="Number of decode threads (default: 2)")
    parser.add_argument("--verbose", "-v", action="store_true", default=None,
                        help="Show detailed processing steps")
    parser.add_argument("--num-qr", type=int, default=None,
                        help="[experimental] Number of QR codes per frame (default: 1)")
    parser.add_argument("--grid", type=str, default=None,
                        help="Expected data grid W,H for V3 grayN decoding (default: 160,96)")
    parser.add_argument("--gray-levels", type=int, default=None, choices=[4, 8],
                        help="Number of gray levels for V3 decoding (default: 4). "
                             "Auto-detected from calibration frame if sender sends one.")
    parser.add_argument("--colors", type=int, default=None, choices=[1, 2, 4],
                        help="Number of color channels for V3 decoding (default: 1). "
                             "Auto-detected from calibration frame if sender sends one.")
    args = parser.parse_args()

    cfg = _load_config(args.config)
    rcfg = cfg.get("receiver", {})
    _DEFAULTS = {"outdir": "received", "fps": 5, "timeout": 15, "decode_workers": 2,
                 "verbose": False, "grid": "160,96", "gray_levels": 4, "colors": 1,
                 "auto_next": True, "num_qr": 1, "region": None}
    for key, fallback in _DEFAULTS.items():
        cli_val = getattr(args, key, None)
        if cli_val is None:
            cfg_val = rcfg.get(key)
            setattr(args, key, cfg_val if cfg_val is not None else fallback)

    args.alert_cfg = cfg.get("alert", {})

    global _receiver_verbose
    _receiver_verbose = args.verbose

    import re
    _grid_parts = re.split(r'[x,\s]+', args.grid.strip().lower())
    try:
        if len(_grid_parts) != 2:
            raise ValueError
        _grid_w, _grid_h = int(_grid_parts[0]), int(_grid_parts[1])
    except ValueError:
        print("Error: --grid must be W,H (e.g. 160,96 or 160x96)")
        sys.exit(1)

    print("=== QR Air Gap Receiver ===")

    if not _startup_check():
        print("WARNING: pyzbar cannot decode QR codes. Check zbar installation.")
        print("  macOS: brew install zbar")
        print("  V3 grayN decoding will still work, but QR (V1/V2) will not.")
        print()

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
    multi_qr = args.num_qr > 1
    _n_levels = args.gray_levels
    _n_colors = args.colors
    decode_threads = []
    for _ in range(args.decode_workers):
        t = threading.Thread(
            target=_decode_thread,
            args=(frame_queue, result_queue, stop_event, multi_qr,
                  _grid_w, _grid_h, _n_levels, _n_colors),
            daemon=True,
        )
        decode_threads.append(t)

    multi_label = ", {} QR codes/frame".format(args.num_qr) if multi_qr else ""
    print("Capturing at {:.1f} fps, {} decode workers{}. Press q to stop.\n".format(
        args.fps, args.decode_workers, multi_label
    ))

    speed_tracker = SpeedTracker()
    # slog = SessionLog(args.outdir)
    files_saved = []
    received = {}
    session_id = None
    total = None
    current_filename = ""
    last_new_time = time.time()
    warned = False
    decode_fail_count = 0
    end_received = False
    completed_sessions = set()  # (sid, filename) pairs
    chunk_last_seen = {}  # idx -> timestamp of last decode
    _CYCLE_GAP = 10.0     # seconds: dup gap threshold for cycle detection
    _last_calib_key = None  # (n_colors, n_levels) of last calibration that triggered Space
    _session_switch_frame = -1  # frame_num at last session switch; ignore older results
    _alert_sent = False
    _alert_stall = float(args.alert_cfg.get("stall_seconds", 300))

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
            if ch == 'c':
                print("\n[RECALIB] Sending 'c' to sender for re-calibration")
                _last_calib_key = None
                _send_keystroke_async("c", delay=0.5)

            batch_count = 0
            while batch_count < 20:
                try:
                    frame_num, shape, raw_texts = result_queue.get_nowait()
                except queue.Empty:
                    break
                batch_count += 1

                if not raw_texts:
                    decode_fail_count += 1
                    _pipeline_stats.tick("decode_fail")
                    # slog.decode_fail(frame_num)
                    if decode_fail_count <= 3 or decode_fail_count % 50 == 0:
                        h, w = shape
                        try:
                            tw = os.get_terminal_size().columns
                        except (AttributeError, ValueError, OSError):
                            tw = 120
                        pipe = _pipeline_stats.summary()
                        msg = "[scan] #{} ({}x{}) no decode ({}x)  {}".format(
                            frame_num, w, h, decode_fail_count, pipe
                        )
                        sys.stdout.write("\r" + msg.ljust(tw - 1))
                        sys.stdout.flush()
                    continue

                for raw_item in raw_texts:
                    if isinstance(raw_item, bytes) or _is_calib_result(raw_item):
                        from visual_transport import (is_v3_end_packet, decode_v3_packet,
                                                      is_calib_marker, CalibResult)
                        if is_calib_marker(raw_item):
                            calib_nc = raw_item.n_colors if isinstance(raw_item, CalibResult) else _n_colors
                            calib_nl = raw_item.n_levels if isinstance(raw_item, CalibResult) else _n_levels
                            calib_key = (calib_nc, calib_nl)
                            if calib_key != _last_calib_key:
                                _last_calib_key = calib_key
                                _n_colors = calib_nc
                                _n_levels = calib_nl
                                sys.stdout.write("\r\033[2K")
                                calib_label = "gray{}".format(calib_nl) if calib_nc == 1 else \
                                    "{}c×gray{}".format(calib_nc, calib_nl)
                                print("[CALIB] {} calibration OK, sending Space to sender".format(
                                    calib_label))
                                _send_keystroke_async(" ", delay=1.0)
                            continue
                        if is_v3_end_packet(raw_item):
                            print("\n\nEND signal received — sender has finished all files.")
                            end_received = True
                            break
                        chunk_info = decode_v3_packet(raw_item)
                        if chunk_info is None:
                            from visual_transport import diagnose_v3_packet
                            diag = diagnose_v3_packet(raw_item)
                            _pipeline_stats.tick("crc_fail")
                            # slog.crc_fail(frame_num, diag)
                            if _receiver_verbose:
                                sys.stdout.write("\r\033[2K")
                                print("[V3 FAIL] #{}: {} ({} B)".format(
                                    frame_num, diag, len(raw_item)))
                            continue
                    else:
                        raw_text = raw_item
                        if is_end_signal(raw_text):
                            print("\n\nEND signal received — sender has finished all files.")
                            end_received = True
                            break
                        chunk_info = decode_chunk(raw_text)
                        if chunk_info is None:
                            # slog.crc_fail(frame_num, "QR decode fail")
                            if _receiver_verbose or session_id is None:
                                _, reason = decode_chunk_verbose(raw_text)
                                preview = raw_text[:80] + ("..." if len(raw_text) > 80 else "")
                                sys.stdout.write("\r\033[2K")
                                print("[DECODE FAIL] {}\n  payload({} chars): {}".format(
                                    reason, len(raw_text), preview))
                            continue

                    sid = chunk_info["sid"]
                    idx = chunk_info["idx"]
                    chunk_total = chunk_info["total"]
                    fname = chunk_info.get("filename", "")

                    if (sid, fname) in completed_sessions:
                        continue

                    if not (0 <= idx < chunk_total):
                        continue

                    if sid != session_id and frame_num < _session_switch_frame:
                        continue

                    if session_id is None:
                        session_id = sid
                        total = chunk_total
                        current_filename = fname
                        chunk_last_seen.clear()
                        speed_tracker.reset()
                        _session_switch_frame = frame_num
                        # slog.begin_session(sid, fname, total)
                        sys.stdout.write("\r\033[2K")
                        print("Session detected: {}  File: {}  Chunks: {}".format(
                            sid, fname or "(unnamed)", total
                        ))

                    if sid != session_id:
                        # slog.end_session("interrupted")
                        received = {}
                        session_id = sid
                        total = chunk_total
                        current_filename = fname
                        warned = False
                        decode_fail_count = 0
                        chunk_last_seen.clear()
                        _session_switch_frame = frame_num
                        speed_tracker.reset()
                        # slog.begin_session(sid, fname, total)
                        sys.stdout.write("\r\033[2K")
                        print("\nNew session detected: {}  File: {}  Chunks: {}".format(
                            sid, fname or "(unnamed)", total
                        ))

                    if chunk_total != total:
                        if sid == session_id and fname == current_filename and total is not None:
                            from visual_transport import chunk_capacity_bytes
                            ci_nl = chunk_info.get("n_levels", 4)
                            ci_nc = chunk_info.get("n_colors", 1)
                            new_cap = chunk_capacity_bytes(
                                _grid_w, _grid_h, fname, ci_nl, ci_nc)
                            if new_cap > 0:
                                new_recv = _rebuild_received_for_degradation(
                                    received, total, chunk_total, new_cap)
                                old_count = len(received)
                                received = new_recv
                                total = chunk_total
                                chunk_last_seen.clear()
                                last_new_time = time.time()
                                warned = False
                                sys.stdout.write("\r\033[2K")
                                print("[DEGRADE] Parameter change: total {} → {}, "
                                      "rebuilt {}/{} chunks from previous data".format(
                                          old_count, chunk_total,
                                          len(received), chunk_total))
                            else:
                                continue
                        else:
                            continue

                    now = time.time()
                    prev_seen = chunk_last_seen.get(idx)
                    chunk_last_seen[idx] = now

                    is_new = idx not in received
                    if is_new:
                        received[idx] = chunk_info["data"]
                        speed_tracker.add(len(chunk_info["data"]))
                        _pipeline_stats.tick("decode_ok")
                        # slog.chunk_ok(idx, len(chunk_info["data"]), frame_num)
                        last_new_time = now
                        warned = False
                        if total is not None:
                            print_progress(session_id, len(received), total, args.fps,
                                           current_filename, speed_tracker.speed_kbps())
                    else:
                        _pipeline_stats.tick("dup")
                        # slog.chunk_dup(idx, frame_num)
                        if (prev_seen is not None
                              and now - prev_seen > _CYCLE_GAP
                              and total is not None
                              and len(received) < total
                              and not _sending_missing):
                            missing = sorted(set(range(total)) - set(received.keys()))
                            if missing:
                                gap = now - prev_seen
                                # slog.cycle_detected(idx, gap, len(missing))
                                missing_show = missing[:20]
                                suffix = " ..." if len(missing) > 20 else ""
                                print("\n[resend] Cycle detected (dup {} after {:.0f}s), "
                                      "missing {} frames [{}{}]".format(
                                          idx, gap, len(missing),
                                          ", ".join(str(x) for x in missing_show), suffix))
                                # slog.resend_triggered("cycle", missing)
                                _send_missing_frames_async(missing)

                    if total is not None and len(received) >= total:
                        _save_file(received, total, current_filename, args.outdir)
                        _try_merge_parts(args.outdir, current_filename)
                        files_saved.append(current_filename or session_id)
                        completed_sessions.add((session_id, current_filename))
                        # slog.end_session("complete")

                        received = {}
                        session_id = None
                        total = None
                        current_filename = ""
                        warned = False
                        decode_fail_count = 0
                        chunk_last_seen.clear()

                        if args.auto_next:
                            print("Auto-sending 'n' to switch to next file...")
                            _send_keystroke_async("n")
                        print("Waiting for next file... (q to quit)\n")

                if end_received:
                    break

            if end_received:
                break

            if session_id is not None and total is not None and len(received) < total:
                now = time.time()
                stall_duration = now - last_new_time

                if stall_duration > args.timeout and not _sending_missing:
                    missing = sorted(set(range(total)) - set(received.keys()))
                    if missing:
                        if not warned:
                            print("\n[WARN] No new chunks for {:.0f}s. Missing {} frames.".format(
                                stall_duration, len(missing)))
                            warned = True
                        missing_show = missing[:20]
                        suffix = " ..." if len(missing) > 20 else ""
                        print("\n[resend] Timeout ({:.0f}s no new), missing {} frames [{}{}]".format(
                            stall_duration, len(missing),
                            ", ".join(str(x) for x in missing_show), suffix))
                        # slog.resend_triggered("timeout", missing)
                        _send_missing_frames_async(missing)
                        last_new_time = now

                if (stall_duration > _alert_stall
                        and not _alert_sent
                        and args.alert_cfg.get("email")):
                    _alert_sent = True
                    _send_alert_email(args.alert_cfg, current_filename,
                                      len(received), total, stall_duration)

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
        is_complete = len(received) >= total
        save_name = current_filename
        if not is_complete and current_filename:
            save_name = current_filename + ".incomplete"
        _save_file(received, total, save_name, args.outdir)
        if is_complete:
            _try_merge_parts(args.outdir, current_filename)
        else:
            print("[info] Part incomplete ({}/{} chunks) — saved as .incomplete".format(
                len(received), total))
        files_saved.append(current_filename or session_id or "unknown")

    # slog.save_report()


if __name__ == "__main__":
    main()
