import tkinter as tk
from tkinter import messagebox
import subprocess
import cv2
import numpy as np
from PIL import Image, ImageTk
import threading
import sys
import datetime
import os

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
        self._stopping = False          # flag to avoid multiple stops
        self._current_photo = None      # BUGFIX: keep a real reference to the live PhotoImage

        # Check if FFmpeg is available
        try:
            subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        except (subprocess.SubprocessError, FileNotFoundError):
            messagebox.showerror("FFmpeg not found", "Please install FFmpeg and add it to your PATH.")
            sys.exit(1)

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

        # Use explicit device names – change these if needed
        video_device = "video=GV-USB2, Analog Capture"
        audio_device = "audio=GV-USB2, Analog WaveIn"

        cmd = [
            "ffmpeg", "-y",
            # Video input
            "-f", "dshow",
            "-video_size", "720x480",
            "-framerate", "29.97",
            "-pixel_format", "yuyv422",
            "-rtbufsize", "256M",
            "-i", video_device,
            # Audio input
            "-f", "dshow",
            "-guess_layout_max", "0",
            "-ac", "2",
            "-rtbufsize", "256M",
            "-i", audio_device,
            # Recording output
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "mpeg2video",
            "-b:v", "15000k",
            "-minrate", "15000k",
            "-maxrate", "15000k",
            "-bufsize", "3000k",
            "-profile:v", "main",
            "-level:v", "main",
            "-g", "15",
            "-flags", "+ilme+ildct", "-aspect", "4:3", "-pix_fmt", "yuv420p",
            "-vf", r"select=gte(n\,2),setfield=tff",
            "-fps_mode", "cfr",
            "-af", r"adelay=200|200,aselect=gte(n\,2),aresample=async=1",
            "-ar", "48000",
            "-c:a", "mp2", "-b:a", "320k",
            "-f", "vob",
            self.current_output,
            # Preview output (MJPEG pipe)
            "-map", "0:v:0",
            "-c:v", "mjpeg",
            "-q:v", "2",
            "-f", "image2pipe",
            "pipe:1"
        ]

        CREATE_NO_WINDOW = 0x08000000

        try:
            self.process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=sys.stderr,
                bufsize=10**7,
                creationflags=CREATE_NO_WINDOW
            )
            self.running = True
            self._stopping = False
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
                chunk = proc.stdout.read(4096)
                if not chunk:
                    if proc.poll() is not None:
                        break  # FFmpeg exited cleanly, break loop
                    continue

                buffer.extend(chunk)

                if len(buffer) > MAX_BUFFER_SIZE:
                    # Find last complete frame and keep only that portion
                    last_start = buffer.rfind(b'\xff\xd8')
                    if last_start != -1:
                        buffer = buffer[last_start:]
                    else:
                        buffer.clear()
                    continue

                # Process all complete JPEG frames in the buffer
                while True:
                    start = buffer.find(b'\xff\xd8')
                    if start == -1:
                        break
                    end = buffer.find(b'\xff\xd9', start + 2)
                    if end == -1:
                        break

                    jpg_data = buffer[start:end+2]
                    del buffer[:end+2]

                    np_arr = np.frombuffer(jpg_data, dtype=np.uint8)
                    frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
                    if frame is not None:
                        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                        img = Image.fromarray(rgb)
                        photo = ImageTk.PhotoImage(image=img)
                        self.root.after(0, self.update_canvas, photo)

            except (BrokenPipeError, OSError):
                break
            except Exception:
                import traceback
                traceback.print_exc()
                break
                
        # Properly flag the thread as stopped once the loop breaks
        self.running = False

    def update_canvas(self, photo):
        # Ensure the canvas hasn't been destroyed by app closure
        if self.running and self.canvas.winfo_exists():
            self._current_photo = photo
            if self.image_item is None:
                self.image_item = self.canvas.create_image(0, 0, anchor=tk.NW, image=photo)
            else:
                self.canvas.itemconfig(self.image_item, image=photo)

    def stop_capture(self):
        if not self.running or self._stopping:
            return
        self._stopping = True
        self.status_label.config(text="Status: Stopping stream capture...", fg="#f39c12")
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.DISABLED)

        def async_teardown():
            proc = self.process
            if proc:
                # 1. Ask FFmpeg to quit gracefully by sending 'q' + newline.
                if proc.stdin:
                    try:
                        proc.stdin.write(b'q\n')
                        proc.stdin.flush()
                    except (BrokenPipeError, OSError):
                        pass

                # 2. Wait for FFmpeg to exit. When it does, stdout reaches EOF
                #    and the preview thread's read() returns b'' -> loop breaks.
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
                    proc.terminate()
                    try:
                        proc.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()

                # 3. Close pipes safely.
                for pipe in [proc.stdout, proc.stdin]:
                    if pipe:
                        try:
                            pipe.close()
                        except Exception:
                            pass
                self.process = None

            # 4. Make sure the preview thread has actually exited before we
            #    tell the GUI we are done. Without this, finalize_stop() could
            #    clear the canvas while a stale after() callback is still queued.
            if self.preview_thread and self.preview_thread.is_alive():
                self.preview_thread.join(timeout=2)

            # 5. Notify GUI after cleanup (only if the window is still alive).
            if self.root.winfo_exists():
                self.root.after(0, self.finalize_stop)

        threading.Thread(target=async_teardown, daemon=True).start()

    def finalize_stop(self):
        self.canvas.delete("all")
        self.image_item = None
        self._current_photo = None
        self.status_label.config(text="Status: Finished / Ready", fg="black")
        self.start_btn.config(state=tk.NORMAL)
        self._stopping = False

        filename = self.current_output if self.current_output else "output.mpg"
        # Show message box only if the root window still exists
        if self.root.winfo_exists():
            messagebox.showinfo("Success", f"Recording finished completely!\nYour file '{filename}' is ready.")

    def on_closing(self):
        if self.running:
            if messagebox.askokcancel("Quit", "A recording is in progress.\nDo you want to stop recording and exit?"):
                self.stop_capture()
                self.wait_for_shutdown()
        else:
            self.root.destroy()

    def wait_for_shutdown(self):
        if self.process is not None and self.root.winfo_exists():
            self.root.after(100, self.wait_for_shutdown)
        else:
            # Give finalize_stop a moment to show the message box before destroying
            self.root.after(300, self.root.destroy)

if __name__ == "__main__":
    root = tk.Tk()
    app = GVUSB2CaptureGUI(root)
    root.mainloop()
