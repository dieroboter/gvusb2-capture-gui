# gvusb2-capture-gui

A small Windows GUI for capturing video with the I-O Data GV-USB2 capture device. It records interlaced MPEG-2 program streams (.mpg) at higher quality than OBS produces, while keeping the interlacing intact and the file sizes reasonable.

## Features

- Live preview with audio monitoring while the capture engine runs
- One-click start/stop recording
- Timer recording with presets (15m, 30m, 1h, 2h, 4h) or a custom duration up to 24 hours, with a countdown in the status bar
- Checks on launch that FFmpeg is installed and both GV-USB2 devices are present, with an error message if not
- Recordings are finalized on stop, and closing the window mid-recording finalizes the file before exiting

## Requirements

- Windows
- An I-O Data GV-USB2 with an NTSC source
- [FFmpeg](https://ffmpeg.org/) on your PATH (includes ffprobe, which the script also uses)
- Python 3 with tkinter (included with the standard Windows Python installer)
- The `sounddevice` and `pillow` Python packages

## Usage

1. Install FFmpeg:

   ```
   winget install ffmpeg
   ```

2. Install the required Python packages:

   ```
   python -m pip install sounddevice pillow
   ```

3. Run the script:

   ```
   python capture.py
   ```

Press **Start Recording** to record, **Stop Recording** to finish, or **Set Timer...** to record for a set duration. Recordings are saved to the folder the script was run from, named by date and time, e.g. `capture_20261016_160500.mpg`.

## How it works

A single FFmpeg process captures from the GV-USB2's DirectShow devices continuously. The video is split into two branches:

- **Recording:** kept interlaced (top field first) and encoded in real time, then streamed over a local TCP connection to the GUI, which writes it to a temporary MPEG-TS file.
- **Preview:** deinterlaced with `bwdif` for display in the window. Only the preview is deinterlaced; the recording is not.

Audio is split the same way, with a 300 ms delay applied to keep it in sync: one branch is encoded into the recording, the other plays live through `sounddevice` so you can monitor it.

When recording stops, the temporary file is converted to the final .mpg with FFmpeg using stream copy (no re-encoding). The script uses ffprobe to find the first video keyframe so the output starts cleanly, and timer recordings are trimmed to the requested duration.

## Output format

| | |
|---|---|
| Container | MPEG program stream (.mpg) |
| Video | MPEG-2, 720x480, 29.97 fps, interlaced (TFF), 4:3 |
| Bitrate | 12 Mbps average, 15 Mbps max, no B-frames, GOP 15 |
| Audio | AC-3, 448 kbps, 48 kHz, stereo |

## Configuration

A few settings are constants at the top of `capture.py`:

- `REQ_VID` / `REQ_AUD` — the DirectShow device names to look for. They are set to the GV-USB2's names (`GV-USB2, Analog Capture` / `GV-USB2, Analog WaveIn`); another capture device would need these changed to its own names.
- `PREV_AUD_DELAY` / `REC_AUD_DELAY` — audio delay compensation in milliseconds (300 by default), for preview and recording separately.

## Notes and limitations

- Windows only, and NTSC only as written: the capture resolution and frame rate are hardcoded to 720x480 at 29.97 fps.
- The GV-USB2's device names are hardcoded (see Configuration), so it won't pick up other capture hardware without editing the script.
- During recording a `temp_*.ts` file sits alongside the output; it's deleted once the final .mpg is written, or renamed to the .mpg name if finalizing fails, so a failed finalize shouldn't lose the recording.

## AI disclaimer

This script was written by AI. I use it for my own captures and put it on GitHub in case it's useful to anyone else. Bug reports are welcome, but there's no guarantee they'll be fixed, and feature requests probably won't be taken.
