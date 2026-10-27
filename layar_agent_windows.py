"""Windows backend for the Layar HDMI PC agent: touch injection on one monitor.

Uses InitializeTouchInjection / InjectTouchInput (Windows 8+), so apps see real
multi-touch (tap, drag, pinch, two-finger scroll) on the monitor the capture
card is plugged into. Standard library only (ctypes).
"""

import ctypes
import threading
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)

MAX_CONTACTS = 10
# Windows cancels injected contacts that stop receiving frames, so fingers held
# still are re-sent at this interval.
KEEPALIVE_S = 0.1

PT_TOUCH = 2
TOUCH_FEEDBACK_DEFAULT = 0x1
TOUCH_MASK_CONTACTAREA = 0x1
POINTER_FLAG_INRANGE = 0x2
POINTER_FLAG_INCONTACT = 0x4
POINTER_FLAG_PRIMARY = 0x2000
POINTER_FLAG_DOWN = 0x10000
POINTER_FLAG_UPDATE = 0x20000
POINTER_FLAG_UP = 0x40000
CONTACT_RADIUS_PX = 4


class POINTER_INFO(ctypes.Structure):
    _fields_ = [
        ("pointerType", wintypes.DWORD),
        ("pointerId", ctypes.c_uint32),
        ("frameId", ctypes.c_uint32),
        ("pointerFlags", ctypes.c_uint32),
        ("sourceDevice", wintypes.HANDLE),
        ("hwndTarget", wintypes.HWND),
        ("ptPixelLocation", wintypes.POINT),
        ("ptHimetricLocation", wintypes.POINT),
        ("ptPixelLocationRaw", wintypes.POINT),
        ("ptHimetricLocationRaw", wintypes.POINT),
        ("dwTime", wintypes.DWORD),
        ("historyCount", ctypes.c_uint32),
        ("InputData", ctypes.c_int32),
        ("dwKeyStates", wintypes.DWORD),
        ("PerformanceCount", ctypes.c_uint64),
        ("ButtonChangeType", ctypes.c_int),
    ]


class POINTER_TOUCH_INFO(ctypes.Structure):
    _fields_ = [
        ("pointerInfo", POINTER_INFO),
        ("touchFlags", ctypes.c_uint32),
        ("touchMask", ctypes.c_uint32),
        ("rcContact", wintypes.RECT),
        ("rcContactRaw", wintypes.RECT),
        ("orientation", ctypes.c_uint32),
        ("pressure", ctypes.c_uint32),
    ]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


class Monitor:
    def __init__(self, index, device, friendly, rect, primary):
        self.index = index        # 1-based, as printed to the user
        self.name = device        # e.g. \\.\DISPLAY2
        self.friendly = friendly  # EDID name, e.g. "SUNTECH PVR"
        self.left, self.top, self.right, self.bottom = rect
        self.primary = primary

    def label(self):
        w, h = self.right - self.left, self.bottom - self.top
        return f"{self.index} {self.friendly or self.name} {w}x{h} at {self.left},{self.top}" + \
            (" (primary)" if self.primary else "")


def _dpi_aware():
    """Work in physical pixels so monitor rectangles and touch points agree."""
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    except (AttributeError, OSError):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            user32.SetProcessDPIAware()


# --- monitor names (QueryDisplayConfig gives the EDID "friendly" name) ---------------

QDC_ONLY_ACTIVE_PATHS = 2
DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME = 1
DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME = 2


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class DISPLAYCONFIG_PATH_SOURCE_INFO(ctypes.Structure):
    _fields_ = [("adapterId", LUID), ("id", ctypes.c_uint32), ("modeInfoIdx", ctypes.c_uint32), ("statusFlags", ctypes.c_uint32)]


class DISPLAYCONFIG_RATIONAL(ctypes.Structure):
    _fields_ = [("Numerator", ctypes.c_uint32), ("Denominator", ctypes.c_uint32)]


class DISPLAYCONFIG_PATH_TARGET_INFO(ctypes.Structure):
    _fields_ = [
        ("adapterId", LUID), ("id", ctypes.c_uint32), ("modeInfoIdx", ctypes.c_uint32),
        ("outputTechnology", ctypes.c_uint32), ("rotation", ctypes.c_uint32), ("scaling", ctypes.c_uint32),
        ("refreshRate", DISPLAYCONFIG_RATIONAL), ("scanLineOrdering", ctypes.c_uint32),
        ("targetAvailable", wintypes.BOOL), ("statusFlags", ctypes.c_uint32),
    ]


class DISPLAYCONFIG_PATH_INFO(ctypes.Structure):
    _fields_ = [("sourceInfo", DISPLAYCONFIG_PATH_SOURCE_INFO), ("targetInfo", DISPLAYCONFIG_PATH_TARGET_INFO), ("flags", ctypes.c_uint32)]


class DISPLAYCONFIG_MODE_INFO(ctypes.Structure):
    _fields_ = [("infoType", ctypes.c_uint32), ("id", ctypes.c_uint32), ("adapterId", LUID), ("info", ctypes.c_byte * 48)]


class DISPLAYCONFIG_DEVICE_INFO_HEADER(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("size", ctypes.c_uint32), ("adapterId", LUID), ("id", ctypes.c_uint32)]


class DISPLAYCONFIG_SOURCE_DEVICE_NAME(ctypes.Structure):
    _fields_ = [("header", DISPLAYCONFIG_DEVICE_INFO_HEADER), ("viewGdiDeviceName", wintypes.WCHAR * 32)]


class DISPLAYCONFIG_TARGET_DEVICE_NAME(ctypes.Structure):
    _fields_ = [
        ("header", DISPLAYCONFIG_DEVICE_INFO_HEADER), ("flags", ctypes.c_uint32), ("outputTechnology", ctypes.c_uint32),
        ("edidManufactureId", ctypes.c_uint16), ("edidProductCodeId", ctypes.c_uint16), ("connectorInstance", ctypes.c_uint32),
        ("monitorFriendlyDeviceName", wintypes.WCHAR * 64), ("monitorDevicePath", wintypes.WCHAR * 128),
    ]


def _friendly_names():
    """{r'\\\\.\\DISPLAY2': 'SUNTECH PVR', ...}; empty if the API is unavailable."""
    names = {}
    try:
        n_paths, n_modes = ctypes.c_uint32(), ctypes.c_uint32()
        if user32.GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, ctypes.byref(n_paths), ctypes.byref(n_modes)):
            return names
        paths = (DISPLAYCONFIG_PATH_INFO * n_paths.value)()
        modes = (DISPLAYCONFIG_MODE_INFO * n_modes.value)()
        if user32.QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS, ctypes.byref(n_paths), paths, ctypes.byref(n_modes), modes, None):
            return names
        for p in paths[: n_paths.value]:
            src = DISPLAYCONFIG_SOURCE_DEVICE_NAME()
            src.header = DISPLAYCONFIG_DEVICE_INFO_HEADER(
                DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME, ctypes.sizeof(src), p.sourceInfo.adapterId, p.sourceInfo.id)
            tgt = DISPLAYCONFIG_TARGET_DEVICE_NAME()
            tgt.header = DISPLAYCONFIG_DEVICE_INFO_HEADER(
                DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME, ctypes.sizeof(tgt), p.targetInfo.adapterId, p.targetInfo.id)
            if user32.DisplayConfigGetDeviceInfo(ctypes.byref(src)) == 0 and user32.DisplayConfigGetDeviceInfo(ctypes.byref(tgt)) == 0:
                names[src.viewGdiDeviceName] = tgt.monitorFriendlyDeviceName
    except (AttributeError, OSError):
        pass
    return names


def monitors():
    _dpi_aware()
    friendly = _friendly_names()
    found = []
    MonitorEnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def collect(hmon, _hdc, _rect, _data):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            r = info.rcMonitor
            found.append((info.szDevice, (r.left, r.top, r.right, r.bottom), bool(info.dwFlags & 1)))
        return True

    user32.EnumDisplayMonitors(None, None, MonitorEnumProc(collect), 0)
    found.sort(key=lambda m: (m[1][0], m[1][1]))
    return [Monitor(i, dev, friendly.get(dev, ""), rect, primary) for i, (dev, rect, primary) in enumerate(found, 1)]


def pick_monitor(mons, wanted, hints):
    if wanted:
        for m in mons:
            if wanted in (str(m.index), m.name, m.friendly):
                return m
        return None
    for m in mons:
        if hints.search(m.friendly):
            return m
    # Two monitors and one is the laptop panel: the other one is almost certainly the card.
    others = [m for m in mons if not m.primary]
    return others[0] if len(mons) == 2 and len(others) == 1 else None


# --- injection ------------------------------------------------------------------------

class TouchInjector:
    """Same interface as the Linux TouchScreen: update([(id, x, y)]), release_all(), close()."""

    def __init__(self, monitor):
        self.mon = monitor
        if not user32.InitializeTouchInjection(MAX_CONTACTS, TOUCH_FEEDBACK_DEFAULT):
            raise OSError(ctypes.get_last_error(), "InitializeTouchInjection failed (needs Windows 8 or later)")
        self.contacts = {}  # phone pointer id -> (contact id, x px, y px)
        self.primary = None  # contact id of the first finger down; keeps PRIMARY until it lifts
        self.lock = threading.Lock()
        self.running = True
        threading.Thread(target=self._keepalive, name="touch-keepalive", daemon=True).start()

    def _point(self, x, y):
        m = self.mon
        px = m.left + round(min(max(x, 0.0), 1.0) * (m.right - m.left - 1))
        py = m.top + round(min(max(y, 0.0), 1.0) * (m.bottom - m.top - 1))
        return px, py

    def _info(self, cid, px, py, flags):
        if cid == self.primary:
            flags |= POINTER_FLAG_PRIMARY
        t = POINTER_TOUCH_INFO()
        t.pointerInfo.pointerType = PT_TOUCH
        t.pointerInfo.pointerId = cid
        t.pointerInfo.pointerFlags = flags
        t.pointerInfo.ptPixelLocation = wintypes.POINT(px, py)
        t.touchMask = TOUCH_MASK_CONTACTAREA
        t.rcContact = wintypes.RECT(px - CONTACT_RADIUS_PX, py - CONTACT_RADIUS_PX, px + CONTACT_RADIUS_PX, py + CONTACT_RADIUS_PX)
        return t

    def _inject(self, infos):
        if not infos:
            return
        arr = (POINTER_TOUCH_INFO * len(infos))(*infos)
        if not user32.InjectTouchInput(len(infos), arr):
            err = ctypes.get_last_error()
            # 87 = a contact Windows already cancelled; start those fingers over.
            print(f"[agent] InjectTouchInput failed ({err})")
            if err == 87:
                self.contacts.clear()
                self.primary = None

    def set_monitor(self, monitor):
        """Send touches to another monitor; fingers that are down are lifted first."""
        self.release_all()
        with self.lock:
            self.mon = monitor

    def update(self, pointers):
        with self.lock:
            infos = []
            live = {pid for pid, _, _ in pointers}
            for pid in list(self.contacts):
                if pid not in live:
                    cid, px, py = self.contacts.pop(pid)
                    infos.append(self._info(cid, px, py, POINTER_FLAG_UP))
                    if cid == self.primary:
                        self.primary = None
            for pid, x, y in pointers:
                px, py = self._point(x, y)
                if pid in self.contacts:
                    cid = self.contacts[pid][0]
                    flags = POINTER_FLAG_UPDATE | POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT
                else:
                    used = {c[0] for c in self.contacts.values()}
                    free = [i for i in range(MAX_CONTACTS) if i not in used]
                    if not free:
                        continue
                    cid = free[0]
                    flags = POINTER_FLAG_DOWN | POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT
                    if not self.contacts and self.primary is None:
                        self.primary = cid
                self.contacts[pid] = (cid, px, py)
                infos.append(self._info(cid, px, py, flags))
            self._inject(infos)

    def _keepalive(self):
        while self.running:
            time.sleep(KEEPALIVE_S)
            with self.lock:
                infos = [self._info(cid, px, py, POINTER_FLAG_UPDATE | POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT)
                         for cid, px, py in self.contacts.values()]
                self._inject(infos)

    def release_all(self):
        if self.contacts:
            self.update([])

    def close(self):
        self.running = False
        self.release_all()
