#!/usr/bin/env python3
"""DisplayGuard Screen Dim daemon.

Dim-on-inactivity for GNOME (Wayland/Mutter). After a configurable idle period
(detected via GNOME's Mutter IdleMonitor, which covers keyboard/mouse/
controller) the whole screen smoothly dims; any input restores it.

It dims by scaling the display's gamma ramp via
org.gnome.Mutter.DisplayConfig.SetCrtcGamma -- the same software method
gammastep / redshift / f.lux / GNOME Night Light use. This is NOT a hardware
backlight or DPMS change (those crash this PS5's HDMI pipeline); it only
reshapes the color LUT the compositor applies before scanout. Works on stock
Ubuntu GNOME Wayland with no extra packages.

A translucent overlay *window* was tried first and does NOT work here: Mutter
won't blend a normal window's alpha over other windows. Gamma is the right tool.

Config (live-reloaded on change): ~/.config/displayguard/dim.conf, INI-style:
    [displayguard]
    enabled = true         ; stage 1 (dim) on/off
    darkness = 0.70        ; 0.0 = no dim, 0.99 = darkest allowed (never full black)
    idle_seconds = 600     ; inactivity before dimming
    sleep_enabled = true   ; stage 2 (deeper "screen saver" dim) on/off
    sleep_darkness = 0.99
    sleep_seconds = 1800   ; inactivity before the deeper dim

Live preview from the GUI (while dragging the darkness slider), via this
daemon's session-bus object org.displayguard.Dim /org/displayguard/Dim:
    Preview(d level)   -> dim to `level` immediately (ignores idle)
    PreviewEnd()       -> leave preview, restore brightness, resume idle logic
    Reload()           -> re-read the config file now
Anything on the session bus can call these, so Preview() is failsafed: it
auto-expires after PREVIEW_TIMEOUT_MS without a PreviewEnd(), restoring
brightness — a buggy or hostile caller can't wedge the screen near-black.
"""
import configparser
import os

import gi  # noqa: F401  (kept for consistent GI init across DisplayGuard)
from gi.repository import Gio, GLib

CONFIG_DIR = os.path.join(GLib.get_user_config_dir(), "displayguard")
CONFIG_PATH = os.path.join(CONFIG_DIR, "dim.conf")

IDLE_MONITOR = ("org.gnome.Mutter.IdleMonitor",
                "/org/gnome/Mutter/IdleMonitor/Core",
                "org.gnome.Mutter.IdleMonitor")
DISPLAY_CONFIG = ("org.gnome.Mutter.DisplayConfig",
                  "/org/gnome/Mutter/DisplayConfig",
                  "org.gnome.Mutter.DisplayConfig")

DBUS_NAME = "org.displayguard.Dim"
DBUS_PATH = "/org/displayguard/Dim"
DBUS_XML = """
<node>
  <interface name='org.displayguard.Dim'>
    <method name='Preview'><arg type='d' name='level' direction='in'/></method>
    <method name='PreviewEnd'/>
    <method name='Reload'/>
  </interface>
</node>
"""

GAMMA_SIZE = 256       # ramp entries per channel
FADE_MS = 600
FADE_STEP_MS = 16
# Failsafe hard cap: even a hand-edited config or a stray Preview()/sleep value
# can never dim past this, so the screen never goes fully black (you can always
# see enough to move the mouse and undim). This is what keeps "sleep" = 0.99
# safe — it is near-black but the HDMI link stays fully powered (no DPMS).
MAX_DARKNESS = 0.99
# A Preview() that never gets its PreviewEnd() expires after this long. The
# D-Bus interface is reachable by any same-user process, and while previewing
# the idle/activity restore logic is suspended — without this cap one stray
# call could leave the screen stuck near-black.
PREVIEW_TIMEOUT_MS = 30000


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


class DimConfig:
    def __init__(self):
        # Stage 1 — "night mode" replacement: dim after a short idle.
        self.enabled = True
        self.darkness = 0.70
        self.idle_seconds = 600
        # Stage 2 — "sleep" replacement: dim deeper after a longer idle. Still a
        # gamma dim (never DPMS power-off), so it can't crash the HDMI chip.
        self.sleep_enabled = True
        self.sleep_darkness = 0.99
        self.sleep_seconds = 1800

    def load(self):
        cp = configparser.ConfigParser()
        try:
            cp.read(CONFIG_PATH)
            if cp.has_section("displayguard"):
                self.enabled = cp.getboolean("displayguard", "enabled", fallback=self.enabled)
                self.darkness = clamp(
                    cp.getfloat("displayguard", "darkness", fallback=self.darkness), 0.0, MAX_DARKNESS)
                self.idle_seconds = max(
                    5, cp.getint("displayguard", "idle_seconds", fallback=self.idle_seconds))
                self.sleep_enabled = cp.getboolean(
                    "displayguard", "sleep_enabled", fallback=self.sleep_enabled)
                self.sleep_darkness = clamp(
                    cp.getfloat("displayguard", "sleep_darkness", fallback=self.sleep_darkness),
                    0.0, MAX_DARKNESS)
                self.sleep_seconds = max(
                    self.idle_seconds + 5,
                    cp.getint("displayguard", "sleep_seconds", fallback=self.sleep_seconds))
        except Exception as e:
            print("dim: config read error:", e, flush=True)
        return self


class DimDaemon:
    def __init__(self):
        self.cfg = DimConfig().load()
        # brightness: 1.0 = normal, (1 - darkness) = fully dimmed.
        self.brightness = 1.0
        self._fade_source = None
        self._idle_watch_id = None
        self._sleep_watch_id = None
        self._active_watch_id = None
        self._subscribed = False
        self._previewing = False
        self._preview_timeout = None

        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.crtcs = self._discover_crtcs()
        print("dim: driving CRTCs:", self.crtcs, flush=True)

        # Capture each CRTC's *current* gamma ramp as the brightness=1.0
        # baseline. This is the ICC-/colord-corrected ("white") ramp that
        # gnome-settings-daemon's color plugin maintains. We dim by scaling
        # THIS per-channel ramp, never by writing a synthetic flat linear
        # ramp — otherwise we wipe the colour correction (panel goes yellow)
        # and gsd-color fights us back to white, causing the yellow<->white
        # flicker. Scaling the corrected ramp keeps colour neutral and gives
        # gsd-color nothing to reassert.
        self._baseline = {}      # crtc_id -> (red[], green[], blue[]) at b=1.0
        self._capture_baseline()

        self._export_dbus()
        self._rearm_idle()

        try:
            gf = Gio.File.new_for_path(CONFIG_PATH)
            self._monitor = gf.monitor_file(Gio.FileMonitorFlags.NONE, None)
            self._monitor.connect("changed", lambda *_: self._reload())
        except Exception as e:
            print("dim: file monitor failed:", e, flush=True)

    # --- display / gamma ---------------------------------------------------
    def _serial(self):
        r = self.bus.call_sync(*DISPLAY_CONFIG, "GetResources", None, None,
                               Gio.DBusCallFlags.NONE, -1, None)
        return r.unpack()[0]

    def _discover_crtcs(self):
        """Return the CRTC ids that have a mode set (active outputs)."""
        try:
            r = self.bus.call_sync(*DISPLAY_CONFIG, "GetResources", None, None,
                                   Gio.DBusCallFlags.NONE, -1, None)
            data = r.unpack()
            crtcs = data[1]   # a(uxiiiiiuaua{sv})
            active = []
            for c in crtcs:
                crtc_id = c[0]
                current_mode = c[6]   # -1 when no mode (disabled)
                if current_mode is not None and current_mode != -1:
                    active.append(crtc_id)
            return active or [0]
        except Exception as e:
            print("dim: CRTC discovery failed, defaulting to [0]:", e, flush=True)
            return [0]

    def _linear_ramp(self):
        """A neutral full-range linear ramp (fallback baseline)."""
        return [min(65535, int((i / (GAMMA_SIZE - 1)) * 65535))
                for i in range(GAMMA_SIZE)]

    def _capture_baseline(self):
        """Snapshot each CRTC's current per-channel gamma as the b=1.0 base.

        Falls back to a neutral linear ramp if the read fails or the panel
        returns an all-zero/empty ramp (which would otherwise make the
        screen stay black). We only trust a ramp whose top entry is bright.
        """
        try:
            serial = self._serial()
        except Exception as e:
            print("dim: baseline serial read failed:", e, flush=True)
            serial = None
        for crtc in self.crtcs:
            base = None
            if serial is not None:
                try:
                    g = self.bus.call_sync(
                        *DISPLAY_CONFIG, "GetCrtcGamma",
                        GLib.Variant("(uu)", (serial, crtc)), None,
                        Gio.DBusCallFlags.NONE, -1, None)
                    red, green, blue = g.unpack()
                    red, green, blue = list(red), list(green), list(blue)
                    # Sanity: right length and not an all-dark ramp.
                    if (len(red) == GAMMA_SIZE and max(red) > 1000
                            and len(green) == GAMMA_SIZE
                            and len(blue) == GAMMA_SIZE):
                        base = (red, green, blue)
                except Exception as e:
                    print("dim: GetCrtcGamma(crtc={}) failed: {}".format(
                        crtc, e), flush=True)
            if base is None:
                lin = self._linear_ramp()
                base = (lin[:], lin[:], lin[:])
                print("dim: crtc {} baseline -> neutral linear "
                      "(no usable ICC ramp)".format(crtc), flush=True)
            self._baseline[crtc] = base

    def _apply_brightness(self, b):
        b = clamp(b, 1.0 - MAX_DARKNESS, 1.0)
        self.brightness = b
        try:
            serial = self._serial()
            for crtc in self.crtcs:
                base = self._baseline.get(crtc)
                if base is None:
                    lin = self._linear_ramp()
                    base = (lin, lin, lin)
                # Scale the COLOUR-CORRECTED baseline by brightness, per
                # channel, so the hue is preserved (no yellow shift).
                red = [min(65535, int(v * b)) for v in base[0]]
                green = [min(65535, int(v * b)) for v in base[1]]
                blue = [min(65535, int(v * b)) for v in base[2]]
                args = GLib.Variant("(uuaqaqaq)",
                                    (serial, crtc, red, green, blue))
                self.bus.call_sync(*DISPLAY_CONFIG, "SetCrtcGamma", args, None,
                                   Gio.DBusCallFlags.NONE, -1, None)
        except Exception as e:
            print("dim: SetCrtcGamma failed:", e, flush=True)

    # --- D-Bus (live preview from the GUI) ---------------------------------
    def _export_dbus(self):
        node = Gio.DBusNodeInfo.new_for_xml(DBUS_XML)
        self._iface = node.interfaces[0]
        Gio.bus_own_name_on_connection(
            self.bus, DBUS_NAME, Gio.BusNameOwnerFlags.NONE, None, None)
        self.bus.register_object(
            DBUS_PATH, self._iface, self._dbus_call, None, None)

    def _dbus_call(self, conn, sender, path, iface, method, params, inv):
        if method == "Preview":
            level = clamp(params.unpack()[0], 0.0, MAX_DARKNESS)
            self._previewing = True
            self._arm_preview_timeout()
            self._fade_to(1.0 - level)
        elif method == "PreviewEnd":
            self._end_preview()
        elif method == "Reload":
            self._reload()
        inv.return_value(None)

    def _arm_preview_timeout(self):
        self._cancel_preview_timeout()
        self._preview_timeout = GLib.timeout_add(
            PREVIEW_TIMEOUT_MS, self._on_preview_expired)

    def _cancel_preview_timeout(self):
        if self._preview_timeout:
            GLib.source_remove(self._preview_timeout)
            self._preview_timeout = None

    def _end_preview(self):
        self._cancel_preview_timeout()
        self._previewing = False
        self._fade_to(1.0)

    def _on_preview_expired(self):
        print("dim: preview expired without PreviewEnd(), restoring",
              flush=True)
        self._preview_timeout = None
        self._end_preview()
        return False

    # --- config reload -----------------------------------------------------
    def _reload(self):
        old = (self.cfg.idle_seconds, self.cfg.sleep_seconds,
               self.cfg.sleep_enabled, self.cfg.enabled)
        self.cfg = DimConfig().load()
        if not self.cfg.enabled and not self.cfg.sleep_enabled:
            self._fade_to(1.0)
        new = (self.cfg.idle_seconds, self.cfg.sleep_seconds,
               self.cfg.sleep_enabled, self.cfg.enabled)
        if new != old:
            self._rearm_idle()

    # --- idle monitor wiring ----------------------------------------------
    def _add_idle_watch(self, seconds):
        res = self.bus.call_sync(
            IDLE_MONITOR[0], IDLE_MONITOR[1], IDLE_MONITOR[2], "AddIdleWatch",
            GLib.Variant("(t)", (int(seconds) * 1000,)),
            GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, -1, None)
        return res.unpack()[0]

    def _rearm_idle(self):
        # Drop any prior watches so changed timings take effect cleanly.
        # The two stages are independent: either may be enabled on its own
        # (e.g. dim = "Never" in the GUI but the screen saver still on).
        self._idle_watch_id = None
        self._sleep_watch_id = None
        if not self.cfg.enabled and not self.cfg.sleep_enabled:
            return
        # Stage 1: dim after idle_seconds.
        if self.cfg.enabled:
            self._idle_watch_id = self._add_idle_watch(self.cfg.idle_seconds)
        # Stage 2: deeper "sleep" dim after sleep_seconds.
        if self.cfg.sleep_enabled:
            self._sleep_watch_id = self._add_idle_watch(self.cfg.sleep_seconds)
        if not self._subscribed:
            self.bus.signal_subscribe(
                IDLE_MONITOR[0], IDLE_MONITOR[2], "WatchFired", IDLE_MONITOR[1],
                None, Gio.DBusSignalFlags.NONE, self._on_watch_fired)
            self._subscribed = True

    def _on_watch_fired(self, conn, sender, path, iface, signal, params):
        fired = params.unpack()[0]
        if self._previewing:
            return
        if fired == self._idle_watch_id:
            # Stage 1 — night-mode dim.
            if self.cfg.enabled:
                self._fade_to(1.0 - self.cfg.darkness)
                self._arm_active()
        elif fired == self._sleep_watch_id:
            # Stage 2 — sleep (deeper dim). Still gamma, never DPMS/power-off.
            if self.cfg.sleep_enabled:
                self._fade_to(1.0 - self.cfg.sleep_darkness)
                self._arm_active()
        elif fired == self._active_watch_id:
            self._active_watch_id = None
            self._fade_to(1.0)
            self._rearm_idle()

    def _arm_active(self):
        res = self.bus.call_sync(
            IDLE_MONITOR[0], IDLE_MONITOR[1], IDLE_MONITOR[2],
            "AddUserActiveWatch", GLib.Variant("()", ()),
            GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, -1, None)
        self._active_watch_id = res.unpack()[0]

    # --- fading ------------------------------------------------------------
    def _fade_to(self, target):
        target = clamp(target, 1.0 - MAX_DARKNESS, 1.0)
        if self._fade_source:
            GLib.source_remove(self._fade_source)
            self._fade_source = None
        steps = max(1, FADE_MS // FADE_STEP_MS)
        delta = (target - self.brightness) / steps
        if abs(delta) < 1e-4:
            self._apply_brightness(target)
            return

        def tick():
            new = self.brightness + delta
            done = (delta >= 0 and new >= target) or (delta < 0 and new <= target)
            if done:
                self._apply_brightness(target)
                self._fade_source = None
                return False
            self._apply_brightness(new)
            return True

        self._fade_source = GLib.timeout_add(FADE_STEP_MS, tick)


def main():
    daemon = DimDaemon()
    loop = GLib.MainLoop()

    def restore_and_quit(*_):
        # Always hand the screen back at full brightness on shutdown, so a
        # stopped/killed daemon never leaves the display stuck dim.
        try:
            daemon._apply_brightness(1.0)
        except Exception:
            pass
        loop.quit()
        return False

    # Restore on SIGTERM (systemctl stop) and SIGINT (Ctrl-C).
    import signal
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, restore_and_quit)
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, restore_and_quit)

    loop.run()


if __name__ == "__main__":
    main()
