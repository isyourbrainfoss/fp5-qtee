#!/usr/bin/python3
# FP5 BengalOS 2026-10-31: feedbackd sets the aw86927 force-feedback gain to 75% (0xc000)
# when it opens the motor; raise it to 100% (0xffff). Usage: fp5-ff-gain.py [gain]
import glob, os, struct, sys
g = int(sys.argv[1], 0) if len(sys.argv) > 1 else 0xFFFF
for ev in glob.glob("/sys/class/input/event*"):
    try:
        name = open(ev + "/device/name").read().strip()
    except OSError:
        continue
    if name == "aw86927-haptics":
        fd = os.open("/dev/input/" + os.path.basename(ev), os.O_WRONLY)
        os.write(fd, struct.pack("@llHHi", 0, 0, 0x15, 0x60, g))  # EV_FF / FF_GAIN
        os.close(fd); print("aw86927 gain", hex(g)); sys.exit(0)
sys.exit("aw86927-haptics not found")
