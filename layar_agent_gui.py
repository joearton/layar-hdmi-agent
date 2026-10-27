"""Status window for the Layar HDMI PC agent (tkinter), with an optional tray icon.

The network server runs on a background asyncio thread; it reports events
through a queue that the Tk main loop drains. The tray icon uses pystray when
it is installed (it is bundled in LayarHDMIAgent.exe); without it the window
simply minimizes like any other.
"""

import asyncio
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import messagebox

import layar_agent as core

GROUND = "#0E0F12"
PANEL = "#17191C"
LINE = "#2A2D32"
TEXT = "#E9EAEC"
MUTED = "#9AA0A9"
ACCENT = "#FFB020"
OK = "#4FD18B"
REC = "#FF3B30"

if core.WINDOWS:
    SANS, MONO = "Segoe UI", "Consolas"
else:
    SANS, MONO = "DejaVu Sans", "DejaVu Sans Mono"


def resource(name):
    """A file next to this script, or inside the PyInstaller bundle."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


class AgentWindow:
    def __init__(self, args):
        self.args = args
        self.events = queue.Queue()
        self.loop = None
        self.thread = None
        self.touch = None
        self.agent = None
        self.tray = None
        self.monitors = []

        self.root = tk.Tk()
        self.root.title("Layar HDMI Agent")
        self.root.configure(bg=GROUND)
        self.root.resizable(False, False)
        try:
            self.icon_image = tk.PhotoImage(file=resource("icon.png"))
            self.root.iconphoto(True, self.icon_image)
        except tk.TclError:
            pass
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._build()

    # ---- layout --------------------------------------------------------------------

    def _label(self, parent, text="", size=10, color=TEXT, font=SANS, weight="normal", **kw):
        return tk.Label(parent, text=text, bg=parent["bg"], fg=color, font=(font, size, weight), **kw)

    def _button(self, parent, text, command, primary=False):
        return tk.Button(
            parent, text=text, command=command, relief="flat", cursor="hand2",
            font=(SANS, 10, "bold" if primary else "normal"), padx=14, pady=6, bd=0,
            bg=ACCENT if primary else PANEL, fg="#141414" if primary else TEXT,
            activebackground="#FFC857" if primary else LINE, activeforeground="#141414" if primary else TEXT,
            highlightthickness=1, highlightbackground=ACCENT if primary else LINE,
        )

    def _build(self):
        pad = {"padx": 22}
        top = tk.Frame(self.root, bg=GROUND)
        top.pack(fill="x", pady=(18, 4), **pad)
        self._label(top, "Layar HDMI Agent", 15, weight="bold").pack(anchor="w")
        self._label(top, "Touch control for the Layar HDMI app", 9, MUTED).pack(anchor="w")

        card = tk.Frame(self.root, bg=PANEL, highlightthickness=1, highlightbackground=LINE)
        card.pack(fill="x", pady=12, **pad)
        inner = tk.Frame(card, bg=PANEL)
        inner.pack(fill="x", padx=18, pady=14)
        self._label(inner, "PIN", 9, MUTED, MONO).pack(anchor="w")
        self.pin_label = self._label(inner, "------", 30, ACCENT, MONO, "bold")
        self.pin_label.pack(anchor="w")
        self._label(inner, "Enter this PIN once in the app: Settings › PC touch.", 9, MUTED).pack(anchor="w")

        rows = tk.Frame(self.root, bg=GROUND)
        rows.pack(fill="x", **pad)
        status_row = tk.Frame(rows, bg=GROUND)
        status_row.pack(fill="x", pady=3)
        self.dot = tk.Canvas(status_row, width=12, height=12, bg=GROUND, highlightthickness=0)
        self.dot_id = self.dot.create_oval(2, 2, 11, 11, fill=ACCENT, outline="")
        self.dot.pack(side="left", padx=(0, 8))
        self.status_label = self._label(status_row, "Starting…", 10)
        self.status_label.pack(side="left")

        self._label(rows, "TOUCHES GO TO", 8, MUTED, MONO).pack(anchor="w", pady=(10, 0))
        self.monitor_var = tk.StringVar(value="—")
        self.monitor_menu = tk.OptionMenu(rows, self.monitor_var, "—")
        self.monitor_menu.configure(
            bg=PANEL, fg=TEXT, activebackground=LINE, activeforeground=TEXT, relief="flat",
            highlightthickness=1, highlightbackground=LINE, font=(SANS, 10), anchor="w",
        )
        self.monitor_menu["menu"].configure(bg=PANEL, fg=TEXT, activebackground=ACCENT, activeforeground="#141414")
        self.monitor_menu.pack(fill="x", pady=(2, 0))

        self._label(rows, "THIS PC", 8, MUTED, MONO).pack(anchor="w", pady=(10, 0))
        ip = core.local_ip()
        self._label(rows, f"{ip}  ·  port {core.PORT}" if ip else "No network", 10, TEXT, MONO).pack(anchor="w")

        buttons = tk.Frame(self.root, bg=GROUND)
        buttons.pack(fill="x", pady=(18, 6), **pad)
        self._button(buttons, "Quit", self.quit).pack(side="right")
        self.hide_button = self._button(buttons, "Hide to tray", self.hide)
        self._button(buttons, "New PIN", self.new_pin).pack(side="left")

        self.footer = self._label(
            self.root, "Keep the agent running while you use touch control.", 8, MUTED, wraplength=360, justify="left",
        )
        self.footer.pack(anchor="w", pady=(4, 16), **pad)

    # ---- agent lifecycle -----------------------------------------------------------

    def start(self):
        try:
            self.touch, output_name = core.open_touch(self.args)
        except (core.AgentError, OSError) as e:
            messagebox.showerror("Layar HDMI Agent", str(e))
            self.root.destroy()
            return False
        self.agent = core.Agent(self.touch, core.load_pin(), output_name)
        self.agent.on_event = lambda kind, detail: self.events.put((kind, detail))
        self.pin_label.configure(text=self.agent.pin)
        self._fill_monitors(output_name)
        self._set_status("Waiting for the phone…", ACCENT)

        def run():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            try:
                self.loop.run_until_complete(core.serve(self.agent))
            except asyncio.CancelledError:
                pass
            except core.AgentError as e:
                self.events.put(("error", str(e)))
            finally:
                self.loop.close()

        self.thread = threading.Thread(target=run, name="agent-network", daemon=True)
        self.thread.start()
        self._start_tray()
        self.root.after(200, self._drain)
        return True

    def _fill_monitors(self, current):
        menu = self.monitor_menu["menu"]
        menu.delete(0, "end")
        if core.WINDOWS:
            import layar_agent_windows as win
            self.monitors = win.monitors()
            for m in self.monitors:
                menu.add_command(label=m.label(), command=lambda m=m: self._pick_monitor(m))
            chosen = next((m for m in self.monitors if m.name == current), None)
            self.monitor_var.set(chosen.label() if chosen else "—")
        else:
            # Linux maps the virtual touchscreen in the desktop settings (or COSMIC config).
            self.monitor_var.set(current or "Set in the desktop's touchscreen settings")
            self.monitor_menu.configure(state="disabled")

    def _pick_monitor(self, m):
        self.touch.set_monitor(m)
        self.agent.output_name = m.name
        self.monitor_var.set(m.label())

    def _set_status(self, text, color):
        self.status_label.configure(text=text)
        self.dot.itemconfigure(self.dot_id, fill=color)
        if self.tray is not None:
            self.tray.title = f"Layar HDMI Agent · {text}"

    def _drain(self):
        try:
            while True:
                kind, detail = self.events.get_nowait()
                if kind == "connected":
                    self._set_status(f"Connected to {detail}", OK)
                elif kind == "disconnected":
                    self._set_status("Waiting for the phone…", ACCENT)
                elif kind == "wrong_pin":
                    self._set_status(f"Wrong PIN from {detail}", REC)
                elif kind == "error":
                    self._set_status(detail, REC)
                    self.show()
                    messagebox.showerror("Layar HDMI Agent", detail)
                elif kind == "show":
                    self.show()
                elif kind == "quit":
                    self.quit()
                    return
        except queue.Empty:
            pass
        self.root.after(200, self._drain)

    def new_pin(self):
        if not messagebox.askyesno(
            "New PIN", "Make a new PIN? Phones that paired before will have to enter it again.", parent=self.root,
        ):
            return
        self.agent.pin = core.load_pin(new=True)
        self.pin_label.configure(text=self.agent.pin)

    # ---- tray ----------------------------------------------------------------------------

    def _start_tray(self):
        try:
            import pystray
            from PIL import Image
        except ImportError:
            return
        try:
            image = Image.open(resource("icon.png"))
            menu = pystray.Menu(
                pystray.MenuItem("Show", lambda: self.events.put(("show", None)), default=True),
                pystray.MenuItem("Quit", lambda: self.events.put(("quit", None))),
            )
            self.tray = pystray.Icon("layar-hdmi-agent", image, "Layar HDMI Agent", menu)
            self.tray.run_detached()
        except Exception as e:  # no tray on this desktop (e.g. some Wayland sessions)
            print(f"[agent] no tray icon: {e}")
            self.tray = None
            return
        self.hide_button.pack(side="left", padx=8)
        self.footer.configure(text="Closing this window keeps the agent running in the tray. Use Quit to stop it.")

    def hide(self):
        if self.tray is not None:
            self.root.withdraw()

    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def on_close(self):
        if self.tray is not None:
            self.hide()
        else:
            self.quit()

    def quit(self):
        if self.loop is not None and self.loop.is_running():
            def cancel_all():
                for task in asyncio.all_tasks(self.loop):
                    task.cancel()
            self.loop.call_soon_threadsafe(cancel_all)
            self.thread.join(timeout=2)
        if self.touch is not None:
            self.touch.close()
            self.touch = None
        if self.tray is not None:
            self.tray.stop()
            self.tray = None
        self.root.destroy()


def run(args):
    window = AgentWindow(args)
    if window.start():
        try:
            window.root.mainloop()
        except KeyboardInterrupt:
            window.quit()
