import os, sys, time, datetime, threading, socket, subprocess, re
from collections import deque
import tkinter as tk
from tkinter import messagebox
from PIL import Image, ImageTk
import sounddevice as sd

REQ_VID, REQ_AUD = "GV-USB2, Analog Capture", "GV-USB2, Analog WaveIn"
CAP_W, CAP_H, PREV_W, PREV_H = 720, 480, 640, 480
FRAME_SZ = PREV_W * PREV_H * 3

# Audio delay in ms to synchronize with video
REC_AUD_DELAY = 300
PREV_AUD_DELAY = 300

NO_WIN = 0x08000000 if sys.platform == "win32" else 0


def verify_devices():
    """Queries DirectShow devices via FFmpeg before the GUI is allowed to open."""
    try:
        proc = subprocess.run(
            ["ffmpeg", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="ignore",
            creationflags=NO_WIN
        )
        output = proc.stderr
    except FileNotFoundError:
        return False, "FFmpeg was not found in your system PATH.\nPlease install FFmpeg and try again."
    except Exception as e:
        return False, f"Failed to query capture devices:\n{e}"

    video_devices, audio_devices = [], []
    current_section = None

    for line in output.splitlines():
        if "Alternative name" in line:
            continue

        new_style = re.search(r'"([^"]+)"\s*\(([^)]*)\)', line)
        if new_style:
            dev_name = new_style.group(1)
            dev_types = [t.strip().lower() for t in new_style.group(2).split(",")]
            if "video" in dev_types:
                video_devices.append(dev_name)
            if "audio" in dev_types:
                audio_devices.append(dev_name)
            continue

        if "DirectShow video devices" in line:
            current_section = "video"
            continue
        elif "DirectShow audio devices" in line:
            current_section = "audio"
            continue

        if current_section:
            match = re.search(r'"([^"]+)"', line)
            if match:
                dev_name = match.group(1)
                if current_section == "video":
                    video_devices.append(dev_name)
                elif current_section == "audio":
                    audio_devices.append(dev_name)

    missing = []
    if REQ_VID not in video_devices:
        missing.append(f"• Video Device: '{REQ_VID}'")
    if REQ_AUD not in audio_devices:
        missing.append(f"• Audio Device: '{REQ_AUD}'")

    if missing:
        msg = "The required capture devices were not found:\n\n" + "\n".join(missing)
        msg += "\n\nPlease connect your I-O Data GV-USB2 adapter and try again."
        return False, msg

    return True, ""


class LiveAudioPlayer:
    def __init__(self, sample_rate=48000, channels=2, blocksize=1024):
        self.sr, self.ch, self.blocksize = sample_rate, channels, blocksize
        self.bytes_per_sample = 2 * channels  # 4 bytes for 16-bit stereo
        self.stream = None
        self.running = False
        self._lock = threading.Lock()
        self.buffer = bytearray()

        # Buffer thresholds to balance low latency with smooth playback:
        self.prebuffer_bytes = int(self.sr * self.bytes_per_sample * 0.06)
        self.max_buffer_bytes = int(self.sr * self.bytes_per_sample * 0.28)
        self.target_buffer_bytes = int(self.sr * self.bytes_per_sample * 0.12)
        self.started_playing = False

    def _callback(self, outdata, frames, time_info, status):
        bytes_needed = frames * self.bytes_per_sample
        with self._lock:
            if not self.started_playing:
                if len(self.buffer) >= self.prebuffer_bytes:
                    self.started_playing = True
                else:
                    outdata[:] = b"\x00" * bytes_needed
                    return

            if len(self.buffer) >= bytes_needed:
                outdata[:] = self.buffer[:bytes_needed]
                del self.buffer[:bytes_needed]
            else:
                avail = len(self.buffer)
                if avail > 0:
                    outdata[:avail] = self.buffer
                    self.buffer.clear()
                    outdata[avail:] = b"\x00" * (bytes_needed - avail)
                else:
                    outdata[:] = b"\x00" * bytes_needed

    def start(self):
        with self._lock:
            if self.running:
                return
            try:
                self.buffer.clear()
                self.started_playing = False
                self.stream = sd.RawOutputStream(
                    samplerate=self.sr,
                    channels=self.ch,
                    dtype="int16",
                    blocksize=self.blocksize,
                    callback=self._callback,
                )
                self.stream.start()
                self.running = True
            except Exception as e:
                print(f"[Audio] Initialization error: {e}", flush=True)

    def write(self, data):
        if self.running and data:
            with self._lock:
                self.buffer.extend(data)
                if len(self.buffer) > self.max_buffer_bytes:
                    excess = len(self.buffer) - self.target_buffer_bytes
                    excess -= (excess % self.bytes_per_sample)
                    if excess > 0:
                        del self.buffer[:excess]

    def stop(self):
        with self._lock:
            if not self.running:
                return
            self.running = False
            stream = self.stream
            self.stream = None
        if stream:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        with self._lock:
            self.buffer.clear()
            self.started_playing = False


class DVDRecorderGUI:
    def __init__(self, root):
        self.root = root
        root.title("GV-USB2 Recorder")
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.process, self.is_recording, self._is_closing = None, False, False
        self._rec_file, self._rec_lock = None, threading.Lock()
        self.current_output, self._temp_output = None, None
        self.audio_player = LiveAudioPlayer()
        self._audio_sock, self._rec_sock = None, None
        self._audio_conn, self._rec_conn = None, None
        self._frame_lock, self._frame_queue = threading.Lock(), deque(maxlen=2)

        self.status_label = tk.Label(root, text="Status: Initializing capture engine...", font=("Arial", 11, "bold"), fg="#f39c12")
        self.status_label.pack(pady=(10, 6))
        self.canvas = tk.Canvas(root, width=PREV_W, height=PREV_H, bg="black", highlightthickness=0)
        self.canvas.pack(padx=14, pady=4)
        self.photo = ImageTk.PhotoImage(Image.new("RGB", (PREV_W, PREV_H), "black"))
        self.canvas.create_image(0, 0, anchor=tk.NW, image=self.photo)

        btn_frame = tk.Frame(root)
        btn_frame.pack(pady=(8, 12))
        self.start_btn = tk.Button(btn_frame, text="Start Recording", command=self.start_recording, bg="#2ecc71", fg="white", font=("Arial", 11, "bold"), padx=15, pady=6, state=tk.DISABLED)
        self.start_btn.grid(row=0, column=0, padx=15)
        self.stop_btn = tk.Button(btn_frame, text="Stop Recording", command=self.stop_recording, bg="#e74c3c", fg="white", font=("Arial", 11, "bold"), padx=15, pady=6, state=tk.DISABLED)
        self.stop_btn.grid(row=0, column=1, padx=15)

        self.root.after(15, self._render_tick)
        threading.Thread(target=self.start_continuous_capture, daemon=True).start()

    def _render_tick(self):
        if self._is_closing: return
        with self._frame_lock:
            frame = self._frame_queue.popleft() if self._frame_queue else None
        if frame and self.process:
            try: self.photo.paste(Image.frombytes("RGB", (PREV_W, PREV_H), frame))
            except Exception: pass
        if not self._is_closing: self.root.after(15, self._render_tick)

    def _make_tcp_server(self, rcvbuf=None):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if rcvbuf:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        s.settimeout(0.5)
        return s, s.getsockname()[1]

    def _get_seek_offset(self, filepath):
        """Calculates the relative seek offset between file start and the first VIDEO keyframe."""
        cmd_start = [
            "ffprobe", "-v", "error",
            "-show_entries", "packet=pts_time",
            "-of", "csv=p=0",
            filepath
        ]
        file_start_pts = None
        try:
            proc = subprocess.Popen(cmd_start, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=NO_WIN)
            for line in proc.stdout:
                line = line.strip()
                if line:
                    try:
                        file_start_pts = float(line.split(",")[0])
                        break
                    except ValueError:
                        pass
            try: proc.kill()
            except Exception: pass
        except Exception as e:
            print(f"[Start Probe Error] {e}", flush=True)

        cmd_key = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "packet=pts_time,flags",
            "-of", "csv=p=0",
            filepath
        ]
        first_video_key_pts = None
        try:
            proc = subprocess.Popen(cmd_key, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=NO_WIN)
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                parts = line.split(",")
                pts_val = None
                is_key = False
                for p in parts:
                    if "K" in p:
                        is_key = True
                    try:
                        pts_val = float(p)
                    except ValueError:
                        pass
                if is_key and pts_val is not None:
                    first_video_key_pts = pts_val
                    break
            try: proc.kill()
            except Exception: pass
        except Exception as e:
            print(f"[Key Probe Error] {e}", flush=True)

        if file_start_pts is not None and first_video_key_pts is not None:
            return max(0.0, first_video_key_pts - file_start_pts)
        return 0.0

    def start_continuous_capture(self):
        try:
            self._audio_sock, prev_audio_port = self._make_tcp_server(rcvbuf=2 * 1024 * 1024)
            self._rec_sock, rec_port = self._make_tcp_server(rcvbuf=8 * 1024 * 1024)
        except Exception as e:
            err = str(e)
            self.root.after(0, lambda: messagebox.showerror("Error", f"Socket allocation failed: {err}"))
            return

        cmd = [
            "ffmpeg", "-y", "-fflags", "nobuffer", "-thread_queue_size", "1024",
            "-f", "dshow", "-video_size", f"{CAP_W}x{CAP_H}", "-framerate", "29.97", "-pixel_format", "yuyv422", "-rtbufsize", "256M", "-i", f"video={REQ_VID}",
            "-thread_queue_size", "1024", "-f", "dshow", "-guess_layout_max", "0", "-ac", "2", "-rtbufsize", "256M", "-i", f"audio={REQ_AUD}",
            "-filter_complex", (
                f"[0:v]split=2[rec_v][prev_v];"
                f"[rec_v]setfield=tff[out_rec_v];"
                f"[prev_v]setfield=tff,bwdif=mode=0:parity=0:deint=0,scale={PREV_W}:{PREV_H}:flags=fast_bilinear,format=rgb24[out_prev_v];"
                f"[1:a]asplit=2[rec_a_in][prev_a_in];"
                f"[rec_a_in]adelay={REC_AUD_DELAY}|{REC_AUD_DELAY},aresample=async=1000:first_pts=0[out_rec_a];"
                f"[prev_a_in]adelay={PREV_AUD_DELAY}|{PREV_AUD_DELAY},aresample=async=1000:first_pts=0[out_prev_a]"
            ),
            "-map", "[out_prev_v]", "-an", "-c:v", "rawvideo", "-pix_fmt", "rgb24", "-fps_mode", "passthrough", "-flush_packets", "1", "-f", "rawvideo", "pipe:1",
            "-map", "[out_prev_a]", "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", "-flush_packets", "1", "-f", "s16le", f"tcp://127.0.0.1:{prev_audio_port}",
            "-map", "[out_rec_v]", "-map", "[out_rec_a]",
            "-c:v", "mpeg2video",
            "-b:v", "8500k", "-maxrate", "9000k", "-bufsize", "1835k",
            "-profile:v", "main", "-level:v", "main",
            "-g", "15", "-bf", "0",
            "-flags:v", "+ilme+ildct",
            "-trellis", "1",
            "-aspect", "4:3", "-pix_fmt", "yuv420p",
            "-c:a", "ac3", "-b:a", "448k", "-ar", "48000",
            "-mpegts_flags", "resend_headers", "-f", "mpegts", f"tcp://127.0.0.1:{rec_port}"
        ]

        try:
            self.process = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=FRAME_SZ * 2, creationflags=NO_WIN)
            for target, args in [(self._pipe_reader_worker, ()), (self._audio_receiver_worker, ()), (self._rec_receiver_worker, ()), (self._drain_stderr, (self.process.stderr,))]:
                threading.Thread(target=target, args=args, daemon=True).start()
            time.sleep(0.5)
            if self.process.poll() is not None:
                self.root.after(0, lambda: self.status_label.config(text="Status: Capture device error / failed to start", fg="#e74c3c"))
                return
            self.root.after(0, lambda: self.status_label.config(text="Status: Live Preview (Ready)", fg="#2ecc71"))
            self.root.after(0, lambda: self.start_btn.config(state=tk.NORMAL))
        except Exception as e:
            err = str(e)
            self.root.after(0, lambda: messagebox.showerror("Error", f"Failed to start capture: {err}"))

    def _drain_stderr(self, pipe):
        try:
            for line in iter(pipe.readline, b""):
                text = line.decode("utf-8", errors="ignore").strip()
                if any(k in text.lower() for k in ("error", "fail", "invalid", "cannot", "abort")):
                    print(f"[FFmpeg] {text}", flush=True)
        except Exception: pass

    def _pipe_reader_worker(self):
        while not self._is_closing and self.process:
            try:
                frame = self.process.stdout.read(FRAME_SZ)
                if len(frame) < FRAME_SZ: break
                with self._frame_lock: self._frame_queue.append(frame)
            except Exception: break

    def _audio_receiver_worker(self):
        self.audio_player.start()
        while not self._is_closing and self.process:
            try:
                self._audio_conn, _ = self._audio_sock.accept()
                self._audio_conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self._audio_conn.settimeout(0.2)
                break
            except socket.timeout:
                continue
            except OSError:
                break

        while not self._is_closing and self.process and self._audio_conn:
            try:
                data = self._audio_conn.recv(65536)
                if not data:
                    break
                self.audio_player.write(data)
            except (socket.timeout, OSError):
                if self._is_closing: break
        self.audio_player.stop()

    def _rec_receiver_worker(self):
        while not self._is_closing and self.process:
            try:
                self._rec_conn, _ = self._rec_sock.accept()
                self._rec_conn.settimeout(0.2)
                break
            except socket.timeout:
                continue
            except OSError:
                break

        while not self._is_closing and self._rec_conn:
            try:
                data = self._rec_conn.recv(65536)
                if not data:
                    break
                with self._rec_lock:
                    if self.is_recording and self._rec_file:
                        self._rec_file.write(data)
            except (socket.timeout, OSError):
                if self._is_closing: break

    def start_recording(self):
        if not self.process or self.is_recording: return
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.current_output, self._temp_output = f"capture_{ts}.mpg", f"temp_{ts}.ts"
        try:
            f = open(self._temp_output, "wb")
            with self._rec_lock: self._rec_file, self.is_recording = f, True
            self.start_btn.config(state=tk.DISABLED)
            self.stop_btn.config(state=tk.NORMAL)
            self.status_label.config(text=f"Status: RECORDING ({self.current_output})...", fg="#e74c3c")
        except Exception as e: messagebox.showerror("Error", f"Could not create file: {e}")

    def stop_recording(self):
        if not self.is_recording: return
        with self._rec_lock:
            self.is_recording = False
            f, self._rec_file = self._rec_file, None
        if f:
            try: f.close()
            except Exception: pass
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        self.status_label.config(text="Status: Live Preview (Finalizing file...)", fg="#3498db")
        threading.Thread(target=self._finalize_file, args=(self._temp_output, self.current_output), daemon=True).start()

    def _finalize_file(self, temp_out, final_out):
        seek_offset = self._get_seek_offset(temp_out)
        
        cmd = [
            "ffmpeg", "-y",
            "-fflags", "+discardcorrupt+genpts",
            "-ss", str(seek_offset),
            "-i", temp_out,
            "-map", "0:v:0", "-map", "0:a:0",
            "-c:v", "copy",
            "-c:a", "ac3", "-b:a", "448k", "-ar", "48000",
            "-avoid_negative_ts", "make_zero",
            "-f", "dvd", final_out
        ]
        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=NO_WIN
            )
            if proc.returncode != 0:
                print(f"[Finalize Error] FFmpeg failed with exit code {proc.returncode}:\n{proc.stderr}", flush=True)

            if os.path.exists(final_out) and os.path.getsize(final_out) > 10000:
                if os.path.exists(temp_out): os.remove(temp_out)
            elif os.path.exists(temp_out):
                os.replace(temp_out, final_out)
        except Exception as e:
            print(f"[Finalize] Error: {e}", flush=True)
            if os.path.exists(temp_out): os.replace(temp_out, final_out)
        if not self.is_recording:
            self.root.after(0, lambda: self.status_label.config(text=f"Status: Live Preview (Saved '{final_out}')", fg="#2ecc71"))

    def on_closing(self):
        self._is_closing = True
        with self._rec_lock:
            if self._rec_file:
                try: self._rec_file.close()
                except Exception: pass
        if self.process:
            try: self.process.kill()
            except Exception: pass
        for conn in (self._audio_conn, self._rec_conn):
            if conn:
                try: conn.close()
                except Exception: pass
        for s in (self._audio_sock, self._rec_sock):
            if s:
                try: s.close()
                except Exception: pass
        self.audio_player.stop()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    root.withdraw()

    devices_ok, err_msg = verify_devices()
    if not devices_ok:
        messagebox.showerror("Device Not Found", err_msg)
        root.destroy()
        sys.exit(1)

    root.deiconify()
    app = DVDRecorderGUI(root)
    root.mainloop()
