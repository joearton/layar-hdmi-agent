#!/usr/bin/env python3
"""Layar HDMI PC agent: turns touches on the phone into touches on this PC.

The phone shows this PC's screen through a USB HDMI capture card. This agent
injects the touches the phone sends over the local network into the monitor
the capture card is connected to:

  Linux    a virtual multi-touch screen (uinput), mapped to that monitor
  Windows  touch injection (InjectTouchInput, Windows 8+), placed on that monitor

Standard library only. Run as your normal user (on Linux it needs write access
to /dev/uinput; see README.md).

Protocol: TCP, one JSON object per line.
  phone -> agent  {"t":"hello","pin":"123456","app":1}
                  {"t":"touch","p":[{"id":0,"x":0.5,"y":0.25}, ...]}  # all pointers currently down
  agent -> phone  {"t":"welcome","name":"host","output":"HDMI-A-2"} | {"t":"error","msg":"..."}
Coordinates are normalized (0..1) within the captured picture. A touch
message lists every finger currently down; an empty list lifts all fingers.
"""

import argparse
import asyncio
import json
import os
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import time

WINDOWS = sys.platform == "win32"
if not WINDOWS:
    import fcntl

PORT = 47800
BEACON_PORT = 47801
DEVICE_NAME = "Layar HDMI Touch"
CONFIG_DIR = (os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "layar-agent") if WINDOWS
              else os.path.expanduser("~/.config/layar-agent"))
COSMIC_INPUT_DEVICES = os.path.expanduser("~/.config/cosmic/com.system76.CosmicComp/v1/input_devices")
MAX_SLOTS = 10
ABS_MAX = 32767

# --- Linux: uinput ---------------------------------------------------------------------

EV_SYN, EV_KEY, EV_ABS = 0x00, 0x01, 0x03
SYN_REPORT = 0
BTN_TOUCH = 0x14A
ABS_X, ABS_Y = 0x00, 0x01
ABS_MT_SLOT, ABS_MT_POSITION_X, ABS_MT_POSITION_Y, ABS_MT_TRACKING_ID = 0x2F, 0x35, 0x36, 0x39
INPUT_PROP_DIRECT = 0x01
BUS_VIRTUAL = 0x06


def _ioc(direction, nr, size):
    return (direction << 30) | (size << 16) | (ord("U") << 8) | nr


UI_DEV_CREATE = _ioc(0, 1, 0)
UI_DEV_DESTROY = _ioc(0, 2, 0)
UI_DEV_SETUP = _ioc(1, 3, 92)     # struct uinput_setup
UI_ABS_SETUP = _ioc(1, 4, 28)     # struct uinput_abs_setup
UI_SET_EVBIT = _ioc(1, 100, 4)
UI_SET_KEYBIT = _ioc(1, 101, 4)
UI_SET_ABSBIT = _ioc(1, 103, 4)
UI_SET_PROPBIT = _ioc(1, 110, 4)


class TouchScreen:
    """A virtual direct-touch screen with MAX_SLOTS contacts."""

    def __init__(self, width_mm, height_mm):
        self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        for ev in (EV_SYN, EV_KEY, EV_ABS):
            fcntl.ioctl(self.fd, UI_SET_EVBIT, ev)
        fcntl.ioctl(self.fd, UI_SET_KEYBIT, BTN_TOUCH)
        fcntl.ioctl(self.fd, UI_SET_PROPBIT, INPUT_PROP_DIRECT)
        # Resolution (units/mm) lets libinput know the physical size.
        res_x = max(1, ABS_MAX // max(1, width_mm))
        res_y = max(1, ABS_MAX // max(1, height_mm))
        for code, maximum, res in (
            (ABS_X, ABS_MAX, res_x), (ABS_Y, ABS_MAX, res_y),
            (ABS_MT_POSITION_X, ABS_MAX, res_x), (ABS_MT_POSITION_Y, ABS_MAX, res_y),
            (ABS_MT_SLOT, MAX_SLOTS - 1, 0), (ABS_MT_TRACKING_ID, 65535, 0),
        ):
            fcntl.ioctl(self.fd, UI_SET_ABSBIT, code)
            # struct uinput_abs_setup { __u16 code; struct input_absinfo {value,min,max,fuzz,flat,res} }
            fcntl.ioctl(self.fd, UI_ABS_SETUP, struct.pack("<Hxx6i", code, 0, 0, maximum, 0, 0, res))
        name = DEVICE_NAME.encode()[:79]
        # struct uinput_setup { struct input_id {bustype,vendor,product,version}; char name[80]; __u32 ff_effects_max }
        fcntl.ioctl(self.fd, UI_DEV_SETUP, struct.pack("<4H80sI", BUS_VIRTUAL, 0x1209, 0x4D48, 1, name, 0))
        fcntl.ioctl(self.fd, UI_DEV_CREATE)
        self.slots = {}          # phone pointer id -> slot
        self.next_tracking = 1

    def _emit(self, etype, code, value):
        now = time.time()
        sec = int(now)
        os.write(self.fd, struct.pack("llHHi", sec, int((now - sec) * 1e6), etype, code, value))

    def update(self, pointers):
        """Apply the full set of pointers currently down: [(id, x, y)] normalized."""
        active = {pid for pid, _, _ in pointers}
        had_touch = bool(self.slots)
        # Lift fingers that are gone.
        for pid in list(self.slots):
            if pid not in active:
                self._emit(EV_ABS, ABS_MT_SLOT, self.slots.pop(pid))
                self._emit(EV_ABS, ABS_MT_TRACKING_ID, -1)
        first = None
        for pid, x, y in pointers:
            ax = round(min(max(x, 0.0), 1.0) * ABS_MAX)
            ay = round(min(max(y, 0.0), 1.0) * ABS_MAX)
            if pid not in self.slots:
                free = sorted(set(range(MAX_SLOTS)) - set(self.slots.values()))
                if not free:
                    continue
                self.slots[pid] = free[0]
                self._emit(EV_ABS, ABS_MT_SLOT, free[0])
                self._emit(EV_ABS, ABS_MT_TRACKING_ID, self.next_tracking)
                self.next_tracking = self.next_tracking % 65535 + 1
            else:
                self._emit(EV_ABS, ABS_MT_SLOT, self.slots[pid])
            self._emit(EV_ABS, ABS_MT_POSITION_X, ax)
            self._emit(EV_ABS, ABS_MT_POSITION_Y, ay)
            if first is None:
                first = (ax, ay)
        if first:
            self._emit(EV_ABS, ABS_X, first[0])
            self._emit(EV_ABS, ABS_Y, first[1])
        if bool(self.slots) != had_touch:
            self._emit(EV_KEY, BTN_TOUCH, 1 if self.slots else 0)
        self._emit(EV_SYN, SYN_REPORT, 0)

    def release_all(self):
        if self.slots:
            self.update([])

    def close(self):
        try:
            self.release_all()
            fcntl.ioctl(self.fd, UI_DEV_DESTROY)
        finally:
            os.close(self.fd)


# --- display mapping --------------------------------------------------------------

CAPTURE_HINTS = re.compile(r"pvr|capture|ezcap|macrosilicon|ms21\d\d|usb3|hdmi[- ]?to[- ]?usb|elgato|avermedia", re.I)


def cosmic_outputs():
    """[(name, make, model, width_mm, height_mm)] from cosmic-randr, or [] if unavailable."""
    if not shutil.which("cosmic-randr"):
        return []
    try:
        text = subprocess.run(["cosmic-randr", "list"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    outputs = []
    for block in re.split(r"\n(?=\S)", text):
        head = re.match(r"(\S+) \((enabled|disabled)\)", block)
        if not head or head.group(2) != "enabled":
            continue
        make = re.search(r"Make:\s*(.*)", block)
        model = re.search(r"Model:\s*(.*)", block)
        size = re.search(r"Physical Size:\s*(\d+) x (\d+)", block)
        outputs.append((
            head.group(1),
            make.group(1).strip() if make else "",
            model.group(1).strip() if model else "",
            int(size.group(1)) if size else 480,
            int(size.group(2)) if size else 270,
        ))
    return outputs


def pick_output(outputs, wanted):
    if wanted:
        for o in outputs:
            if o[0] == wanted:
                return o
        raise AgentError(f"Output {wanted!r} not found. Available: {', '.join(o[0] for o in outputs) or 'none'}")
    for o in outputs:
        if CAPTURE_HINTS.search(f"{o[1]} {o[2]}"):
            return o
    return None


def map_touch_to_output_cosmic(output_name):
    """Ask COSMIC to map our touch device to the capture card's monitor (keeps other entries)."""
    os.makedirs(os.path.dirname(COSMIC_INPUT_DEVICES), exist_ok=True)
    entry = f'    "{DEVICE_NAME}": (\n        state: Enabled,\n        map_to_output: Some("{output_name}"),\n    ),\n'
    current = ""
    if os.path.exists(COSMIC_INPUT_DEVICES):
        with open(COSMIC_INPUT_DEVICES) as f:
            current = f.read()
        shutil.copy2(COSMIC_INPUT_DEVICES, COSMIC_INPUT_DEVICES + ".bak-layar")
    # Drop a previous entry of ours, then insert the new one into the RON map.
    current = re.sub(r'\s*"' + re.escape(DEVICE_NAME) + r'":\s*\(.*?\n    \),?', "", current, flags=re.S)
    body = current.strip()
    if body.startswith("{") and body.endswith("}"):
        inner = body[1:-1].strip()
        if inner and not inner.endswith(","):
            inner += ","
        new = "{\n" + (("    " + inner + "\n") if inner else "") + entry + "}\n"
    else:
        new = "{\n" + entry + "}\n"
    with open(COSMIC_INPUT_DEVICES, "w") as f:
        f.write(new)


class AgentError(Exception):
    """A problem the user has to fix (shown in the window, or printed)."""


# --- pairing PIN --------------------------------------------------------------------

def load_pin(new=False):
    """The saved 6-digit PIN; a fresh one when there is none or [new] is set."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    path = os.path.join(CONFIG_DIR, "pin")
    if os.path.exists(path) and not new:
        with open(path) as f:
            pin = f.read().strip()
        if re.fullmatch(r"\d{6}", pin):
            return pin
    pin = f"{secrets.randbelow(1_000_000):06d}"
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write(pin)
    return pin


# --- network ----------------------------------------------------------------------------

class Agent:
    def __init__(self, touch, pin, output_name):
        self.touch = touch
        self.pin = pin
        self.output_name = output_name
        # Called from the network thread: ("connected" | "disconnected" | "wrong_pin", peer IP).
        self.on_event = lambda kind, detail: None
        self.client = None

    async def handle(self, reader, writer):
        peer = writer.get_extra_info("peername")
        authed = False
        try:
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=30 if not authed else None)
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                kind = msg.get("t")
                if kind == "hello":
                    if not secrets.compare_digest(str(msg.get("pin", "")), self.pin):
                        writer.write(b'{"t":"error","msg":"Wrong PIN"}\n')
                        await writer.drain()
                        print(f"[agent] wrong PIN from {peer[0]}")
                        self.on_event("wrong_pin", peer[0])
                        await asyncio.sleep(1)  # slow down guessing
                        break
                    authed = True
                    # Newest authenticated phone wins, so a reconnect after a Wi-Fi
                    # hiccup is never locked out (unauthenticated probes change nothing).
                    if self.client is not None and self.client is not writer:
                        self.client.close()
                        self.touch.release_all()
                    self.client = writer
                    print(f"[agent] phone connected from {peer[0]}")
                    self.on_event("connected", peer[0])
                    welcome = {"t": "welcome", "name": socket.gethostname(), "output": self.output_name}
                    writer.write((json.dumps(welcome) + "\n").encode())
                    await writer.drain()
                elif kind == "touch" and authed:
                    pointers = [(int(p["id"]), float(p["x"]), float(p["y"])) for p in msg.get("p", [])]
                    self.touch.update(pointers)
                elif kind == "ping" and authed:
                    writer.write(b'{"t":"pong"}\n')
                    await writer.drain()
        except (asyncio.TimeoutError, ConnectionError, KeyError, TypeError, ValueError):
            pass
        finally:
            if self.client is writer:
                # Never leave a finger "stuck" down when the phone disappears.
                self.touch.release_all()
                self.client = None
                print(f"[agent] phone {peer[0]} disconnected")
                self.on_event("disconnected", peer[0])
            writer.close()


async def beacon(name):
    """Broadcast our presence so the phone can find us without typing an IP."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    payload = json.dumps({"service": "hdmi-monitor-agent", "name": name, "port": PORT, "v": 1}).encode()
    while True:
        # Limited broadcast is often filtered by phones/routers; the subnet's
        # directed broadcast (e.g. 192.168.1.255) gets through more often.
        for target in ["255.255.255.255", *directed_broadcasts()]:
            try:
                sock.sendto(payload, (target, BEACON_PORT))
            except OSError:
                pass
        await asyncio.sleep(1)


def directed_broadcasts():
    """Broadcast addresses of this machine's IPv4 networks, from `ip -4 addr`."""
    found = []
    if not WINDOWS:
        try:
            text = subprocess.run(["ip", "-4", "-o", "addr"], capture_output=True, text=True, timeout=2).stdout
            found = [m for m in re.findall(r"\bbrd (\d+\.\d+\.\d+\.\d+)", text) if not m.startswith("127.")]
        except (OSError, subprocess.SubprocessError):
            pass
    if not found:
        # No `ip` (Windows): assume a /24 around the address of the default route.
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("192.0.2.1", 9))  # no packet is sent for UDP connect
                found = [s.getsockname()[0].rsplit(".", 1)[0] + ".255"]
        except OSError:
            pass
    return found


def open_linux(args):
    outputs = cosmic_outputs()
    out = pick_output(outputs, args.output)
    width_mm, height_mm = (out[3], out[4]) if out else (480, 270)
    try:
        touch = TouchScreen(width_mm, height_mm)
    except PermissionError:
        raise AgentError("No write access to /dev/uinput. See README.md (udev rule) and log in again.")
    if out and not args.no_map and outputs:
        map_touch_to_output_cosmic(out[0])
        print(f"[agent] touch mapped to {out[0]} ({out[1]} {out[2]}) via COSMIC config")
    elif not out:
        print("[agent] could not identify the capture card's monitor; pass --output NAME."
              " On GNOME/KDE, map 'Layar HDMI Touch' to that monitor in the touchscreen settings.")
    return touch, out[0] if out else None


def open_windows(args):
    import layar_agent_windows as win
    monitors = win.monitors()
    for i, m in enumerate(monitors, 1):
        print(f"[agent] monitor {i}: {m.label()}")
    mon = win.pick_monitor(monitors, args.output, CAPTURE_HINTS)
    if mon is None:
        raise AgentError("Could not tell which monitor is the capture card. Run again with --output N "
                         "(the monitor number above).")
    print(f"[agent] touches go to monitor {mon.label()}")
    return win.TouchInjector(mon), mon.name


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--output", help="monitor the capture card is plugged into: a name like HDMI-A-2 (Linux) "
                                     "or a monitor number from the list (Windows); auto-detected when possible")
    ap.add_argument("--no-map", action="store_true", help="Linux: don't touch the desktop's input-mapping config")
    ap.add_argument("--no-gui", action="store_true", help="run in the terminal without the status window")
    ap.add_argument("--self-test", action="store_true", help=argparse.SUPPRESS)  # CI: imports and monitor list
    return ap.parse_args()


def open_touch(args):
    return open_windows(args) if WINDOWS else open_linux(args)


async def serve(agent):
    """Accept phones and announce this PC until cancelled."""
    try:
        server = await asyncio.start_server(agent.handle, "0.0.0.0", PORT)
    except OSError as e:
        raise AgentError(f"Port {PORT} is busy; is the agent already running? ({e.strerror})") from e
    print(f"[agent] listening on port {PORT}.  PIN: {agent.pin}")
    async with server:
        await asyncio.gather(server.serve_forever(), beacon(socket.gethostname()))


def local_ip():
    """This PC's LAN address (the one the default route uses), for typing into the phone."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # no packet is sent for UDP connect
            return s.getsockname()[0]
    except OSError:
        return None


def run_console(args):
    touch, output_name = open_touch(args)
    try:
        asyncio.run(serve(Agent(touch, load_pin(), output_name)))
    finally:
        touch.close()


def gui_available():
    if not WINDOWS and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    try:
        import tkinter  # noqa: F401  (python3-tk may be missing on Linux)
        return True
    except ImportError:
        return False


def self_test():
    """Exit 0 when the bundled modules load and (on Windows) monitors can be listed."""
    import layar_agent_gui  # noqa: F401
    if WINDOWS:
        import layar_agent_windows as win
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
        if not win.monitors():
            sys.exit(2)
    sys.exit(0)


def main():
    args = parse_args()
    if args.self_test:
        self_test()
    if not args.no_gui and gui_available():
        import layar_agent_gui
        layar_agent_gui.run(args)
        return
    try:
        run_console(args)
    except AgentError as e:
        print(f"[agent] {e}")
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
