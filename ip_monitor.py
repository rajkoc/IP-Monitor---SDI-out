#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IP Multi-Viewer  (2 / 4 / 8 monitora)
=====================================
- Svaki monitor: video + audio VU/peak indikator sa desne strane
- Selektovan monitor -> audio na zvučnik računara
- Selektovan monitor -> izlaz na Blackmagic DeckLink karticu (Studio, Extreme, Duo, Quad ...)

Zavisnosti: Python 3.9+, PySide6, PyGObject, GStreamer 1.20+ (base, good, bad, libav)
DeckLink: Blackmagic Desktop Video drajver + GStreamer plugin "decklink" (gst-plugins-bad)

Primeri URL-ova:
  udp://239.1.1.1:5000        (MPEG-TS multicast / unicast)
  srt://192.168.1.10:9000     (?mode=caller / listener ...)
  rtsp://user:pass@host/stream
  http://host/live/index.m3u8 (HLS)
  rtmp://host/app/key
  C:\\video\\fajl.mp4          (lokalni fajl, za test)
  ndi://MASINA (Naziv izvora) (NDI izvor; ili ndi-ip://192.168.1.50:5961)  - treba gst-plugin-ndi
  test                        (interni generator slike i tona, za proveru GUI-ja)

Prečice:  1-8 = selektuj monitor | A = audio na selektovanom | D = DeckLink na selektovanom
          R = snimanje selektovanog | Shift+R = snimanje svih (start/stop)
          F11 = fullscreen | dvoklik na monitor = podešavanje strima
"""
import sys
import os
import re
import json
import math
import time

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib  # noqa: E402

from PySide6.QtCore import Qt, QTimer, Signal, QRectF  # noqa: E402
from PySide6.QtGui import (QImage, QPainter, QColor, QFont, QPen,  # noqa: E402
                           QKeySequence, QShortcut, QTextOption)
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QFrame,  # noqa: E402
                               QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
                               QPushButton, QComboBox, QSpinBox, QSlider,
                               QDialog, QFormLayout, QLineEdit,
                               QDialogButtonBox, QMessageBox, QFileDialog,
                               QInputDialog, QCheckBox)

Gst.init(None)

CONFIG_PATH = os.path.expanduser("~/.ip_monitor.json")
DB_FLOOR = -60.0
MAX_TILES = 8
DECODE_WIDTH = {2: 1280, 4: 960, 8: 640}     # širina slike za prikaz u GUI-ju
GRID_COLS = {2: 2, 4: 2, 8: 4}

# snimanje: naziv -> (ekstenzija, muxer)
REC_CONTAINERS = {
    "MKV": ("mkv", "matroskamux"),                       # najotpornije na pad programa/struje
    "MP4": ("mp4", "mp4mux fragment-duration=2000"),     # fragmentiran mp4 (puštiv i posle pada)
    "TS": ("ts", "mpegtsmux"),                           # MPEG-TS
}
DEFAULT_REC_DIR = os.path.join(os.path.expanduser("~"), "IPMonitor_rec")

# mode -> (w, h, fps_num, fps_den, interlaced)
DECKLINK_MODES = {
    "auto": None,
    "1080p25": (1920, 1080, 25, 1, False),
    "1080p50": (1920, 1080, 50, 1, False),
    "1080p2997": (1920, 1080, 30000, 1001, False),
    "1080p5994": (1920, 1080, 60000, 1001, False),
    "1080p24": (1920, 1080, 24, 1, False),
    "1080p30": (1920, 1080, 30, 1, False),
    "1080p60": (1920, 1080, 60, 1, False),
    "720p50": (1280, 720, 50, 1, False),
    "720p5994": (1280, 720, 60000, 1001, False),
    "720p60": (1280, 720, 60, 1, False),
    # interlaced: izvor mora već biti interlaced, inače koristi "auto"/progressive
    "1080i50": (1920, 1080, 25, 1, True),
    "1080i5994": (1920, 1080, 30000, 1001, True),
}


# --------------------------------------------------------------------------
# Pomoćne funkcije
# --------------------------------------------------------------------------
HW_DEC_RE = re.compile(
    r"^(d3d11|d3d12|nv|qsv|vaapi|va|mf|amf|msdk|vulkan)"
    r"(h264|h265|hevc|vp8|vp9|av1|mpeg2|mpeg4|jpeg|mjpeg|vc1|avc)\w*dec$")
ALLOW_HW_DECODE = False        # "hw_decode": true u ~/.ip_monitor.json


def _is_hw_video_decoder(factory):
    try:
        return HW_DEC_RE.match(factory.get_name()) is not None
    except Exception:
        return False


def disable_hw_decoders():
    """Spušta rang hardverskih video dekodera (d3d11/d3d12/nvcodec/qsv/va/...) na nulu.
    Dodatno ih uridecodebin preskače kroz 'autoplug-select' (vidi _on_autoplug_select)."""
    names = []
    try:
        for f in Gst.Registry.get().get_feature_list(Gst.ElementFactory):
            if _is_hw_video_decoder(f):
                f.set_rank(0)
                names.append(f.get_name())
    except Exception as e:
        print("[GST] disable_hw_decoders:", e, flush=True)
    print(f"[GST] hardverski video dekoderi isključeni: {len(names)} {names}", flush=True)
    sw = [n for n in ("avdec_h264", "openh264dec") if Gst.ElementFactory.find(n)]
    print(f"[GST] softverski H.264 dekoderi: {sw or 'NEMA (instaliraj gst-libav)'}", flush=True)


def tune_decoder_ranks():
    """Softverski H.264: preferiraj avdec_h264 (gst-libav), izbaci openh264dec."""
    reg = Gst.Registry.get()
    f = reg.lookup_feature("avdec_h264")
    if f is not None:
        f.set_rank(266)
    f = reg.lookup_feature("openh264dec")
    if f is not None:
        f.set_rank(0)


def _on_autoplug_select(_dec, _pad, _caps, factory):
    # povratne vrednosti GstAutoplugSelectResult: 0 = TRY, 1 = EXPOSE, 2 = SKIP
    if not ALLOW_HW_DECODE and _is_hw_video_decoder(factory):
        print(f"[GST] preskačem hardverski dekoder '{factory.get_name()}'", flush=True)
        return 2
    return 0


def is_live_url(url):
    u = url.strip().lower()
    return is_test(u) or u.startswith(("rtmp://", "rtmps://", "rtsp://", "rtsps://", "udp://",
                                       "srt://", "rtp://", "ndi://", "ndi-ip://"))


def to_uri(url):
    url = url.strip()
    if "://" in url:
        return url
    return GLib.filename_to_uri(os.path.abspath(url), None)


def is_test(url):
    return re.fullmatch(r"test[:\-]?\d*", url.strip().lower()) is not None


def test_source_desc(url, audio=True):
    """Interni generator (video + audio) koji se vezuje na queue 'vq' i 'aq'."""
    m = re.match(r"test[:\-]?(\d*)", url.strip().lower())
    n = int(m.group(1)) if m and m.group(1) else 0
    patterns = ["ball", "smpte", "snow", "circular", "checkers-8"]
    pat = patterns[n % len(patterns)]
    out = (
        f" videotestsrc is-live=true pattern={pat} ! "
        "video/x-raw,width=1280,height=720,framerate=25/1 ! vq. "
    )
    if audio:
        out += (
            f" audiotestsrc is-live=true wave=sine freq={220 * (n + 1)} volume={0.15 + 0.08 * (n % 5):.2f} ! "
            "audio/x-raw,rate=48000,channels=2 ! aq. "
        )
    return out


def _pad_kind(pad):
    """'video' / 'audio' / None. Prvo po imenu pada (caps često još nisu poznate)."""
    name = pad.get_name().lower()
    if name.startswith("video"):
        return "video"
    if name.startswith("audio"):
        return "audio"
    caps = pad.get_current_caps() or pad.query_caps(None)
    if caps is not None and not caps.is_any() and caps.get_size() > 0:
        n = caps.get_structure(0).get_name()
        if n.startswith("video/"):
            return "video"
        if n.startswith("audio/"):
            return "audio"
    return None


def _discard_pad(pipeline, pad):
    """Višak/nepoznat tok (titlovi, metapodaci, drugi video): odbaci ga u fakesink
    da izvor ne dobije 'not-linked'."""
    sink = Gst.ElementFactory.make("fakesink")
    sink.set_property("sync", False)
    sink.set_property("async", False)
    pipeline.add(sink)
    sink.sync_state_with_parent()
    pad.link(sink.get_static_pad("sink"))


def _link_by_caps(pipeline, pad):
    """Dinamički pad: video -> queue 'vq', audio -> queue 'aq'."""
    try:
        kind = _pad_kind(pad)
        if kind is None:
            print(f"[GST] nepoznat tok '{pad.get_name()}' - odbacujem", flush=True)
            _discard_pad(pipeline, pad)
            return
        target = pipeline.get_by_name("vq" if kind == "video" else "aq")
        if target is None:                      # npr. DeckLink izlaz bez zvuka
            _discard_pad(pipeline, pad)
            return
        sinkpad = target.get_static_pad("sink")
        if sinkpad.is_linked():
            _discard_pad(pipeline, pad)
            return
        ret = pad.link(sinkpad)
        if ret != Gst.PadLinkReturn.OK:
            print(f"[GST] link {kind} pada '{pad.get_name()}' nije uspeo: {ret}", flush=True)
    except Exception:
        import traceback
        traceback.print_exc()


def attach_source(pipeline, url):
    """Dodaje izvor u pipeline (NDI ili uridecodebin) i povezuje ga na 'vq' / 'aq'.
    Vraća None ako je OK, inače tekst greške."""
    u = url.strip()
    low = u.lower()
    if low.startswith(("ndi://", "ndi-ip://")):
        for el in ("ndisrc", "ndisrcdemux"):
            if Gst.ElementFactory.find(el) is None:
                return ("NDI plugin nije dostupan (ndisrc/ndisrcdemux).\n"
                        "Instaliraj gst-plugin-ndi + NDI Runtime i proveri: gst-inspect-1.0 ndisrc")
        src = Gst.ElementFactory.make("ndisrc", "ndisrc")
        if low.startswith("ndi-ip://"):
            src.set_property("url-address", u[9:].strip())
        else:
            src.set_property("ndi-name", u[6:].strip())
        demux = Gst.ElementFactory.make("ndisrcdemux", "ndidemux")
        pipeline.add(src)
        pipeline.add(demux)
        src.link(demux)
        demux.connect("pad-added", lambda _d, pad, pl=pipeline: _link_by_caps(pl, pad))
        return None
    dec = Gst.ElementFactory.make("uridecodebin", "dec")
    if dec is None:
        return "GStreamer element 'uridecodebin' nije dostupan."
    dec.set_property("uri", to_uri(u))
    dec.connect("autoplug-select", _on_autoplug_select)
    dec.connect("pad-added", lambda _d, pad, pl=pipeline: _link_by_caps(pl, pad))
    pipeline.add(dec)
    return None


def parse_db_list(structure, field):
    """Čita listu dB vrednosti iz 'level' poruke."""
    vals = None
    try:
        v = structure.get_value(field)
        if isinstance(v, (list, tuple)):
            vals = [float(x) for x in v]
    except Exception:
        vals = None
    if vals is None:
        m = re.search(field + r"=\(double\)\{([^}]*)\}", structure.to_string())
        vals = [float(x) for x in m.group(1).split(",")] if m else []
    out = []
    for x in vals:
        if math.isnan(x) or x < DB_FLOOR:
            x = DB_FLOOR
        out.append(min(x, 6.0))
    return out


def db_frac(db):
    return max(0.0, min(1.0, (db - DB_FLOOR) / (-DB_FLOOR)))


# --------------------------------------------------------------------------
# Prijemnik jednog strima (za monitor tile)
# --------------------------------------------------------------------------
class Receiver:
    def __init__(self):
        self.pipeline = None
        self.bus = None
        self.url = ""
        self.width = 640
        self.frame = None
        self.peaks = []
        self.last_level = 0.0
        self.last_frame = 0.0
        self.started = 0.0
        self.retry_at = 0.0
        self.state = "IDLE"
        self.error = ""
        self.listen = False
        self.volume = 1.0
        self.latency_ms = 1000     # bafer za uživo izvore (0 = bez sinhronizacije)
        self.fps = 0.0
        self._fps_n = 0
        self._fps_t = 0.0
        self.decoder = ""
        self.frame_id = 0
        self._decoders = []
        self._buffering = False
        self._live_pb = False
        self.vol_el = None
        self.off = False           # korisnik je ugasio ovaj prikaz

    # ---- životni ciklus -------------------------------------------------
    def start(self, url, width=None):
        self._teardown()
        self.url = url.strip()
        if width:
            self.width = width
        self.frame = None
        self.peaks = []
        self.retry_at = 0.0
        self.error = ""
        self.fps = 0.0
        self._fps_n = 0
        self._fps_t = time.time()
        self.decoder = ""
        self._decoders = []
        self._buffering = False
        self._live_pb = False
        self.vol_el = None
        if self.off or not self.url:
            self.state = "IDLE"
            return
        self.state = "CONNECTING"
        low = self.url.lower()
        if is_test(self.url) or low.startswith(("ndi://", "ndi-ip://")):
            self._start_custom()        # test generator i NDI: sopstveni pipeline
        else:
            self._start_playbin()       # sve ostalo: playbin (isto kao gst-play)

    # ---- playbin (RTMP, RTSP, SRT, UDP, HLS, fajlovi...) ------------------
    def _start_playbin(self):
        try:
            buf = int(self.latency_ms)
            pb = Gst.ElementFactory.make("playbin", "pb")
            if pb is None:
                self._fail("GStreamer element 'playbin' nije dostupan.")
                return
            pb.set_property("uri", to_uri(self.url))
            flags = "video+audio+soft-volume+buffering"
            if not ALLOW_HW_DECODE:
                flags += "+force-sw-decoders"
            Gst.util_set_object_arg(pb, "flags", flags)
            if buf > 0:
                pb.set_property("buffer-duration", buf * 1000000)

            vbin = Gst.parse_bin_from_description(
                "videoscale ! "
                f"video/x-raw,width={self.width},pixel-aspect-ratio=1/1 ! "
                "videoconvert ! video/x-raw,format=BGRx ! "
                "appsink name=vsink emit-signals=true max-buffers=1 drop=true sync=true qos=false", True)
            abin = Gst.parse_bin_from_description(
                "level name=lvl interval=50000000 post-messages=true ! "
                "volume name=vol mute=true ! audioconvert ! audioresample ! "
                "audio/x-raw,channels=2 ! autoaudiosink", True)
            pb.set_property("video-sink", vbin)
            pb.set_property("audio-sink", abin)
            vbin.get_by_name("vsink").connect("new-sample", self._on_sample)
            self.vol_el = abin.get_by_name("vol")
            pb.connect("deep-element-added", self._on_deep_element)
            self.pipeline = pb
            self.bus = pb.get_bus()
            self._apply_audio()
            self.started = time.time()
            ret = pb.set_state(Gst.State.PLAYING)
            self._live_pb = (ret == Gst.StateChangeReturn.NO_PREROLL)
            if ret == Gst.StateChangeReturn.FAILURE:
                self._fail("playbin se nije pokrenuo")
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._fail(str(e))

    def _on_deep_element(self, _pb, _sub, el):
        try:
            f = el.get_factory()
            n = f.get_name() if f is not None else ""
            if n == "uridecodebin":
                el.connect("autoplug-select", _on_autoplug_select)
            elif "dec" in n and not n.startswith(("uridecodebin", "decodebin")):
                if n not in self._decoders:
                    self._decoders.append(n)
        except Exception as e:
            print("[GST] deep-element-added:", e, flush=True)

    # ---- sopstveni pipeline (test generator, NDI) ---------------------------
    def _start_custom(self):
        live = is_live_url(self.url)
        buf = int(self.latency_ms) if live else 0
        sync = "false" if (live and buf <= 0) else "true"
        qtime = (max(buf, 0) + 1500) * 1000000
        desc = (
            "queue name=vq max-size-buffers=2 max-size-time=0 max-size-bytes=0 ! "
            "videoscale ! "
            f"video/x-raw,width={self.width},pixel-aspect-ratio=1/1 ! "
            "videoconvert ! video/x-raw,format=BGRx ! "
            f"queue max-size-buffers=0 max-size-bytes=0 max-size-time={qtime} leaky=downstream ! "
            f"appsink name=vsink emit-signals=true max-buffers=1 drop=true sync={sync} async=false qos=false "
            f"queue name=aq max-size-buffers=0 max-size-bytes=0 max-size-time={qtime} leaky=downstream ! "
            "audioconvert ! audioresample ! tee name=at "
            f"at. ! queue max-size-buffers=0 max-size-bytes=0 max-size-time={qtime} leaky=downstream ! "
            f"level name=lvl interval=50000000 post-messages=true ! fakesink sync={sync} async=false "
            f"at. ! queue max-size-buffers=0 max-size-bytes=0 max-size-time={qtime} leaky=downstream ! "
            "audioconvert ! audio/x-raw,channels=2 ! volume name=vol mute=true ! autoaudiosink "
        )
        if is_test(self.url):
            desc += test_source_desc(self.url)
        try:
            self.pipeline = Gst.parse_launch(desc)
        except GLib.Error as e:
            self.error = str(e)
            self._fail()
            return
        if not is_test(self.url):
            err = attach_source(self.pipeline, self.url)
            if err:
                self._fail(err)
                return
        self.pipeline.get_by_name("vsink").connect("new-sample", self._on_sample)
        self.pipeline.use_clock(Gst.SystemClock.obtain())
        if live and buf > 0:
            try:
                self.pipeline.set_latency(buf * 1000000)
            except Exception as e:
                print("[GST] set_latency:", e, flush=True)
        self.bus = self.pipeline.get_bus()
        self._apply_audio()
        self.started = time.time()
        self.pipeline.set_state(Gst.State.PLAYING)

    def stop(self):
        self._teardown()
        self.retry_at = 0.0
        self.frame = None
        self.peaks = []
        self.state = "IDLE"

    def _teardown(self):
        if self.pipeline is not None:
            pl = self.pipeline
            self.pipeline = None
            self.bus = None
            pl.set_state(Gst.State.NULL)
            pl.get_state(2 * Gst.SECOND)       # sačekaj da se stvarno zaustavi

    def _fail(self, why=None):
        if why:
            self.error = why
        self._teardown()
        self.frame = None
        self.peaks = []
        self.state = "NO SIGNAL"
        self.retry_at = time.time() + 3.0     # auto-reconnect

    # ---- audio na zvučnik ----------------------------------------------
    def set_listen(self, on):
        self.listen = on
        self._apply_audio()

    def set_volume(self, v):
        self.volume = v
        self._apply_audio()

    def _apply_audio(self):
        if self.pipeline is None:
            return
        vol = self.vol_el or self.pipeline.get_by_name("vol")
        if vol is not None:
            vol.set_property("volume", float(self.volume))
            vol.set_property("mute", not self.listen)

    # ---- callback-ovi ---------------------------------------------------
    def _on_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        st = sample.get_caps().get_structure(0)
        w, h = st.get_value("width"), st.get_value("height")
        ok, info = buf.map(Gst.MapFlags.READ)
        if ok:
            try:
                stride = len(info.data) // h
                if stride < w * 4:
                    stride = w * 4
                # BGRx == Qt Format_RGB32 (nativni format, crtanje bez konverzije)
                self.frame = QImage(info.data, w, h, stride,
                                    QImage.Format.Format_RGB32).copy()
                self.frame_id += 1
                self.last_frame = time.time()
                self.state = "LIVE"
                self._fps_n += 1
            finally:
                buf.unmap(info)
        return Gst.FlowReturn.OK

    def poll(self):
        """Poziva se iz GUI niti (timer)."""
        now = time.time()
        if self.pipeline is None:
            if self.url and self.retry_at and now >= self.retry_at:
                self.start(self.url)
            return
        if now - self._fps_t >= 1.0:
            self.fps = self._fps_n / (now - self._fps_t)
            self._fps_n = 0
            self._fps_t = now
            if self._decoders:
                self.decoder = ",".join(self._decoders)
            elif not self.decoder and self.frame is not None:
                self.decoder = self._find_decoder()
        mask = (Gst.MessageType.ELEMENT | Gst.MessageType.ERROR | Gst.MessageType.EOS
                | Gst.MessageType.BUFFERING)
        while self.bus is not None:
            msg = self.bus.pop_filtered(mask)
            if msg is None:
                break
            if msg.type == Gst.MessageType.ELEMENT:
                s = msg.get_structure()
                if s is not None and s.get_name() == "level":
                    self.peaks = parse_db_list(s, "peak")
                    self.last_level = now
            elif msg.type == Gst.MessageType.ERROR:
                err, dbg = msg.parse_error()
                who = msg.src.get_name() if msg.src is not None else "?"
                print(f"[MONITOR {self.url}] ERROR iz elementa '{who}': {err.message}\n    debug: {dbg}",
                      flush=True)
                self._fail(f"{err.message} [{who}]")
                return
            elif msg.type == Gst.MessageType.EOS:
                self._fail("EOS")
                return
            elif msg.type == Gst.MessageType.BUFFERING:
                pct = msg.parse_buffering()
                if not self._live_pb:
                    if pct < 100:
                        self.state = f"BUFFERING {pct}%"
                        if not self._buffering:
                            self._buffering = True
                            self.pipeline.set_state(Gst.State.PAUSED)
                    elif self._buffering:
                        self._buffering = False
                        self.pipeline.set_state(Gst.State.PLAYING)
        # watchdog (UDP izvor npr. ne javlja grešku kad se signal izgubi)
        if not self._buffering and now - max(self.last_frame, self.started) > 15.0:
            self._fail("timeout")

    def _find_decoder(self):
        names = []
        try:
            for el in self.pipeline.iterate_recurse():
                f = el.get_factory()
                n = f.get_name() if f is not None else ""
                if "dec" in n and not n.startswith(("uridecodebin", "decodebin")):
                    names.append(n)
        except Exception as e:
            print("[GST] _find_decoder:", e, flush=True)
            return "?"
        return ",".join(names)

    def status_text(self):
        if self.state == "IDLE":
            if self.off:
                return "STREAM ISKLJUČEN\n(⏻ za uključivanje)"
            return "Dvoklik za unos URL-a"
        if self.state == "CONNECTING":
            return "CONNECTING..."
        if self.state == "NO SIGNAL":
            return "NO SIGNAL" + (("\n" + self.error[:200]) if self.error else "")
        return "WAITING FOR VIDEO"


# --------------------------------------------------------------------------
# DeckLink izlaz
# --------------------------------------------------------------------------
class DeckLinkOutput:
    def __init__(self):
        self.pipeline = None
        self.bus = None
        self.note = ""

    @staticmethod
    def available():
        return Gst.ElementFactory.find("decklinkvideosink") is not None

    @property
    def running(self):
        return self.pipeline is not None

    def start(self, url, device, mode, audio=True):
        """Vraća None ako je OK, inače tekst greške. self.note = upozorenje (npr. bez zvuka)."""
        self.stop()
        self.note = ""
        if not self.available():
            return ("GStreamer plugin 'decklink' nije pronađen.\n"
                    "Instaliraj Blackmagic Desktop Video i proveri: gst-inspect-1.0 decklinkvideosink")
        err = self._run(url, device, mode, audio)
        if err and audio and "[das]" in err:
            # audio izlaz kartice ne može da se otvori: probaj samo sliku
            if self._run(url, device, mode, False) is None:
                self.note = ("Audio izlaz DeckLink-a nije mogao da se pokrene – šalje se samo slika.\n\n"
                             "Razlog audio greške:\n" + err)
                return None
        return err

    def _run(self, url, device, mode, audio):
        cfg = DECKLINK_MODES.get(mode)
        if cfg is None:
            vchain = "videoconvert"
            mode = "auto"
        else:
            w, h, fn, fd, inter = cfg
            caps = f"video/x-raw,width={w},height={h},framerate={fn}/{fd}"
            caps += ",interlace-mode=interleaved" if inter else ",interlace-mode=progressive"
            if h >= 720:
                caps += ",pixel-aspect-ratio=1/1"
            vchain = f"videoconvert ! videoscale ! videorate ! {caps}"
        desc = (
            f"queue name=vq max-size-time=1000000000 ! {vchain} ! "
            f"decklinkvideosink name=dvs device-number={device} mode={mode} "
        )
        if audio:
            desc += (
                "queue name=aq max-size-time=1000000000 ! audioconvert ! audioresample ! "
                "audio/x-raw,format=S16LE,rate=48000,channels=2 ! "
                f"decklinkaudiosink name=das device-number={device} "
            )
        if is_test(url):
            desc += test_source_desc(url, audio)
        try:
            pl = Gst.parse_launch(desc)
        except GLib.Error as e:
            return f"Greška u pipeline-u: {e.message}"
        if not is_test(url):
            err = attach_source(pl, url)
            if err:
                pl.set_state(Gst.State.NULL)
                return err
        ret = pl.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            detail = ""
            try:
                msg = pl.get_bus().timed_pop_filtered(1500 * Gst.MSECOND, Gst.MessageType.ERROR)
                if msg is not None:
                    err, dbg = msg.parse_error()
                    who = msg.src.get_name() if msg.src is not None else "?"
                    detail = f"\n\n[{who}] {err.message}\n{dbg or ''}"
                    print(f"[DECKLINK] ERROR iz '{who}': {err.message}\n    debug: {dbg}", flush=True)
            except Exception as e:
                print("[DECKLINK] ne mogu da pročitam grešku:", e, flush=True)
            pl.set_state(Gst.State.NULL)
            return ("DeckLink izlaz nije mogao da se pokrene (proveri device-number, da li je izlaz "
                    "zauzet i da li kartica podržava izabrani mod)." + detail)
        self.pipeline = pl
        self.bus = pl.get_bus()
        return None

    def stop(self):
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
        self.pipeline = None
        self.bus = None

    def poll(self):
        if self.bus is None:
            return None
        msg = self.bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.EOS)
        if msg is None:
            return None
        if msg.type == Gst.MessageType.ERROR:
            err, _ = msg.parse_error()
            self.stop()
            return err.message
        self.stop()
        return "EOS (izvor je prekinut)"


# --------------------------------------------------------------------------
# Snimanje (recording) + NDI pretraga
# --------------------------------------------------------------------------
def list_decklink_devices():
    """Vraća tekst sa uređajima koje GStreamer vidi preko decklink device provider-a."""
    fac = Gst.DeviceProviderFactory.find("decklinkdeviceprovider")
    if fac is None:
        return "GStreamer nema 'decklinkdeviceprovider' (decklink plugin nije učitan)."
    prov = fac.get()
    prov.start()
    t0 = time.time()
    while time.time() - t0 < 1.0:
        QApplication.processEvents()
        time.sleep(0.05)
    lines = []
    for d in prov.get_devices():
        props = d.get_properties()
        lines.append(f"• {d.get_display_name()}  [{d.get_device_class()}]"
                     + (f"\n    {props.to_string()}" if props is not None else ""))
    prov.stop()
    return "\n".join(lines) if lines else "GStreamer ne vidi nijedan DeckLink uređaj."


def first_available(names):
    for n in names:
        if Gst.ElementFactory.find(n) is not None:
            return n
    return None


def safe_name(s):
    return re.sub(r"[^\w\-]+", "_", s, flags=re.UNICODE).strip("_") or "stream"


def discover_ndi(timeout=3.0):
    """Vraća listu NDI izvora na mreži, ili None ako plugin ne postoji."""
    fac = Gst.DeviceProviderFactory.find("ndideviceprovider")
    if fac is None:
        return None
    prov = fac.get()
    prov.start()
    t0 = time.time()
    while time.time() - t0 < timeout:
        QApplication.processEvents()
        time.sleep(0.05)
    names = []
    for d in prov.get_devices():
        n = None
        try:
            props = d.get_properties()
            if props is not None and props.has_field("ndi-name"):
                n = props.get_string("ndi-name")
        except Exception:
            n = None
        names.append(n or d.get_display_name())
    prov.stop()
    return sorted(set(names))


class Recorder:
    """Snima strim u fajl (H.264 + AAC) u zasebnom pipeline-u (nova konekcija ka izvoru).
    Na prekid signala automatski nastavlja u novom fajlu. Fajl se zatvara pravilno (EOS)."""
    encoder_pref = "auto"

    def __init__(self):
        self.pipeline = None
        self.bus = None
        self.wanted = False        # korisnik želi snimanje
        self.stopping = False
        self.params = None
        self.path = ""
        self.t_start = 0.0
        self.stop_deadline = 0.0
        self.retry_at = 0.0
        self.filled = False
        self.last_size = 0
        self.last_growth = 0.0
        self.last_check = 0.0
        self.error = ""
        self.encoder = ""

    # ---- API ------------------------------------------------------------
    def start(self, url, name, folder, container, kbps, mode="encode"):
        if self.pipeline is not None:
            return "Prethodni snimak se još finalizuje, sačekaj trenutak."
        self.params = (url, name, folder, container, kbps, mode)
        err = self._launch()
        if err:
            self.wanted = False
            return err
        self.wanted = True
        return None

    def request_stop(self):
        self.wanted = False
        self.retry_at = 0.0
        if self.pipeline is None:
            return
        self.stopping = True
        self.stop_deadline = time.time() + 8.0
        self.pipeline.send_event(Gst.Event.new_eos())

    def label(self):
        if self.pipeline is None:
            return "REC (restart...)" if self.wanted else ""
        if self.stopping:
            return "SAVING..."
        return "● REC " + time.strftime("%H:%M:%S", time.gmtime(time.time() - self.t_start))

    # ---- interno --------------------------------------------------------
    def _launch(self):
        url, name, folder, container, kbps, mode = self.params
        if mode == "copy" and not is_test(url):
            return self._launch_copy(url, name, folder, container)
        pref = Recorder.encoder_pref
        order = ([pref] if pref not in ("auto", "") else
                 ["x264enc", "nvh264enc", "qsvh264enc", "vah264enc", "vtenc_h264", "mfh264enc"])
        venc = first_available(order)
        aenc = first_available(["avenc_aac", "voaacenc", "fdkaacenc", "faac"])
        if venc is None or aenc is None:
            return ("Nedostaje H.264 ili AAC enkoder.\n"
                    "Instaliraj gst-plugins-ugly (x264enc) i gst-libav (avenc_aac).")
        self.encoder = venc
        venc_desc = f"{venc} bitrate={int(kbps)}"
        if venc == "x264enc":
            venc_desc += " speed-preset=veryfast tune=zerolatency key-int-max=50"
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as e:
            return f"Ne mogu da napravim folder za snimke: {e}"
        ext, mux = REC_CONTAINERS[container]
        self.path = os.path.join(folder, f"{safe_name(name)}_{time.strftime('%Y%m%d_%H%M%S')}.{ext}")
        desc = (
            "queue name=vq max-size-buffers=0 max-size-bytes=0 max-size-time=4000000000 ! "
            f"videoconvert ! {venc_desc} ! h264parse ! mux. "
            "queue name=aq max-size-buffers=0 max-size-bytes=0 max-size-time=4000000000 ! "
            f"audioconvert ! audioresample ! {aenc} bitrate=192000 ! aacparse ! mux. "
            f"{mux} name=mux ! filesink name=fsink async=false "
        )
        if is_test(url):
            desc += test_source_desc(url)
        try:
            pl = Gst.parse_launch(desc)
        except GLib.Error as e:
            return f"Greška u pipeline-u za snimanje: {e.message}"
        pl.get_by_name("fsink").set_property("location", self.path)
        if not is_test(url):
            err = attach_source(pl, url)
            if err:
                pl.set_state(Gst.State.NULL)
                return err
        if pl.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            pl.set_state(Gst.State.NULL)
            return "Snimanje nije moglo da se pokrene."
        now = time.time()
        self.pipeline, self.bus = pl, pl.get_bus()
        self.stopping = False
        self.filled = False
        self.t_start = now
        self.last_size = 0
        self.last_growth = now
        self.last_check = now
        return None

    # ---- snimanje bez rekodiranja (remux): kopira H.264/H.265/AAC/... u kontejner ----
    def _launch_copy(self, url, name, folder, container):
        for el in ("urisourcebin", "parsebin"):
            if Gst.ElementFactory.find(el) is None:
                return f"Nedostaje GStreamer element '{el}'."
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as e:
            return f"Ne mogu da napravim folder za snimke: {e}"
        ext, mux_desc = REC_CONTAINERS[container]
        self.path = os.path.join(folder, f"{safe_name(name)}_{time.strftime('%Y%m%d_%H%M%S')}.{ext}")
        self.encoder = "copy"
        try:
            pl = Gst.parse_launch(f"{mux_desc} name=mux ! filesink name=fsink async=false")
        except GLib.Error as e:
            return f"Greška u pipeline-u za snimanje: {e.message}"
        pl.get_by_name("fsink").set_property("location", self.path)
        mux = pl.get_by_name("mux")
        src = Gst.ElementFactory.make("urisourcebin", "usb")
        src.set_property("uri", to_uri(url))
        pbin = Gst.ElementFactory.make("parsebin", "pbin")
        pl.add(src)
        pl.add(pbin)
        src.connect("pad-added", lambda _s, pad, pb=pbin: self._on_src_pad(pb, pad))
        pbin.connect("pad-added", lambda _p, pad, pl=pl, mux=mux: self._on_parsed_pad(pl, mux, pad))
        if pl.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            pl.set_state(Gst.State.NULL)
            return "Snimanje (kopija) nije moglo da se pokrene."
        now = time.time()
        self.pipeline, self.bus = pl, pl.get_bus()
        self.stopping = False
        self.filled = True            # nema veštačke tišine/crne slike u režimu kopije
        self.t_start = now
        self.last_size = 0
        self.last_growth = now
        self.last_check = now
        return None

    @staticmethod
    def _on_src_pad(pbin, pad):
        sink = pbin.get_static_pad("sink")
        if not sink.is_linked():
            pad.link(sink)

    @staticmethod
    def _on_parsed_pad(pl, mux, pad):
        try:
            caps = pad.get_current_caps() or pad.query_caps(None)
            name = ""
            if caps is not None and not caps.is_any() and caps.get_size() > 0:
                name = caps.get_structure(0).get_name()
            kind = "video" if name.startswith("video/") else ("audio" if name.startswith("audio/") else None)
            q = Gst.ElementFactory.make("queue")
            q.set_property("max-size-buffers", 0)
            q.set_property("max-size-bytes", 0)
            q.set_property("max-size-time", 4000000000)
            pl.add(q)
            q.sync_state_with_parent()
            mux_pad = None
            if kind is not None and name not in ("video/x-raw", "audio/x-raw"):
                for tpl in (f"{kind}_%u", "sink_%d"):
                    try:
                        mux_pad = mux.request_pad_simple(tpl)
                    except Exception:
                        mux_pad = None
                    if mux_pad is not None:
                        break
            pad.link(q.get_static_pad("sink"))
            if mux_pad is not None:
                q.get_static_pad("src").link(mux_pad)
            else:
                print(f"[REC] tok '{name or pad.get_name()}' se ne snima (odbačen)", flush=True)
                fs = Gst.ElementFactory.make("fakesink")
                fs.set_property("sync", False)
                fs.set_property("async", False)
                pl.add(fs)
                fs.sync_state_with_parent()
                q.link(fs)
        except Exception:
            import traceback
            traceback.print_exc()

    def _fill_missing(self):
        # strim bez audia (ili videa): dodaj tišinu / crnu sliku da muxer ne čeka u beskraj
        pl = self.pipeline
        for qname, desc in (
                ("aq", "audiotestsrc wave=silence is-live=true ! audio/x-raw,rate=48000,channels=2"),
                ("vq", "videotestsrc pattern=black is-live=true ! video/x-raw,width=1280,height=720,framerate=25/1")):
            pad = pl.get_by_name(qname).get_static_pad("sink")
            if pad.is_linked():
                continue
            b = Gst.parse_bin_from_description(desc, True)
            pl.add(b)
            b.get_static_pad("src").link(pad)
            b.sync_state_with_parent()

    def _teardown(self):
        if self.pipeline is not None:
            pl = self.pipeline
            self.pipeline = None
            self.bus = None
            pl.set_state(Gst.State.NULL)
            pl.get_state(2 * Gst.SECOND)
        self.pipeline = None
        self.bus = None

    def _ended(self, text):
        self._teardown()
        if self.stopping or not self.wanted:
            self.wanted = False
            self.stopping = False
        else:
            self.retry_at = time.time() + 3.0     # automatski nastavi u novom fajlu
        return text

    def stop_now(self):
        self._teardown()
        self.wanted = False
        self.stopping = False

    def poll(self):
        """Vraća None ili poruku za statusnu liniju."""
        now = time.time()
        if self.pipeline is None:
            if self.wanted and self.retry_at and now >= self.retry_at:
                self.retry_at = 0.0
                err = self._launch()
                if err:
                    self.error = err
                    self.retry_at = now + 5.0
            return None
        if not self.filled and not self.stopping and now - self.t_start > 2.5:
            self.filled = True
            try:
                self._fill_missing()
            except Exception as e:
                self.error = str(e)
        while self.bus is not None:
            msg = self.bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.EOS)
            if msg is None:
                break
            if msg.type == Gst.MessageType.ERROR:
                err, _ = msg.parse_error()
                self.error = err.message
                return self._ended(f"Snimanje prekinuto ({err.message}) – {os.path.basename(self.path)}")
            return self._ended(f"Snimak sačuvan: {self.path}")
        if self.stopping and now > self.stop_deadline:
            return self._ended(f"Snimak zatvoren (timeout): {self.path}")
        # watchdog: fajl ne raste -> izvor je stao
        if not self.stopping and now - self.t_start > 15 and now - self.last_check >= 1.0:
            self.last_check = now
            try:
                size = os.path.getsize(self.path)
            except OSError:
                size = 0
            if size > self.last_size:
                self.last_size, self.last_growth = size, now
            elif now - self.last_growth > 10:
                return self._ended("Nema podataka 10 s – snimak zatvoren, nastavlja u novom fajlu")
        return None


# --------------------------------------------------------------------------
# Video + audio meter widget
# --------------------------------------------------------------------------
class VideoView(QWidget):
    clicked = Signal()
    double_clicked = Signal()

    def __init__(self, rx):
        super().__init__()
        self.rx = rx
        self.setMinimumSize(200, 110)
        self.disp = [DB_FLOOR] * 2
        self.hold = [DB_FLOOR] * 2
        self.hold_t = [0.0] * 2
        self._fid = -1
        self.selected = False
        self.listen = False
        self.onair = False
        self.rec_text = ""

    def tick(self, dt):
        now = time.time()
        fresh = (now - self.rx.last_level) < 0.5
        peaks = self.rx.peaks if fresh else []
        n = min(8, max(2, len(self.rx.peaks)))
        if n != len(self.disp):
            self.disp = (self.disp + [DB_FLOOR] * 8)[:n]
            self.hold = (self.hold + [DB_FLOOR] * 8)[:n]
            self.hold_t = (self.hold_t + [0.0] * 8)[:n]
        for i in range(n):
            target = peaks[i] if i < len(peaks) else DB_FLOOR
            if target >= self.disp[i]:
                self.disp[i] = target
            else:
                self.disp[i] = max(target, self.disp[i] - 45.0 * dt)
            if target >= self.hold[i]:
                self.hold[i] = target
                self.hold_t[i] = now
            elif now - self.hold_t[i] > 1.2:
                self.hold[i] = max(DB_FLOOR, self.hold[i] - 20.0 * dt)
        fid = self.rx.frame_id
        if fid != self._fid or self.rx.frame is None:
            self._fid = fid
            self.update()                       # stigao je novi frejm: iscrtaj sve
        else:
            mw = 36 + len(self.disp) * 10
            self.update(self.width() - mw, 0, mw, self.height())   # samo merač

    # ---- crtanje --------------------------------------------------------
    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(8, 8, 8))
        nch = len(self.disp)
        mw = 36 + nch * 10
        vw = max(10, self.width() - mw)
        vr = QRectF(0, 0, vw, self.height())

        img = self.rx.frame
        if img is not None and not img.isNull():
            sc = min(vr.width() / img.width(), vr.height() / img.height())
            tw, th = img.width() * sc, img.height() * sc
            target = QRectF((vr.width() - tw) / 2, (vr.height() - th) / 2, tw, th)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.drawImage(target, img)
        else:
            p.setPen(QColor(140, 140, 140))
            f = QFont()
            f.setPointSize(13)
            f.setBold(True)
            p.setFont(f)
            opt = QTextOption(Qt.AlignmentFlag.AlignCenter)
            opt.setWrapMode(QTextOption.WrapMode.WordWrap)
            p.drawText(vr.adjusted(10, 0, -10, 0), self.rx.status_text(), opt)

        # bedževi
        x = 6
        for text, color, on in (("AUDIO", QColor(0, 160, 60), self.listen),
                                ("DECKLINK OUT", QColor(210, 30, 30), self.onair),
                                (self.rec_text, QColor(150, 0, 0), bool(self.rec_text))):
            if not on:
                continue
            f = QFont()
            f.setPointSize(8)
            f.setBold(True)
            p.setFont(f)
            tw = p.fontMetrics().horizontalAdvance(text) + 12
            r = QRectF(x, 6, tw, 18)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color)
            p.drawRoundedRect(r, 3, 3)
            p.setPen(QColor(255, 255, 255))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)
            x += tw + 6

        if img is not None and self.rx.fps > 0:
            f = QFont()
            f.setPointSize(8)
            p.setFont(f)
            p.setPen(QColor(255, 230, 0))
            p.drawText(QRectF(6, self.height() - 22, vw - 12, 18),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       f"{self.rx.decoder or '?'} | {self.rx.fps:.1f} fps")

        self._paint_meter(p, QRectF(vw, 0, mw, self.height()), nch)

        if self.selected:
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(255, 200, 0), 4))
            p.drawRect(self.rect().adjusted(2, 2, -2, -2))
        p.end()

    def _paint_meter(self, p, r, nch):
        p.fillRect(r, QColor(18, 18, 18))
        top, bottom = r.top() + 10, r.bottom() - 10
        H = bottom - top

        def y_of(db):
            return bottom - db_frac(db) * H

        # skala
        f = QFont()
        f.setPointSize(7)
        p.setFont(f)
        for t in (0, -6, -12, -18, -24, -36, -48):
            y = y_of(t)
            p.setPen(QColor(90, 90, 90))
            p.drawLine(int(r.left() + 26), int(y), int(r.left() + 30), int(y))
            p.setPen(QColor(170, 170, 170))
            p.drawText(QRectF(r.left(), y - 7, 25, 14),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, str(t))

        zones = [(DB_FLOOR, -12, QColor(0, 200, 70)),
                 (-12, -3, QColor(235, 205, 0)),
                 (-3, 0, QColor(235, 45, 45))]
        bw = 7
        for i in range(nch):
            x = r.left() + 33 + i * 10
            p.fillRect(QRectF(x, top, bw, H), QColor(40, 40, 40))
            lvl = self.disp[i]
            for lo, hi, col in zones:
                top_db = min(lvl, hi)
                if top_db > lo:
                    p.fillRect(QRectF(x, y_of(top_db), bw, y_of(lo) - y_of(top_db)), col)
            if self.hold[i] > DB_FLOOR + 1:
                p.fillRect(QRectF(x, y_of(self.hold[i]) - 1, bw, 2), QColor(255, 255, 255))

    # ---- miš ------------------------------------------------------------
    def mousePressEvent(self, _e):
        self.clicked.emit()

    def mouseDoubleClickEvent(self, _e):
        self.double_clicked.emit()


# --------------------------------------------------------------------------
# Dijalog za podešavanje strima
# --------------------------------------------------------------------------
class StreamDialog(QDialog):
    def __init__(self, name, url, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Podešavanje strima")
        form = QFormLayout(self)
        self.e_name = QLineEdit(name)
        self.e_url = QLineEdit(url)
        self.e_url.setMinimumWidth(520)
        self.e_url.setPlaceholderText("udp://239.1.1.1:5000 | srt://host:9000 | rtsp://... | http://...m3u8 | ndi://NAZIV (Izvor) | test")
        form.addRow("Naziv:", self.e_name)
        form.addRow("URL:", self.e_url)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        b_ndi = QPushButton("Pretraži NDI izvore...")
        b_ndi.clicked.connect(self.pick_ndi)
        form.addRow("", b_ndi)
        form.addRow(bb)

    def pick_ndi(self):
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            names = discover_ndi()
        finally:
            QApplication.restoreOverrideCursor()
        if names is None:
            QMessageBox.warning(self, "NDI", "NDI plugin (gst-plugin-ndi) nije instaliran.")
            return
        if not names:
            QMessageBox.information(
                self, "NDI",
                "Nijedan NDI izvor nije pronađen.\nMožeš ručno upisati:\n"
                "ndi://NAZIV (Izvor)   ili   ndi-ip://192.168.1.50:5961")
            return
        item, ok = QInputDialog.getItem(self, "NDI izvori", "Izaberi izvor:", names, 0, False)
        if ok and item:
            self.e_url.setText("ndi://" + item)
            if self.e_name.text().startswith("CAM "):
                self.e_name.setText(item)


# --------------------------------------------------------------------------
# Jedan monitor (tile)
# --------------------------------------------------------------------------
class Tile(QFrame):
    sig_select = Signal(int)
    sig_edit = Signal(int)
    sig_audio = Signal(int)
    sig_dl = Signal(int)
    sig_rec = Signal(int)
    sig_power = Signal(int)

    def __init__(self, idx, name, url):
        super().__init__()
        self.idx, self.name, self.url = idx, name, url
        self.enabled = True
        self._last_state_txt = ""
        self.rx = Receiver()
        self.rec = Recorder()
        self.setFrameShape(QFrame.Shape.StyledPanel)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(2)
        head = QHBoxLayout()
        self.lbl = QLabel()
        self.lbl.setStyleSheet("font-weight:600;")
        self.lbl_state = QLabel("")
        self.b_audio = QPushButton("🔊")
        self.b_audio.setCheckable(True)
        self.b_audio.setToolTip("Audio ovog monitora na zvučnik računara")
        self.b_dl = QPushButton("DL")
        self.b_dl.setCheckable(True)
        self.b_dl.setToolTip("Pusti ovaj monitor na DeckLink izlaz")
        self.b_rec = QPushButton("⏺")
        self.b_rec.setCheckable(True)
        self.b_rec.setToolTip("Snimanje ovog strima u fajl")
        self.b_rec.setStyleSheet("QPushButton:checked { background:#a01010; border-color:#e03030; }")
        self.b_pow = QPushButton("⏻")
        self.b_pow.setCheckable(True)
        self.b_pow.setChecked(True)
        self.b_pow.setToolTip("Uključi / isključi prikaz ovog strima (štedi CPU).\n"
                              "Snimanje i DeckLink izlaz se ne gase.")
        self.b_cfg = QPushButton("⚙")
        self.b_cfg.setToolTip("Naziv i URL strima")
        for b in (self.b_audio, self.b_dl, self.b_rec, self.b_cfg, self.b_pow):
            b.setFixedWidth(34)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        head.addWidget(self.lbl)
        head.addStretch(1)
        head.addWidget(self.lbl_state)
        head.addWidget(self.b_audio)
        head.addWidget(self.b_dl)
        head.addWidget(self.b_rec)
        head.addWidget(self.b_cfg)
        head.addWidget(self.b_pow)
        lay.addLayout(head)

        self.view = VideoView(self.rx)
        lay.addWidget(self.view, 1)

        self.view.clicked.connect(lambda: self.sig_select.emit(self.idx))
        self.view.double_clicked.connect(lambda: self.sig_edit.emit(self.idx))
        self.b_cfg.clicked.connect(lambda: self.sig_edit.emit(self.idx))
        self.b_audio.clicked.connect(lambda: self.sig_audio.emit(self.idx))
        self.b_dl.clicked.connect(lambda: self.sig_dl.emit(self.idx))
        self.b_rec.clicked.connect(lambda: self.sig_rec.emit(self.idx))
        self.b_pow.clicked.connect(lambda: self.sig_power.emit(self.idx))
        self.refresh_label()

    def refresh_label(self):
        self.lbl.setText(f"{self.idx + 1} · {self.name}")

    def set_flags(self, selected, listen, onair):
        self.view.selected = selected
        self.view.listen = listen
        self.view.onair = onair
        self.b_audio.setChecked(listen)
        self.b_dl.setChecked(onair)

    def tick(self, dt):
        self.rx.poll()
        color = {"LIVE": "#35d07f", "CONNECTING": "#e0c000", "NO SIGNAL": "#ff5050"}.get(self.rx.state, "#888")
        txt = f"<span style='color:{color}'>● {self.rx.state}</span>"
        if txt != self._last_state_txt:
            self._last_state_txt = txt
            self.lbl_state.setText(txt)
        self.view.rec_text = self.rec.label()
        self.b_rec.setChecked(self.rec.wanted)
        self.b_pow.setChecked(self.enabled)
        self.view.tick(dt)


# --------------------------------------------------------------------------
# Glavni prozor
# --------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("IP Multi-Viewer")
        self.resize(1600, 900)
        cfg = self.load_config()

        self.n = cfg.get("layout", 4)
        self.sel = 0
        self.audio_idx = None
        self.dl_idx = None
        self.dl = DeckLinkOutput()
        self.last_tick = time.time()
        self.latency_ms = int(cfg.get("latency_ms", 1000))
        self.rec_dir = cfg.get("rec_dir", DEFAULT_REC_DIR)
        self.rec_container = cfg.get("rec_container", "MKV")
        self.rec_kbps = int(cfg.get("rec_kbps", 8000))
        self.rec_mode = cfg.get("rec_mode", "encode")          # "encode" ili "copy"
        Recorder.encoder_pref = cfg.get("rec_encoder", "auto")   # npr. "nvh264enc"

        streams = cfg.get("streams", [])
        self.tiles = []
        for i in range(MAX_TILES):
            s = streams[i] if i < len(streams) else {}
            t = Tile(i, s.get("name", f"CAM {i + 1}"), s.get("url", ""))
            t.enabled = bool(s.get("enabled", True))
            t.rx.off = not t.enabled
            t.rx.latency_ms = self.latency_ms
            t.sig_power.connect(self.toggle_power)
            t.sig_select.connect(self.select)
            t.sig_edit.connect(self.edit_stream)
            t.sig_audio.connect(self.toggle_audio)
            t.sig_dl.connect(self.toggle_dl)
            t.sig_rec.connect(self.toggle_rec)
            self.tiles.append(t)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(6, 6, 6, 6)

        # ---- toolbar ----
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Layout:"))
        self.cb_layout = QComboBox()
        for n in (2, 4, 8):
            self.cb_layout.addItem(f"{n} monitora", n)
        self.cb_layout.setCurrentIndex({2: 0, 4: 1, 8: 2}.get(self.n, 1))
        self.cb_layout.currentIndexChanged.connect(
            lambda _i: self.apply_layout(self.cb_layout.currentData()))
        bar.addWidget(self.cb_layout)
        bar.addSpacing(16)

        self.b_audio = QPushButton("🔊 Audio na zvučnik (selektovan)")
        self.b_audio.setCheckable(True)
        self.b_audio.clicked.connect(lambda: self.toggle_audio(self.sel))
        bar.addWidget(self.b_audio)
        bar.addWidget(QLabel("Jačina:"))
        self.sl_vol = QSlider(Qt.Orientation.Horizontal)
        self.sl_vol.setRange(0, 100)
        self.sl_vol.setValue(cfg.get("volume", 80))
        self.sl_vol.setFixedWidth(120)
        self.sl_vol.valueChanged.connect(self.on_volume)
        bar.addWidget(self.sl_vol)
        bar.addSpacing(16)

        bar.addWidget(QLabel("Buffer:"))
        self.sp_lat = QSpinBox()
        self.sp_lat.setRange(0, 10000)
        self.sp_lat.setSingleStep(100)
        self.sp_lat.setSuffix(" ms")
        self.sp_lat.setValue(self.latency_ms)
        self.sp_lat.setToolTip("Bafer za uživo strimove (RTMP/UDP/SRT/RTSP/NDI).\n"
                               "Veći = glatkija slika i zvuk, ali veće kašnjenje.\n"
                               "0 = podrazumevani bafer. Za NDI i test izvor: 0 = bez sinhronizacije.")
        self.sp_lat.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._lat_timer = QTimer(self)
        self._lat_timer.setSingleShot(True)
        self._lat_timer.timeout.connect(self.apply_latency)
        self.sp_lat.valueChanged.connect(lambda _v: self._lat_timer.start(700))
        bar.addWidget(self.sp_lat)
        bar.addSpacing(16)

        bar.addWidget(QLabel("DeckLink uređaj:"))
        self.sp_dev = QSpinBox()
        self.sp_dev.setRange(0, 15)
        self.sp_dev.setValue(cfg.get("device", 0))
        self.sp_dev.setToolTip("device-number (0 = prvi izlaz; Duo/Quad kartice imaju više)")
        bar.addWidget(self.sp_dev)
        b_dev = QPushButton("Uređaji...")
        b_dev.setToolTip("Prikaži DeckLink uređaje koje GStreamer vidi")
        b_dev.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b_dev.clicked.connect(self.show_decklink_devices)
        bar.addWidget(b_dev)
        bar.addWidget(QLabel("Mod:"))
        self.cb_mode = QComboBox()
        self.cb_mode.addItems(list(DECKLINK_MODES.keys()))
        self.cb_mode.setCurrentText(cfg.get("mode", "1080p25"))
        bar.addWidget(self.cb_mode)
        self.chk_dl_audio = QCheckBox("Audio")
        self.chk_dl_audio.setChecked(bool(cfg.get("dl_audio", True)))
        self.chk_dl_audio.setToolTip("Šalji i zvuk na DeckLink izlaz (ako ne uspe, šalje se samo slika)")
        self.chk_dl_audio.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        bar.addWidget(self.chk_dl_audio)
        self.b_dl = QPushButton("▶ Na DeckLink (selektovan)")
        self.b_dl.setCheckable(True)
        self.b_dl.clicked.connect(lambda: self.toggle_dl(self.sel))
        bar.addWidget(self.b_dl)
        bar.addStretch(1)
        for w in (self.cb_layout, self.cb_mode, self.sp_dev, self.sl_vol,
                  self.b_audio, self.b_dl):
            w.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        root.addLayout(bar)

        bar2 = QHBoxLayout()
        bar2.addWidget(QLabel("Snimanje:"))
        rec_css = "QPushButton:checked { background:#a01010; border-color:#e03030; }"
        self.b_rec = QPushButton("⏺ Snimaj selektovan")
        self.b_rec.setCheckable(True)
        self.b_rec.setStyleSheet(rec_css)
        self.b_rec.clicked.connect(lambda: self.toggle_rec(self.sel))
        bar2.addWidget(self.b_rec)
        self.b_rec_all = QPushButton("⏺ Snimaj sve")
        self.b_rec_all.setCheckable(True)
        self.b_rec_all.setStyleSheet(rec_css)
        self.b_rec_all.clicked.connect(self.toggle_rec_all)
        bar2.addWidget(self.b_rec_all)
        bar2.addWidget(QLabel("Format:"))
        self.cb_cont = QComboBox()
        self.cb_cont.addItems(list(REC_CONTAINERS.keys()))
        self.cb_cont.setCurrentText(self.rec_container)
        self.cb_cont.currentTextChanged.connect(lambda s: setattr(self, "rec_container", s))
        bar2.addWidget(self.cb_cont)
        bar2.addWidget(QLabel("Režim:"))
        self.cb_rec_mode = QComboBox()
        self.cb_rec_mode.addItem("Rekodiranje (H.264)", "encode")
        self.cb_rec_mode.addItem("Kopija (bez rekodiranja)", "copy")
        self.cb_rec_mode.setCurrentIndex(1 if self.rec_mode == "copy" else 0)
        self.cb_rec_mode.currentIndexChanged.connect(self._on_rec_mode)
        bar2.addWidget(self.cb_rec_mode)
        bar2.addWidget(QLabel("Video bitrate:"))
        self.sp_kbps = QSpinBox()
        self.sp_kbps.setRange(500, 100000)
        self.sp_kbps.setSingleStep(500)
        self.sp_kbps.setSuffix(" kbps")
        self.sp_kbps.setValue(self.rec_kbps)
        self.sp_kbps.valueChanged.connect(lambda v: setattr(self, "rec_kbps", v))
        self.sp_kbps.setEnabled(self.rec_mode == "encode")
        bar2.addWidget(self.sp_kbps)
        self.b_dir = QPushButton()
        self.b_dir.clicked.connect(self.choose_dir)
        self._update_dir_button()
        bar2.addWidget(self.b_dir)
        bar2.addStretch(1)
        for w in (self.b_rec, self.b_rec_all, self.cb_cont, self.cb_rec_mode, self.sp_kbps, self.b_dir):
            w.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        root.addLayout(bar2)

        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(6)
        root.addWidget(self.grid_host, 1)

        self.status = QLabel("")
        self.statusBar().addWidget(self.status, 1)
        self.lbl_rec = QLabel("")
        self.statusBar().addPermanentWidget(self.lbl_rec)
        self.lbl_pipes = QLabel("")
        self.statusBar().addPermanentWidget(self.lbl_pipes)

        # prečice
        for i in range(MAX_TILES):
            QShortcut(QKeySequence(str(i + 1)), self,
                      activated=lambda i=i: self.select(i) if i < self.n else None)
        QShortcut(QKeySequence("A"), self, activated=lambda: self.toggle_audio(self.sel))
        QShortcut(QKeySequence("D"), self, activated=lambda: self.toggle_dl(self.sel))
        QShortcut(QKeySequence("R"), self, activated=lambda: self.toggle_rec(self.sel))
        QShortcut(QKeySequence("Shift+R"), self, activated=self.toggle_rec_all)
        QShortcut(QKeySequence("S"), self, activated=lambda: self.toggle_power(self.sel))
        QShortcut(QKeySequence("F11"), self, activated=self.toggle_fullscreen)
        QShortcut(QKeySequence("Esc"), self, activated=self.showNormal)

        self.apply_layout(self.n)
        self.on_volume(self.sl_vol.value())

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(33)

    # ---- konfiguracija --------------------------------------------------
    @staticmethod
    def load_config():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save_config(self):
        cfg = {
            "layout": self.n,
            "volume": self.sl_vol.value(),
            "device": self.sp_dev.value(),
            "mode": self.cb_mode.currentText(),
            "rec_dir": self.rec_dir,
            "rec_container": self.rec_container,
            "rec_kbps": self.rec_kbps,
            "rec_mode": self.rec_mode,
            "dl_audio": self.chk_dl_audio.isChecked(),
            "rec_encoder": Recorder.encoder_pref,
            "latency_ms": self.latency_ms,
            "streams": [{"name": t.name, "url": t.url, "enabled": t.enabled} for t in self.tiles],
        }
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print("Ne mogu da sačuvam konfiguraciju:", e)

    # ---- layout ---------------------------------------------------------
    def apply_layout(self, n):
        self.n = n
        for i in reversed(range(self.grid.count())):
            self.grid.itemAt(i).widget().setParent(self.grid_host)
        cols = GRID_COLS[n]
        rows = math.ceil(n / cols)
        for c in range(cols):
            self.grid.setColumnStretch(c, 1)
        for r in range(rows):
            self.grid.setRowStretch(r, 1)
        for i, t in enumerate(self.tiles):
            if i < n:
                self.grid.addWidget(t, i // cols, i % cols)
                t.show()
                t.rx.latency_ms = self.latency_ms
                t.rx.off = not t.enabled
                if t.enabled:
                    t.rx.start(t.url, DECODE_WIDTH[n])
                else:
                    t.rx.stop()
                t.rx.set_volume(self.sl_vol.value() / 100.0)
            else:
                t.hide()
                t.rx.stop()
        if self.audio_idx is not None and self.audio_idx >= n:
            self.audio_idx = None
        if self.sel >= n:
            self.sel = 0
        self.refresh_flags()

    def toggle_fullscreen(self):
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    # ---- selekcija / audio / DeckLink ----------------------------------
    def select(self, idx):
        self.sel = idx
        self.refresh_flags()

    def refresh_flags(self):
        for i, t in enumerate(self.tiles):
            t.rx.set_listen(i == self.audio_idx)
            t.set_flags(i == self.sel, i == self.audio_idx, i == self.dl_idx and self.dl.running)
        self.b_audio.setChecked(self.audio_idx is not None and self.audio_idx == self.sel)
        self.b_dl.setChecked(self.dl.running and self.dl_idx == self.sel)
        parts = [f"Selektovan: {self.sel + 1} ({self.tiles[self.sel].name})"]
        parts.append(f"Audio: {self.audio_idx + 1}" if self.audio_idx is not None else "Audio: -")
        if self.dl.running and self.dl_idx is not None:
            parts.append(f"DeckLink OUT ← monitor {self.dl_idx + 1} "
                         f"({self.cb_mode.currentText()}, dev {self.sp_dev.value()})")
        else:
            parts.append("DeckLink: isključen")
        self.status.setText("   |   ".join(parts))

    def toggle_audio(self, idx):
        self.sel = idx
        self.audio_idx = None if self.audio_idx == idx else idx
        self.refresh_flags()

    def toggle_dl(self, idx):
        self.sel = idx
        if self.dl.running and self.dl_idx == idx:
            self.dl.stop()
            self.dl_idx = None
        else:
            url = self.tiles[idx].url
            if not url.strip():
                QMessageBox.information(self, "DeckLink", "Ovaj monitor nema podešen URL.")
                self.refresh_flags()
                return
            err = self.dl.start(url, self.sp_dev.value(), self.cb_mode.currentText(),
                                self.chk_dl_audio.isChecked())
            if err:
                self.dl_idx = None
                QMessageBox.warning(self, "DeckLink", err)
            else:
                self.dl_idx = idx
                if self.dl.note:
                    QMessageBox.information(self, "DeckLink", self.dl.note)
        self.refresh_flags()

    def show_decklink_devices(self):
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            text = list_decklink_devices()
        finally:
            QApplication.restoreOverrideCursor()
        QMessageBox.information(self, "DeckLink uređaji", text)

    # ---- gašenje prikaza / bafer ----------------------------------------
    def toggle_power(self, idx):
        t = self.tiles[idx]
        self.sel = idx
        t.enabled = not t.enabled
        t.rx.off = not t.enabled
        t.rx.latency_ms = self.latency_ms
        if t.enabled:
            t.rx.start(t.url, DECODE_WIDTH[self.n])
        else:
            t.rx.stop()
        self.save_config()
        self.refresh_flags()

    def apply_latency(self):
        self.latency_ms = self.sp_lat.value()
        for i, t in enumerate(self.tiles):
            t.rx.latency_ms = self.latency_ms
            if i < self.n and t.enabled and t.url.strip():
                t.rx.start(t.url, DECODE_WIDTH[self.n])
        self.save_config()

    # ---- snimanje -------------------------------------------------------
    def _update_dir_button(self):
        name = os.path.basename(self.rec_dir.rstrip("/\\")) or self.rec_dir
        self.b_dir.setText("📁 " + name)
        self.b_dir.setToolTip(f"Folder za snimke: {self.rec_dir}")

    def choose_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Folder za snimke", self.rec_dir)
        if d:
            self.rec_dir = d
            self._update_dir_button()

    def _on_rec_mode(self, _i):
        self.rec_mode = self.cb_rec_mode.currentData()
        self.sp_kbps.setEnabled(self.rec_mode == "encode")

    def toggle_rec(self, idx):
        self.sel = idx
        t = self.tiles[idx]
        if t.rec.wanted:
            t.rec.request_stop()
        elif not t.url.strip():
            QMessageBox.information(self, "Snimanje", "Ovaj monitor nema podešen URL.")
        else:
            err = t.rec.start(t.url, t.name, self.rec_dir, self.rec_container, self.rec_kbps, self.rec_mode)
            if err:
                QMessageBox.warning(self, "Snimanje", err)
        self.refresh_flags()

    def toggle_rec_all(self):
        active = [t for t in self.tiles if t.rec.wanted]
        if active:
            for t in active:
                t.rec.request_stop()
        else:
            errors = []
            for t in self.tiles[:self.n]:
                if t.url.strip():
                    err = t.rec.start(t.url, t.name, self.rec_dir, self.rec_container, self.rec_kbps, self.rec_mode)
                    if err:
                        errors.append(f"{t.name}: {err}")
            if errors:
                QMessageBox.warning(self, "Snimanje", "\n\n".join(errors))
        self.refresh_flags()

    def on_volume(self, v):
        for t in self.tiles:
            t.rx.set_volume(v / 100.0)

    # ---- podešavanje strima --------------------------------------------
    def edit_stream(self, idx):
        t = self.tiles[idx]
        dlg = StreamDialog(t.name, t.url, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            t.name = dlg.e_name.text().strip() or f"CAM {idx + 1}"
            t.url = dlg.e_url.text().strip()
            t.refresh_label()
            t.rx.latency_ms = self.latency_ms
            if t.enabled:
                t.rx.start(t.url, DECODE_WIDTH[self.n])
            self.save_config()
            self.refresh_flags()

    # ---- glavni tajmer --------------------------------------------------
    def tick(self):
        now = time.time()
        dt = min(0.2, now - self.last_tick)
        self.last_tick = now
        for t in self.tiles[:self.n]:
            t.tick(dt)
        for t in self.tiles:                      # snimanje radi i na skrivenim monitorima
            msg = t.rec.poll()
            if msg:
                self.statusBar().showMessage(f"[{t.name}] {msg}", 10000)
        nrec = sum(1 for t in self.tiles if t.rec.wanted)
        self.lbl_rec.setText(f"<span style='color:#ff5050'>● REC ×{nrec}</span>  ({self.rec_dir})" if nrec else "")
        nv = sum(1 for t in self.tiles if t.rx.pipeline is not None)
        nr = sum(1 for t in self.tiles if t.rec.pipeline is not None)
        self.lbl_pipes.setText(f"  [aktivni pipeline-ovi: prikaz {nv}, snimanje {nr}]")
        self.b_rec.setChecked(self.tiles[self.sel].rec.wanted)
        self.b_rec_all.setChecked(nrec > 0)
        err = self.dl.poll()
        if err:
            self.dl_idx = None
            self.refresh_flags()
            QMessageBox.warning(self, "DeckLink", f"DeckLink izlaz zaustavljen:\n{err}")

    def closeEvent(self, e):
        if any(t.rec.wanted for t in self.tiles):
            r = QMessageBox.question(self, "Snimanje u toku",
                                     "Snimanje je u toku. Zaustaviti snimanje i zatvoriti program?")
            if r != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
        self.timer.stop()
        self.save_config()
        self.dl.stop()
        for t in self.tiles:
            t.rec.request_stop()
        t_end = time.time() + 8.0              # sačekaj da se fajlovi pravilno zatvore
        while time.time() < t_end and any(t.rec.pipeline is not None for t in self.tiles):
            for t in self.tiles:
                t.rec.poll()
            time.sleep(0.05)
        for t in self.tiles:
            t.rec.stop_now()
            t.rx.stop()
        super().closeEvent(e)


def selftest():
    """Provera paketa: 'IPMonitor.exe --selftest' (koristi se i u GitHub Actions)."""
    need = ["playbin", "uridecodebin", "appsink", "level", "videoconvert", "avdec_h264",
            "avdec_aac", "x264enc", "matroskamux", "autoaudiosink"]
    missing = [n for n in need if Gst.ElementFactory.find(n) is None]
    print("GStreamer:", Gst.version_string())
    print("decklink plugin:", Gst.ElementFactory.find("decklinkvideosink") is not None)
    print("ndi plugin:", Gst.ElementFactory.find("ndisrc") is not None)
    print("NEDOSTAJE:", missing or "nista")
    sys.stdout.flush()
    os._exit(1 if missing else 0)


def main():
    if "--selftest" in sys.argv:
        selftest()
    global ALLOW_HW_DECODE
    ALLOW_HW_DECODE = bool(MainWindow.load_config().get("hw_decode", False))
    if not ALLOW_HW_DECODE:
        disable_hw_decoders()
        tune_decoder_ranks()
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet("""
        QWidget { background:#1b1b1f; color:#e6e6e6; }
        QPushButton { background:#2c2c33; border:1px solid #444; padding:4px 8px; border-radius:3px; }
        QPushButton:checked { background:#0a7d3c; border-color:#12b35a; }
        QPushButton:hover { background:#3a3a44; }
        QFrame { border:1px solid #333; }
        QLabel, QStatusBar { border:none; }
        QLineEdit, QComboBox, QSpinBox { background:#26262c; border:1px solid #444; padding:2px; }
    """)
    w = MainWindow()
    w.show()
    code = app.exec()
    sys.stdout.flush()
    os._exit(code)          # bez zaostalih GStreamer niti / python.exe procesa u pozadini


if __name__ == "__main__":
    main()
