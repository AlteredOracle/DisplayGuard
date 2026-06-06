#!/usr/bin/env python3

import configparser
import os
import subprocess
import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib
GLib.set_prgname("displayguard")

APP_NAME = "DisplayGuard"
CONFIG = os.path.expanduser("~/.config/displayguard/dim.conf")
SERVICE = "displayguard.service"


def run(cmd):
    subprocess.run(cmd, shell=True)


class DisplayGuard(Gtk.Window):
    def __init__(self):
        super().__init__(title=APP_NAME)

        self.set_default_size(620, 560)
        self.set_size_request(480, 420)
        self.set_resizable(True)

        self.config = self.load_config()

        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        main.set_border_width(28)
        self.add(main)

        title = Gtk.Label()
        title.set_markup("<span size='xx-large' weight='bold'>DisplayGuard</span>")
        main.pack_start(title, False, False, 5)

        subtitle = Gtk.Label(label="Simple screen protection for Linux")
        main.pack_start(subtitle, False, False, 0)

        self.status_label = Gtk.Label()
        self.status_label.set_xalign(0)
        main.pack_start(self.status_label, False, False, 5)

        self.enabled = Gtk.Switch()
        self.enabled.set_active(
            self.config.getboolean("enabled", fallback=False)
            or self.config.getboolean("sleep_enabled", fallback=False)
        )

        enable_box = Gtk.Box(spacing=12)
        enable_label = Gtk.Label(label="Enable Screen Protection", xalign=0)
        enable_box.pack_start(enable_label, True, True, 0)
        enable_box.pack_end(self.enabled, False, False, 0)
        main.pack_start(enable_box, False, False, 12)

        main.pack_start(Gtk.Label(label="Dim screen after", xalign=0), False, False, 0)

        self.dim_combo = Gtk.ComboBoxText()
        for item in ["1 minute", "5 minutes", "10 minutes", "15 minutes", "30 minutes", "Never"]:
            self.dim_combo.append_text(item)
        self.dim_combo.set_active(self.get_dim_index())
        main.pack_start(self.dim_combo, False, False, 0)

        main.pack_start(Gtk.Label(label="Screen saver after", xalign=0), False, False, 0)

        self.sleep_combo = Gtk.ComboBoxText()
        for item in ["5 minutes", "10 minutes", "30 minutes", "1 hour", "Never"]:
            self.sleep_combo.append_text(item)
        self.sleep_combo.set_active(self.get_sleep_index())
        main.pack_start(self.sleep_combo, False, False, 0)

        main.pack_start(Gtk.Label(label="Protection strength", xalign=0), False, False, 0)

        self.strength_combo = Gtk.ComboBoxText()
        for item in ["Light", "Medium", "Strong"]:
            self.strength_combo.append_text(item)
        self.strength_combo.set_active(self.get_strength_index())
        main.pack_start(self.strength_combo, False, False, 0)

        save_btn = Gtk.Button(label="Save")
        save_btn.connect("clicked", self.save_config)
        main.pack_start(save_btn, False, False, 12)

        self.refresh_status()

    def load_config(self):
        os.makedirs(os.path.dirname(CONFIG), exist_ok=True)

        cfg = configparser.ConfigParser()
        cfg.read(CONFIG)

        if "displayguard" not in cfg:
            cfg["displayguard"] = {
                "enabled": "false",
                "darkness": "0.70",
                "idle_seconds": "600",
                "sleep_enabled": "false",
                "sleep_darkness": "0.99",
                "sleep_seconds": "1800",
            }

            with open(CONFIG, "w") as f:
                cfg.write(f)

        return cfg["displayguard"]

    def refresh_status(self):
        result = subprocess.run(
            "systemctl --user is-active displayguard.service",
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True
        )

        status = result.stdout.strip()

        if status == "active":
            self.status_label.set_markup(
                "<span foreground='green'><b>Status:</b> Running</span>"
            )
        else:
            self.status_label.set_markup(
                "<span foreground='red'><b>Status:</b> Stopped</span>"
            )

    def get_dim_index(self):
        if not self.config.getboolean("enabled", fallback=False):
            return 5

        value = self.config.getint("idle_seconds", fallback=600)

        mapping = {
            60: 0,
            300: 1,
            600: 2,
            900: 3,
            1800: 4,
        }

        return mapping.get(value, 2)

    def get_sleep_index(self):
        if not self.config.getboolean("sleep_enabled", fallback=False):
            return 4

        value = self.config.getint("sleep_seconds", fallback=1800)

        mapping = {
            300: 0,
            600: 1,
            1800: 2,
            3600: 3,
        }

        return mapping.get(value, 2)

    def get_strength_index(self):
        value = self.config.getfloat("darkness", fallback=0.70)

        if value <= 0.30:
            return 0
        elif value <= 0.70:
            return 1
        else:
            return 2

    def save_config(self, button):
        dim_map = {
            "1 minute": 60,
            "5 minutes": 300,
            "10 minutes": 600,
            "15 minutes": 900,
            "30 minutes": 1800,
            "Never": 600,
        }

        sleep_map = {
            "5 minutes": 300,
            "10 minutes": 600,
            "30 minutes": 1800,
            "1 hour": 3600,
            "Never": 1800,
        }

        strength_map = {
            "Light": 0.30,
            "Medium": 0.70,
            "Strong": 0.90,
        }

        main_enabled = self.enabled.get_active()

        dim_choice = self.dim_combo.get_active_text()
        sleep_choice = self.sleep_combo.get_active_text()
        strength_choice = self.strength_combo.get_active_text()

        dim_enabled = main_enabled and dim_choice != "Never"
        sleep_enabled = main_enabled and sleep_choice != "Never"

        if not dim_enabled and not sleep_enabled:
            main_enabled = False

        cfg = configparser.ConfigParser()
        cfg["displayguard"] = {
            "enabled": str(dim_enabled).lower(),
            "darkness": str(strength_map.get(strength_choice, 0.70)),
            "idle_seconds": str(dim_map.get(dim_choice, 600)),
            "sleep_enabled": str(sleep_enabled).lower(),
            "sleep_darkness": "0.99",
            "sleep_seconds": str(sleep_map.get(sleep_choice, 1800)),
        }

        with open(CONFIG, "w") as f:
            cfg.write(f)

        if main_enabled:
            run(f"systemctl --user enable --now {SERVICE}")
            run(f"systemctl --user restart {SERVICE}")
        else:
            run(f"systemctl --user disable --now {SERVICE}")
            self.enabled.set_active(False)

        self.config = self.load_config()
        self.refresh_status()
        self.message("Settings saved.")

    def message(self, text):
        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
            text=text,
        )
        dialog.run()
        dialog.destroy()


win = DisplayGuard()
win.connect("destroy", Gtk.main_quit)
win.show_all()
Gtk.main()
