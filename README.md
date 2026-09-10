# gvusb2-capture-gui
capture interlaced mpgs using the i-o data gv-usb2 capture card at a better quality than obs is capable of while keeping interlacing intact and producing a smaller file size

## instructions
1. install ffmpeg with ``winget install ffmpeg``
2. install the required libraries with ``python -m pip install sounddevice pillow``
3. run the script with with ``python capture.py``

## notes
- videos recorded with this may occasionally generate a "video ring buffer overflow" error when opened in certain software. this can be fixed by running an ffmpeg command on the broken file: ``ffmpeg -err_detect ignore_err -fflags +genpts+discardcorrupt -i "capture_12345678_123456.mpg" -c copy -f vob -muxrate 25M -avoid_negative_ts make_zero "capture_repaired.mpg"``
- this script will only run on windows and will only work with ntsc sources
- this script was made with ai. it's on github because i found it useful and i thought others might too. if you make a bug report there's no guarantee it'll be fixed. feature requests probably won't be taken, but make one if you want
