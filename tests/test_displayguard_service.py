#!/usr/bin/env python3
"""Unit tests for displayguard_service.py.

PyGObject and a live GNOME/Mutter session are not required: a fake `gi`
package is injected into sys.modules before the daemon is imported, with
just enough of GLib/Gio for the daemon's config, idle-watch, fade and
gamma logic to run. D-Bus traffic is recorded on a FakeBus so tests can
assert what the daemon asked Mutter to do.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import types
import unittest

# --- fake gi / GLib / Gio ---------------------------------------------------

class FakeVariant:
    def __init__(self, fmt, value=None):
        self.fmt = fmt
        self.value = value

    def unpack(self):
        return self.value


class FakeVariantType:
    def __init__(self, fmt):
        self.fmt = fmt


class FakeGLibState:
    def __init__(self):
        self.timeouts = {}   # source id -> callback
        self.next_id = 1


_glib_state = FakeGLibState()


def _timeout_add(interval_ms, fn):
    _glib_state.next_id += 1
    _glib_state.timeouts[_glib_state.next_id] = fn
    return _glib_state.next_id


def _source_remove(source_id):
    _glib_state.timeouts.pop(source_id, None)
    return True


def flush_timeouts(max_iter=100000):
    """Run scheduled timeout callbacks until none remain (fades settle)."""
    for _ in range(max_iter):
        if not _glib_state.timeouts:
            return
        tid, fn = next(iter(_glib_state.timeouts.items()))
        if not fn():
            _glib_state.timeouts.pop(tid, None)
    raise AssertionError("timeout sources did not settle")


class FakeReply:
    def __init__(self, value):
        self.value = value

    def unpack(self):
        return self.value


class FakeInvocation:
    def __init__(self):
        self.returned = False

    def return_value(self, _):
        self.returned = True


class FakeBus:
    """Records the daemon's Mutter D-Bus calls and lets tests fire signals."""

    SERIAL = 7

    def __init__(self):
        self.idle_watches = []        # (watch_id, timeout_ms)
        self.active_watch_ids = []
        self.set_gamma_calls = []     # (serial, crtc, red, green, blue)
        self.signal_cb = None
        self.dbus_method_cb = None
        self._next_watch_id = 100

    def call_sync(self, name, path, iface, method, params,
                  reply_type, flags, timeout, cancellable):
        if method == "GetResources":
            # a(uxiiiiiuaua{sv}): index 0 = crtc id, index 6 = current mode.
            crtcs = [
                (0, 0, 0, 0, 0, 0, 0, 0, [], {}),    # active
                (1, 0, 0, 0, 0, 0, -1, 0, [], {}),   # disabled
            ]
            return FakeReply((self.SERIAL, crtcs))
        if method == "GetCrtcGamma":
            ramp = [int(i / 255 * 65535) for i in range(256)]
            return FakeReply((ramp, ramp, ramp))
        if method == "SetCrtcGamma":
            self.set_gamma_calls.append(params.unpack())
            return FakeReply(None)
        if method == "AddIdleWatch":
            self._next_watch_id += 1
            self.idle_watches.append((self._next_watch_id, params.unpack()[0]))
            return FakeReply((self._next_watch_id,))
        if method == "AddUserActiveWatch":
            self._next_watch_id += 1
            self.active_watch_ids.append(self._next_watch_id)
            return FakeReply((self._next_watch_id,))
        raise AssertionError("unexpected D-Bus method: " + method)

    def signal_subscribe(self, sender, iface, member, path,
                         arg0, flags, callback):
        self.signal_cb = callback
        return 1

    def register_object(self, path, iface, method_cb, getter, setter):
        self.dbus_method_cb = method_cb
        return 1

    def fire_watch(self, watch_id):
        self.signal_cb(None, None, None, None, "WatchFired",
                       FakeReply((watch_id,)))


_current_bus = FakeBus()


def _install_fake_gi(tmpdir):
    glib = types.ModuleType("GLib")
    glib.Variant = FakeVariant
    glib.VariantType = FakeVariantType
    glib.timeout_add = _timeout_add
    glib.source_remove = _source_remove
    glib.get_user_config_dir = lambda: tmpdir
    glib.set_prgname = lambda name: None
    glib.PRIORITY_HIGH = -100
    glib.unix_signal_add = lambda *a: 1
    glib.MainLoop = lambda: None

    class _File:
        @staticmethod
        def new_for_path(path):
            f = types.SimpleNamespace()
            f.monitor_file = lambda flags, c: types.SimpleNamespace(
                connect=lambda *a: None)
            return f

    class _DBusNodeInfo:
        @staticmethod
        def new_for_xml(xml):
            return types.SimpleNamespace(interfaces=[object()])

    gio = types.ModuleType("Gio")
    gio.BusType = types.SimpleNamespace(SESSION=2)
    gio.DBusCallFlags = types.SimpleNamespace(NONE=0)
    gio.DBusSignalFlags = types.SimpleNamespace(NONE=0)
    gio.BusNameOwnerFlags = types.SimpleNamespace(NONE=0)
    gio.FileMonitorFlags = types.SimpleNamespace(NONE=0)
    gio.bus_get_sync = lambda bus_type, cancellable: _current_bus
    gio.bus_own_name_on_connection = lambda *a: 1
    gio.File = _File
    gio.DBusNodeInfo = _DBusNodeInfo

    gi = types.ModuleType("gi")
    gi.require_version = lambda *a: None
    repository = types.ModuleType("gi.repository")
    repository.GLib = glib
    repository.Gio = gio
    gi.repository = repository

    sys.modules["gi"] = gi
    sys.modules["gi.repository"] = repository


_TMPDIR = tempfile.mkdtemp(prefix="displayguard-test-")
_install_fake_gi(_TMPDIR)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import displayguard_service as svc  # noqa: E402


def write_config(**kv):
    path = os.path.join(_TMPDIR, "dim.conf")
    with open(path, "w") as f:
        f.write("[displayguard]\n")
        for k, v in kv.items():
            f.write("{} = {}\n".format(k, v))
    svc.CONFIG_PATH = path
    return path


class ConfigTests(unittest.TestCase):
    def test_defaults_without_config_file(self):
        svc.CONFIG_PATH = os.path.join(_TMPDIR, "missing.conf")
        cfg = svc.DimConfig().load()
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.idle_seconds, 600)
        self.assertEqual(cfg.sleep_seconds, 1800)
        self.assertAlmostEqual(cfg.darkness, 0.70)
        self.assertAlmostEqual(cfg.sleep_darkness, 0.99)

    def test_reads_all_keys_from_displayguard_section(self):
        write_config(enabled="true", darkness="0.5", idle_seconds="120",
                     sleep_enabled="false", sleep_darkness="0.8",
                     sleep_seconds="900")
        cfg = svc.DimConfig().load()
        self.assertTrue(cfg.enabled)
        self.assertAlmostEqual(cfg.darkness, 0.5)
        self.assertEqual(cfg.idle_seconds, 120)
        # Regression: sleep_enabled used to be read from a nonexistent
        # [dim] section, so "false" here was silently ignored.
        self.assertFalse(cfg.sleep_enabled)
        self.assertAlmostEqual(cfg.sleep_darkness, 0.8)
        self.assertEqual(cfg.sleep_seconds, 900)

    def test_clamps_and_minimums(self):
        write_config(enabled="true", darkness="1.5", idle_seconds="1",
                     sleep_enabled="true", sleep_darkness="2.0",
                     sleep_seconds="2")
        cfg = svc.DimConfig().load()
        self.assertAlmostEqual(cfg.darkness, svc.MAX_DARKNESS)
        self.assertAlmostEqual(cfg.sleep_darkness, svc.MAX_DARKNESS)
        self.assertEqual(cfg.idle_seconds, 5)
        self.assertEqual(cfg.sleep_seconds, cfg.idle_seconds + 5)

    def test_example_config_parses(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        svc.CONFIG_PATH = os.path.join(repo_root, "dim.conf.example")
        cfg = svc.DimConfig().load()
        self.assertTrue(cfg.enabled)
        self.assertAlmostEqual(cfg.darkness, 0.9)
        self.assertEqual(cfg.idle_seconds, 600)
        self.assertTrue(cfg.sleep_enabled)
        self.assertEqual(cfg.sleep_seconds, 1800)


class DaemonTests(unittest.TestCase):
    def make_daemon(self, **config):
        global _current_bus
        _current_bus = FakeBus()
        _glib_state.timeouts.clear()
        write_config(**config)
        daemon = svc.DimDaemon()
        return daemon, _current_bus

    def test_both_stages_armed(self):
        _, bus = self.make_daemon(enabled="true", idle_seconds="600",
                                  sleep_enabled="true", sleep_seconds="1800")
        timeouts = sorted(ms for _, ms in bus.idle_watches)
        self.assertEqual(timeouts, [600 * 1000, 1800 * 1000])

    def test_only_active_crtcs_driven(self):
        daemon, _ = self.make_daemon(enabled="true")
        self.assertEqual(daemon.crtcs, [0])

    def test_sleep_only_mode_arms_sleep_watch(self):
        # Regression: dim "Never" (enabled=false) + screen saver on used to
        # disable everything; the sleep stage must still arm on its own.
        daemon, bus = self.make_daemon(enabled="false", sleep_enabled="true",
                                       sleep_seconds="1800")
        self.assertIsNone(daemon._idle_watch_id)
        self.assertIsNotNone(daemon._sleep_watch_id)
        self.assertEqual(bus.idle_watches[0][1], 1800 * 1000)

    def test_all_disabled_arms_nothing(self):
        daemon, bus = self.make_daemon(enabled="false", sleep_enabled="false")
        self.assertEqual(bus.idle_watches, [])
        self.assertIsNone(daemon._idle_watch_id)
        self.assertIsNone(daemon._sleep_watch_id)

    def test_idle_fire_dims_and_activity_restores(self):
        daemon, bus = self.make_daemon(enabled="true", darkness="0.70",
                                       idle_seconds="600",
                                       sleep_enabled="false")
        bus.fire_watch(daemon._idle_watch_id)
        flush_timeouts()
        self.assertAlmostEqual(daemon.brightness, 0.30)
        self.assertTrue(bus.set_gamma_calls)
        # Gamma ramps are the baseline scaled by brightness, on the active CRTC.
        serial, crtc, red, green, blue = bus.set_gamma_calls[-1]
        self.assertEqual(crtc, 0)
        self.assertEqual(red[-1], int(65535 * 0.30))
        # User activity restores full brightness.
        self.assertTrue(bus.active_watch_ids)
        bus.fire_watch(daemon._active_watch_id)
        flush_timeouts()
        self.assertAlmostEqual(daemon.brightness, 1.0)
        serial, crtc, red, green, blue = bus.set_gamma_calls[-1]
        self.assertEqual(red[-1], 65535)

    def test_sleep_fire_in_sleep_only_mode(self):
        daemon, bus = self.make_daemon(enabled="false", sleep_enabled="true",
                                       sleep_darkness="0.99",
                                       sleep_seconds="1800")
        bus.fire_watch(daemon._sleep_watch_id)
        flush_timeouts()
        self.assertAlmostEqual(daemon.brightness, 0.01)
        self.assertGreater(daemon.brightness, 0)  # never fully black
        self.assertTrue(bus.active_watch_ids)

    def test_preview_clamped_to_max_darkness(self):
        daemon, bus = self.make_daemon(enabled="true")
        inv = FakeInvocation()
        daemon._dbus_call(None, None, None, None, "Preview",
                          FakeReply((5.0,)), inv)
        flush_timeouts()
        self.assertTrue(inv.returned)
        self.assertAlmostEqual(daemon.brightness, 1.0 - svc.MAX_DARKNESS)
        daemon._dbus_call(None, None, None, None, "PreviewEnd",
                          FakeReply(()), FakeInvocation())
        flush_timeouts()
        self.assertAlmostEqual(daemon.brightness, 1.0)

    def test_reload_restores_only_when_both_stages_off(self):
        daemon, bus = self.make_daemon(enabled="true", darkness="0.70",
                                       idle_seconds="600",
                                       sleep_enabled="true",
                                       sleep_seconds="1800")
        bus.fire_watch(daemon._idle_watch_id)
        flush_timeouts()
        self.assertAlmostEqual(daemon.brightness, 0.30)
        # Turning dim off but keeping the screen saver must NOT force the
        # screen back to full brightness or drop the sleep watch.
        write_config(enabled="false", sleep_enabled="true",
                     sleep_seconds="1800")
        daemon._reload()
        flush_timeouts()
        self.assertIsNotNone(daemon._sleep_watch_id)
        # Turning both off restores full brightness.
        write_config(enabled="false", sleep_enabled="false")
        daemon._reload()
        flush_timeouts()
        self.assertAlmostEqual(daemon.brightness, 1.0)


if __name__ == "__main__":
    unittest.main()
