import hashlib
import os
import sys
import threading
import time

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from src.backend.PluginManager.ActionBase import ActionBase
from src.backend.PluginManager.ActionHolder import ActionHolder
from src.backend.PluginManager.PluginBase import PluginBase
from src.backend.PluginManager.ActionInputSupport import ActionInputSupport
from src.backend.DeckManagement.InputIdentifier import Input

try:
    from loguru import logger as log
except Exception:  # pragma: no cover
    import logging
    log = logging.getLogger("neonp")

# own modules (unique names so they don't collide with other plugins)
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import neonp_render  # noqa: E402
import neonp_mpris   # noqa: E402
import neonp_art     # noqa: E402

DEFAULTS = {
    "player": "",            # empty = automatic
    "accent_mode": "auto",   # auto (from cover art) | fixed
    "accent_hex": "#ff4fa3",
    "show_eq": True,
    "show_times": True,
    "fps": 6,
    "margin_l": 12,
    "margin_r": 14,
    "debug_dump": True,      # writes debug/last_frame.png and debug/status.txt
}


class NowPlaying(ActionBase):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.has_configuration = True
        self._thread = None
        self._stop = threading.Event()
        self._renderer = None
        self._cfg = dict(DEFAULTS)
        self._cfg_t = 0.0
        self._last_hash = None
        self._track_key = None
        self._track_t0 = time.monotonic()
        self._t_start = time.monotonic()
        self._last_dump = 0.0
        self._last_status_dump = 0.0
        self._logged_geometry = False
        self._err_logged = False

    # ---------- lifecycle ----------
    def on_ready(self):
        self.plugin_base.start_services()
        self._ensure_thread()

    def on_tick(self):
        self._ensure_thread()

    def on_remove(self):
        self._stop.set()

    def on_removed_from_cache(self):
        self._stop.set()

    def _ensure_thread(self):
        if self._thread and self._thread.is_alive() and not self._stop.is_set():
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, args=(self._stop,),
                                        name="neonp-render", daemon=True)
        self._thread.start()

    # ---------- settings ----------
    def _refresh_cfg(self, force=False):
        now = time.monotonic()
        if not force and now - self._cfg_t < 2.0:
            return
        self._cfg_t = now
        try:
            s = self.get_settings() or {}
        except Exception:
            s = {}
        cfg = dict(DEFAULTS)
        cfg.update({k: v for k, v in s.items() if k in DEFAULTS})
        self._cfg = cfg

    def _save(self, key, value):
        try:
            s = self.get_settings() or {}
            s[key] = value
            self.set_settings(s)
        except Exception as e:
            log.warning(f"[NeoNowPlaying] saving settings: {e}")
        self._refresh_cfg(force=True)
        self._last_hash = None

    # ---------- render loop ----------
    def _present(self):
        fn = getattr(self, "get_is_present", None)
        try:
            return True if fn is None else bool(fn())
        except Exception:
            return True

    def _loop(self, stop):
        if self._renderer is None:
            self._renderer = neonp_render.Renderer()
        while not stop.is_set():
            t0 = time.monotonic()
            delay = 0.5
            try:
                if self._present():
                    delay = self._frame()
            except Exception as e:
                if not self._err_logged:
                    self._err_logged = True
                    import traceback
                    log.error(f"[NeoNowPlaying] render error: {e}\n{traceback.format_exc()}")
            stop.wait(max(0.05, delay - (time.monotonic() - t0)))

    def _frame(self):
        self._refresh_cfg()
        cfg = self._cfg
        pb = self.plugin_base
        r = self._renderer
        now = time.monotonic()
        fps = max(2, min(15, int(cfg["fps"])))

        if not self._logged_geometry:
            self._logged_geometry = True
            self._log_geometry()

        snap = pb.mpris.snapshot(cfg["player"])
        if snap is None:
            img = r.render_idle(now - self._t_start)
            self._push(img)
            return 1.0

        key = (snap["bus"], snap["title"], snap["artist"], snap["track_id"])
        if key != self._track_key:
            self._track_key = key
            self._track_t0 = now

        # cover art: browser bridge > mpris:artUrl > placeholder
        bridge_url = pb.bridge.lookup(snap["title"])
        art_url = bridge_url or snap["art_url"]
        art = pb.art.get(art_url) if art_url else None
        if art is None and bridge_url and snap["art_url"]:
            art = pb.art.get(snap["art_url"])

        r.prepare((key, art_url if art is not None else ""), art, snap["title"], snap["artist"],
                  cfg["accent_mode"], cfg["accent_hex"])
        img = r.render(snap, now - self._track_t0, now - self._t_start,
                       show_eq=bool(cfg["show_eq"]), show_times=bool(cfg["show_times"]),
                       margin_l=int(cfg["margin_l"]), margin_r=int(cfg["margin_r"]))
        self._push(img)
        self._dump_status(snap, bridge_url, art_url, art)
        if r.is_animating(snap, bool(cfg["show_eq"]), int(cfg["margin_l"]), int(cfg["margin_r"])):
            return 1.0 / fps
        return 1.0

    def _dump_status(self, snap, bridge_url, art_url, art):
        if not self._cfg.get("debug_dump") or time.monotonic() - self._last_status_dump < 5:
            return
        self._last_status_dump = time.monotonic()
        try:
            d = os.path.join(self.plugin_base.PATH, "debug")
            os.makedirs(d, exist_ok=True)
            st = self.plugin_base.art.status
            lines = [
                f"player: {snap['bus']}",
                f"title: {snap['title']}",
                f"mpris:artUrl: {snap['art_url']}",
                f"  status: {st.get(snap['art_url'], '(not tried yet)')}",
                f"bridge url: {bridge_url}",
                f"  status: {st.get(bridge_url, '-') if bridge_url else '-'}",
                f"cover in use: {'real' if art is not None else 'placeholder'} ({art_url[:100]})",
            ]
            with open(os.path.join(d, "status.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except Exception:
            pass

    def _push(self, img):
        h = hashlib.md5(img.tobytes()).hexdigest()
        if h == self._last_hash:
            return
        self._last_hash = h
        self.set_media(image=img.convert("RGBA"), size=1.0)
        cfg = self._cfg
        if cfg.get("debug_dump") and time.monotonic() - self._last_dump > 5:
            self._last_dump = time.monotonic()
            try:
                d = os.path.join(self.plugin_base.PATH, "debug")
                os.makedirs(d, exist_ok=True)
                img.save(os.path.join(d, "last_frame.png"))
            except Exception:
                pass

    def _log_geometry(self):
        try:
            deck = self.deck_controller.deck
            log.info(f"[NeoNowPlaying] deck={deck.deck_type()} key_image_format={deck.key_image_format()}")
            fn = getattr(deck, "screen_image_format", None)
            if fn:
                log.info(f"[NeoNowPlaying] screen_image_format={fn()}")
        except Exception as e:
            log.info(f"[NeoNowPlaying] geometria: {e}")
        log.info(f"[NeoNowPlaying] input={getattr(self, 'input_ident', None)}; rendering {neonp_render.W}x{neonp_render.H}")

    # ---------- control ----------
    def on_key_down(self):
        snap = self.plugin_base.mpris.snapshot(self._cfg["player"])
        if snap:
            self.plugin_base.mpris.command(snap["bus"], "PlayPause")

    # ---------- configuration ----------
    def get_config_rows(self):
        rows = []
        try:
            self._refresh_cfg(force=True)
            cfg = self._cfg
            players = self.plugin_base.mpris.labels()
            # player
            model = Gtk.StringList()
            model.append("Automatic")
            tokens = [""]
            for bus, label in players:
                model.append(label)
                tokens.append(bus)
            if cfg["player"] and cfg["player"] not in tokens:
                model.append(cfg["player"])
                tokens.append(cfg["player"])
            row = Adw.ComboRow(title="Player", model=model)
            row.set_selected(tokens.index(cfg["player"]) if cfg["player"] in tokens else 0)
            row.connect("notify::selected", lambda r, _p: self._save("player", tokens[r.get_selected()]))
            rows.append(row)

            # accent colour
            m2 = Gtk.StringList()
            m2.append("From cover art")
            m2.append("Fixed")
            acc = Adw.ComboRow(title="Accent colour", model=m2)
            acc.set_selected(0 if cfg["accent_mode"] == "auto" else 1)
            acc.connect("notify::selected",
                        lambda r, _p: self._save("accent_mode", "auto" if r.get_selected() == 0 else "fixed"))
            rows.append(acc)

            hexrow = Adw.EntryRow(title="Fixed colour (hex, e.g. #ff4fa3)")
            hexrow.set_text(cfg["accent_hex"])
            hexrow.connect("notify::text", lambda r, _p: self._save("accent_hex", r.get_text()))
            rows.append(hexrow)

            for key, title in (("show_eq", "Animated equalizer"),
                               ("show_times", "Show times next to progress bar"),
                               ("debug_dump", "Write debug files (debug/last_frame.png, status.txt)")):
                sw = Adw.SwitchRow(title=title)
                sw.set_active(bool(cfg[key]))
                sw.connect("notify::active", lambda r, _p, k=key: self._save(k, r.get_active()))
                rows.append(sw)

            spin = Adw.SpinRow.new_with_range(2, 15, 1)
            spin.set_title("Animation frames per second")
            spin.set_value(int(cfg["fps"]))
            spin.connect("notify::value", lambda r, _p: self._save("fps", int(r.get_value())))
            rows.append(spin)

            for key, title in (("margin_l", "Left margin (px)"), ("margin_r", "Right margin (px)")):
                ms = Adw.SpinRow.new_with_range(0, 30, 1)
                ms.set_title(title)
                ms.set_value(int(cfg[key]))
                ms.connect("notify::value", lambda r, _p, k=key: self._save(k, int(r.get_value())))
                rows.append(ms)
        except Exception as e:
            log.error(f"[NeoNowPlaying] get_config_rows: {e}")
        return rows


class NeoNowPlayingPlugin(PluginBase):
    def __init__(self):
        super().__init__()
        self.mpris = neonp_mpris.MprisClient()
        self.art = neonp_art.ArtLoader()
        self.bridge = neonp_art.Bridge()
        self._services_lock = threading.Lock()
        self._services_started = False

        self.add_action_holder(ActionHolder(
            plugin_base=self,
            action_base=NowPlaying,
            action_id="com_user_NeoNowPlaying::NowPlaying",
            action_name="Neo Now Playing (infobar)",
            action_support={
                Input.Key: ActionInputSupport.SUPPORTED,
                Input.Dial: ActionInputSupport.UNSUPPORTED,
                Input.Touchscreen: ActionInputSupport.UNSUPPORTED,
            },
        ))

        self.register(
            plugin_name="Neo Now Playing",
            github_repo="https://github.com/theclubmrzer/NeoNowPlaying",
            plugin_version="1.1.0",
            app_version="1.0.0-alpha",
        )
        self.request_dbus_permission("org.mpris.MediaPlayer2.*")

    def start_services(self):
        with self._services_lock:
            if self._services_started:
                return
            self._services_started = True
        self.mpris.start()
        self.bridge.start()
