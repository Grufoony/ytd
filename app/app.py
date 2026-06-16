import asyncio
import logging
import os
from pathlib import Path
import re
import requests
import subprocess
import sys
import threading

from PySide6.QtCore import Qt, Signal, QObject
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QLineEdit,
    QPushButton, QCheckBox, QComboBox, QScrollArea, QVBoxLayout,
    QHBoxLayout, QGridLayout, QFrame, QMessageBox
)

from mutagen.mp4 import MP4, MP4Cover
from shazamio import Shazam
import yt_dlp

__author__ = "Grufoony"
__version__ = "0.5 (PySide6)"

# --- Logging setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# --- FFmpeg path detection for PyInstaller ---
def get_ffmpeg_path():
    if getattr(sys, "frozen", False):
        bundle_dir = sys._MEIPASS
        p = Path(bundle_dir) / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        if p.exists():
            return p.as_posix()
        logging.warning(f"FFmpeg not found in bundle at {p}")
    return "ffmpeg"


def get_ffprobe_path():
    if getattr(sys, "frozen", False):
        bundle_dir = sys._MEIPASS
        p = Path(bundle_dir) / ("ffprobe.exe" if os.name == "nt" else "ffprobe")
        if p.exists():
            return p.as_posix()
        logging.warning(f"FFprobe not found in bundle at {p}")
    return "ffprobe"


FFMPEG_PATH = get_ffmpeg_path()
FFPROBE_PATH = get_ffprobe_path()
logging.info(f"Using FFmpeg at:  {FFMPEG_PATH}")
logging.info(f"Using FFprobe at: {FFPROBE_PATH}")

try:
    from pydub import AudioSegment
    if FFMPEG_PATH != "ffmpeg" and Path(FFMPEG_PATH).exists():
        AudioSegment.converter = FFMPEG_PATH
        AudioSegment.ffmpeg = FFMPEG_PATH
        AudioSegment.ffprobe = FFPROBE_PATH
        logging.info("Configured pydub to use bundled FFmpeg/FFprobe.")
except ImportError:
    logging.warning("pydub not available")


# --- Output folder ---
out_dir = Path("./ytd_download")
out_dir.mkdir(exist_ok=True)

OUTTMLP = str(out_dir / "%(id)s.%(ext)s")
OUTTMLP_PLAYLIST = str(out_dir / "%(playlist_title)s/%(id)s.%(ext)s")

YT_OPTIONS_BASE = {
    "format": "bestaudio[ext=m4a]",
    "addmetadata": True,
    "postprocessors": [{"key": "FFmpegMetadata"}],
    "ffmpeg_location": FFMPEG_PATH if FFMPEG_PATH != "ffmpeg" else None,
}

shazam = Shazam()


def recognize_track(file_path: str) -> dict:
    return asyncio.run(shazam.recognize(file_path))


# ---------------------------------------------------------------------------
# Communication Signals for Thread-safety
# ---------------------------------------------------------------------------
class WorkerSignals(QObject):
    progress_updated = Signal(dict, int, int)  # row_dict, value, maximum
    label_updated = Signal(dict, str)          # row_dict, text
    row_finished = Signal(dict, bool, str)     # row_dict, success, message


# ---------------------------------------------------------------------------
# Custom Progress Bar for PySide
# ---------------------------------------------------------------------------
class SimpleProgressBar(QFrame):
    """A clean, styled color block acting as a progress bar."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.NoFrame)
        self.setFixedHeight(12)
        self.setFixedWidth(250)
        self.setStyleSheet("background-color: #e0e0e0; border-radius: 4px;")
        
        self.fill = QFrame(self)
        self.fill.setGeometry(0, 0, 0, 12)
        self.set_color("#3385ff")
        
        self._max = 100
        self._val = 0

    def set_color(self, hex_color: str):
        self.fill.setStyleSheet(f"background-color: {hex_color}; border-radius: 4px;")

    def set_value(self, value: int, maximum: int = 100):
        self._max = max(1, maximum)
        self._val = max(0, min(value, self._max))
        
        # Calculate proportional width
        pct = self._val / self._max
        new_width = int(self.width() * pct)
        self.fill.setGeometry(0, 0, new_width, 12)


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------
class YouTubeDownloaderApp(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"YTD v{__version__}")
        self.resize(860, 560)
        self.setMinimumSize(640, 400)

        self._row_count = 0
        self._row_lock = threading.Lock()
        
        # Instantiate thread signals
        self.signals = WorkerSignals()
        self.signals.progress_updated.connect(self._handle_progress_update)
        self.signals.label_updated.connect(self._handle_label_update)
        self.signals.row_finished.connect(self._handle_row_finish)

        # Main Layout Setup
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(12, 10, 12, 6)
        main_layout.setSpacing(6)

        # --- URL Bar ---
        url_frame = QWidget()
        url_layout = QHBoxLayout(url_frame)
        url_layout.setContentsMargins(0, 0, 0, 0)
        
        url_layout.addWidget(QLabel("Video / Playlist URL:"))
        self.url_entry = QLineEdit()
        self.url_entry.returnPressed.connect(self.download_url)
        url_layout.addWidget(self.url_entry, stretch=1)
        
        main_layout.addWidget(url_frame)

        # --- Controls ---
        ctrl_frame = QWidget()
        ctrl_layout = QHBoxLayout(ctrl_frame)
        ctrl_layout.setContentsMargins(0, 0, 0, 0)

        self.allow_playlist_cb = QCheckBox("Download Playlist")
        ctrl_layout.addWidget(self.allow_playlist_cb)

        ctrl_layout.addSpacing(16)
        ctrl_layout.addWidget(QLabel("Format:"))
        
        self.format_combo = QComboBox()
        self.format_combo.addItems(["m4a", "mp3", "flac", "wav"])
        self.format_combo.setFixedWidth(75)
        ctrl_layout.addWidget(self.format_combo)

        ctrl_layout.addSpacing(16)
        download_btn = QPushButton("Download")
        download_btn.clicked.connect(self.download_url)
        ctrl_layout.addWidget(download_btn)
        
        ctrl_layout.addStretch()
        main_layout.addWidget(ctrl_frame)

        # --- Column Headers ---
        hdr_frame = QWidget()
        hdr_layout = QGridLayout(hdr_frame)
        hdr_layout.setContentsMargins(5, 4, 5, 4)
        
        headers = ["Title / ID", "Progress", "Result"]
        alignments = [Qt.AlignLeft, Qt.AlignLeft, Qt.AlignLeft]
        
        for col, (text, align) in enumerate(zip(headers, alignments)):
            lbl = QLabel(text)
            lbl.setStyleSheet("color: #888;")
            hdr_layout.addWidget(lbl, 0, col, align)
            
        hdr_layout.setColumnStretch(0, 2)
        hdr_layout.setColumnStretch(1, 3)
        hdr_layout.setColumnStretch(2, 3)
        main_layout.addWidget(hdr_frame)

        # Divider Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setFrameShadow(QFrame.Sunken)
        sep.setStyleSheet("color: #ccc;")
        main_layout.addWidget(sep)

        # --- Scrollable Area for Content Rows ---
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.NoFrame)
        
        self.scroll_content = QWidget()
        self.scroll_layout = QGridLayout(self.scroll_content)
        self.scroll_layout.setContentsMargins(5, 2, 5, 2)
        self.scroll_layout.setAlignment(Qt.AlignTop)
        
        self.scroll_layout.setColumnStretch(0, 2)
        self.scroll_layout.setColumnStretch(1, 3)
        self.scroll_layout.setColumnStretch(2, 3)
        
        self.scroll_area.setWidget(self.scroll_content)
        main_layout.addWidget(self.scroll_area, stretch=1)

    # -----------------------------------------------------------------------
    # Signal Event Handlers (Executed safely on the UI Main Thread)
    # -----------------------------------------------------------------------
    def _handle_progress_update(self, row: dict, value: int, maximum: int):
        if "progress" in row:
            row["progress"].set_value(value, maximum)

    def _handle_label_update(self, row: dict, text: str):
        if "label" in row:
            row["label"].setText(text[:50])

    def _handle_row_finish(self, row: dict, success: bool, text: str):
        if "progress" in row:
            color = "#4BB543" if success else "#FF3333"
            row["progress"].set_color(color)
            row["progress"].set_value(100, 100)
        if "result_label" in row:
            row["result_label"].setText(text[:80])
            if not success:
                row["result_label"].setStyleSheet("color: #FF3333;")

    # -----------------------------------------------------------------------
    # UI Row Management
    # -----------------------------------------------------------------------
    def _add_row(self, title: str) -> dict:
        """Appends one download row to the grid view layout. Must be main thread."""
        with self._row_lock:
            row_idx = self._row_count
            self._row_count += 1

        lbl = QLabel(title[:50])
        self.scroll_layout.addWidget(lbl, row_idx, 0, Qt.AlignLeft | Qt.AlignVCenter)

        pb = SimpleProgressBar()
        self.scroll_layout.addWidget(pb, row_idx, 1, Qt.AlignLeft | Qt.AlignVCenter)

        res = QLabel("")
        self.scroll_layout.addWidget(res, row_idx, 2, Qt.AlignLeft | Qt.AlignVCenter)

        # Force scroll area to shift down to see the latest item
        self.scroll_area.verticalScrollBar().setValue(
            self.scroll_area.verticalScrollBar().maximum()
        )

        return {
            "label": lbl,
            "progress": pb,
            "result_label": res,
        }

    def _make_row_from_thread(self, title: str) -> dict:
        """Asks UI main thread to make a row and blocks safely till generated."""
        row = {}
        ready = threading.Event()

        def _do():
            row.update(self._add_row(title))
            ready.set()

        # Simple cross-thread safety fallback hook technique
        # Using a single-shot timer invocation to fire up UI generation logic 
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, _do)
        ready.wait(timeout=10.0)
        return row

    # -----------------------------------------------------------------------
    # Public Execution Entrypoints
    # -----------------------------------------------------------------------
    def download_url(self):
        url = self.url_entry.text().strip()
        url_pattern = re.compile(
            r"^(https?://)?([\w.-]+)\.([a-zA-Z]{2,})(:[0-9]+)?(/\S*)?$"
        )
        if not url or not url_pattern.match(url):
            QMessageBox.warning(self, "Input Error", "Please enter a valid video URL.")
            self.url_entry.clear()
            return

        self.url_entry.clear()

        m = re.search(r"[?&]v=([^&]+)", url)
        display_id = m.group(1) if m else url.split("/")[-1].split("?")[0]

        # Generate structural UI tracking components right away
        placeholder = self._add_row(display_id)

        allow_playlist = self.allow_playlist_cb.isChecked()
        selected_format = self.format_combo.currentText()

        threading.Thread(
            target=self._download_job,
            args=(url, allow_playlist, selected_format, placeholder),
            daemon=True,
        ).start()

    # -----------------------------------------------------------------------
    # Background Worker Procedures
    # -----------------------------------------------------------------------
    def _download_job(self, url: str, allow_playlist: bool, selected_format: str, placeholder: dict):
        if not allow_playlist:
            url = url.split("&list=")[0]

        is_playlist_url = allow_playlist and ("list=" in url or "/playlist" in url)

        if is_playlist_url:
            info_opts = {
                "quiet": True,
                "no_warnings": True,
                "extract_flat": "in_playlist",
                "ffmpeg_location": FFMPEG_PATH if FFMPEG_PATH != "ffmpeg" else None,
            }
            try:
                with yt_dlp.YoutubeDL(info_opts) as ydl:
                    meta = ydl.extract_info(url, download=False)
            except Exception as exc:
                clean_err = re.sub(r"\x1b\[[0-9;]*m", "", str(exc))[:80]
                self.signals.row_finished.emit(placeholder, False, clean_err)
                return

            if not meta:
                self.signals.row_finished.emit(placeholder, False, "Could not fetch playlist info")
                return

            raw_entries = meta.get("entries") or []
            entries = [e for e in raw_entries if e]

            if not entries:
                entries = [meta]

            title0 = entries[0].get("title") or entries[0].get("id") or "Unknown"
            self.signals.label_updated.emit(placeholder, title0)
            rows = [placeholder]

            for entry in entries[1:]:
                title = entry.get("title") or entry.get("id") or "Unknown"
                rows.append(self._make_row_from_thread(title))
        else:
            entries = [{"webpage_url": url, "id": ""}]
            rows = [placeholder]

        for entry, row in zip(entries, rows):
            self._download_single(entry, row, selected_format, is_playlist_url)

    def _download_single(self, entry: dict, row: dict, selected_format: str, is_playlist: bool):
        self.signals.progress_updated.emit(row, 5, 100)

        video_url = entry.get("webpage_url") or entry.get("url") or ""
        video_id = entry.get("id", "")
        if not video_url.startswith("http") and video_id:
            video_url = f"https://www.youtube.com/watch?v={video_id}"
        if not video_url:
            self.signals.row_finished.emit(row, False, "No URL found")
            return

        def hook(d):
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                downloaded = d.get("downloaded_bytes", 0)
                if total:
                    pct = max(10, min(89, int(downloaded / total * 80) + 10))
                    self.signals.progress_updated.emit(row, pct, 100)
                actual_title = d.get("info_dict", {}).get("title", "")
                if actual_title:
                    self.signals.label_updated.emit(row, actual_title)
            elif d["status"] == "finished":
                self.signals.progress_updated.emit(row, 90, 100)

        yt_opts = YT_OPTIONS_BASE.copy()
        yt_opts["progress_hooks"] = [hook]
        yt_opts["outtmpl"] = OUTTMLP_PLAYLIST if is_playlist else OUTTMLP

        try:
            self.signals.progress_updated.emit(row, 10, 100)
            with yt_dlp.YoutubeDL(yt_opts) as ydl:
                dl_info = ydl.extract_info(video_url, download=True)
                dl_entries = (
                    [e for e in dl_info.get("entries", []) if e]
                    if "entries" in dl_info else [dl_info]
                )
                prepared_paths = [Path(ydl.prepare_filename(e)) for e in dl_entries]
        except Exception as exc:
            clean = re.sub(r"\x1b\[[0-9;]*m", "", str(exc))
            logging.error(f"yt-dlp error: {clean}")
            self.signals.row_finished.emit(row, False, clean[:80])
            return

        final_name = None
        for dl_entry, file_path in zip(dl_entries, prepared_paths):
            actual_title = dl_entry.get("title", "")
            if actual_title:
                self.signals.label_updated.emit(row, actual_title)

            try:
                result_path = self._process_entry(
                    dl_entry, file_path,
                    lambda v: self.signals.progress_updated.emit(row, v, 100)
                )
                if result_path is None:
                    self.signals.row_finished.emit(row, False, "Rename failed")
                    return

                if selected_format != "m4a":
                    converted = result_path.with_suffix(f".{selected_format}")
                    subprocess.run(
                        [FFMPEG_PATH, "-y", "-i", str(result_path), str(converted)],
                        check=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=CREATE_NO_WINDOW,
                    )
                    result_path.unlink(missing_ok=True)
                    final_name = converted.stem
                else:
                    final_name = result_path.stem

            except Exception as exc:
                logging.error(f"Processing error: {exc}")
                self.signals.row_finished.emit(row, False, f"Error: {exc}"[:80])
                return

        self.signals.row_finished.emit(row, True, final_name or "Done")

    def _process_entry(self, entry: dict, downloaded_path: Path, update_progress=None) -> Path | None:
        if not downloaded_path.exists():
            raise FileNotFoundError(f"Expected file {downloaded_path} not found; tagging skipped.")

        uploader = entry.get("uploader", "Unknown").replace("/", "_")
        raw_title = entry.get("title", "Unknown").replace("/", "_")
        title = raw_title
        if uploader in title:
            title = title.replace(uploader, "").strip()
        title = title.split("[")[0].strip()
        title = title.split("(Official")[0].strip()
        title = title.replace("(Visual)", "").strip()
        title = title.strip(" -_?!.,;:()[]{}'\"\\")

        logging.info("Identifying track via ShazamIO…")
        if update_progress:
            update_progress(92)

        temp_mp3 = downloaded_path.with_suffix(".temp.mp3")
        result = {}
        try:
            subprocess.run(
                [
                    FFMPEG_PATH, "-y",
                    "-i", str(downloaded_path),
                    "-ss", "60", "-t", "30",
                    "-acodec", "mp3", "-ab", "256k",
                    str(temp_mp3),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
            )
            result = recognize_track(str(temp_mp3))
        except Exception as exc:
            logging.warning(f"Shazam recognition failed: {exc}")
        finally:
            if temp_mp3.exists():
                try:
                    temp_mp3.unlink()
                except Exception as exc:
                    logging.warning(f"Could not delete temp file {temp_mp3}: {exc}")

        if update_progress:
            update_progress(96)

        track_data = result.get("track", {})
        if not track_data:
            raise RuntimeError("Shazam returned no track info.")

        artist = track_data.get("subtitle", uploader)
        song_title = track_data.get("title", title)
        images = track_data.get("images", {})
        cover_url = images.get("coverart") or images.get("background")

        album = ""
        year = entry.get("upload_date", "")[:4]
        genre = ""
        if track_data.get("genres", {}).get("primary"):
            genre = track_data["genres"]["primary"]
        for section in track_data.get("sections", []):
            for item in section.get("metadata", []):
                key = item.get("title", "").lower()
                text = item.get("text", "")
                if key == "album":
                    album = text
                elif key in {"released", "release date", "year"}:
                    year = text.split("-")[0]
                elif key == "genre":
                    genre = text

        audio = MP4(str(downloaded_path))
        audio.tags["\xa9nam"] = [song_title]
        audio.tags["\xa9ART"] = [artist]
        audio.tags["\xa9alb"] = [album]
        audio.tags["\xa9day"] = [year]
        audio.tags["\xa9gen"] = [genre]
        audio.tags["desc"] = [entry.get("description", "")]
        audio.tags["ldes"] = [entry.get("webpage_url", "")]

        if cover_url:
            logging.info("Downloading cover art…")
            try:
                img_data = requests.get(cover_url, timeout=10).content
                audio.tags["covr"] = [
                    MP4Cover(img_data, imageformat=MP4Cover.FORMAT_JPEG)
                ]
            except Exception as exc:
                logging.warning(f"Could not embed cover art: {exc}")

        audio.save()

        safe_artist = artist.replace("/", "_")
        safe_title = song_title.replace("/", "_")
        if "(" in safe_title and ")" not in safe_title:
            safe_title += ")"
        new_path = downloaded_path.parent / f"{safe_artist} - {safe_title}.m4a"

        try:
            downloaded_path.rename(new_path)
            logging.info(f"Saved: {new_path.relative_to(out_dir)}")
            return new_path
        except Exception as exc:
            logging.error(f"Rename failed {downloaded_path} → {new_path}: {exc}")
            return None


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = YouTubeDownloaderApp()
    window.show()
    sys.exit(app.exec())