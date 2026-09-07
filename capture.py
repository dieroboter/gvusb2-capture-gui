import tkinter as tk
from tkinter import messagebox
import subprocess
import os
import time
from PIL import Image, ImageTk
import threading
import sys
import datetime
import re
from collections import deque
import socket
import ctypes
from ctypes import wintypes

REQUIRED_VIDEO_DEVICE = "GV-USB2, Analog Capture"
REQUIRED_AUDIO_DEVICE = "GV-USB2, Analog WaveIn"

# Source capture resolution (720x480 NTSC)
CAPTURE_WIDTH = 720
CAPTURE_HEIGHT = 480

# 4:3 Corrected Display Resolution
PREVIEW_WIDTH = 640
PREVIEW_HEIGHT = 480
PREVIEW_FRAME_SIZE = PREVIEW_WIDTH * PREVIEW_HEIGHT * 3  # RGB24 (921,600 bytes)


# ==========================================
# Native Windows Live Audio Playback Engine
# ==========================================
WAVE_FORMAT_PCM = 1
WAVE_MAPPER = -1
WHDR_DONE = 0x00000001
WHDR_PREPARED = 0x00000002


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", wintypes.WORD),
        ("nChannels", wintypes.WORD),
        ("nSamplesPerSec", wintypes.DWORD),
        ("nAvgBytesPerSec", wintypes.DWORD),
        ("nBlockAlign", wintypes.WORD),
        ("wBitsPerSample", wintypes.WORD),
        ("cbSize", wintypes.WORD),
    ]


class WAVEHDR(ctypes.Structure):
    pass


WAVEHDR._fields_ = [
    ("lpData", ctypes.c_void_p),
    ("dwBufferLength", wintypes.DWORD),
    ("dwBytesRecorded", wintypes.DWORD),
    ("dwUser", ctypes.c_size_t),
    ("dwFlags", wintypes.DWORD),
    ("dwLoops", wintypes.DWORD),
    ("lpNext", ctypes.POINTER(WAVEHDR)),
    ("reserved", ctypes.c_size_t),
]


class WindowsLiveAudioPlayer:
    """Zero-dependency, low-latency live PCM audio player using Win32 waveOut API."""
    def __init__(self, sample_rate=48000, channels=2, num_buffers=8, buffer_size=4096):
        self.sample_rate = sample_rate
        self.channels = channels
        self.num_buffers = num_buffers
        self.buffer_size = buffer_size
        self.hWaveOut = wintypes.HANDLE(0)
        self.running = False
        self.buffers = []
        self.raw_buffers = []
        self.cur_buf_idx = 0
        self._lock = threading.Lock()
        self.winmm = ctypes.windll.winmm if sys.platform == "win32" else None

    def start(self):
        if not self.winmm:
            return
        with self._lock:
            wfx = WAVEFORMATEX()
            wfx.wFormatTag = WAVE_FORMAT_PCM
            wfx.nChannels = self.channels
            wfx.nSamplesPerSec = self.sample_rate
            wfx.wBitsPerSample = 16
            wfx.nBlockAlign = self.channels * 2
            wfx.nAvgBytesPerSec = self.sample_rate * wfx.nBlockAlign
            wfx.cbSize = 0

            res = self.winmm.waveOutOpen(ctypes.byref(self.hWaveOut), WAVE_MAPPER, ctypes.byref(wfx), 0, 0, 0)
            if res != 0:
                self.hWaveOut = wintypes.HANDLE(0)
                return

            self.buffers = []
            self.raw_buffers = []
            for _ in range(self.num_buffers):
                raw_buf = ctypes.create_string_buffer(self.buffer_size)
                hdr = WAVEHDR()
                hdr.lpData = ctypes.cast(raw_buf, ctypes.c_void_p)
                hdr.dwBufferLength = self.buffer_size
                hdr.dwFlags = WHDR_DONE
                self.buffers.append(hdr)
                self.raw_buffers.append(raw_buf)

            self.cur_buf_idx = 0
            self.running = True

    def write_chunk(self, data):
        with self._lock:
            if not self.running or not self.hWaveOut.value:
                return

            hdr = self.buffers[self.cur_buf_idx]
            raw_buf = self.raw_buffers[self.cur_buf_idx]

            for _ in range(15):
                if hdr.dwFlags & WHDR_DONE:
                    break
                time.sleep(0.002)

            if (hdr.dwFlags & WHDR_DONE) and (hdr.dwFlags & WHDR_PREPARED):
                self.winmm.waveOutUnprepareHeader(self.hWaveOut, ctypes.byref(hdr), ctypes.sizeof(WAVEHDR))

            chunk_len = min(len(data), self.buffer_size)
            ctypes.memmove(raw_buf, data, chunk_len)
            hdr.dwBufferLength = chunk_len
            hdr.dwFlags = 0

            self.winmm.waveOutPrepareHeader(self.hWaveOut, ctypes.byref(hdr), ctypes.sizeof(WAVEHDR))
            self.winmm.waveOutWrite(self.hWaveOut, ctypes.byref(hdr), ctypes.sizeof(WAVEHDR))

            self.cur_buf_idx = (self.cur_buf_idx + 1) % self.num_buffers

    def stop(self):
        with self._lock:
            self.running = False
            if self.winmm and self.hWaveOut.value:
                self.winmm.waveOutReset(self.hWaveOut)
                for hdr in self.buffers:
                    if hdr.dwFlags & WHDR_PREPARED:
                        self.winmm.waveOutUnprepareHeader(self.hWaveOut, ctypes.byref(hdr), ctypes.sizeof(WAVEHDR))
                self.winmm.waveOutClose(self.hWaveOut)
                self.hWaveOut = wintypes.HANDLE(0)
            self.buffers.clear()
            self.raw_buffers.clear()


def safe_remove_file(filepath):
    """Robustly deletes a file on Windows/Unix."""
    if not filepath or not os.path.exists(filepath):
        return

    for _ in range(5):
        try:
            os.remove(filepath)
            return
        except (PermissionError, OSError):
            time.sleep(0.1)

    if sys.platform == "win32" and os.path.exists(filepath):
        try:
            subprocess.run(
                f'del /f /q "{os.path.abspath(filepath)}"',
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=0x08000000
            )
        except Exception:
            pass


def get_media_duration(filepath):
    """Accurately extracts media duration for MPEG-PS / VOB formats without Unicode crashes."""
    kw = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "ignore"
    }
    if sys.platform == "win32":
        kw["creationflags"] = 0x08000000

    try:
        res = subprocess.run([
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            filepath
        ], **kw)
        dur_str = res.stdout.strip()
        if dur_str and dur_str != "N/A":
            dur = float(dur_str)
            if dur > 0:
                return dur
    except Exception:
        pass

    try:
        proc = subprocess.run(["ffmpeg", "-i", filepath, "-f", "null", "-"], **kw)
        matches = re.findall(r'time=(\d+):(\d+):(\d+(?:\.\d+)?)', proc.stderr)
        if matches:
            h, m, s = matches[-1]
            return int(h) * 3600 + int(m) * 60 + float(s)
        matches_sec = re.findall(r'time=(\d+\.\d+)(?!\:)', proc.stderr)
        if matches_sec:
            return float(matches_sec[-1])
    except Exception:
        pass

    return None


class GVUSB2CaptureGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("I-O Data GV-USB2 Recorder")
        self.root.geometry("680x620")
        self.root.resizable(False, False)

        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        self.process = None
        self.running = False
        self.preview_thread = None
        self.audio_thread = None
        self.image_item = None
        self.current_output = None
        self._temp_output = None
        self._stopping = False
        self._is_closing = False
        self._had_error = False
        self._current_photo = None

        # Audio player & socket
        self.audio_player = WindowsLiveAudioPlayer(sample_rate=48000, channels=2)
        self._audio_sock = None

        # Lock-free video frame queue & event dispatch
        self._frame_lock = threading.Lock()
        self._frame_queue = deque(maxlen=2)
        self._render_scheduled = False

        self.status_label = tk.Label(
            root, text="Status: Detecting capture devices...", 
            font=("Arial", 12, "bold"), fg="#f39c12"
        )
        self.status_label.pack(pady=10)

        self.canvas = tk.Canvas(root, width=PREVIEW_WIDTH, height=PREVIEW_HEIGHT, bg="black")
        self.canvas.pack(pady=5)

        btn_frame = tk.Frame(root)
        btn_frame.pack(pady=15)

        self.start_btn = tk.Button(
            btn_frame, text="Start Recording & Preview",
            command=self.start_capture, bg="#2ecc71", fg="white",
            font=("Arial", 11, "bold"), padx=15, pady=8, state=tk.DISABLED
        )
        self.start_btn.grid(row=0, column=0, padx=15)

        self.stop_btn = tk.Button(
            btn_frame, text="Stop Recording",
            command=self.stop_capture, bg="#e74c3c", fg="white",
            font=("Arial", 11, "bold"), state=tk.DISABLED, padx=15, pady=8
        )
        self.stop_btn.grid(row=0, column=1, padx=15)

        threading.Thread(target=self.async_verify_devices, daemon=True).start()

    def async_verify_devices(self):
        """Asynchronously queries DirectShow devices without freezing the UI."""
        kwargs = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "ignore"
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000

        try:
            proc = subprocess.run(
                ["ffmpeg", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                **kwargs
            )
            output = proc.stderr
        except FileNotFoundError:
            self.root.after(0, self.on_ffmpeg_not_found)
            return
        except Exception as e:
            self.root.after(0, self.on_device_check_failed, str(e))
            return

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
        if REQUIRED_VIDEO_DEVICE not in video_devices:
            missing.append(f"Video device not found: '{REQUIRED_VIDEO_DEVICE}'")
        if REQUIRED_AUDIO_DEVICE not in audio_devices:
            missing.append(f"Audio device not found: '{REQUIRED_AUDIO_DEVICE}'")

        if missing:
            self.root.after(0, self.on_required_devices_missing, missing, video_devices, audio_devices)
        else:
            self.root.after(0, self.on_devices_ready)

    def on_ffmpeg_not_found(self):
        if not self.root.winfo_exists():
            return
        self.status_label.config(text="Status: FFmpeg not found in PATH", fg="#e74c3c")
        messagebox.showerror("Error", "FFmpeg is not installed or not found in system PATH.")

    def on_device_check_failed(self, err_msg):
        if not self.root.winfo_exists():
            return
        self.status_label.config(text="Status: Error detecting devices", fg="#e74c3c")
        messagebox.showerror("Error", f"Error querying DirectShow devices:\n{err_msg}")

    def on_required_devices_missing(self, missing, video_devices, audio_devices):
        if not self.root.winfo_exists():
            return
        self.status_label.config(text="Status: Required capture device not connected", fg="#e74c3c")
        err_msg = "\n".join(missing) + "\n\nPlease connect your I-O Data GV-USB2 adapter and restart."
        messagebox.showwarning("Device Not Found", err_msg)

    def on_devices_ready(self):
        if not self.root.winfo_exists():
            return
        self.status_label.config(text="Status: Ready", fg="black")
        self.start_btn.config(state=tk.NORMAL)

    def start_capture(self):
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.current_output = f"capture_{timestamp}.mpg"
        self._temp_output = f"temp_capture_{timestamp}.mpg"
        self._had_error = False

        video_device = f"video={REQUIRED_VIDEO_DEVICE}"
        audio_device = f"audio={REQUIRED_AUDIO_DEVICE}"

        # Initialize persistent single PhotoImage on canvas
        self.canvas.delete("all")
        blank = Image.new("RGB", (PREVIEW_WIDTH, PREVIEW_HEIGHT), "black")
        self._current_photo = ImageTk.PhotoImage(image=blank)
        self.image_item = self.canvas.create_image(0, 0, anchor=tk.NW, image=self._current_photo)

        self._render_scheduled = False
        with self._frame_lock:
            self._frame_queue.clear()

        # Pre-bind UDP socket before launching FFmpeg to prevent race condition
        try:
            self._audio_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._audio_sock.bind(('127.0.0.1', 0))
            self._audio_sock.settimeout(0.2)
            audio_port = self._audio_sock.getsockname()[1]
        except Exception as e:
            messagebox.showerror("Error", f"Failed to initialize audio preview socket:\n{e}")
            return

        self.status_label.config(text="Status: Initializing preview & recording...", fg="#f39c12")

        cmd = [
            "ffmpeg", "-y",
            # DirectShow Video Input
            "-thread_queue_size", "1024",
            "-f", "dshow",
            "-video_size", f"{CAPTURE_WIDTH}x{CAPTURE_HEIGHT}",
            "-framerate", "29.97",
            "-pixel_format", "yuyv422",
            "-rtbufsize", "256M",
            "-i", video_device,
            # DirectShow Audio Input
            "-thread_queue_size", "1024",
            "-f", "dshow",
            "-guess_layout_max", "0",
            "-ac", "2",
            "-rtbufsize", "256M",
            "-i", audio_device,
            # Multi-threaded filter graph with video & audio split
            "-filter_threads", "0",
            "-filter_complex_threads", "0",
            "-filter_complex",
            "[0:v]split=2[rec_v][prev_v];"
            r"[rec_v]select=gte(n\,30),setpts=PTS-STARTPTS,setfield=tff[out_rec_v];"
            f"[prev_v]field=top,scale={PREVIEW_WIDTH}:{PREVIEW_HEIGHT}:flags=fast_bilinear,format=rgb24[out_prev_v];"
            "[1:a]asplit=2[rec_a_in][prev_a_in];"
            "[rec_a_in]atrim=start=0.5005,asetpts=PTS-STARTPTS,aresample=async=1,afade=t=in:st=0:d=0.040[out_rec_a];"
            "[prev_a_in]aresample=async=1[out_prev_a]",
            # 1. Main Recording output (Multi-threaded MPEG-2 encoder)
            "-map", "[out_rec_v]", "-map", "[out_rec_a]",
            "-c:v", "mpeg2video", "-b:v", "15000k", "-minrate", "8000k",
            "-maxrate", "15000k", "-bufsize", "1835k*8", "-profile:v", "main",
            "-level:v", "main", "-g", "15", "-flags", "+ilme+ildct",
            "-aspect", "4:3", "-pix_fmt", "yuv420p", "-fps_mode", "cfr",
            "-threads", "0",
            "-ar", "48000", "-c:a", "ac3", "-b:a", "384k",
            "-f", "vob", self._temp_output,
            # 2. Video Preview output (Zero-conversion raw pipe)
            "-map", "[out_prev_v]", "-an",
            "-c:v", "rawvideo", "-pix_fmt", "rgb24",
            "-f", "rawvideo", "pipe:1",
            # 3. Audio Preview output (Low-latency UDP stream to localhost)
            "-map", "[out_prev_a]",
            "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2",
            "-f", "s16le", f"udp://127.0.0.1:{audio_port}?pkt_size=4096"
        ]

        kwargs = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.DEVNULL,
            "bufsize": PREVIEW_FRAME_SIZE * 4
        }

        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000

        try:
            self.process = subprocess.Popen(cmd, **kwargs)
            self.running = True
            self._stopping = False
            self._is_closing = False

            self.start_btn.config(state=tk.DISABLED)
            self.stop_btn.config(state=tk.NORMAL)

            # Start video stream consumer thread
            self.preview_thread = threading.Thread(target=self._pipe_reader_worker, daemon=True)
            self.preview_thread.start()

            # Start audio receiver & playback thread
            self.audio_thread = threading.Thread(
                target=self._audio_receiver_worker, 
                args=(self._audio_sock,), 
                daemon=True
            )
            self.audio_thread.start()

        except Exception as e:
            if self._audio_sock:
                try:
                    self._audio_sock.close()
                except Exception:
                    pass
                self._audio_sock = None
            messagebox.showerror("Error", f"Failed to start FFmpeg:\n{e}")
            self.stop_capture()

    def _audio_receiver_worker(self, sock):
        """Receives live PCM chunks over UDP and feeds the native Windows soundcard."""
        self.audio_player.start()

        while self.running:
            try:
                data, _ = sock.recvfrom(8192)
                if data:
                    self.audio_player.write_chunk(data)
            except socket.timeout:
                continue
            except Exception:
                break

        try:
            sock.close()
        except Exception:
            pass
        self.audio_player.stop()

    def _pipe_reader_worker(self):
        """Continuously pulls video frames at full 29.97 FPS."""
        pipe = self.process.stdout if self.process else None
        buf = bytearray(PREVIEW_FRAME_SIZE)
        view = memoryview(buf)

        while self.running:
            if not pipe:
                break

            received = 0
            while received < PREVIEW_FRAME_SIZE and self.running:
                try:
                    n = pipe.readinto(view[received:])
                    if not n:
                        break
                    received += n
                except Exception:
                    break

            if received < PREVIEW_FRAME_SIZE:
                break

            with self._frame_lock:
                self._frame_queue.append(bytes(buf))
                if not self._render_scheduled:
                    self._render_scheduled = True
                    self.root.after(0, self._render_frame)

        if not self._stopping and self.process is not None:
            self._had_error = True
            self.root.after(0, self.handle_unexpected_exit)

    def _render_frame(self):
        """In-place buffer update on persistent PhotoImage (no GDI recreation)."""
        if not self.running or not self.canvas.winfo_exists():
            return

        frame_data = None
        with self._frame_lock:
            self._render_scheduled = False
            if self._frame_queue:
                frame_data = self._frame_queue.pop()
                self._frame_queue.clear()

        if frame_data and self._current_photo is not None:
            try:
                img = Image.frombuffer("RGB", (PREVIEW_WIDTH, PREVIEW_HEIGHT), frame_data, "raw", "RGB", 0, 1)
                self._current_photo.paste(img)

                if "RECORDING" not in self.status_label.cget("text"):
                    self.status_label.config(
                        text=f"Status: RECORDING ({self.current_output})...", 
                        fg="#2ecc71"
                    )
            except Exception:
                pass

    def handle_unexpected_exit(self):
        messagebox.showwarning("Stream Interrupted", "FFmpeg process ended unexpectedly or device was disconnected.")
        self.stop_capture()

    def stop_capture(self, is_closing=False):
        if (not self.running and self.process is None) or self._stopping:
            return

        self._stopping = True
        self.running = False
        self._is_closing = is_closing
        self.status_label.config(text="Status: Stopping & finalizing recording...", fg="#f39c12")
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.DISABLED)

        def async_teardown():
            proc = self.process
            if proc:
                if proc.stdin:
                    try:
                        proc.stdin.write(b'q\n')
                        proc.stdin.flush()
                    except (BrokenPipeError, OSError):
                        pass

                try:
                    proc.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                except Exception:
                    pass

                for p in [proc.stdout, proc.stdin]:
                    if p:
                        try:
                            p.close()
                        except Exception:
                            pass

                self.process = None

            if self.preview_thread and self.preview_thread.is_alive():
                self.preview_thread.join(timeout=2)

            if self.audio_thread and self.audio_thread.is_alive():
                self.audio_thread.join(timeout=2)

            # Post-processing: Remove trailing driver buzz & fade to silence
            if self._temp_output and os.path.exists(self._temp_output):
                if os.path.getsize(self._temp_output) > 0:
                    try:
                        kw = {
                            "stdout": subprocess.PIPE,
                            "stderr": subprocess.PIPE,
                            "text": True,
                            "encoding": "utf-8",
                            "errors": "ignore"
                        }
                        if sys.platform == "win32":
                            kw["creationflags"] = 0x08000000

                        total_dur = get_media_duration(self._temp_output)

                        if total_dur and total_dur > 0.5:
                            if total_dur > 2.0:
                                TRIM_TAIL = 0.750
                                FADE_DUR = 0.250
                            else:
                                TRIM_TAIL = total_dur * 0.35
                                FADE_DUR = total_dur * 0.15

                            target_dur = max(0.1, total_dur - TRIM_TAIL)
                            fade_start = max(0.0, target_dur - FADE_DUR)

                            trim_cmd = [
                                "ffmpeg", "-y", "-i", self._temp_output,
                                "-t", f"{target_dur:.4f}",
                                "-c:v", "copy",
                                "-af", f"afade=t=out:st={fade_start:.4f}:d={FADE_DUR:.4f}",
                                "-c:a", "ac3", "-b:a", "384k", "-ar", "48000",
                                "-f", "vob", self.current_output
                            ]
                            trim_proc = subprocess.run(trim_cmd, **kw)

                            if trim_proc.returncode == 0 and os.path.exists(self.current_output) and os.path.getsize(self.current_output) > 0:
                                safe_remove_file(self._temp_output)
                            else:
                                if os.path.exists(self.current_output):
                                    safe_remove_file(self.current_output)
                                os.rename(self._temp_output, self.current_output)
                        else:
                            os.rename(self._temp_output, self.current_output)

                    except Exception:
                        if os.path.exists(self._temp_output) and not os.path.exists(self.current_output):
                            os.rename(self._temp_output, self.current_output)
                else:
                    safe_remove_file(self._temp_output)

            self.root.after(0, self.safe_finalize_stop)

        threading.Thread(target=async_teardown, daemon=True).start()

    def safe_finalize_stop(self):
        try:
            if self.root.winfo_exists():
                self.finalize_stop()
        except tk.TclError:
            pass

    def finalize_stop(self):
        self.canvas.delete("all")
        self.image_item = None
        self._current_photo = None
        self._render_scheduled = False
        with self._frame_lock:
            self._frame_queue.clear()

        self.start_btn.config(state=tk.NORMAL)
        self._stopping = False

        if self._had_error:
            self.status_label.config(text="Status: Stopped with Error / Interrupted", fg="#e74c3c")
        else:
            self.status_label.config(text="Status: Finished / Ready", fg="black")
            if not self._is_closing:
                filename = self.current_output if self.current_output else "output.mpg"
                messagebox.showinfo("Success", f"Recording finished completely!\nYour file '{filename}' is ready.")

    def on_closing(self):
        if self.running or self._stopping or self.process is not None:
            if self._stopping:
                self.wait_for_shutdown()
                return
            if messagebox.askokcancel("Quit", "A recording is in progress.\nDo you want to stop recording and exit?"):
                self.stop_capture(is_closing=True)
                self.wait_for_shutdown()
        else:
            self.audio_player.stop()
            self.root.destroy()

    def wait_for_shutdown(self):
        if self.process is not None or self._stopping:
            self.root.after(100, self.wait_for_shutdown)
        else:
            self.audio_player.stop()
            self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = GVUSB2CaptureGUI(root)
    root.mainloop()
