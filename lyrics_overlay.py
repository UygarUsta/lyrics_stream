import asyncio, bisect, json, re, sys, threading, time
from datetime import datetime, timezone
from pathlib import Path

import requests
from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (QApplication, QGraphicsDropShadowEffect, QLabel,
                               QMenu, QStyle, QSystemTrayIcon, QVBoxLayout, QWidget)
from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as MediaManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
)

import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t

HWND_TOPMOST = wintypes.HWND(-1)
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x0001, 0x0002, 0x0010, 0x0040
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x00000080, 0x08000000

# ---------------- Ayarlar ----------------
OFFSET = 0.3          # sn. Sözler geç kalıyorsa artır, erken geliyorsa azalt
WIDTH, HEIGHT = 560, 170
MARGIN = 24           # ekran kenarından uzaklık (varsayılan: sağ alt köşe)
CACHE_DIR = Path.home() / ".lyrics_cache"
CACHE_DIR.mkdir(exist_ok=True)
CONFIG_FILE = Path.home() / ".lyrics_overlay.json"
PREFERRED_APPS = ("chrome", "msedge", "firefox", "brave", "opera", "youtube")
HEADERS = {"User-Agent": "personal-lyrics-overlay/0.1"}


# ---------------- Konum kaydı ----------------
def load_position():
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return int(data["x"]), int(data["y"])
    except Exception:
        return None


def save_position(x, y):
    try:
        CONFIG_FILE.write_text(json.dumps({"x": x, "y": y}), encoding="utf-8")
    except OSError as e:
        print("Konum kaydedilemedi:", e)


# ---------------- Medya durumu ----------------
class MediaState:
    def __init__(self):
        self.lock = threading.Lock()
        self.track = None          # (title, artist, album, duration)
        self.anchor_pos = 0.0      # bilinen son pozisyon (sn)
        self.anchor_time = time.monotonic()
        self.playing = False

    def position(self):
        with self.lock:
            if self.playing:
                return self.anchor_pos + (time.monotonic() - self.anchor_time)
            return self.anchor_pos


def pick_session(manager):
    """Tarayıcı/YT Music oturumunu tercih et, yoksa aktif oturumu al."""
    for s in manager.get_sessions():
        app = (s.source_app_user_model_id or "").lower()
        if any(k in app for k in PREFERRED_APPS):
            return s
    return manager.get_current_session()


async def poll_media(state: MediaState):
    manager = await MediaManager.request_async()
    last_reported = None
    while True:
        try:
            session = pick_session(manager)
            if session:
                props = await session.try_get_media_properties_async()
                tl = session.get_timeline_properties()
                playing = session.get_playback_info().playback_status == PlaybackStatus.PLAYING
                pos = tl.position.total_seconds()
                dur = tl.end_time.total_seconds()
                track = (props.title or "", props.artist or "", props.album_title or "", dur)

                with state.lock:
                    if not state.track or track[:2] != state.track[:2]:
                        state.track = track
                        last_reported = None
                    # Sadece Windows yeni bir pozisyon bildirdiğinde yeniden "çapala"
                    if pos != last_reported or playing != state.playing:
                        age = 0.0
                        if playing:
                            upd = tl.last_updated_time
                            if upd.tzinfo is None:
                                upd = upd.replace(tzinfo=timezone.utc)
                            age = (datetime.now(timezone.utc) - upd).total_seconds()
                            age = max(0.0, min(age, 5.0))
                        state.anchor_pos = pos + age
                        state.anchor_time = time.monotonic()
                        last_reported = pos
                    state.playing = playing
        except Exception as e:
            print("Medya okuma hatası:", e)
        await asyncio.sleep(0.5)


# ---------------- Sözler ----------------
def clean_title(s):
    s = re.sub(r"\s*[\(\[][^\)\]]*(official|video|audio|lyric|remaster|mv|visualizer)[^\)\]]*[\)\]]",
               "", s, flags=re.I)
    return s.strip()


def parse_lrc(lrc):
    out = []
    tag = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")
    for raw in lrc.splitlines():
        text = tag.sub("", raw).strip()
        for m in tag.finditer(raw):
            out.append((int(m.group(1)) * 60 + float(m.group(2)), text or "♪"))
    out.sort(key=lambda x: x[0])
    return out


def first_artist(artist):
    return re.split(r"\s*(?:,|&| x | feat\.?| ft\.?| ve )\s*", artist, flags=re.I)[0].strip()


def lrclib_search(params):
    try:
        r = requests.get("https://lrclib.net/api/search", headers=HEADERS, timeout=10, params=params)
        if r.ok:
            return r.json()
        print(f"  search {params} -> HTTP {r.status_code}")
    except requests.RequestException as e:
        print("  search hatası:", e)
    return []


def fetch_lyrics(title, artist, album, duration):
    key = re.sub(r"[^\w\-]+", "_", f"{artist}-{title}")[:150]
    cache = CACHE_DIR / f"{key}.lrc"
    if cache.exists():
        return parse_lrc(cache.read_text(encoding="utf-8"))

    title_c = clean_title(title)
    artist_1 = first_artist(artist)
    print(f"\n[ARAMA] başlık='{title}' -> '{title_c}' | sanatçı='{artist}' -> '{artist_1}' "
          f"| albüm='{album}' | süre={duration:.0f}s")

    lrc = ""
    # 1) Tam eşleşme
    if album and duration > 0:
        try:
            r = requests.get("https://lrclib.net/api/get", headers=HEADERS, timeout=10, params={
                "track_name": title_c, "artist_name": artist,
                "album_name": album, "duration": round(duration)})
            print(f"  get -> HTTP {r.status_code}")
            if r.ok:
                lrc = r.json().get("syncedLyrics") or ""
        except requests.RequestException as e:
            print("  get hatası:", e)

    # 2) Giderek gevşeyen aramalar
    if not lrc:
        attempts = [
            {"track_name": title_c, "artist_name": artist},
            {"track_name": title_c, "artist_name": artist_1},
            {"q": f"{title_c} {artist_1}"},
            {"track_name": title_c},
        ]
        a1 = artist_1.lower()
        for params in attempts:
            results = lrclib_search(params)
            synced = [x for x in results if x.get("syncedLyrics")]
            print(f"  {params} -> {len(results)} sonuç, {len(synced)} senkron")
            if not synced:
                continue

            def score(x):
                artist_ok = a1 in (x.get("artistName") or "").lower()
                dur_diff = abs((x.get("duration") or 0) - duration) if duration > 0 else 0
                return (not artist_ok, dur_diff)

            synced.sort(key=score)
            best = synced[0]
            # Sadece başlıkla aramada yanlış şarkıyı almamak için sanatçı ya da süre tutmalı
            if "artist_name" not in params and "q" not in params:
                if a1 not in (best.get("artistName") or "").lower() and \
                   (duration <= 0 or abs((best.get("duration") or 0) - duration) > 3):
                    print("  eşleşme güvenilir değil, atlandı")
                    continue
            print(f"  SEÇİLDİ: {best.get('artistName')} – {best.get('trackName')} "
                  f"({best.get('duration')}s)")
            lrc = best["syncedLyrics"]
            break

    if lrc:
        cache.write_text(lrc, encoding="utf-8")
    else:
        print("  -> senkron söz bulunamadı")
    return parse_lrc(lrc)


# ---------------- Overlay penceresi ----------------
class Loader(QObject):
    loaded = Signal(object, list)


class Overlay(QWidget):
    def __init__(self, state: MediaState):
        super().__init__()
        self.state = state
        self.times, self.lines = [], []
        self.current_track = None
        self.last_idx = None
        self.edit_mode = False
        self.drag_offset = None

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint |
                            Qt.Tool | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        layout = QVBoxLayout(self)
        self.labels = []
        for size, alpha in ((13, 120), (20, 255), (13, 120)):
            lbl = QLabel("")
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setWordWrap(True)
            lbl.setFont(QFont("Segoe UI", size, QFont.Bold))
            lbl.setStyleSheet(f"color: rgba(255,255,255,{alpha});")
            shadow = QGraphicsDropShadowEffect(blurRadius=12, offset=0, color=QColor(0, 0, 0, 230))
            lbl.setGraphicsEffect(shadow)
            layout.addWidget(lbl)
            self.labels.append(lbl)

        # Başlangıç konumu: kayıtlı konum varsa ve hâlâ bir ekranın içindeyse onu kullan
        geo = QApplication.primaryScreen().geometry()
        x, y = geo.right() - WIDTH - MARGIN, geo.bottom() - HEIGHT - MARGIN
        saved = load_position()
        if saved:
            for s in QApplication.screens():
                if s.geometry().contains(saved[0] + 10, saved[1] + 10):
                    x, y = saved
                    break
        self.setGeometry(x, y, WIDTH, HEIGHT)

        self.loader = Loader()
        self.loader.loaded.connect(self.on_loaded)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(50)   # saniyede 20 güncelleme

        self.top_timer = QTimer(self)
        self.top_timer.timeout.connect(self.force_topmost)
        self.top_timer.start(500)

    # ----- Pencere davranışı -----
    def showEvent(self, event):
        super().showEvent(event)
        hwnd = int(self.winId())
        ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        # Mevcut bayrakları koru, odak almama + görev çubuğunda görünmeme ekle
        user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
        self.force_topmost()

    def force_topmost(self):
        user32.SetWindowPos(int(self.winId()), HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)

    # ----- Düzenleme (sürükleme) modu -----
    def set_edit_mode(self, on):
        self.edit_mode = on
        pos = self.pos()
        # Bayrağı Qt üzerinden değiştir. Qt pencereyi yeniden oluşturduğu için tekrar gösteriyoruz.
        self.setWindowFlag(Qt.WindowTransparentForInput, not on)
        self.move(pos)
        self.show()
        if on:
            self.setCursor(Qt.SizeAllCursor)
        else:
            self.unsetCursor()
            self.drag_offset = None
            save_position(self.x(), self.y())
        self.update()

    def paintEvent(self, event):
        # Normal modda arka plan tamamen şeffaf; düzenleme modunda tutulabilir bir çerçeve çiz
        if not self.edit_mode:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor(0, 0, 0, 120))
        p.setPen(QPen(QColor(255, 255, 255, 200), 2, Qt.DashLine))
        p.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 10, 10)

    def mousePressEvent(self, event):
        if self.edit_mode and event.button() == Qt.LeftButton:
            self.drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if self.edit_mode and self.drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self.drag_offset)

    def mouseReleaseEvent(self, event):
        self.drag_offset = None

    # ----- Sözler -----
    def show_lines(self, prev, cur, nxt):
        for lbl, txt in zip(self.labels, (prev, cur, nxt)):
            lbl.setText(txt)

    def on_loaded(self, track, parsed):
        if self.current_track and track[:2] == self.current_track[:2]:
            self.times = [t for t, _ in parsed]
            self.lines = [l for _, l in parsed]
            self.last_idx = None
            if not parsed:
                self.show_lines("", "Senkron söz bulunamadı", f"{track[1]} – {track[0]}")

    def tick(self):
        track = self.state.track
        if track and (not self.current_track or track[:2] != self.current_track[:2]):
            self.current_track = track
            self.times, self.lines = [], []
            self.show_lines("", "…", f"{track[1]} – {track[0]}")
            threading.Thread(target=lambda t=track: self.loader.loaded.emit(t, fetch_lyrics(*t)),
                             daemon=True).start()
            return

        if not self.lines:
            return
        idx = bisect.bisect_right(self.times, self.state.position() + OFFSET) - 1
        if idx != self.last_idx:
            self.last_idx = idx
            get = lambda i: self.lines[i] if 0 <= i < len(self.lines) else ""
            self.show_lines(get(idx - 1), get(idx), get(idx + 1))


def main():
    state = MediaState()
    threading.Thread(target=lambda: asyncio.run(poll_media(state)), daemon=True).start()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    overlay = Overlay(state)
    overlay.show()

    # Pencere tıklama geçirgen olduğu için kontroller sistem tepsisinden
    tray = QSystemTrayIcon(app.style().standardIcon(QStyle.SP_MediaPlay), app)
    menu = QMenu()
    edit_action = menu.addAction("Konumu düzenle")
    edit_action.setCheckable(True)
    edit_action.toggled.connect(overlay.set_edit_mode)
    menu.addSeparator()
    menu.addAction("Çıkış").triggered.connect(app.quit)
    tray.setContextMenu(menu)
    tray.setToolTip("Lyrics Overlay")

    # Tepsi simgesine çift tıklamak da düzenleme modunu açıp kapatır
    tray.activated.connect(
        lambda reason: edit_action.toggle() if reason == QSystemTrayIcon.DoubleClick else None)
    tray.show()

    # Düzenleme modu açıkken çıkılırsa konumu yine de kaydet
    app.aboutToQuit.connect(lambda: save_position(overlay.x(), overlay.y()))

    sys.exit(app.exec())


if __name__ == "__main__":
    main()