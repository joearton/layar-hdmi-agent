# Layar HDMI – PC agent

Turns touches on the phone into touches on this PC. The picture still comes
over HDMI through the capture card; only the touch events travel over the
local network.

Works on **Linux** and **Windows 10/11**. macOS is not supported yet.

## Windows

**Easiest:** download `LayarHDMIAgent.exe` from the
[latest release](https://github.com/joearton/layar-hdmi-agent/releases/latest)
and double-click it. Windows may show "Windows protected your PC" because the
file is not code-signed yet: click **More info → Run anyway**. Allow access on
private networks when the firewall asks. To pick another monitor, run
`LayarHDMIAgent.exe --output 2` from a terminal.

**From source:**

1. Install Python 3.10 or newer from https://www.python.org/downloads/ (tick
   "Add python.exe to PATH").
2. Put `layar_agent.py`, `layar_agent_windows.py` and `run-layar-agent.bat` in
   one folder and double-click `run-layar-agent.bat`.
3. When Windows Defender Firewall asks, allow access on **private networks**.
4. The window lists your monitors and picks the capture card's monitor by its
   name (for example `SUNTECH PVR`), or the non-primary one when there are
   exactly two. If it picks the wrong one, run
   `run-layar-agent.bat --output 2` with the right monitor number.

Touches are real Windows touch input (tap, drag, pinch, two-finger scroll),
limited to that monitor.

## Linux requirements

- Python 3.10+ (standard library only).
- Write access to `/dev/uinput`. Check with `ls -l /dev/uinput`. If it is not
  yours, install this udev rule once, then log out and back in:

  ```sh
  echo 'KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"' \
    | sudo tee /etc/udev/rules.d/70-layar-uinput.rules
  sudo udevadm control --reload && sudo udevadm trigger
  ```

- The phone and the PC on the same network. If a firewall is on, allow TCP
  47800 from the LAN (UFW example):

  ```sh
  sudo ufw allow from 192.168.0.0/16 to any port 47800 proto tcp
  ```

## Run (Linux)

```sh
python3 pc-agent/layar_agent.py
```

The agent prints a 6-digit **PIN**. Enter it once on the phone: open
**Settings › Kontrol sentuh PC** and pick the PC from the list, or type its IP
address. The PIN is stored in `~/.config/layar-agent/pin`; delete that
file to generate a new one.

## Which monitor receives the touches

The touches must land on the monitor that is connected to the capture card.

- **COSMIC:** the agent finds that monitor automatically from its EDID name
  (for example `SUNTECH PVR`) and writes `map_to_output` into
  `~/.config/cosmic/com.system76.CosmicComp/v1/input_devices`. A backup of the
  previous file is kept next to it with the suffix `.bak-layar`.
  Override the choice with `--output HDMI-A-2`, or use `--no-map` to leave the
  config untouched.
- **GNOME / KDE:** map the touchscreen named "Layar HDMI Touch" to that
  monitor in the desktop's touchscreen settings.

## Security

- Only a phone that knows the PIN can send touches.
- A wrong PIN closes the connection, and each attempt is slowed down to make
  guessing harder.
- Stop the agent (Ctrl+C) when you are not using it.

## License

MIT — see `LICENSE`.
