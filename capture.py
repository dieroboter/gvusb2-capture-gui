import os, sys, time, datetime, threading, socket, subprocess, re, math
from collections import deque
import tkinter as tk
from tkinter import ttk, messagebox
from PIL import Image, ImageTk
import sounddevice as sd

REQ_VID, REQ_AUD = "GV-USB2, Analog Capture", "GV-USB2, Analog WaveIn"
CAP_W, CAP_H, PREV_W, PREV_H = 720, 480, 640, 480
FRAME_SZ = PREV_W * PREV_H * 3

# Audio delay in ms to synchronize with video
PREV_AUD_DELAY = 300  # Offset for live preview playback
REC_AUD_DELAY = 300   # Offset for saved video file recording

NO_WIN = 0x08000000 if sys.platform == "win32" else 0


def verify_devices():
    """Queries DirectShow devices via FFmpeg before the GUI is allowed to open."""
    try:
        out = subprocess.run(
            ["ffmpeg", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="ignore", creationflags=NO_WIN
        ).stderr
    except FileNotFoundError:
        return False, "FFmpeg was not found in your system PATH.\nPlease install FFmpeg and try again."
    except Exception as e:
        return False, f"Failed to query capture devices:\n{e}"

    v_devs, a_devs, sec = [], [], None
    for line in out.splitlines():
        if "Alternative name" in line: continue
        if m := re.search(r'"([^"]+)"\s*\(([^)]*)\)', line):
            types = [t.strip().lower() for t in m.group(2).split(",")]
            if "video" in types: v_devs.append(m.group(1))
            if "audio" in types: a_devs.append(m.group(1))
            continue
        if "DirectShow video devices" in line: sec = "v"; continue
        if "DirectShow audio devices" in line: sec = "a"; continue
        if sec and (m := re.search(r'"([^"]+)"', line)):
            (v_devs if sec == "v" else a_devs).append(m.group(1))

    missing = [f"• {k} Device: '{n}'" for k, n, d in (("Video", REQ_VID, v_devs), ("Audio", REQ_AUD, a_devs)) if n not in d]
    if missing:
        return False, "The required inputs weren't found:\n\n" + "\n".join(missing) + "\n\nPlease connect the I-O Data GV-USB2 and try again."
    return True, ""


class LiveAudioPlayer:
    def __init__(self, sample_rate=48000, channels=2, blocksize=1024):
        self.sr, self.ch, self.blocksize, self.bps = sample_rate, channels, blocksize, 2 * channels
        self.stream, self.running, self.buffer, self.started, self._lock = None, False, bytearray(), False, threading.Lock()
        self.prebuf, self.max_buf, self.tgt_buf = int(self.sr * self.bps * 0.06), int(self.sr * self.bps * 0.28), int(self.sr * self.bps * 0.12)

    def _callback(self, outdata, frames, time_info, status):
        req = frames * self.bps
        with self._lock:
            if not self.started:
                if len(self.buffer) < self.prebuf:
                    outdata[:] = b"\x00" * req; return
                self.started = True
            n = min(len(self.buffer), req)
            outdata[:n] = self.buffer[:n]
            del self.buffer[:n]
            if n < req: outdata[n:] = b"\x00" * (req - n)

    def start(self):
        with self._lock:
            if self.running: return
            try:
                self.buffer.clear(); self.started = False
                self.stream = sd.RawOutputStream(samplerate=self.sr, channels=self.ch, dtype="int16", blocksize=self.blocksize, callback=self._callback)
                self.stream.start()
                self.running = True
            except Exception as e: print(f"[Audio] Init error: {e}", flush=True)

    def write(self, data):
        if self.running and data:
            with self._lock:
                self.buffer.extend(data)
                excess = len(self.buffer) - self.tgt_buf
                if len(self.buffer) > self.max_buf and (excess := excess - (excess % self.bps)) > 0:
                    del self.buffer[:excess]

    def stop(self):
        with self._lock:
            self.running, s, self.stream = False, self.stream, None
            self.buffer.clear(); self.started = False
        if s:
            try: s.stop(); s.close()
            except Exception: pass


class TimerDialog(tk.Toplevel):
    def __init__(self, parent, on_start_timer_cb):
        super().__init__(parent)
        self.title("Timer Record")
        self.resizable(False, False); self.transient(parent); self.grab_set()
        self.cb, self.vars = on_start_timer_cb, {}

        tk.Label(self, text="Set Recording Duration", font=("Arial", 11, "bold")).pack(pady=(12, 6))
        f_in = tk.Frame(self); f_in.pack(padx=10, pady=6)
        for i, (unit, limit) in enumerate((("Hours", 24), ("Minutes", 59), ("Seconds", 59))):
            tk.Label(f_in, text=f"{unit}:").grid(row=0, column=i * 2, padx=4)
            v = tk.StringVar(value="0")
            sp = ttk.Spinbox(f_in, from_=0, to=limit, width=4, textvariable=v, wrap=True)
            sp.grid(row=0, column=i * 2 + 1, padx=(0, 10 if i < 2 else 0))
            self.vars[unit] = (v, sp)

        f_pre = tk.Frame(self); f_pre.pack(pady=(4, 8))
        for lbl, h, m in [("15m", 0, 15), ("30m", 0, 30), ("1h", 1, 0), ("2h", 2, 0), ("4h", 4, 0)]:
            tk.Button(f_pre, text=lbl, width=8, command=lambda h=h, m=m: self._set_preset(h, m)).pack(side=tk.LEFT, padx=3)

        f_btn = tk.Frame(self); f_btn.pack(pady=(8, 14))
        tk.Button(f_btn, text="Start Timer Recording", command=self._confirm, bg="#2980b9", fg="white", font=("Arial", 10, "bold"), padx=10, pady=4).pack(side=tk.LEFT, padx=6)
        tk.Button(f_btn, text="Cancel", command=self.destroy, padx=8, pady=4).pack(side=tk.LEFT, padx=6)

        self.bind("<Return>", lambda e: self._confirm()); self.bind("<Escape>", lambda e: self.destroy())
        self.update_idletasks()
        w, h, pw, ph = self.winfo_width(), self.winfo_height(), parent.winfo_width(), parent.winfo_height()
        self.geometry(f"+{parent.winfo_rootx() + (pw - w) // 2}+{parent.winfo_rooty() + (ph - h) // 2}")
        self.vars["Hours"][1].focus_set()

    def _set_preset(self, h, m):
        self.vars["Hours"][0].set(str(h)); self.vars["Minutes"][0].set(str(m)); self.vars["Seconds"][0].set("0")

    def _confirm(self):
        try:
            h, m, s = (int(self.vars[k][0].get().strip()) for k in ("Hours", "Minutes", "Seconds"))
            if not (h >= 0 and 0 <= m <= 59 and 0 <= s <= 59): raise ValueError
        except ValueError:
            return messagebox.showerror("Invalid Input", "Please enter valid integers (0-24 hrs, 0-59 mins/secs).", parent=self)
        tot = h * 3600 + m * 60 + s
        if tot <= 0: return messagebox.showerror("Invalid Duration", "Recording duration must be at least 1 second.", parent=self)
        self.destroy(); self.cb(tot)


class DVDRecorderGUI:
    def __init__(self, root):
        self.root, self.process = root, None
        self.is_recording, self.is_finalizing, self._is_closing = False, False, False
        self._rec_file, self._rec_lock, self.current_output, self._temp_output = None, threading.Lock(), None, None
        self.audio_player, self._audio_sock, self._rec_sock = LiveAudioPlayer(), None, None
        self._audio_conn, self._rec_conn, self.timer_end_time, self.target_duration, self._timer_after_id = None, None, None, None, None
        self._frame_lock, self._frame_queue = threading.Lock(), deque(maxlen=2)

        root.title("GV-USB2 Recorder"); root.resizable(False, False); root.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.status_label = tk.Label(root, text="Status: Initializing capture engine...", font=("Arial", 11, "bold"), fg="#f39c12")
        self.status_label.pack(pady=(10, 6))
        
        self.canvas = tk.Canvas(root, width=PREV_W, height=PREV_H, bg="black", highlightthickness=0)
        self.canvas.pack(padx=14, pady=4)
        self.photo = ImageTk.PhotoImage(Image.new("RGB", (PREV_W, PREV_H), "black"))
        self.canvas.create_image(0, 0, anchor=tk.NW, image=self.photo)

        btn_frame = tk.Frame(root); btn_frame.pack(pady=(8, 12))
        self.start_btn = tk.Button(btn_frame, text="Start Recording", command=self.start_recording, bg="#2ecc71", fg="white", font=("Arial", 11, "bold"), padx=12, pady=6, state=tk.DISABLED)
        self.start_btn.grid(row=0, column=0, padx=8)
        self.stop_btn = tk.Button(btn_frame, text="Stop Recording", command=self.stop_recording, bg="#e74c3c", fg="white", font=("Arial", 11, "bold"), padx=12, pady=6, state=tk.DISABLED)
        self.stop_btn.grid(row=0, column=1, padx=8)
        self.timer_btn = tk.Button(btn_frame, text="Set Timer...", command=self.open_timer_dialog, bg="#2980b9", fg="white", font=("Arial", 11, "bold"), padx=12, pady=6, state=tk.DISABLED)
        self.timer_btn.grid(row=0, column=2, padx=8)

        self.root.after(15, self._render_tick)
        threading.Thread(target=self.start_continuous_capture, daemon=True).start()

    def _render_tick(self):
        if self._is_closing: return
        with self._frame_lock:
            frame = self._frame_queue.popleft() if self._frame_queue else None
        if frame and self.process and self.process.poll() is None:
            try: self.photo.paste(Image.frombytes("RGB", (PREV_W, PREV_H), frame))
            except Exception: pass
        if not self._is_closing: self.root.after(15, self._render_tick)

    def _make_tcp_server(self, rcvbuf=None):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if rcvbuf: s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        s.bind(("127.0.0.1", 0)); s.listen(1); s.settimeout(0.5)
        return s, s.getsockname()[1]

    @staticmethod
    def _audio_filter_expr(delay_ms):
        try: d = int(delay_ms)
        except Exception: d = 0
        if d > 0: return f"adelay={d}|{d},aresample=async=1000:first_pts=0"
        if d < 0: return f"atrim=start={abs(d)/1000.0},asetpts=PTS-STARTPTS,aresample=async=1000:first_pts=0"
        return "aresample=async=1000:first_pts=0"

    def _get_seek_offset(self, filepath):
        def _get_pts(cmd, keyframe_only=False):
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, creationflags=NO_WIN)
                for line in proc.stdout:
                    parts = line.strip().split(",")
                    if not parts or not parts[0] or (keyframe_only and not any("K" in p for p in parts)): continue
                    for p in parts:
                        try: return float(p)
                        except ValueError: pass
            except Exception: pass
            finally:
                try: proc.kill(); proc.wait()
                except Exception: pass
            return None

        f_start = _get_pts(["ffprobe", "-v", "error", "-show_entries", "packet=pts_time", "-of", "csv=p=0", filepath])
        v_key = _get_pts(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", filepath], True)
        return max(0.0, v_key - f_start) if f_start is not None and v_key is not None else 0.0

    def start_continuous_capture(self):
        try:
            self._audio_sock, prev_port = self._make_tcp_server(rcvbuf=2 * 1024 * 1024)
            self._rec_sock, rec_port = self._make_tcp_server(rcvbuf=8 * 1024 * 1024)
        except Exception as e:
            return self.root.after(0, lambda: messagebox.showerror("Error", f"Socket allocation failed: {e}"))

        prev_aud_f = self._audio_filter_expr(PREV_AUD_DELAY)
        rec_aud_f = self._audio_filter_expr(REC_AUD_DELAY)

        cmd = [
            "ffmpeg", "-y", "-fflags", "nobuffer", "-thread_queue_size", "1024",
            "-f", "dshow", "-video_size", f"{CAP_W}x{CAP_H}", "-framerate", "29.97",
            "-pixel_format", "yuyv422", "-rtbufsize", "256M", "-i", f"video={REQ_VID}",
            "-thread_queue_size", "1024", "-f", "dshow", "-guess_layout_max", "0",
            "-ac", "2", "-rtbufsize", "256M", "-i", f"audio={REQ_AUD}",
            "-filter_complex", (
                f"[0:v]split=2[rec_v][prev_v];"
                f"[rec_v]setfield=tff[out_rec_v];"
                f"[prev_v]setfield=tff,bwdif=mode=0:parity=0:deint=0,scale={PREV_W}:{PREV_H}:flags=fast_bilinear,format=rgb24[out_prev_v];"
                f"[1:a]asplit=2[rec_a_in][prev_a_in];"
                f"[rec_a_in]{rec_aud_f}[out_rec_a];"
                f"[prev_a_in]{prev_aud_f}[out_prev_a]"
            ),
            "-map", "[out_prev_v]", "-an", "-c:v", "rawvideo", "-pix_fmt", "rgb24",
            "-fps_mode", "passthrough", "-flush_packets", "1", "-f", "rawvideo", "pipe:1",
            "-map", "[out_prev_a]", "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2",
            "-flush_packets", "1", "-f", "s16le", f"tcp://127.0.0.1:{prev_port}",
            "-map", "[out_rec_v]", "-map", "[out_rec_a]",
            "-c:v", "mpeg2video", "-b:v", "7500k", "-maxrate", "9000k", "-bufsize", "3670k",
            "-g", "12", "-bf", "0",
            "-qmin", "2", "-qmax", "12", "-intra_dc_precision", "2",
            "-flags:v", "+ilme+ildct",
            "-aspect", "4:3", "-pix_fmt", "yuv420p",
            "-c:a", "ac3", "-b:a", "448k", "-ar", "48000",
            "-mpegts_flags", "resend_headers", "-f", "mpegts", f"tcp://127.0.0.1:{rec_port}"
        ]
        try:
            self.process = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=FRAME_SZ * 2, creationflags=NO_WIN)
            for t, a in ((self._pipe_reader_worker, ()), (self._audio_receiver_worker, ()), (self._rec_receiver_worker, ()), (self._drain_stderr, (self.process.stderr,))):
                threading.Thread(target=t, args=a, daemon=True).start()
            time.sleep(0.5)
            if self.process.poll() is not None:
                return self.root.after(0, lambda: self.status_label.config(text="Status: Capture device error / failed to start", fg="#e74c3c"))
            self.root.after(0, lambda: [self.status_label.config(text="Status: Live Preview (Ready)", fg="#2ecc71"), self.start_btn.config(state=tk.NORMAL), self.timer_btn.config(state=tk.NORMAL)])
        except Exception as e:
            self.root.after(0, lambda: messagebox.showerror("Error", f"Failed to start capture: {e}"))

    def _drain_stderr(self, pipe):
        try:
            for line in iter(pipe.readline, b""):
                t = line.decode("utf-8", errors="ignore").strip()
                if any(k in t.lower() for k in ("error", "fail", "invalid", "cannot", "abort")): print(f"[FFmpeg] {t}", flush=True)
        except Exception: pass

    def _pipe_reader_worker(self):
        while not self._is_closing and self.process and self.process.poll() is None:
            try:
                frame = self.process.stdout.read(FRAME_SZ)
                if len(frame) < FRAME_SZ: break
                with self._frame_lock: self._frame_queue.append(frame)
            except Exception: break

    def _audio_receiver_worker(self):
        self.audio_player.start()
        for _ in range(2):
            while not self._is_closing and self.process and self.process.poll() is None and not self._audio_conn:
                try:
                    self._audio_conn, _ = self._audio_sock.accept()
                    self._audio_conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1); self._audio_conn.settimeout(0.2)
                except (socket.timeout, OSError): pass
        while not self._is_closing and self.process and self.process.poll() is None and self._audio_conn:
            try:
                data = self._audio_conn.recv(65536)
                if not data: break
                self.audio_player.write(data)
            except (socket.timeout, OSError):
                if self._is_closing: break
        self.audio_player.stop()

    def _rec_receiver_worker(self):
        while not self._is_closing and self.process and self.process.poll() is None and not self._rec_conn:
            try:
                self._rec_conn, _ = self._rec_sock.accept(); self._rec_conn.settimeout(0.2)
            except (socket.timeout, OSError): pass
        while not self._is_closing and self._rec_conn:
            try:
                data = self._rec_conn.recv(65536)
                if not data: break
                with self._rec_lock:
                    if self.is_recording and self._rec_file: self._rec_file.write(data)
            except (socket.timeout, OSError):
                if self._is_closing: break

    def open_timer_dialog(self):
        if self.process and not self.is_recording: TimerDialog(self.root, self.start_timer_recording)

    def start_timer_recording(self, duration_seconds):
        self.target_duration = duration_seconds
        # Buffer capture by +1.0s to ensure full duration after keyframe alignment
        self.timer_end_time = time.time() + duration_seconds + 1.0
        if self.start_recording():
            self._update_timer_countdown()
        else:
            self.timer_end_time = None
            self.target_duration = None

    def _update_timer_countdown(self):
        if not self.is_recording or self.timer_end_time is None: return
        now = time.time()
        rem = (self.timer_end_time - 1.0) - now
        if now >= self.timer_end_time:
            return self.stop_recording()
        display_rem = max(0, math.ceil(rem))
        h, r = divmod(display_rem, 3600); m, s = divmod(r, 60)
        self.status_label.config(text=f"Status: TIMER RECORDING ({self.current_output}) [Remaining: {h:02d}:{m:02d}:{s:02d}]", fg="#e74c3c")
        self._timer_after_id = self.root.after(200, self._update_timer_countdown)

    def start_recording(self):
        if not self.process or self.is_recording: return False
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.current_output, self._temp_output = f"capture_{ts}.mpg", f"temp_{ts}.ts"
        try:
            f = open(self._temp_output, "wb")
            with self._rec_lock: self._rec_file, self.is_recording = f, True
            self.start_btn.config(state=tk.DISABLED); self.timer_btn.config(state=tk.DISABLED); self.stop_btn.config(state=tk.NORMAL)
            if self.timer_end_time is None: self.status_label.config(text=f"Status: RECORDING ({self.current_output})...", fg="#e74c3c")
            return True
        except Exception as e:
            messagebox.showerror("Error", f"Could not create file: {e}")
            self.timer_end_time = None
            self.target_duration = None
            return False

    def stop_recording(self):
        if self._timer_after_id:
            try: self.root.after_cancel(self._timer_after_id)
            except Exception: pass
            self._timer_after_id = None
        target_dur = self.target_duration
        self.timer_end_time = None
        self.target_duration = None
        if not self.is_recording: return
        with self._rec_lock:
            self.is_recording = False
            f, self._rec_file = self._rec_file, None
        if f:
            try: f.close()
            except Exception: pass
        self.start_btn.config(state=tk.NORMAL); self.timer_btn.config(state=tk.NORMAL); self.stop_btn.config(state=tk.DISABLED)
        self.status_label.config(text="Status: Live Preview (Finalizing file...)", fg="#3498db")
        self.is_finalizing = True
        threading.Thread(target=self._finalize_file, args=(self._temp_output, self.current_output, target_dur), daemon=True).start()

    def _finalize_file(self, temp_out, final_out, target_duration=None):
        seek = self._get_seek_offset(temp_out)
        cmd = ["ffmpeg", "-y", "-fflags", "+discardcorrupt+genpts", "-ss", str(seek), "-i", temp_out]
        if target_duration is not None:
            cmd.extend(["-t", str(target_duration)])
        cmd.extend([
            "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy", "-c:a", "ac3", "-b:a", "448k", "-ar", "48000",
            "-avoid_negative_ts", "make_zero", "-f", "dvd", final_out
        ])
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=NO_WIN)
            if proc.returncode != 0: print(f"[Finalize Error] {proc.stderr}", flush=True)
            if os.path.exists(final_out) and os.path.getsize(final_out) > 10000:
                if os.path.exists(temp_out): os.remove(temp_out)
            elif os.path.exists(temp_out): os.replace(temp_out, final_out)
        except Exception as e:
            print(f"[Finalize] Error: {e}", flush=True)
            if os.path.exists(temp_out): os.replace(temp_out, final_out)
        
        self.is_finalizing = False
        if self._is_closing:
            self.root.after(0, self._finish_closing)
        else:
            self.root.after(0, lambda: not self.is_recording and not self._is_closing and self.status_label.config(text=f"Status: Live Preview (Saved '{final_out}')", fg="#2ecc71"))

    def on_closing(self):
        if self._is_closing: return
        self._is_closing = True
        if self._timer_after_id:
            try: self.root.after_cancel(self._timer_after_id)
            except Exception: pass

        if self.is_recording:
            self.status_label.config(text="Status: Finalizing and saving recording before exit...", fg="#e67e22")
            self.start_btn.config(state=tk.DISABLED); self.stop_btn.config(state=tk.DISABLED); self.timer_btn.config(state=tk.DISABLED)
            self.stop_recording()
            return

        if self.is_finalizing:
            self.status_label.config(text="Status: Finalizing and saving recording before exit...", fg="#e67e22")
            self.start_btn.config(state=tk.DISABLED); self.stop_btn.config(state=tk.DISABLED); self.timer_btn.config(state=tk.DISABLED)
            return

        self._finish_closing()

    def _finish_closing(self):
        try: self.root.withdraw()
        except Exception: pass

        def _cleanup():
            if self.process:
                try: self.process.kill(); self.process.wait(timeout=1.0)
                except Exception: pass
            for obj in (self._audio_conn, self._rec_conn):
                if obj:
                    try: obj.shutdown(socket.SHUT_RDWR); obj.close()
                    except Exception: pass
            for s in (self._audio_sock, self._rec_sock):
                if s:
                    try: s.close()
                    except Exception: pass
            self.audio_player.stop()
            self.root.after(0, self.root.destroy)

        threading.Thread(target=_cleanup, daemon=True).start()


if __name__ == "__main__":
    root = tk.Tk(); root.withdraw()
    ok, err = verify_devices()
    if not ok:
        messagebox.showerror("Device Not Found", err)
        root.destroy(); sys.exit(1)
    root.deiconify()
    DVDRecorderGUI(root)
    root.mainloop()
