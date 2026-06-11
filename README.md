# DisplayGuard

DisplayGuard is a lightweight screen protection utility for Linux and PS5 Linux systems.

It helps prevent image retention and burn-in by automatically dimming the display after a period of inactivity and optionally entering a near-black screen saver mode.

## Features

* Automatic screen dimming after inactivity
* Optional screen saver mode
* GTK-based graphical interface
* Systemd user service integration
* Lightweight Python implementation
* Designed for Linux desktop environments
* Tested on PS5 Linux

## Screenshots

Add screenshots here.

## Installation

Clone the repository:

```bash
git clone https://github.com/AlteredOracle/DisplayGuard.git
cd DisplayGuard
```

Run the installer:

```bash
chmod +x install.sh
./install.sh
```

## Usage

Launch DisplayGuard from the Applications menu or run:

```bash
python3 ~/.local/share/displayguard/displayguard.py
```

Configure:

* Enable Screen Protection
* Dim screen timeout
* Screen saver timeout
* Protection strength

Click **Save** to apply settings.

## Configuration

DisplayGuard stores its configuration in:

```text
~/.config/displayguard/dim.conf
```

Example:

```ini
[displayguard]
enabled = true
darkness = 0.70
idle_seconds = 600
sleep_enabled = true
sleep_darkness = 0.99
sleep_seconds = 1800
```

## Service Management

Check status:

```bash
systemctl --user status displayguard.service
```

Restart:

```bash
systemctl --user restart displayguard.service
```

Disable:

```bash
systemctl --user disable --now displayguard.service
```

Enable:

```bash
systemctl --user enable --now displayguard.service
```

## Project Structure

```text
DisplayGuard/
├── displayguard.py
├── displayguard_service.py
├── displayguard.desktop
├── dim.conf.example
├── install.sh
├── tests/
│   ├── test_displayguard_service.py
│   └── test_install.sh
└── README.md
```

## Testing

The tests need no GNOME session or PyGObject (the D-Bus layer is faked), so they run anywhere:

```bash
python3 -m unittest discover -s tests -v   # daemon logic
bash tests/test_install.sh                 # installer (sandboxed $HOME)
```

## Requirements

* Python 3
* GTK 3 (PyGObject)
* systemd user services
* Linux desktop environment
