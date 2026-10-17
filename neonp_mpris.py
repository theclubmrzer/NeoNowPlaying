"""Minimal MPRIS client based on Gio (no dbus-python needed) - works inside Flatpak."""
import threading
import time

import gi
from gi.repository import Gio, GLib

PREFIX = "org.mpris.MediaPlayer2."
IFACE_PLAYER = "org.mpris.MediaPlayer2.Player"
IFACE_ROOT = "org.mpris.MediaPlayer2"
PATH = "/org/mpris/MediaPlayer2"

try:
    from loguru import logger as log
except Exception:  # pragma: no cover
    import logging
    log = logging.getLogger("neonp")


class MprisClient:
    def __init__(self, poll_interval=1.0):
        self._interval = poll_interval
        self._lock = threading.Lock()
        self._players = {}
        self._labels = {}
        self._thread = None
        self._stop = threading.Event()
        self._conn = None
        self._last_playing = {}

    # ---------- lifecycle ----------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="neonp-mpris", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                self._poll()
            except Exception as e:
                log.warning(f"[NeoNowPlaying] MPRIS poll error: {e}")
                self._conn = None
            self._stop.wait(self._interval)

    # ---------- D-Bus ----------
    def _connection(self):
        if self._conn is None:
            self._conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        return self._conn

    def _call(self, name, path, iface, method, params, reply, timeout=1500):
        return self._connection().call_sync(
            name, path, iface, method, params,
            GLib.VariantType(reply) if reply else None,
            Gio.DBusCallFlags.NONE, timeout, None)

    def _poll(self):
        res = self._call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                         "ListNames", None, "(as)")
        names = [n for n in res.unpack()[0] if n.startswith(PREFIX)]
        found = {}
        for name in names:
            try:
                props = self._call(name, PATH, "org.freedesktop.DBus.Properties", "GetAll",
                                   GLib.Variant("(s)", (IFACE_PLAYER,)), "(a{sv})").unpack()[0]
            except Exception:
                continue
            md = props.get("Metadata", {}) or {}
            artist = md.get("xesam:artist", "")
            if isinstance(artist, (list, tuple)):
                artist = ", ".join(str(a) for a in artist)
            length = md.get("mpris:length", 0) or 0
            try:
                pos = props.get("Position")
                if pos is None:
                    pos = self._call(name, PATH, "org.freedesktop.DBus.Properties", "Get",
                                     GLib.Variant("(ss)", (IFACE_PLAYER, "Position")),
                                     "(v)").unpack()[0]
            except Exception:
                pos = 0
            if name not in self._labels:
                label = name[len(PREFIX):]
                try:
                    ident = self._call(name, PATH, "org.freedesktop.DBus.Properties", "Get",
                                       GLib.Variant("(ss)", (IFACE_ROOT, "Identity")),
                                       "(v)").unpack()[0]
                    label = f"{ident} ({label})"
                except Exception:
                    pass
                self._labels[name] = label
            status = props.get("PlaybackStatus", "Stopped")
            if status == "Playing":
                self._last_playing[name] = time.monotonic()
            found[name] = {
                "bus": name,
                "label": self._labels[name],
                "status": status,
                "title": str(md.get("xesam:title", "") or ""),
                "artist": str(artist or ""),
                "album": str(md.get("xesam:album", "") or ""),
                "length": float(length) / 1e6,
                "art_url": str(md.get("mpris:artUrl", "") or ""),
                "track_id": str(md.get("mpris:trackid", "") or ""),
                "pos": float(pos or 0) / 1e6,
                "rate": float(props.get("Rate", 1.0) or 1.0),
                "sample_t": time.monotonic(),
            }
        with self._lock:
            self._players = found
            for k in list(self._labels):
                if k not in found:
                    self._labels.pop(k, None)

    # ---------- API for the action ----------
    def labels(self):
        """[(bus_name, label)] for the player selector in the config UI."""
        with self._lock:
            return [(b, p["label"]) for b, p in self._players.items()]

    def snapshot(self, preferred=""):
        with self._lock:
            players = list(self._players.values())
        if preferred:
            pl = preferred.lower()
            players = [p for p in players if pl in p["bus"].lower()]
        if not players:
            return None

        def rank(p):
            has_title = 1 if p["title"] else 0
            st = {"Playing": 2, "Paused": 1}.get(p["status"], 0)
            url = p["art_url"]
            # cover art that can be fetched from outside the sandbox (https/data) beats a file in /tmp
            remote_art = 2 if url.startswith(("http://", "https://", "data:")) else (1 if url else 0)
            # (plasma-browser-integration / some players expose https instead of a file)
            return (has_title, st, remote_art, self._last_playing.get(p["bus"], 0))

        best = max(players, key=rank)
        if not best["title"]:
            return None
        out = dict(best)
        if out["status"] == "Playing":
            out["pos"] = out["pos"] + (time.monotonic() - out["sample_t"]) * out["rate"]
        if out["length"] > 0:
            out["pos"] = min(out["pos"], out["length"])
        out["position"] = out["pos"]
        return out

    def command(self, bus, method):
        try:
            self._call(bus, PATH, IFACE_PLAYER, method, None, None)
        except Exception as e:
            log.warning(f"[NeoNowPlaying] {method} failed: {e}")
