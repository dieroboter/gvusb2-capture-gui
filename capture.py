import tkinter as tk
from tkinter import messagebox
import subprocess
import cv2
import numpy as np
from PIL import Image, ImageTk
import threading
import sys
import datetime
import re

REQUIRED_VIDEO_DEVICE = "GV-USB2, Analog Capture"
REQUIRED_AUDIO_DEVICE = "GV-USB2, Analog WaveIn"


def verify_dshow_devices():
    """Checks for FFmpeg and verifies that the specified DirectShow devices exist."""
    kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "ignore"
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW

    try:
        proc = subprocess.run(
            ["ffmpeg", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            **kwargs
        )
        output = proc.stderr
    except FileNotFoundError:
        if sys.stderr:
            sys.stderr.write("Error: FFmpeg is not installed or not found in system PATH.\n")
        sys.exit(1)
    except Exception as e:
        if sys.stderr:
            sys.stderr.write(f"Error querying DirectShow devices: {e}\n")
        sys.exit(1)

    video_devices = []
    audio_devices = []
    current_section = None

    for line in output.splitlines():
        if "DirectShow video devices" in line:
            current_section = "video"
            continue
        elif "DirectShow audio devices" in line:
            current_section = "audio"
            continue

        if current_section and "Alternative name" not in line:
            match = re.search(r'"([^"]+)"', line)
            if match:
                dev_name = match.group(1)
                if current_section == "video":
                    video_devices.append(dev_name)
                elif current_section == "audio":
                    audio_devices.append(dev_name)

    missing = []
    if REQUIRED_VIDEO_DEVICE not in video_devices:
        missing.append(f"Required video device not found: '{REQUIRED_VIDEO_DEVICE}'")
    if REQUIRED_AUDIO_DEVICE not in audio_devices:
        missing.append(f"Required audio device not found: '{REQUIRED_AUDIO_DEVICE}'")

    if missing:
        lines = ["=== DirectShow Device Verification Error ==="]
        for err in missing:
            lines.append(f" [!] {err}")

        lines.append("\nDetected Video Devices:")
        if video_devices:
            for v in video_devices:
                lines.append(f"  - {v}")
        else:
            lines.append("  (No video devices detected)")

        lines.append("\nDetected Audio Devices:")
        if audio_devices:
            for a in audio_devices:
                lines.append(f"  - {a}")
        else:
            lines.append("  (No audio devices detected)")

        lines.append("=============================================")
        full_err_msg = "\n".join(lines)

        if sys.stderr:
            sys.stderr.write(full_err_msg + "\n")
            sys.stderr.flush()

        sys.exit(1)


class GVUSB2CaptureGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("I-O Data GV-USB2 Recorder")
        self.root.geometry("760x620")
        self.root.resizable(False, False)

        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        self.process = None
        self.running = False
        self.preview_thread = None
        self.image_item = None
        self.current_output = None
        self._stopping = False
        self._is_closing = False
        self._is_rendering = False
        self._had_error = False
        self._current_photo = None

        self.status_label = tk.Label(root, text="Status: Ready", font=("Arial", 12, "bold"))
        self.status_label.pack(pady=10)

        self.canvas = tk.Canvas(root, width=720, height=480, bg="black")
        self.canvas.pack(pady=5)

        btn_frame = tk.Frame(root)
        btn_frame.pack(pady=15)

        self.start_btn = tk.Button(
            btn_frame, text="Start Recording & Preview",
            command=self.start_capture, bg="#2ecc71", fg="white",
            font=("Arial", 11, "bold"), padx=15, pady=8
        )
        self.start_btn.grid(row=0, column=0, padx=15)

        self.stop_btn = tk.Button(
            btn_frame, text="Stop Recording",
            command=self.stop_capture, bg="#e74c3c", fg="white",
            font=("Arial", 11, "bold"), state=tk.DISABLED, padx=15, pady=8
        )
        self.stop_btn.grid(row=0, column=1, padx=15)

    def start_capture(self):
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.current_output = f"capture_{timestamp}.mpg"
        self._had_error = False

        video_device = f"video={REQUIRED_VIDEO_DEVICE}"
        audio_device = f"audio={REQUIRED_AUDIO_DEVICE}"

        cmd = [
            "ffmpeg", "-y",
            "-f", "dshow", "-video_size", "720x480", "-framerate", "29.97",
            "-pixel_format", "yuyv422", "-rtbufsize", "256M", "-i", video_device,
            "-f", "dshow", "-guess_layout_max", "0", "-ac", "2",
            "-rtbufsize", "256M", "-i", audio_device,
            # Recording output
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "mpeg2video", "-b:v", "15000k", "-minrate", "15000k",
            "-maxrate", "15000k", "-bufsize", "3000k", "-profile:v", "main",
            "-level:v", "main", "-g", "15", "-flags", "+ilme+ildct",
            "-aspect", "4:3", "-pix_fmt", "yuv420p",
            "-vf", r"select=gte(n\,2),setfield=tff", "-fps_mode", "cfr",
            "-af", r"adelay=200|200,aselect=gte(n\,2),aresample=async=1",
            "-ar", "48000", "-c:a", "mp2", "-b:a", "320k",
            "-f", "vob", self.current_output,
            # Preview output (MJPEG pipe, video only)
            "-map", "0:v:0", "-an", "-c:v", "mjpeg", "-q:v", "2",
            "-f", "image2pipe", "pipe:1"
        ]

        kwargs = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": sys.stderr if sys.stderr is not None else subprocess.DEVNULL,
            "bufsize": 10**7
        }

        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000

        try:
            self.process = subprocess.Popen(cmd, **kwargs)
            self.running = True
            self._stopping = False
            self._is_closing = False
            self.status_label.config(text=f"Status: RECORDING ({self.current_output})...", fg="#2ecc71")
            self.start_btn.config(state=tk.DISABLED)
            self.stop_btn.config(state=tk.NORMAL)

            self.preview_thread = threading.Thread(target=self.read_pipe_stream, daemon=True)
            self.preview_thread.start()

        except Exception as e:
            messagebox.showerror("Error", f"Failed to start FFmpeg:\n{e}")
            self.stop_capture()

    def read_pipe_stream(self):
        buffer = bytearray()
        MAX_BUFFER_SIZE = 20 * 1024 * 1024

        while self.running:
            proc = self.process
            if proc is None:
                break

            try:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break

                buffer.extend(chunk)

                if len(buffer) > MAX_BUFFER_SIZE:
                    last_start = buffer.rfind(b'\xff\xd8')
                    if last_start != -1:
                        buffer = buffer[last_start:]
                    else:
                        buffer.clear()
                    continue

                while True:
                    start = buffer.find(b'\xff\xd8')
                    if start == -1:
                        buffer.clear()
                        break

                    if start > 0:
                        del buffer[:start]
                        start = 0

                    end = buffer.find(b'\xff\xd9', start + 2)
                    if end == -1:
                        break

                    jpg_data = buffer[start:end + 2]
                    del buffer[:end + 2]

                    np_arr = np.frombuffer(jpg_data, dtype=np.uint8)
                    frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)

                    if frame is not None:
                        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                        img = Image.fromarray(rgb)

                        if not self._is_rendering:
                            self._is_rendering = True
                            self.root.after(0, self.update_canvas, img)

            except (BrokenPipeError, OSError):
                break
            except Exception:
                break

        if not self._stopping and self.process is not None:
            self._had_error = True
            self.root.after(0, self.handle_unexpected_exit)

    def handle_unexpected_exit(self):
        """Recovers GUI state when FFmpeg terminates unexpectedly."""
        messagebox.showwarning("Stream Interrupted", "FFmpeg process ended unexpectedly or device was disconnected.")
        self.stop_capture()

    def update_canvas(self, img):
        try:
            if self.running and self.canvas.winfo_exists():
                photo = ImageTk.PhotoImage(image=img)
                self._current_photo = photo

                if self.image_item is None:
                    self.image_item = self.canvas.create_image(0, 0, anchor=tk.NW, image=photo)
                else:
                    self.canvas.itemconfig(self.image_item, image=photo)
        except tk.TclError:
            pass
        finally:
            self._is_rendering = False

    def stop_capture(self, is_closing=False):
        if (not self.running and self.process is None) or self._stopping:
            return

        self._stopping = True
        self.running = False
        self._is_closing = is_closing
        self.status_label.config(text="Status: Stopping stream capture...", fg="#f39c12")
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

                for pipe in [proc.stdout, proc.stdin]:
                    if pipe:
                        try:
                            pipe.close()
                        except Exception:
                            pass

                self.process = None

            if self.preview_thread and self.preview_thread.is_alive():
                self.preview_thread.join(timeout=2)

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
            self.root.destroy()

    def wait_for_shutdown(self):
        if self.process is not None or self._stopping:
            self.root.after(100, self.wait_for_shutdown)
        else:
            self.root.destroy()


if __name__ == "__main__":
    verify_dshow_devices()

    root = tk.Tk()
    app = GVUSB2CaptureGUI(root)
    root.mainloop()
