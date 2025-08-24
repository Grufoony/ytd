import asyncio
import logging
import os
from pathlib import Path
import re
import requests
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from mutagen.mp4 import MP4, MP4Cover
from shazamio import Shazam
import yt_dlp  # pip install yt-dlp


__author__ = "Grufoony"
__version__ = "0.1"

# --- Logging setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


# --- FFmpeg path detection for PyInstaller ---
def get_ffmpeg_path():
    """Get the path to ffmpeg executable, handling PyInstaller bundling."""
    if getattr(sys, "frozen", False):
        # Running as PyInstaller bundle
        bundle_dir = sys._MEIPASS
        if os.name == "nt":  # Windows
            ffmpeg_path = Path(bundle_dir) / "ffmpeg.exe"
        else:  # Linux/Mac
            ffmpeg_path = Path(bundle_dir) / "ffmpeg"

        if ffmpeg_path.exists():
            return ffmpeg_path.as_posix()
        logging.warning(f"FFmpeg not found in bundle at {ffmpeg_path}")
        return "ffmpeg"  # Fallback to system ffmpeg
    else:
        # Running in development
        return "ffmpeg"


def get_ffprobe_path():
    """Get the path to ffprobe executable, handling PyInstaller bundling."""
    if getattr(sys, "frozen", False):
        # Running as PyInstaller bundle
        bundle_dir = sys._MEIPASS
        if os.name == "nt":  # Windows
            ffprobe_path = Path(bundle_dir) / "ffprobe.exe"
        else:  # Linux/Mac
            ffprobe_path = Path(bundle_dir) / "ffprobe"

        if ffprobe_path.exists():
            return ffprobe_path.as_posix()
        logging.warning(f"FFprobe not found in bundle at {ffprobe_path}")
        return "ffprobe"  # Fallback to system ffprobe
    else:
        # Running in development
        return "ffprobe"


FFMPEG_PATH = get_ffmpeg_path()
FFPROBE_PATH = get_ffprobe_path()
logging.info(f"Using FFmpeg at: {FFMPEG_PATH}")
logging.info(f"Using FFprobe at: {FFPROBE_PATH}")

# --- Configure pydub to use our ffmpeg ---
try:
    from pydub import AudioSegment

    # Set ffmpeg path for pydub
    if FFMPEG_PATH != "ffmpeg" and Path(FFMPEG_PATH).exists():
        AudioSegment.converter = FFMPEG_PATH
        AudioSegment.ffmpeg = FFMPEG_PATH
        AudioSegment.ffprobe = FFPROBE_PATH
        logging.info(f"Configured pydub to use FFmpeg at: {FFMPEG_PATH}")
        logging.info(f"Configured pydub to use FFprobe at: {FFPROBE_PATH}")
except ImportError:
    logging.warning("pydub not available")


# --- Output folder ---
out_dir = Path("./ytd_download")
out_dir.mkdir(exist_ok=True)

OUTTMLP = str(out_dir / "%(id)s.%(ext)s")
OUTTMLP_PLAYLIST = str(out_dir / "%(playlist_title)s/%(id)s.%(ext)s")

YT_OPTIONS = {
    "format": "bestaudio[ext=m4a]",
    "ignoreerrors": True,
    "addmetadata": True,
    "postprocessors": [{"key": "FFmpegMetadata"}],
    "ffmpeg_location": FFMPEG_PATH if FFMPEG_PATH != "ffmpeg" else None,
    # "outtmpl": will be set after
}

# Initialize ShazamIO
shazam = Shazam()


# --- Helper to run async Shazam recognition from sync code ---
def recognize_track(file_path: str) -> dict:
    return asyncio.run(shazam.recognize(file_path))


# --- Tkinter GUI ---
class YouTubeDownloaderApp:
    def __init__(self, root):
        self.root = root
        root.title(f"YTD v{__version__}")
        root.geometry("700x500")

        self.allow_playlist = tk.BooleanVar(value=False)

        # URL entry
        self.url_label = ttk.Label(root, text="Insert video URL:")
        self.url_label.pack(pady=5)

        self.url_entry = ttk.Entry(root, width=80)
        self.url_entry.pack(pady=5)

        # Format selection dropdown (Combobox)
        format_frame = ttk.Frame(root)
        format_frame.pack(pady=5)

        self.playlist_check = ttk.Checkbutton(
            format_frame,
            text="Download Playlist",
            variable=self.allow_playlist,
        )
        self.playlist_check.pack(side="left")

        self.format_var = tk.StringVar(value="m4a")
        self.format_label = ttk.Label(format_frame, text="Format:")
        self.format_label.pack(side="left", padx=(10, 2))
        self.format_combo = ttk.Combobox(
            format_frame,
            textvariable=self.format_var,
            values=["m4a", "mp3", "flac", "wav"],
            state="readonly",
            width=6,
        )
        self.format_combo.pack(side="left")
        self.format_combo.configure(justify="center")

        # Buttons
        self.download_button = ttk.Button(
            root, text="Download", command=self.download_url
        )
        self.download_button.pack(pady=5)

        # Download list frame
        self.downloads_frame = ttk.Frame(root)
        self.downloads_frame.pack(fill="both", expand=True, padx=10, pady=10)

        self.active_downloads = {}  # url_id: {label, progress, style, file_label}

    def download_url(self):
        url = self.url_entry.get().strip()
        # Simple URL validation (http/https and basic structure) to avoid misscopying
        url_pattern = re.compile(
            r"^(https?://)?([\w.-]+)\.([a-zA-Z]{2,})(:[0-9]+)?(/\S*)?$"
        )
        if not url or not url_pattern.match(url):
            messagebox.showwarning("Input Error", "Please enter a valid video URL.")
            self.url_entry.delete(0, tk.END)
            return

        # Clear the URL entry field immediately so user can enter another
        self.url_entry.delete(0, tk.END)

        # Strip URL for display: get part after '?v=' and before '&list='
        def get_url_id(url):
            v_idx = url.find("?v=")
            if v_idx == -1:
                v_idx = url.find("v=")
                if v_idx == -1:
                    return url
                v_idx += 2
            else:
                v_idx += 3
            end_idx = url.find("&list=", v_idx)
            if end_idx == -1:
                return url[v_idx:]
            return url[v_idx:end_idx]

        url_id = get_url_id(url)

        # Add to downloads frame
        row = len(self.active_downloads)
        label = ttk.Label(self.downloads_frame, text=url_id)
        label.grid(row=row, column=0, sticky="w", padx=5, pady=2)
        style_name = f"bar{row}.Horizontal.TProgressbar"
        style = ttk.Style()
        style.theme_use("default")
        style.configure(style_name, background="#3385ff")
        progress = ttk.Progressbar(
            self.downloads_frame, length=300, mode="determinate", style=style_name
        )
        progress.grid(row=row, column=1, sticky="ew", padx=5, pady=2)
        progress["value"] = 0
        self.active_downloads[url_id] = {
            "label": label,
            "progress": progress,
            "style": style_name,
            "file_label": None,
        }

        def update_progress(val, maxval=100):
            self.downloads_frame.after(
                0, lambda: progress.config(maximum=maxval, value=val)
            )

        def set_bar_color(color):
            style.configure(style_name, background=color)

        def finish_bar(success, filename=None, error_msg=None):
            if success:
                set_bar_color("#4BB543")  # green
            else:
                set_bar_color("#FF3333")  # red
            update_progress(100)
            # Show filename or error to the right of the progress bar once progress is complete
            display_text = error_msg if error_msg else filename
            if display_text:
                file_label = ttk.Label(
                    self.downloads_frame,
                    text=display_text,
                    foreground="#FF3333" if error_msg else None,
                )
                file_label.grid(row=row, column=2, sticky="w", padx=5)
                self.active_downloads[url_id]["file_label"] = file_label

        def job(url):
            update_progress(5)
            if not self.allow_playlist.get():
                url_local = url.split("&list=")[0]  # strip playlists
            else:
                url_local = url

            error_occurred = False
            selected_format = self.format_var.get()

            def hook(d):
                if d["status"] == "downloading":
                    total = d.get("total_bytes") or d.get("total_bytes_estimate")
                    downloaded = d.get("downloaded_bytes", 0)
                    if total:
                        percent = int(downloaded / total * 100)
                        update_progress(percent)
                elif d["status"] == "finished":
                    update_progress(90)

            yt_options = YT_OPTIONS.copy()
            yt_options["progress_hooks"] = [hook]
            yt_options["outtmpl"] = (
                OUTTMLP_PLAYLIST if "&list=" in url_local else OUTTMLP
            )
            yt_options["format"] = "bestaudio[ext=m4a]"

            try:
                update_progress(10)
                with yt_dlp.YoutubeDL(yt_options) as ydl:
                    info = ydl.extract_info(url_local, download=True)
            except Exception as e:
                error_occurred = True
                finish_bar(False, error_msg=f"Download Error: {e}")
                messagebox.showerror("Download Error", f"Error downloading video: {e}")
                return

            entries = (
                [e for e in info.get("entries", []) if e]
                if "entries" in info
                else [info]
            )

            final_filename = None
            try:
                for entry in entries:
                    update_progress(90)
                    try:
                        result_path = self.process_entry(entry, ydl, update_progress)
                        if result_path:
                            # If format is not m4a, convert using ffmpeg after tagging
                            if selected_format != "m4a":
                                converted_path = result_path.with_suffix(
                                    f".{selected_format}"
                                )

                                ffmpeg_cmd = [
                                    FFMPEG_PATH,
                                    "-y",
                                    "-i",
                                    str(result_path),
                                    str(converted_path),
                                ]
                                try:
                                    subprocess.run(
                                        ffmpeg_cmd,
                                        check=True,
                                        stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE,
                                    )
                                    logging.info(
                                        f"Converted {result_path} to {converted_path}"
                                    )
                                    result_path.unlink()  # Remove original m4a
                                    final_filename = converted_path.stem
                                except Exception as e:
                                    error_occurred = True
                                    logging.error(
                                        f"Error converting to {selected_format}: {e}"
                                    )
                                    finish_bar(False, error_msg=f"Convert Error: {e}")
                                    return
                            else:
                                final_filename = result_path.stem
                    except Exception as e:
                        error_occurred = True
                        logging.error(f"Error in conversion/tagging: {e}")
                        finish_bar(False, error_msg=f"Tagging Error: {e}")
                        return
            except Exception as e:
                error_occurred = True
                logging.error(f"Error in conversion/tagging: {e}")
                finish_bar(False, error_msg=f"Tagging Error: {e}")
                return

            update_progress(100)
            if not error_occurred:
                finish_bar(True, filename=final_filename)

        threading.Thread(target=job, args=(url,), daemon=True).start()

    def process_entry(self, entry, ydl, update_progress=None):
        # video_id = entry.get("id")
        downloaded_path = Path(str(ydl.prepare_filename(entry)))
        if not downloaded_path.exists():
            logging.error(
                f"Expected file {downloaded_path} not found, skipping tagging."
            )
            return

        uploader = entry.get("uploader", "Unknown").replace("/", "_")
        raw_title = entry.get("title", "Unknown").replace("/", "_")
        title = raw_title
        if uploader in title:
            title = title.replace(uploader, "").strip()
        title = title.split("[")[0].strip()
        title = title.split("(Official")[0].strip()
        title = title.replace("(Visual)", "").strip()
        title = title.strip(" -_?!.,;:()[]{}'\"\\")

        # --- shazam recognition ---
        logging.info("Identifying track via Shazam_IO...")
        if update_progress:
            update_progress(92)

        # Convert to temporary MP3 for Shazam to avoid header issues
        temp_mp3_path = downloaded_path.with_suffix(".temp.mp3")
        try:
            # Convert to MP3 using ffmpeg
            ffmpeg_cmd = [
                FFMPEG_PATH,
                "-y",  # Overwrite output file
                "-i",
                str(downloaded_path),
                "-acodec",
                "mp3",
                "-ab",
                "128k",  # Bitrate
                str(temp_mp3_path),
            ]
            subprocess.run(
                ffmpeg_cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            logging.info(f"Created temporary MP3 for Shazam: {temp_mp3_path}")

            # Use the temporary MP3 for recognition
            result = recognize_track(str(temp_mp3_path))
            if update_progress:
                update_progress(96)

        except Exception as e:
            logging.warning(f"Shazam recognition failed: {e}")
            if update_progress:
                update_progress(96)
            result = {}
        finally:
            # Clean up temporary MP3 file
            if temp_mp3_path.exists():
                try:
                    temp_mp3_path.unlink()
                    logging.info("Cleaned up temporary MP3 file")
                except Exception as e:
                    logging.warning(
                        f"Could not delete temporary file {temp_mp3_path}: {e}"
                    )

        track_data = result.get("track", {})
        artist = track_data.get("subtitle", uploader)
        song_title = track_data.get("title", title)
        images = track_data.get("images", {})
        cover_url = images.get("coverart") or images.get("background")

        # --- tagging ---
        audio = MP4(str(downloaded_path))
        audio.tags["\u00a9nam"] = [song_title]
        audio.tags["\u00a9ART"] = [artist]
        album = ""
        # If Shazam failed, make this a download error
        if not result or "track" not in result or not result["track"]:
            raise Exception("Shazam recognition failed: No track info returned.")
        year = entry.get("upload_date", "")[:4]
        genre = ""
        genres_info = track_data.get("genres", {}).get("primary")
        if genres_info:
            genre = genres_info
        sections = track_data.get("sections", [])
        if sections and "metadata" in sections[0]:
            for item in sections[0]["metadata"]:
                key = item.get("title", "").lower()
                text = item.get("text", "")
                if key == "album":
                    album = text
                elif key in {"released", "release date", "year"}:
                    year = text.split("-")[0]
                elif key == "genre":
                    genre = text
        audio.tags["\u00a9alb"] = [album]
        audio.tags["\u00a9day"] = [year]
        audio.tags["\u00a9gen"] = [genre]
        audio.tags["desc"] = [entry.get("description", "")]
        audio.tags["ldes"] = [entry.get("webpage_url", "")]

        if cover_url:
            logging.info("Downloading cover art...")
            try:
                img_data = requests.get(cover_url).content
                audio.tags["covr"] = [
                    MP4Cover(img_data, imageformat=MP4Cover.FORMAT_JPEG)
                ]
            except Exception as e:
                logging.warning(f"Could not embed cover art: {e}")

        audio.save()

        # --- rename inside same folder ---
        safe_artist = artist.replace("/", "_")
        safe_title = song_title.replace("/", "_")
        if "(" in safe_title and ")" not in safe_title:
            safe_title += ")"
        new_name = f"{safe_artist} - {safe_title}.m4a"
        new_path = downloaded_path.parent / new_name

        try:
            downloaded_path.rename(new_path)
            logging.info(f"Downloaded and tagged: {new_path.relative_to(out_dir)}")
            return new_path
        except Exception as e:
            logging.error(f"Could not rename {downloaded_path} -> {new_path}: {e}")
            return None


if __name__ == "__main__":
    root = tk.Tk()
    app = YouTubeDownloaderApp(root)
    root.mainloop()
