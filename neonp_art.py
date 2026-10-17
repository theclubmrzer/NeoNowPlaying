"""Cover-art loading (file://, http(s)://, data:) + a local bridge for a browser userscript."""
import io
import json
import os
import shutil
import subprocess
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image

try:
    from loguru import logger as log
except Exception:  # pragma: no cover
    import logging
    log = logging.getLogger("neonp")

MAX_BYTES = 6 * 1024 * 1024
BRIDGE_PORT = 47821
# the bridge only accepts https URLs from these domains (protects against abuse by random web pages)
ALLOWED_SUFFIXES = (
    "googleusercontent.com", "ggpht.com", "ytimg.com", "gstatic.com",
    "scdn.co", "mzstatic.com", "dzcdn.net", "sndcdn.com", "scdn.com",
)


def norm(s):
    s = unicodedata.normalize("NFKC", s or "").casefold()
    return " ".join(s.split())


def sync_dirs():
    dirs = []
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        dirs.append(os.path.join(xdg, "neonp-art"))
    dirs.append(os.path.expanduser("~/.var/app/com.core447.StreamController/data/neonp-art"))
    return dirs


def sync_dir():
    return sync_dirs()[0]


class ArtLoader:
    _host_disabled = False

    def __init__(self):
        self._cache = {}       # url -> PIL.Image
        self._failed = {}      # url -> time
        self._pending = set()
        self.status = {}       # url -> 'ok' | 'error: ...' (for diagnostics)
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="neonp-art")

    def get(self, url):
        """Return the image if already loaded, otherwise None and start a background fetch."""
        if not url:
            return None
        with self._lock:
            img = self._cache.get(url)
            if img is not None:
                return img
            failed_at = self._failed.get(url)
            ttl = 3 if url.startswith("file:") else 30   # the host file may still be syncing
            if failed_at and time.monotonic() - failed_at < ttl:
                return None
            if url in self._pending:
                return None
            self._pending.add(url)
        self._pool.submit(self._fetch, url)
        return None

    def failed(self, url):
        with self._lock:
            return url in self._failed

    def _fetch(self, url):
        try:
            data = self._read(url)
            img = Image.open(io.BytesIO(data))
            img.load()
            img = img.convert("RGB")
            if max(img.size) > 600:
                img.thumbnail((600, 600), Image.LANCZOS)
            with self._lock:
                if len(self._cache) > 24:
                    self._cache.pop(next(iter(self._cache)))
                self._cache[url] = img
                self._failed.pop(url, None)
            self.status[url] = "ok"
            log.info(f"[NeoNowPlaying] cover OK: {url[:80]}")
        except Exception as e:
            with self._lock:
                self._failed[url] = time.monotonic()
            self.status[url] = f"error: {e}"
            log.info(f"[NeoNowPlaying] cover unavailable ({url[:60]}): {e}")
        finally:
            with self._lock:
                self._pending.discard(url)

    @staticmethod
    def _read(url):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme == "file":
            path = urllib.parse.unquote(parsed.path)
            try:
                with open(path, "rb") as f:
                    return f.read(MAX_BYTES)
            except OSError as e:
                data = ArtLoader._read_synced(path)
                if data is None:
                    data = ArtLoader._read_via_host(path)
                if data is None:
                    raise OSError(f"{e} (also not found in {sync_dir()}, and no access via the host)")
                return data
        if parsed.scheme == "data":
            with urllib.request.urlopen(url, timeout=3) as r:
                return r.read(MAX_BYTES)
        if parsed.scheme in ("http", "https"):
            req = urllib.request.Request(url, headers={"User-Agent": "NeoNowPlaying/1.0"})
            with urllib.request.urlopen(req, timeout=6) as r:
                return r.read(MAX_BYTES)
        raise ValueError(f"unsupported scheme: {parsed.scheme}")


    @staticmethod
    def _read_synced(path):
        """Copy of the file made on the host by extras/neonp-art-sync.sh."""
        name = os.path.basename(path)
        for d in sync_dirs():
            cand = os.path.join(d, name)
            try:
                with open(cand, "rb") as f:
                    return f.read(MAX_BYTES)
            except OSError:
                continue
        return None

    @staticmethod
    def _read_via_host(path):
        """The file lives in the host /tmp which the Flatpak sandbox cannot see -> try flatpak-spawn --host.
        Only works if StreamController has permission to talk to org.freedesktop.Flatpak."""
        if ArtLoader._host_disabled or not shutil.which("flatpak-spawn"):
            return None
        home = os.path.expanduser("~")
        if not path.startswith(("/tmp/", "/var/tmp/", "/run/user/", home + "/")):
            return None
        try:
            res = subprocess.run(["flatpak-spawn", "--host", "head", "-c", str(MAX_BYTES), "--", path],
                                 capture_output=True, timeout=4)
            if res.returncode != 0 and b"head:" not in res.stderr:
                ArtLoader._host_disabled = True   # Flatpak does not allow flatpak-spawn --host
            return res.stdout if res.returncode == 0 and res.stdout else None
        except Exception:
            return None


class Bridge:
    """Tiny server on 127.0.0.1 to which a browser userscript posts the cover URL
    (navigator.mediaSession), for browsers whose MPRIS art is a file in a private /tmp."""

    def __init__(self):
        self._entries = {}   # norm(title) -> (url, time)
        self._lock = threading.Lock()
        self._server = None
        self._thread = None

    def lookup(self, title):
        with self._lock:
            e = self._entries.get(norm(title))
        return e[0] if e else None

    def _store(self, title, url):
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        if urllib.parse.urlparse(url).scheme != "https" or not host.endswith(ALLOWED_SUFFIXES):
            log.info(f"[NeoNowPlaying] mostek: odrzucono URL z domeny {host!r}")
            return False
        with self._lock:
            if len(self._entries) > 32:
                self._entries.pop(next(iter(self._entries)))
            self._entries[norm(title)] = (url, time.monotonic())
        return True

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _ok(self, code=204):
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_OPTIONS(self):  # no CORS -> web pages cannot pass the preflight
                self._ok(403)

            def do_GET(self):
                self._ok(204)

            def do_POST(self):
                try:
                    if self.headers.get("X-NeoNP") != "1":
                        return self._ok(403)
                    n = int(self.headers.get("Content-Length", "0"))
                    if n <= 0 or n > 8192:
                        return self._ok(400)
                    data = json.loads(self.rfile.read(n).decode("utf-8", "ignore"))
                    ok = bridge._store(str(data.get("title", "")), str(data.get("art", "")))
                    self._ok(204 if ok else 400)
                except Exception:
                    self._ok(400)

        try:
            self._server = ThreadingHTTPServer(("127.0.0.1", BRIDGE_PORT), Handler)
        except OSError as e:
            log.warning(f"[NeoNowPlaying] cover bridge failed to start (port {BRIDGE_PORT}): {e}")
            return
        self._thread = threading.Thread(target=self._server.serve_forever, name="neonp-bridge", daemon=True)
        self._thread.start()
        log.info(f"[NeoNowPlaying] cover bridge listening on 127.0.0.1:{BRIDGE_PORT}")
