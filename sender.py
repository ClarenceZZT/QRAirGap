# -*- coding: utf-8 -*-
"""
QR Air Gap Sender -- runs on the remote server (Python 3.7+, Pillow 7.0+).
Displays QR codes in a Tkinter window for the receiver to capture.

Usage:
    python3 sender.py --file input.txt [--fps 3]
    python3 sender.py --dir ./my_files/ [--fps 3]
"""
import argparse
import glob
import math
import os
import sys
import time as _time
import threading
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed

_here = os.path.dirname(os.path.abspath(__file__))
_vendor_dir = os.path.join(_here, 'qrcode_vendor')
_vendor_zip = os.path.join(_here, 'qrcode_vendor.zip')
if os.path.isdir(_vendor_dir):
    sys.path.insert(0, _vendor_dir)
elif os.path.isfile(_vendor_zip):
    sys.path.insert(0, _vendor_zip)

import tkinter as tk
import PIL
from PIL import ImageTk

try:
    import qrcode
    from qrcode.constants import ERROR_CORRECT_L
    _HAS_QRCODE = True
except ImportError:
    _HAS_QRCODE = False

from protocol import (
    chunk_data,
    encode_chunk,
    encode_end_signal,
    normalize_transfer_filename,
    parse_session_id,
)

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
    qr = qrcode.QRCode(
        version=version,
        error_correction=ERROR_CORRECT_L,
        box_size=box_size,
        border=border,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").get_image()
    return idx, img


def _generate_qr_images_into(images, payloads, version, box_size, border,
                              start=0, end=None, on_progress=None, max_workers=None):
    """Generate QR images into an existing list. Uses multiprocessing for true parallelism."""
    if end is None:
        end = len(payloads)
    n = end - start
    cap = max_workers if max_workers is not None else 4
    workers = min(n, cap)
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
            if _stop_event.is_set():
                break
            result_idx, result_img = _generate_single_qr((payloads[i], version, box_size, border, i))
            _tick(result_idx, result_img)
    else:
        tasks = [(payloads[i], version, box_size, border, i) for i in range(start, end)]
        pool = ProcessPoolExecutor(max_workers=workers)
        futures = [pool.submit(_generate_single_qr, t) for t in tasks]
        for future in as_completed(futures):
            if _stop_event.is_set():
                break
            try:
                idx, img = future.result()
            except Exception as e:
                _log("[qr] Frame generation failed: {}".format(e))
                continue
            if img is not None:
                _tick(idx, img)
        for f in futures:
            f.cancel()
        pool.shutdown(wait=True)
    if _verbose:
        sys.stdout.write("\n")
    elapsed = _time.time() - t0
    _log("[qr] Generated {} images in {:.2f}s ({:.1f}ms/frame, {} workers)".format(
        n, elapsed, elapsed / max(n, 1) * 1000, workers))


def _generate_single_v3_frame(args_tuple):
    packet_bytes, grid_w, grid_h, module_size, n_levels, n_colors, color_ch, idx = args_tuple
    from visual_transport import encode_frame, frame_to_pil
    frame_rgb = encode_frame(packet_bytes, grid_w, grid_h, module_size,
                             n_levels, n_colors, color_ch)
    img = frame_to_pil(frame_rgb)
    return idx, img


def _generate_v3_images_into(images, payloads, grid_w, grid_h, module_size,
                              n_levels=4, n_colors=1, color_ch=1,
                              start=0, end=None, on_progress=None, max_workers=None):
    """Generate V3 frame images into an existing list."""
    if end is None:
        end = len(payloads)
    n = end - start
    cap = max_workers if max_workers is not None else 4
    workers = min(n, cap)
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
            if _stop_event.is_set():
                break
            result_idx, result_img = _generate_single_v3_frame(
                (payloads[i], grid_w, grid_h, module_size, n_levels, n_colors, color_ch, i))
            _tick(result_idx, result_img)
    else:
        tasks = [(payloads[i], grid_w, grid_h, module_size, n_levels, n_colors, color_ch, i)
                 for i in range(start, end)]
        pool = ProcessPoolExecutor(max_workers=workers)
        futures = [pool.submit(_generate_single_v3_frame, t) for t in tasks]
        for future in as_completed(futures):
            if _stop_event.is_set():
                break
            try:
                idx, img = future.result()
            except Exception as e:
                _log("[v3] Frame generation failed: {}".format(e))
                continue
            if img is not None:
                _tick(idx, img)
        for f in futures:
            f.cancel()
        pool.shutdown(wait=True)
    if _verbose:
        sys.stdout.write("\n")
    elapsed = _time.time() - t0
    _log("[v3] Generated {} frames in {:.2f}s ({:.1f}ms/frame, {} workers)".format(
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


def prepare_file_meta(filepath, chunk_size, box_size, base_dir=None, protocol=1,
                      session_id=None, grid_w=160, grid_h=96, module_size=4,
                      offset=0, length=None, display_name=None,
                      n_levels=4, n_colors=1, color_ch=1):
    """Prepare file metadata and payloads (fast, no QR/frame generation).

    offset/length: read only a segment of the file (for large file splitting).
    display_name:  override the transfer filename (e.g. 'big.tgz.part01').
    """
    _log("[prepare] Reading {} (offset={}, length={})".format(filepath, offset, length))
    with open(filepath, "rb") as f:
        if offset > 0:
            f.seek(offset)
        data = f.read(length) if length is not None else f.read()
    filename = display_name or normalize_transfer_filename(filepath, base_dir)
    sid = parse_session_id(session_id, protocol)

    if protocol == 3:
        from visual_transport import (encode_v3_packet, chunk_capacity_bytes,
                                       FRAME_OVERHEAD)
        cap = chunk_capacity_bytes(grid_w, grid_h, filename, n_levels, n_colors)
        if cap <= 0:
            print("  [WARN] V3 grid {}x{} too small for filename, falling back to V1".format(
                grid_w, grid_h))
            protocol = 1
        else:
            effective_cs = cap
            chunks = chunk_data(data, effective_cs)
            if not chunks:
                chunks = [b""]
            total = len(chunks)
            _log("[prepare] {} -> sid={}, {} bytes, {} chunks (v3, chunk_size={})".format(
                filename, sid, len(data), total, effective_cs))
            payloads = []
            for idx, chunk in enumerate(chunks):
                payloads.append(encode_v3_packet(sid, idx, total, chunk, filename,
                                                 n_levels=n_levels, n_colors=n_colors))
            fw = grid_w + FRAME_OVERHEAD
            fh = grid_h + FRAME_OVERHEAD
            mode_label = "gray{}".format(n_levels) if n_colors == 1 else \
                "{}c×gray{}".format(n_colors, n_levels)
            print("  V3 {} ({}x{} grid, {}x{} frame) {} B/chunk, {} frames".format(
                mode_label, grid_w, grid_h, fw, fh, effective_cs, total))
            return {
                "filepath": filepath,
                "filename": filename,
                "size": len(data),
                "sid": sid,
                "total": total,
                "payloads": payloads,
                "version": None,
                "pil_images": [None] * total,
                "grid_w": grid_w, "grid_h": grid_h, "module_size": module_size,
                "n_levels": n_levels, "n_colors": n_colors, "color_ch": color_ch,
            }

    chunks = chunk_data(data, chunk_size)
    if not chunks:
        chunks = [b""]
    total = len(chunks)

    if protocol == 2 and total > 99999999:
        print("  [WARN] {} chunks > 99999999 limit for V2, falling back to V1".format(total))
        protocol = 1
        sid = parse_session_id(session_id, protocol)

    _log("[prepare] {} -> sid={}, {} bytes, {} chunks (chunk_size={})".format(
        filename, sid, len(data), total, chunk_size))
    payloads = []
    for idx, chunk in enumerate(chunks):
        payloads.append(encode_chunk(sid, idx, total, chunk, filename=filename,
                                     protocol=protocol))
    _log("[prepare] Payload sizes: min={} max={} chars".format(
        min(len(p) for p in payloads), max(len(p) for p in payloads)))
    version = _determine_qr_version(payloads)
    modules = version * 4 + 17
    mode_str = "alphanumeric" if protocol == 2 else "byte"
    print("  QR v{} ({}x{} {}) for {} frames".format(
        version, modules, modules, mode_str, total))
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


def prepare_file_data(filepath, chunk_size, box_size, base_dir=None, protocol=1,
                      session_id=None, grid_w=160, grid_h=96, module_size=4,
                      offset=0, length=None, display_name=None,
                      n_levels=4, n_colors=1, color_ch=1):
    """Generate all QR/V3 PIL images (blocking). Used by preload thread."""
    meta = prepare_file_meta(filepath, chunk_size, box_size, base_dir,
                             protocol=protocol, session_id=session_id,
                             grid_w=grid_w, grid_h=grid_h, module_size=module_size,
                             offset=offset, length=length, display_name=display_name,
                             n_levels=n_levels, n_colors=n_colors, color_ch=color_ch)
    if protocol == 3 and meta.get("grid_w") is not None:
        nl = meta.get("n_levels", 4)
        nc = meta.get("n_colors", 1)
        cc = meta.get("color_ch", 1)
        from visual_transport import calibration_frame_to_pil
        meta["_calib_pil"] = calibration_frame_to_pil(
            meta["grid_w"], meta["grid_h"], meta["module_size"], nl, nc, cc)
        _generate_v3_images_into(
            meta["pil_images"], meta["payloads"],
            meta["grid_w"], meta["grid_h"], meta["module_size"],
            n_levels=nl, n_colors=nc, color_ch=cc)
    else:
        _generate_qr_images_into(
            meta["pil_images"], meta["payloads"],
            meta["version"], box_size, 6)
    _log("[prepare] Done: {} PIL images ready".format(meta["total"]))
    return meta


class SenderApp(object):
    def __init__(self, root, file_entries, fps, chunk_size, box_size, countdown=3,
                 base_dir=None, num_qr=1, protocol=1, qr_workers=4, session_id=None,
                 grid_w=160, grid_h=96, module_size=4, n_levels=4,
                 n_colors=1, color_ch=1, preload_ahead=0):
        self.root = root
        self.fps = fps
        self.interval = int(1000 / fps)
        self.chunk_size = chunk_size
        self.box_size = box_size
        self.countdown_duration = countdown
        self.base_dir = base_dir
        self.num_qr = num_qr
        self.protocol = protocol
        self.qr_workers = qr_workers
        self.session_id = session_id
        self.grid_w = grid_w
        self.grid_h = grid_h
        self.module_size = module_size
        self.n_levels = n_levels
        self.n_colors = n_colors
        self.color_ch = color_ch
        self.preload_ahead = preload_ahead

        self.file_entries = file_entries
        self.file_index = 0
        self.file_cache = {}   # index -> dict with pil_images
        self._tk_cache = {}    # index -> list of ImageTk.PhotoImage
        self._preload_lock = threading.Lock()
        self._preload_thread = None

        self.current_chunk = 0
        self.paused = False
        self._after_id = None
        self._multi_img_items = []

        self.root.title("QR Air Gap Sender")
        self.root.configure(bg="white")
        try:
            self.root.attributes("-alpha", 1.0)
        except tk.TclError:
            pass

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
        if len(file_entries) == 1:
            help_text = "Space: pause  |  r: reset  |  g: goto  |  +/-: speed  |  q: quit"
        else:
            help_text += "\nr: reset  |  g: goto frame  |  +/-: speed  |  q: quit"
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
        self.root.bind("<r>", self._reset_file)
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
        self._calib_tk_img = None

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
            entry = self.file_entries[self.file_index]
            path = entry["path"]
            fi = self.file_index
            label = entry["display_name"] or os.path.basename(path)
            _log("[app] Encoding file {}/{}: {}".format(
                fi + 1, len(self.file_entries), label))
            self.file_var.set("File {}/{}: {} (loading...)".format(
                fi + 1, len(self.file_entries), label))

            def _bg_prepare():
                meta = prepare_file_meta(
                    path, self.chunk_size, self.box_size, self.base_dir,
                    protocol=self.protocol, session_id=self.session_id,
                    grid_w=self.grid_w, grid_h=self.grid_h,
                    module_size=self.module_size,
                    offset=entry["offset"], length=entry["length"],
                    display_name=entry["display_name"],
                    n_levels=self.n_levels, n_colors=self.n_colors,
                    color_ch=self.color_ch)
                is_v3 = self.protocol == 3 and meta.get("grid_w") is not None
                nl = meta.get("n_levels", 4)
                nc = meta.get("n_colors", 1)
                cc = meta.get("color_ch", 1)

                if is_v3:
                    from visual_transport import calibration_frame_to_pil
                    calib_img = calibration_frame_to_pil(
                        meta["grid_w"], meta["grid_h"], meta["module_size"],
                        nl, nc, cc)
                    meta["_calib_pil"] = calib_img

                    _, first_img = _generate_single_v3_frame((
                        meta["payloads"][0], meta["grid_w"], meta["grid_h"],
                        meta["module_size"], nl, nc, cc, 0))
                else:
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
                    if is_v3:
                        _generate_v3_images_into(
                            meta["pil_images"], meta["payloads"],
                            meta["grid_w"], meta["grid_h"], meta["module_size"],
                            n_levels=nl, n_colors=nc, color_ch=cc,
                            start=1, on_progress=_on_progress,
                            max_workers=self.qr_workers)
                    else:
                        _generate_qr_images_into(
                            meta["pil_images"], meta["payloads"],
                            meta["version"], self.box_size, 6,
                            start=1, on_progress=_on_progress,
                            max_workers=self.qr_workers)
                    _log("[app] File {} all {} frames ready".format(fi + 1, total_frames))
                self.root.after(0, lambda: self._on_file_ready(fi, meta))
                self.root.after(0, self._preload_next)

            threading.Thread(target=_bg_prepare, daemon=True).start()
        else:
            _log("[app] File {}/{} already cached".format(
                self.file_index + 1, len(self.file_entries)))
            info = self._current_info()
            self.file_var.set("File {}/{}: {} ({} B, {} chunks)".format(
                self.file_index + 1, len(self.file_entries),
                info["filename"], info["size"], info["total"],
            ))
            self._preload_next()

    def _on_file_ready(self, fi, meta):
        if fi != self.file_index:
            return
        self.file_var.set("File {}/{}: {} ({} B, {} chunks)".format(
            fi + 1, len(self.file_entries),
            meta["filename"], meta["size"], meta["total"],
        ))

    def _update_gen_progress(self, fi, done, total):
        if fi != self.file_index:
            return
        info = self.file_cache.get(fi)
        if info is None:
            return
        pct = done * 100 // total
        label = self._mode_label() if self.protocol == 3 else "QR"
        self.file_var.set("File {}/{}: {} ({} B) -- {} {}% ({}/{})".format(
            fi + 1, len(self.file_entries),
            info["filename"], info["size"], label, pct, done, total,
        ))

    def _preload_next(self):
        if self._preload_thread is not None and self._preload_thread.is_alive():
            _log("[preload] Thread already running, skip")
            return
        start_idx = self.file_index + 1
        if start_idx >= len(self.file_entries):
            return

        limit = self.preload_ahead
        if limit > 0:
            end_idx = min(start_idx + limit, len(self.file_entries))
        else:
            end_idx = len(self.file_entries)

        with self._preload_lock:
            if all(i in self.file_cache for i in range(start_idx, end_idx)):
                _log("[preload] All target files already cached")
                return

        self._evict_file_caches()

        _log("[preload] Starting background thread for files {}-{}".format(
            start_idx + 1, end_idx))
        def _do():
            for idx in range(start_idx, end_idx):
                with self._preload_lock:
                    if idx in self.file_cache:
                        continue
                entry = self.file_entries[idx]
                info = prepare_file_data(entry["path"], self.chunk_size, self.box_size,
                                        self.base_dir, protocol=self.protocol,
                                        grid_w=self.grid_w, grid_h=self.grid_h,
                                        module_size=self.module_size,
                                        offset=entry["offset"], length=entry["length"],
                                        display_name=entry["display_name"],
                                        n_levels=self.n_levels,
                                        n_colors=self.n_colors,
                                        color_ch=self.color_ch)
                with self._preload_lock:
                    self.file_cache[idx] = info
                print("[preload] Ready {}/{}: {} ({} chunks)".format(
                    idx + 1, len(self.file_entries), info["filename"], info["total"]))
        self._preload_thread = threading.Thread(target=_do, daemon=True)
        self._preload_thread.start()

    def _evict_file_caches(self):
        """Release file caches outside the preload window to free memory."""
        limit = self.preload_ahead
        if limit <= 0:
            return
        keep_lo = self.file_index
        keep_hi = self.file_index + limit
        for k in list(self.file_cache.keys()):
            if k < keep_lo or k > keep_hi:
                del self.file_cache[k]
                self._tk_cache.pop(k, None)
                _log("[evict] Released cache for file {}".format(k + 1))

    def _mode_label(self):
        if self.n_colors == 1:
            return "V3/gray{}".format(self.n_levels)
        return "V3/{}c×gray{}".format(self.n_colors, self.n_levels)

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
            if self.protocol == 3:
                self._show_calibration()
            else:
                self._show_frame()

    def _show_calibration(self):
        """Display a single calibration frame, then wait for Space to start."""
        if self.file_index not in self.file_cache:
            self._after_id = self.root.after(100, self._show_calibration)
            return

        info = self._current_info()
        calib_pil = info.get("_calib_pil")
        if calib_pil is None:
            self._show_frame()
            return

        if self._calib_tk_img is None:
            self._calib_tk_img = ImageTk.PhotoImage(calib_pil)

        cx = self.canvas.winfo_width() // 2
        cy = self.canvas.winfo_height() // 2
        if self.img_on_canvas is not None:
            self.canvas.itemconfig(self.img_on_canvas, image=self._calib_tk_img)
            self.canvas.coords(self.img_on_canvas, cx, cy)
        else:
            self.img_on_canvas = self.canvas.create_image(
                cx, cy, image=self._calib_tk_img)

        nl = info.get("n_levels", 4)
        self.paused = True
        self._calib_auto_countdown = 15
        self._calib_auto_tick()

    def _calib_auto_tick(self):
        """Countdown during calibration; auto-start when it reaches 0."""
        if self._calib_tk_img is None:
            return
        if self._calib_auto_countdown <= 0:
            self._calib_tk_img = None
            self.paused = False
            self._show_frame()
            return
        self.status_var.set("[CALIB] {} — Space to start / auto in {}s".format(
            self._mode_label(), self._calib_auto_countdown))
        self._calib_auto_countdown -= 1
        self._after_id = self.root.after(1000, self._calib_auto_tick)

    def _show_frame(self):
        if self.paused:
            self._after_id = self.root.after(self.interval, self._show_frame)
            return

        if self.file_index not in self.file_cache:
            self.status_var.set("Loading file...")
            self._after_id = self.root.after(100, self._show_frame)
            return

        info = self._current_info()

        if self.num_qr > 1:
            if not self._show_multi_qr(info):
                return
        else:
            if self._missing_frames is not None:
                frame_idx = self._missing_frames[self._missing_pos]
            else:
                frame_idx = self.current_chunk

            tk_img = self._get_tk_image(frame_idx)
            if tk_img is None:
                ready = sum(1 for img in info["pil_images"] if img is not None)
                pct = ready * 100 // info["total"]
                label = self._mode_label() if self.protocol == 3 else "QR"
                self.status_var.set("Generating {} {}%  ({}/{})".format(label, pct, ready, info["total"]))
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

    def _show_multi_qr(self, info):
        """Display multiple QR codes in a grid. Returns False if not ready."""
        ncols = math.ceil(math.sqrt(self.num_qr))
        nrows = math.ceil(self.num_qr / ncols)
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        cell_w = cw / max(ncols, 1)
        cell_h = ch / max(nrows, 1)

        frame_indices = []
        for q in range(self.num_qr):
            if self._missing_frames is not None:
                idx = self._missing_frames[(self._missing_pos + q) % len(self._missing_frames)]
            else:
                idx = (self.current_chunk + q) % info["total"]
            frame_indices.append(idx)

        for idx in frame_indices:
            if info["pil_images"][idx] is None:
                ready = sum(1 for img in info["pil_images"] if img is not None)
                pct = ready * 100 // info["total"]
                label = self._mode_label() if self.protocol == 3 else "QR"
                self.status_var.set("Generating {} {}%  ({}/{})".format(
                    label, pct, ready, info["total"]))
                self._after_id = self.root.after(50, self._show_frame)
                return False

        for q, frame_idx in enumerate(frame_indices):
            tk_img = self._get_tk_image(frame_idx)
            row = q // ncols
            col = q % ncols
            cx = int((col + 0.5) * cell_w)
            cy = int((row + 0.5) * cell_h)

            if q < len(self._multi_img_items):
                self.canvas.itemconfig(self._multi_img_items[q], image=tk_img)
                self.canvas.coords(self._multi_img_items[q], cx, cy)
            else:
                item = self.canvas.create_image(cx, cy, image=tk_img)
                self._multi_img_items.append(item)

        if self._missing_frames is not None:
            self._missing_pos = (self._missing_pos + self.num_qr) % len(self._missing_frames)
            label = "RESEND {}/{} (x{})".format(
                min(self._missing_pos or len(self._missing_frames),
                    len(self._missing_frames)),
                len(self._missing_frames), self.num_qr,
            )
        else:
            self.current_chunk = (self.current_chunk + self.num_qr) % info["total"]
            label = "chunks {}-{}/{}".format(
                frame_indices[0] + 1, frame_indices[-1] + 1, info["total"])

        self.status_var.set("[{}]  {}   {:.1f} fps  x{}".format(
            info["sid"], label, self.fps, self.num_qr
        ))
        return True

    def _switch_file(self):
        _log("[app] Switching to file {}/{}".format(
            self.file_index + 1, len(self.file_entries)))
        self._cancel_pending()
        self.paused = True
        self._missing_frames = None
        self._missing_pos = 0
        self._calib_tk_img = None
        self._input_mode = False
        self._input_buffer = ""
        if self._input_timeout_id is not None:
            self.root.after_cancel(self._input_timeout_id)
            self._input_timeout_id = None
        if self.img_on_canvas is not None:
            self.canvas.delete(self.img_on_canvas)
            self.img_on_canvas = None
        for item in self._multi_img_items:
            self.canvas.delete(item)
        self._multi_img_items.clear()
        self._prepare_current()
        self._countdown_remaining = self.countdown_duration
        self._start_countdown()

    def _next_file(self, event=None):
        if self._input_mode:
            return
        if self.file_index + 1 < len(self.file_entries):
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
        """Display END QR/frame and auto-quit after a few seconds."""
        self._cancel_pending()
        self.paused = True
        if self.img_on_canvas is not None:
            self.canvas.delete(self.img_on_canvas)
            self.img_on_canvas = None
        for item in self._multi_img_items:
            self.canvas.delete(item)
        self._multi_img_items.clear()

        if self.protocol == 3:
            from visual_transport import encode_v3_end_packet, encode_frame, frame_to_pil
            # Always encode END frame as gray-only (n_colors=1) to survive
            # JPEG chroma subsampling — the few colored symbols in a
            # normal END frame get destroyed by remote-desktop compression.
            end_pkt = encode_v3_end_packet(self.n_levels, n_colors=1)
            frame_rgb = encode_frame(end_pkt, self.grid_w, self.grid_h,
                                     self.module_size, self.n_levels,
                                     n_colors=1)
            img = frame_to_pil(frame_rgb)
        else:
            end_payload = encode_end_signal(protocol=self.protocol)
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

        self.file_var.set("All {} files sent!".format(len(self.file_entries)))
        self._end_countdown = 5
        self._end_tick()

    def _end_tick(self):
        if self._end_countdown > 0:
            self.status_var.set("END -- closing in {}s...".format(self._end_countdown))
            self._end_countdown -= 1
            self._after_id = self.root.after(1000, self._end_tick)
        else:
            self.root.destroy()

    def _toggle_pause(self, event=None):
        if self._input_mode:
            return
        was_calib = self._calib_tk_img is not None
        self.paused = not self.paused
        if not self.paused and was_calib:
            self._cancel_pending()
            self._calib_tk_img = None
            self._show_frame()
            return
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

    def _reset_file(self, event=None):
        """Reset current file to frame 0, exit retransmit mode."""
        if self._input_mode:
            return
        self._cancel_pending()
        self.current_chunk = 0
        self._missing_frames = None
        self._missing_pos = 0
        self.paused = False
        if self.file_index in self.file_cache:
            info = self._current_info()
            print("[reset] File {}: {} -- restarting from frame 0".format(
                self.file_index + 1, info["filename"]))
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
        if self.num_qr > 1 and self._multi_img_items:
            ncols = math.ceil(math.sqrt(self.num_qr))
            nrows = math.ceil(self.num_qr / ncols)
            cw = self.canvas.winfo_width()
            ch = self.canvas.winfo_height()
            cell_w = cw / max(ncols, 1)
            cell_h = ch / max(nrows, 1)
            for q, item in enumerate(self._multi_img_items):
                row = q // ncols
                col = q % ncols
                cx = int((col + 0.5) * cell_w)
                cy = int((row + 0.5) * cell_h)
                self.canvas.coords(item, cx, cy)
        elif self.img_on_canvas is not None:
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
    parser.add_argument("--num-qr", type=int, default=1,
                        help="[experimental] Show N QR codes simultaneously (default: 1)")
    parser.add_argument("--protocol", type=int, default=1, choices=[1, 2, 3],
                        help="Encoding protocol: 1=JSON+base64, 2=base45+alphanumeric, "
                             "3=gray4 visual frame (default: 1)")
    parser.add_argument("--grid", type=str, default="160,96",
                        help="Data grid W,H for --protocol 3 (default: 160,96)")
    parser.add_argument("--module-size", type=int, default=4,
                        help="Pixels per module for --protocol 3 (default: 4)")
    parser.add_argument("--session-id", dest="session_id", default=None, metavar="SID",
                        help="Fixed 4-char alphanumeric session id (default: random per file). "
                             "Same input file + same SID + same --chunk-size/--protocol "
                             "yields identical QR payloads on any machine.")
    parser.add_argument("--qr-workers", type=int, default=4,
                        help="Number of parallel QR generation processes (default: 4)")
    parser.add_argument("--gray-levels", type=int, default=4, choices=[4, 8],
                        help="Number of gray levels for --protocol 3 (default: 4). "
                             "4=2 bits/module, 8=3 bits/module.")
    parser.add_argument("--colors", type=int, default=1, choices=[1, 2, 4],
                        help="Number of color channels for --protocol 3 (default: 1). "
                             "1=gray only, 2=gray+1 color, 4=gray+R+G+B.")
    parser.add_argument("--color-channel", type=str, default="R",
                        choices=["R", "G", "B"],
                        help="Which color channel to use when --colors 2 (default: R).")
    parser.add_argument("--split-size", type=str, default="1G",
                        help="Split files larger than this into segments (default: 1G). "
                             "Supports K/M/G suffixes.")
    parser.add_argument("--preload-ahead", type=int, default=0,
                        help="Number of files to preload ahead (default: 0 = all). "
                             "Use a small value (e.g. 1-2) for very large file sets to limit memory.")
    args = parser.parse_args()

    global _verbose
    _verbose = args.verbose

    # Parse --split-size
    def _parse_size(s):
        s = s.strip().upper()
        multipliers = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
        if s[-1] in multipliers:
            return int(float(s[:-1]) * multipliers[s[-1]])
        return int(s)

    try:
        split_size = _parse_size(args.split_size)
    except (ValueError, IndexError):
        print("Error: invalid --split-size '{}'".format(args.split_size))
        sys.exit(1)

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

    # Parse grid dimensions for V3 — accept "WxH", "W,H", or "W H"
    import re
    _grid_parts = re.split(r'[x,\s]+', args.grid.strip().lower())
    try:
        if len(_grid_parts) != 2:
            raise ValueError
        grid_w, grid_h = int(_grid_parts[0]), int(_grid_parts[1])
    except ValueError:
        print("Error: --grid must be W,H (e.g. 160,96 or 160x96)")
        sys.exit(1)

    # Determine base_dir early so split display_name preserves directory structure
    if args.dir:
        base_dir = args.dir
    elif args.glob and len(file_paths) > 1:
        base_dir = os.path.commonpath([os.path.abspath(p) for p in file_paths])
    elif args.glob:
        base_dir = os.path.dirname(os.path.abspath(file_paths[0]))
    else:
        base_dir = None

    # Split large files into segments
    file_entries = []  # list of dicts: {path, offset, length, display_name}
    for fp in file_paths:
        sz = os.path.getsize(fp)
        if sz <= split_size:
            file_entries.append({"path": fp, "offset": 0, "length": sz,
                                 "display_name": None})
        else:
            rel_name = normalize_transfer_filename(fp, base_dir)
            num_parts = (sz + split_size - 1) // split_size
            pad = len(str(num_parts))
            for pi in range(num_parts):
                off = pi * split_size
                seg_len = min(split_size, sz - off)
                part_suffix = ".part{:0{w}d}of{:0{w}d}".format(
                    pi + 1, num_parts, w=pad)
                file_entries.append({"path": fp, "offset": off, "length": seg_len,
                                     "display_name": rel_name + part_suffix})
            print("  [split] {} ({} B) -> {} parts of ~{} B".format(
                fp, sz, num_parts, split_size))

    if args.protocol in (1, 2) and not _HAS_QRCODE:
        print("Error: qrcode library not found. Required for --protocol 1/2.")
        print("  Ensure qrcode_vendor/ is in the same directory as sender.py")
        sys.exit(1)

    print("=== QR Air Gap Sender ===")
    if args.protocol != 3:
        print("Libraries: qrcode {} @ {}".format(
            getattr(qrcode, "__version__", "?") if _HAS_QRCODE else "N/A",
            getattr(qrcode, "__file__", "?") if _HAS_QRCODE else "N/A"))
        print("           Pillow {} @ {}".format(PIL.__version__, PIL.__file__))
    if args.protocol == 2:
        print("Protocol: v2 (base45 + QR alphanumeric mode)")
    elif args.protocol == 3:
        from visual_transport import chunk_capacity_bytes, FRAME_OVERHEAD, _compute_bpm
        fw = grid_w + FRAME_OVERHEAD
        fh = grid_h + FRAME_OVERHEAD
        nl = args.gray_levels
        nc = args.colors
        bpm = _compute_bpm(nl, nc)
        mode_str = "gray{}".format(nl) if nc == 1 else "{}c×gray{}".format(nc, nl)
        print("Protocol: v3 ({} visual frame, {} bits/module)".format(mode_str, bpm))
        print("  Grid: {}x{} modules, frame: {}x{} modules".format(
            grid_w, grid_h, fw, fh))
        print("  Module size: {}px, image: {}x{}px".format(
            args.module_size, fw * args.module_size, fh * args.module_size))
        print("  Capacity: ~{} B/frame".format(
            chunk_capacity_bytes(grid_w, grid_h, n_levels=nl, n_colors=nc)))
    print("Files/segments to send: {}".format(len(file_entries)))
    for i, entry in enumerate(file_entries):
        label = entry["display_name"] or os.path.basename(entry["path"])
        print("  {}: {} ({} B{})".format(
            i + 1, label, entry["length"],
            " @offset {}".format(entry["offset"]) if entry["offset"] > 0 else ""))
    print()

    root = tk.Tk()
    root.configure(bg="white")
    try:
        root.attributes("-alpha", 1.0)
        root.wm_attributes("-topmost", False)
    except tk.TclError:
        pass
    num_qr = max(1, args.num_qr)
    if args.protocol == 3 and num_qr > 1:
        print("[WARN] --num-qr > 1 is not supported with --protocol 3, forcing num-qr=1")
        num_qr = 1
    if args.protocol == 3:
        fw = grid_w + FRAME_OVERHEAD
        fh = grid_h + FRAME_OVERHEAD
        win_w = fw * args.module_size + 20
        win_h = fh * args.module_size + 80
    else:
        ncols = math.ceil(math.sqrt(num_qr))
        nrows = math.ceil(num_qr / ncols)
        win_w = min(700 * ncols, 2800)
        win_h = min(700 * nrows + 50, 1800)
    root.geometry("{}x{}".format(win_w, win_h))
    if num_qr > 1:
        print("[experimental] Multi-QR mode: {} QR codes ({}x{} grid)".format(
            num_qr, ncols, nrows))
    if args.session_id is not None:
        try:
            parse_session_id(args.session_id, args.protocol)
        except ValueError as e:
            print("Error: --session-id: {}".format(e))
            sys.exit(1)

    color_ch_map = {"R": 1, "G": 2, "B": 3}
    n_colors = args.colors
    color_ch = color_ch_map.get(args.color_channel, 1)

    if n_colors > 1 and args.protocol != 3:
        print("[WARN] --colors > 1 requires --protocol 3; ignoring.")
        n_colors = 1

    SenderApp(root, file_entries, args.fps, args.chunk_size, args.box_size,
              countdown=0, base_dir=base_dir, num_qr=num_qr, protocol=args.protocol,
              qr_workers=args.qr_workers, session_id=args.session_id,
              grid_w=grid_w, grid_h=grid_h, module_size=args.module_size,
              n_levels=args.gray_levels, n_colors=n_colors, color_ch=color_ch,
              preload_ahead=args.preload_ahead)
    root.mainloop()


if __name__ == "__main__":
    main()
