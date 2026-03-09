# -*- coding: utf-8 -*-
"""
QR Air Gap Sender — runs on the remote server (Python 3.7+, Pillow 7.0+).
Displays QR codes in a Tkinter window for the receiver to capture.

Usage:
    python3 sender.py --file input.txt [--fps 3]
    python3 sender.py --dir ./my_files/ [--fps 3]
"""
import argparse
import glob
import os
import sys
import time as _time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

_here = os.path.dirname(os.path.abspath(__file__))
_vendor_dir = os.path.join(_here, 'qrcode_vendor')
_vendor_zip = os.path.join(_here, 'qrcode_vendor.zip')
if os.path.isdir(_vendor_dir):
    sys.path.insert(0, _vendor_dir)
elif os.path.isfile(_vendor_zip):
    sys.path.insert(0, _vendor_zip)

import tkinter as tk
from PIL import ImageTk
import qrcode
from qrcode.constants import ERROR_CORRECT_L

from protocol import generate_session_id, chunk_data, encode_chunk, encode_end_signal

_verbose = False
_stop_event = threading.Event()


def _log(msg):
    if _verbose:
        print(msg)


def _determine_qr_version(payloads):
    # type: (list,) -> int
    """Find the minimum QR version that fits the largest payload."""
    if not payloads:
        return 1
    longest = max(payloads, key=len)
    _log("[version] Probing with longest payload ({} bytes)".format(len(longest)))
    qr = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_L)
    qr.add_data(longest)
    qr.make(fit=True)
    _log("[version] Determined version={}".format(qr.version))
    return qr.version


def _generate_single_qr(args_tuple):
    payload, version, box_size, border, idx = args_tuple
    if _stop_event.is_set():
        return idx, None
    qr = qrcode.QRCode(
        version=version,
        error_correction=ERROR_CORRECT_L,
        box_size=box_size,
        border=border,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").get_image()
    _time.sleep(0.1)
    return idx, img


def _generate_qr_images_into(images, payloads, version, box_size, border,
                              start=0, end=None, on_progress=None):
    """Generate QR images into an existing list. Thread-safe for non-overlapping ranges."""
    if end is None:
        end = len(payloads)
    n = end - start
    workers = min(n, 4)
    t0 = _time.time()
    done = [0]

    def _tick(idx, img):
        images[idx] = img
        done[0] += 1
        if _verbose:
            _print_qr_progress(done[0], n, t0)
        if on_progress:
            on_progress(done[0], n)

    if workers <= 1:
        for i in range(start, end):
            result_idx, result_img = _generate_single_qr((payloads[i], version, box_size, border, i))
            _tick(result_idx, result_img)
    else:
        tasks = [(payloads[i], version, box_size, border, i) for i in range(start, end)]
        pool = ThreadPoolExecutor(max_workers=workers)
        futures = [pool.submit(_generate_single_qr, t) for t in tasks]
        for future in as_completed(futures):
            if _stop_event.is_set():
                break
            idx, img = future.result()
            if img is not None:
                _tick(idx, img)
        for f in futures:
            f.cancel()
        pool.shutdown(wait=False)
    if _verbose:
        sys.stdout.write("\n")
    elapsed = _time.time() - t0
    _log("[qr] Generated {} images in {:.2f}s ({:.1f}ms/frame, {} workers)".format(
        n, elapsed, elapsed / max(n, 1) * 1000, workers))


_progress_lock = threading.Lock()


def _print_qr_progress(done, total, t0):
    elapsed = _time.time() - t0
    pct = done * 100 // total
    bar_len = 30
    filled = bar_len * done // total
    bar = "#" * filled + "-" * (bar_len - filled)
    if done > 1:
        eta = elapsed / done * (total - done)
        eta_s = "eta {:.1f}s".format(eta)
    else:
        eta_s = ""
    try:
        tw = os.get_terminal_size().columns
    except (AttributeError, ValueError, OSError):
        tw = 120
    content = "  [qr] [{}] {}/{} ({}%) {:.1f}s {}".format(
        bar, done, total, pct, elapsed, eta_s)
    with _progress_lock:
        sys.stdout.write("\r" + content[:tw - 1].ljust(tw - 1))
        sys.stdout.flush()


def prepare_file_meta(filepath, chunk_size, box_size, base_dir=None):
    """Prepare file metadata and payloads (fast, no QR generation)."""
    _log("[prepare] Reading {}".format(filepath))
    with open(filepath, "rb") as f:
        data = f.read()
    if base_dir:
        filename = os.path.relpath(filepath, base_dir)
    else:
        filename = os.path.basename(filepath)
    sid = generate_session_id()
    chunks = chunk_data(data, chunk_size)
    if not chunks:
        chunks = [b""]
    total = len(chunks)
    _log("[prepare] {} -> sid={}, {} bytes, {} chunks (chunk_size={})".format(
        filename, sid, len(data), total, chunk_size))
    payloads = []
    for idx, chunk in enumerate(chunks):
        payloads.append(encode_chunk(sid, idx, total, chunk, filename=filename))
    _log("[prepare] Payload sizes: min={} max={} bytes".format(
        min(len(p) for p in payloads), max(len(p) for p in payloads)))
    version = _determine_qr_version(payloads)
    modules = version * 4 + 17
    print("  QR version {} ({}x{} modules) for {} frames".format(
        version, modules, modules, total))
    return {
        "filepath": filepath,
        "filename": filename,
        "size": len(data),
        "sid": sid,
        "total": total,
        "payloads": payloads,
        "version": version,
        "pil_images": [None] * total,
    }


def prepare_file_data(filepath, chunk_size, box_size, base_dir=None):
    """Generate all QR PIL images (blocking). Used by preload thread."""
    meta = prepare_file_meta(filepath, chunk_size, box_size, base_dir)
    _generate_qr_images_into(
        meta["pil_images"], meta["payloads"],
        meta["version"], box_size, 6)
    _log("[prepare] Done: {} PIL images ready".format(meta["total"]))
    return meta


class SenderApp(object):
    def __init__(self, root, file_paths, fps, chunk_size, box_size, countdown=3, base_dir=None):
        self.root = root
        self.fps = fps
        self.interval = int(1000 / fps)
        self.chunk_size = chunk_size
        self.box_size = box_size
        self.countdown_duration = countdown
        self.base_dir = base_dir

        self.file_paths = file_paths
        self.file_index = 0
        self.file_cache = {}   # index -> dict with pil_images
        self._tk_cache = {}    # index -> list of ImageTk.PhotoImage
        self._preload_lock = threading.Lock()
        self._preload_thread = None

        self.current_chunk = 0
        self.paused = False
        self._after_id = None

        self.root.title("QR Air Gap Sender")
        self.root.configure(bg="white")

        self.canvas = tk.Canvas(root, bg="white", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)

        self.status_var = tk.StringVar()
        tk.Label(
            root, textvariable=self.status_var,
            font=("Courier", 13), bg="white", fg="#333",
        ).pack(side=tk.BOTTOM, pady=4)

        self.file_var = tk.StringVar()
        tk.Label(
            root, textvariable=self.file_var,
            font=("Courier", 11), bg="white", fg="#666",
        ).pack(side=tk.BOTTOM)

        help_text = "n/Right: next file  |  p/Left: prev file  |  Space: pause"
        if len(file_paths) == 1:
            help_text = "Space: pause  |  g: goto  |  +/-: speed  |  q: quit"
        else:
            help_text += "\ng: goto frame  |  +/-: speed  |  q: quit"
        tk.Label(
            root, text=help_text,
            font=("Courier", 10), bg="white", fg="#999", justify=tk.CENTER,
        ).pack(side=tk.BOTTOM)

        self.root.bind("<space>", self._toggle_pause)
        self.root.bind("<plus>", self._speed_up)
        self.root.bind("<equal>", self._speed_up)
        self.root.bind("<minus>", self._slow_down)
        self.root.bind("<n>", self._next_file)
        self.root.bind("<Right>", self._next_file)
        self.root.bind("<p>", self._prev_file)
        self.root.bind("<Left>", self._prev_file)
        self.root.bind("<g>", self._goto_frame)
        self.root.bind("<q>", self._quit)
        self.root.bind("<Escape>", self._quit)
        self.root.bind("<Configure>", self._on_resize)
        self.root.bind("<Key>", self._on_key)

        self.countdown_text = None
        self.img_on_canvas = None
        self._countdown_remaining = countdown

        self._missing_frames = None  # None = normal mode, list = missing-only mode
        self._missing_pos = 0
        self._input_mode = False
        self._input_buffer = ""
        self._input_timeout_id = None

        self._prepare_current()
        self._start_countdown()

    def _current_info(self):
        return self.file_cache[self.file_index]

    def _get_tk_image(self, frame_idx):
        pil_img = self._current_info()["pil_images"][frame_idx]
        if pil_img is None:
            return None
        fi = self.file_index
        if fi not in self._tk_cache:
            self._tk_cache[fi] = {}
        cache = self._tk_cache[fi]
        if frame_idx not in cache:
            cache[frame_idx] = ImageTk.PhotoImage(pil_img)
        return cache[frame_idx]

    def _prepare_current(self):
        if self.file_index not in self.file_cache:
            path = self.file_paths[self.file_index]
            fi = self.file_index
            _log("[app] Encoding file {}/{}: {}".format(
                fi + 1, len(self.file_paths), path))
            self.file_var.set("File {}/{}: {} (loading...)".format(
                fi + 1, len(self.file_paths), os.path.basename(path)))

            def _bg_prepare():
                meta = prepare_file_meta(path, self.chunk_size, self.box_size, self.base_dir)
                _, first_img = _generate_single_qr((
                    meta["payloads"][0], meta["version"],
                    self.box_size, 6, 0))
                meta["pil_images"][0] = first_img
                with self._preload_lock:
                    self.file_cache[fi] = meta
                total_frames = meta["total"]
                self.root.after(0, lambda: self._on_file_ready(fi, meta))
                if total_frames > 1:
                    _log("[app] First frame ready, generating rest in background...")
                    def _on_progress(batch_done, batch_total):
                        overall = batch_done + 1
                        if batch_done == batch_total or batch_done % max(1, batch_total // 20) == 0:
                            self.root.after(0, lambda d=overall: self._update_gen_progress(fi, d, total_frames))
                    _generate_qr_images_into(
                        meta["pil_images"], meta["payloads"],
                        meta["version"], self.box_size, 6,
                        start=1, on_progress=_on_progress)
                    _log("[app] File {} all {} frames ready".format(fi + 1, total_frames))
                self.root.after(0, lambda: self._on_file_ready(fi, meta))
                self.root.after(0, self._preload_next)

            threading.Thread(target=_bg_prepare, daemon=True).start()
        else:
            _log("[app] File {}/{} already cached".format(
                self.file_index + 1, len(self.file_paths)))
            info = self._current_info()
            self.file_var.set("File {}/{}: {} ({} B, {} chunks)".format(
                self.file_index + 1, len(self.file_paths),
                info["filename"], info["size"], info["total"],
            ))
            self._preload_next()

    def _on_file_ready(self, fi, meta):
        if fi != self.file_index:
            return
        self.file_var.set("File {}/{}: {} ({} B, {} chunks)".format(
            fi + 1, len(self.file_paths),
            meta["filename"], meta["size"], meta["total"],
        ))

    def _update_gen_progress(self, fi, done, total):
        if fi != self.file_index:
            return
        info = self.file_cache.get(fi)
        if info is None:
            return
        pct = done * 100 // total
        self.file_var.set("File {}/{}: {} ({} B) — QR {}% ({}/{})".format(
            fi + 1, len(self.file_paths),
            info["filename"], info["size"], pct, done, total,
        ))

    def _preload_next(self):
        if self._preload_thread is not None and self._preload_thread.is_alive():
            _log("[preload] Thread already running, skip")
            return
        start_idx = self.file_index + 1
        if start_idx >= len(self.file_paths):
            return
        with self._preload_lock:
            if all(i in self.file_cache for i in range(start_idx, len(self.file_paths))):
                _log("[preload] All remaining files already cached")
                return
        _log("[preload] Starting background thread for files {}-{}".format(
            start_idx + 1, len(self.file_paths)))
        def _do():
            for idx in range(start_idx, len(self.file_paths)):
                with self._preload_lock:
                    if idx in self.file_cache:
                        continue
                path = self.file_paths[idx]
                info = prepare_file_data(path, self.chunk_size, self.box_size, self.base_dir)
                with self._preload_lock:
                    self.file_cache[idx] = info
                print("[preload] Ready {}/{}: {} ({} chunks)".format(
                    idx + 1, len(self.file_paths), info["filename"], info["total"]))
        self._preload_thread = threading.Thread(target=_do, daemon=True)
        self._preload_thread.start()

    def _cancel_pending(self):
        if self._after_id is not None:
            self.root.after_cancel(self._after_id)
            self._after_id = None

    def _start_countdown(self):
        if self.countdown_text is not None:
            self.canvas.delete(self.countdown_text)
        cx = self.canvas.winfo_width() // 2 or 300
        cy = self.canvas.winfo_height() // 2 or 300
        if self._countdown_remaining > 0:
            self.countdown_text = self.canvas.create_text(
                cx, cy, text=str(self._countdown_remaining),
                font=("Helvetica", 120, "bold"), fill="#333",
            )
            self.status_var.set("Starting in {}...".format(self._countdown_remaining))
            self._countdown_remaining -= 1
            self._after_id = self.root.after(1000, self._start_countdown)
        else:
            self.countdown_text = None
            self.current_chunk = 0
            self.paused = False
            self._show_frame()

    def _show_frame(self):
        if self.paused:
            self._after_id = self.root.after(self.interval, self._show_frame)
            return

        if self.file_index not in self.file_cache:
            self.status_var.set("Loading file...")
            self._after_id = self.root.after(100, self._show_frame)
            return

        info = self._current_info()

        if self._missing_frames is not None:
            frame_idx = self._missing_frames[self._missing_pos]
        else:
            frame_idx = self.current_chunk

        tk_img = self._get_tk_image(frame_idx)
        if tk_img is None:
            ready = sum(1 for img in info["pil_images"] if img is not None)
            pct = ready * 100 // info["total"]
            self.status_var.set("Generating QR {}%  ({}/{})".format(pct, ready, info["total"]))
            self._after_id = self.root.after(50, self._show_frame)
            return

        if self._missing_frames is not None:
            self._missing_pos = (self._missing_pos + 1) % len(self._missing_frames)
            label = "RESEND {}/{} (frame {})".format(
                self._missing_pos or len(self._missing_frames),
                len(self._missing_frames), frame_idx + 1,
            )
        else:
            self.current_chunk = (self.current_chunk + 1) % info["total"]
            label = "chunk {}/{}".format(frame_idx + 1, info["total"])

        cx = self.canvas.winfo_width() // 2
        cy = self.canvas.winfo_height() // 2
        if self.img_on_canvas is not None:
            self.canvas.itemconfig(self.img_on_canvas, image=tk_img)
            self.canvas.coords(self.img_on_canvas, cx, cy)
        else:
            self.img_on_canvas = self.canvas.create_image(cx, cy, image=tk_img)

        self.status_var.set("[{}]  {}   {:.1f} fps".format(
            info["sid"], label, self.fps
        ))

        self._after_id = self.root.after(self.interval, self._show_frame)

    def _switch_file(self):
        _log("[app] Switching to file {}/{}".format(
            self.file_index + 1, len(self.file_paths)))
        self._cancel_pending()
        self.paused = True
        self._missing_frames = None
        self._missing_pos = 0
        self._input_mode = False
        self._input_buffer = ""
        if self._input_timeout_id is not None:
            self.root.after_cancel(self._input_timeout_id)
            self._input_timeout_id = None
        if self.img_on_canvas is not None:
            self.canvas.delete(self.img_on_canvas)
            self.img_on_canvas = None
        self._prepare_current()
        self._countdown_remaining = self.countdown_duration
        self._start_countdown()

    def _next_file(self, event=None):
        if self._input_mode:
            return
        if self.file_index + 1 < len(self.file_paths):
            self.file_index += 1
            self._switch_file()
        else:
            self._show_end_signal()

    def _prev_file(self, event=None):
        if self._input_mode:
            return
        if self.file_index > 0:
            self.file_index -= 1
            self._switch_file()

    def _show_end_signal(self):
        """Display END QR code and auto-quit after a few seconds."""
        self._cancel_pending()
        self.paused = True
        if self.img_on_canvas is not None:
            self.canvas.delete(self.img_on_canvas)
            self.img_on_canvas = None

        end_payload = encode_end_signal()
        qr = qrcode.QRCode(
            version=1, error_correction=ERROR_CORRECT_L,
            box_size=self.box_size, border=6,
        )
        qr.add_data(end_payload)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white").get_image()
        self._end_tk_img = ImageTk.PhotoImage(img)

        cx = self.canvas.winfo_width() // 2
        cy = self.canvas.winfo_height() // 2
        self.img_on_canvas = self.canvas.create_image(cx, cy, image=self._end_tk_img)

        self.file_var.set("All {} files sent!".format(len(self.file_paths)))
        self._end_countdown = 5
        self._end_tick()

    def _end_tick(self):
        if self._end_countdown > 0:
            self.status_var.set("END — closing in {}s...".format(self._end_countdown))
            self._end_countdown -= 1
            self._after_id = self.root.after(1000, self._end_tick)
        else:
            self.root.destroy()

    def _toggle_pause(self, event=None):
        if self._input_mode:
            return
        self.paused = not self.paused
        if self.paused:
            if self.file_index not in self.file_cache:
                self.status_var.set("PAUSED (loading...)")
                return
            info = self._current_info()
            self.status_var.set("[{}]  PAUSED  chunk {}/{}".format(
                info["sid"], self.current_chunk + 1, info["total"]
            ))

    def _speed_up(self, event=None):
        if self._input_mode:
            return
        self.fps = min(self.fps + 1, 30)
        self.interval = int(1000 / self.fps)

    def _slow_down(self, event=None):
        if self._input_mode:
            self._on_key(event)
            return
        self.fps = max(self.fps - 1, 1)
        self.interval = int(1000 / self.fps)

    def _on_key(self, event):
        ch = event.char
        ks = event.keysym
        is_return = ch in ('\r', '\n') or ks in ('Return', 'KP_Enter')
        is_end = ch == '.' or is_return
        if not ch and not is_end:
            return
        if ch in ('m', 'a') and not self._input_mode and self.file_index in self.file_cache:
            self._input_mode = True
            self._input_replace = (ch == 'm')
            self._input_buffer = ""
            self._input_timeout_id = self.root.after(
                15000, self._input_mode_timeout)
            self.status_var.set("[INPUT] receiving missing frames...")
            return "break"
        if self._input_mode:
            if ch in '0123456789,-':
                self._input_buffer += ch
                return "break"
            if is_end:
                if self._input_timeout_id is not None:
                    self.root.after_cancel(self._input_timeout_id)
                    self._input_timeout_id = None
                self._parse_missing_input(self._input_buffer)
                self._input_mode = False
                self._input_buffer = ""
                return "break"
            return "break"

    def _input_mode_timeout(self):
        if not self._input_mode:
            return
        print("[missing] Input mode timeout (15s), forcing parse")
        self._parse_missing_input(self._input_buffer)
        self._input_mode = False
        self._input_buffer = ""
        self._input_timeout_id = None

    def _parse_missing_input(self, raw):
        parts = raw.split(',')
        parts = [p for p in parts if p]
        if not parts or len(parts) % 2 != 0:
            preview = raw[:200] + ("..." if len(raw) > 200 else "")
            print("[missing] Invalid input ({} parts, odd): {}".format(
                len(parts), preview))
            self.status_var.set("[ERROR] invalid missing frame data")
            return
        half = len(parts) // 2
        first_half = parts[:half]
        second_half = parts[half:]
        if first_half != second_half:
            print("[missing] Verification failed (halves differ)")
            self.status_var.set("[ERROR] missing frame verification failed")
            return
        info = self._current_info()
        total = info["total"]
        frames = []
        for s in first_half:
            if '-' in s:
                try:
                    a, b = s.split('-', 1)
                    for n in range(int(a), int(b) + 1):
                        if 0 <= n < total:
                            frames.append(n)
                except ValueError:
                    print("[missing] Invalid range: {}".format(s))
                    self.status_var.set("[ERROR] invalid range")
                    return
            else:
                try:
                    n = int(s)
                except ValueError:
                    print("[missing] Non-integer frame: {}".format(s))
                    self.status_var.set("[ERROR] invalid frame number")
                    return
                if 0 <= n < total:
                    frames.append(n)
                else:
                    print("[missing] Frame {} out of range (0-{})".format(n, total - 1))
        if not frames:
            if self._missing_frames is None:
                print("[missing] No valid frames")
            return
        if not self._input_replace and self._missing_frames is not None:
            existing = set(self._missing_frames)
            new_frames = [f for f in frames if f not in existing]
            if new_frames:
                self._missing_frames.extend(new_frames)
                print("[missing] Appended: +{} new, total {} frames".format(
                    len(new_frames), len(self._missing_frames)))
            else:
                print("[missing] Append batch: no new frames (total {})".format(
                    len(self._missing_frames)))
        else:
            self._missing_frames = frames
            self._missing_pos = 0
            self._cancel_pending()
            self.paused = False
            print("[missing] Retransmit mode: {} frames".format(len(frames)))
            self._show_frame()

    def _goto_frame(self, event=None):
        if self._input_mode:
            return
        if self.file_index not in self.file_cache:
            return
        was_paused = self.paused
        self.paused = True
        info = self._current_info()
        total = info["total"]
        try:
            import tkinter.simpledialog as sd
            val = sd.askinteger(
                "Go to frame",
                "Enter frame number (1-{}):".format(total),
                minvalue=1, maxvalue=total,
                parent=self.root,
            )
        except Exception:
            val = None
        if val is not None:
            self.current_chunk = val - 1
            self.paused = False
            self._cancel_pending()
            self._show_frame()
        else:
            self.paused = was_paused

    def _quit(self, event=None):
        _stop_event.set()
        self.root.destroy()
        os._exit(0)

    def _on_resize(self, event=None):
        if self.img_on_canvas is not None:
            cx = self.canvas.winfo_width() // 2
            cy = self.canvas.winfo_height() // 2
            self.canvas.coords(self.img_on_canvas, cx, cy)


def main():
    parser = argparse.ArgumentParser(description="QR Air Gap Sender")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", "-f", help="Single file to transmit")
    group.add_argument("--dir", "-d", help="Directory of files to transmit (recursive)")
    group.add_argument("--glob", "-g", help="Glob pattern (e.g. '*.py', '**/*.txt')")
    parser.add_argument("--fps", type=float, default=3, help="Frames per second (default: 3)")
    parser.add_argument("--chunk-size", type=int, default=400, help="Bytes per chunk (default: 400)")
    parser.add_argument("--box-size", type=int, default=10, help="QR box pixel size (default: 10)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show detailed processing steps")
    args = parser.parse_args()

    global _verbose
    _verbose = args.verbose

    if args.file:
        file_paths = [args.file]
    elif args.dir:
        dirpath = args.dir
        if not os.path.isdir(dirpath):
            print("Error: {} is not a directory".format(dirpath))
            sys.exit(1)
        file_paths = []
        for root_dir, _dirs, files in os.walk(dirpath):
            for name in sorted(files):
                full = os.path.join(root_dir, name)
                file_paths.append(full)
        file_paths.sort()
        if not file_paths:
            print("Error: no files found in {}".format(dirpath))
            sys.exit(1)
    else:
        file_paths = sorted(p for p in glob.glob(args.glob, recursive=True) if os.path.isfile(p))
        if not file_paths:
            print("Error: no files match '{}'".format(args.glob))
            sys.exit(1)

    print("=== QR Air Gap Sender ===")
    print("Files to send: {}".format(len(file_paths)))
    for i, fp in enumerate(file_paths):
        sz = os.path.getsize(fp)
        print("  {}: {} ({} B)".format(i + 1, fp, sz))
    print()

    root = tk.Tk()
    root.geometry("700x750")
    if args.dir:
        base_dir = args.dir
    elif args.glob and len(file_paths) > 1:
        base_dir = os.path.commonpath([os.path.abspath(p) for p in file_paths])
    elif args.glob:
        base_dir = os.path.dirname(os.path.abspath(file_paths[0]))
    else:
        base_dir = None
    SenderApp(root, file_paths, args.fps, args.chunk_size, args.box_size, countdown=0, base_dir=base_dir)
    root.mainloop()


if __name__ == "__main__":
    main()
