# short_creator.py
#
# Upgrade: fetches ALL new photos from every configured Telegram channel and
# compiles them into a SINGLE 9:16 vertical MP4 short (slide-show with Ken Burns
# + synced-caption overlay).  Only that one video is uploaded to YouTube.
#
import asyncio
import os
import json
import logging
import requests
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional, Tuple, Union

import moviepy.editor as mp
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def get_env_json(key: str, default: str = "[]") -> Union[list, dict]:
    """Safely get and parse JSON environment variables."""
    try:
        value = os.getenv(key)
        if not value:
            logger.warning(f"Using default value for {key}")
            return json.loads(default)
        return json.loads(value)
    except Exception as e:
        logger.error(f"Error parsing {key}: {str(e)}")
        return json.loads(default)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@dataclass
class Config:
    # Telegram
    TELEGRAM_TOKEN: str
    TELEGRAM_CHANNELS: List[str]

    # YouTube
    YOUTUBE_CLIENT_SECRETS: dict
    TITLE_TEMPLATE: str = "Video Short - {date}"
    DESCRIPTION: str = "Automated YouTube Short created from Telegram content"
    TAGS: List[str] = field(default_factory=lambda: ["Shorts", "Auto-generated", "Telegram"])
    PRIVACY_STATUS: str = "private"
    PLAYLIST_ID: str = "PLKfhqWP2rL8LS6mS4eJk0sx43sD4x8TeV"
    PUBLISH_DELAY_HOURS: int = 1
    BRAND_HASHTAGS: List[str] = field(default_factory=lambda: ["xeonbit24", "xeonbit24.com"])

    # Content
    # Duration *per slide* in seconds when TTS is not used.
    # The actual per-slide duration will be extended to fit TTS audio when present.
    SLIDE_DURATION: int = 5
    # Hard cap on total video length (seconds).  0 = no cap.
    MAX_DURATION: int = 59
    MUSIC_OPTION: str = "music.mp3"
    FONT_PATH: str = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    FONT_BOLD_PATH: str = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    OUTPUT_RESOLUTION: Tuple[int, int] = field(default_factory=lambda: (1080, 1920))
    LOGO_PATH: str = "brand_logo.png"
    PUBLISHED_IDS_FILE: str = ".published_ids.json"


# ---------------------------------------------------------------------------
# Telegram client
# ---------------------------------------------------------------------------
class TelegramClient:
    def __init__(self, token: str):
        self.token = token
        self.base_url = f"https://api.telegram.org/bot{token}/"
        self.session = requests.Session()

    def get_latest_images(
        self,
        channel: str,
        published_ids: set,
        max_posts: int = 10,
    ) -> List[Tuple[str, str, str]]:
        """Return a list of (image_url, caption, unique_key) for up to *max_posts*
        unprocessed photo posts from *channel*, newest-first."""
        results: List[Tuple[str, str, str]] = []
        try:
            url = f'{self.base_url}getUpdates?allowed_updates=["channel_post","message"]'
            updates = self.session.get(url).json()
            logger.info(f"Total updates received: {len(updates.get('result', []))}")
            if not updates["ok"]:
                logger.error(f"Failed to get updates: {updates}")
                return results

            for update in reversed(updates.get("result", [])):
                if len(results) >= max_posts:
                    break
                post = update.get("channel_post") or update.get("message", {})
                sender = post.get("sender_chat", {}).get("username", "none")
                chat_obj = post.get("chat", {}).get("username", "none")
                has_photo = "photo" in post
                logger.info(
                    f"Update: sender_chat=@{sender}, chat=@{chat_obj}, has_photo={has_photo}"
                )
                chat_username = "@" + (
                    post.get("sender_chat", {}).get("username")
                    or post.get("chat", {}).get("username", "")
                )
                logger.info(f"Comparing: '{chat_username}' == '{channel}'")
                if chat_username == channel and has_photo:
                    message_id = str(post.get("message_id", update.get("update_id", "")))
                    unique_key = f"{channel}:{message_id}"
                    if unique_key in published_ids:
                        logger.info(f"Skipping already-published post: {unique_key}")
                        continue
                    try:
                        photo = max(post["photo"], key=lambda x: x["file_size"])
                        file_resp = self.session.get(
                            f"{self.base_url}getFile?file_id={photo['file_id']}"
                        ).json()
                        file_path = file_resp["result"]["file_path"]
                        caption = post.get("caption", "No caption")
                        results.append((
                            f"https://api.telegram.org/file/bot{self.token}/{file_path}",
                            caption,
                            unique_key,
                        ))
                        logger.info(f"Queued post {unique_key} ({len(results)}/{max_posts})")
                    except Exception as inner_e:
                        logger.error(f"Error resolving file for {unique_key}: {inner_e}")
        except Exception as e:
            logger.error(f"Error fetching telegram content: {str(e)}")
        return results


# ---------------------------------------------------------------------------
# Video creator  —  now builds ONE video from multiple images
# ---------------------------------------------------------------------------
class VideoCreator:
    def __init__(self, config: Config):
        self.config = config
        self.music_cache = Path(".music_cache")
        self.music_cache.mkdir(exist_ok=True)

        # Load brand logo once
        self._logo: Optional[Image.Image] = None
        if config.LOGO_PATH and Path(config.LOGO_PATH).exists():
            try:
                logo = Image.open(config.LOGO_PATH).convert("RGBA")
                logo_size = 140
                logo.thumbnail((logo_size, logo_size), Image.LANCZOS)
                self._logo = logo
                logger.info(f"Logo loaded: {logo.size}")
            except Exception as e:
                logger.warning(f"Could not load logo: {e}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def download_music(self, url: str) -> Path:
        try:
            filename = self.music_cache / url.split("/")[-1]
            if not filename.exists():
                logger.info(f"Downloading music from {url}")
                filename.write_bytes(requests.get(url).content)
            return filename
        except Exception as e:
            logger.error(f"Music download failed: {str(e)}")
            return Path(self.music_cache / "default.mp3")

    def _fit_image(self, img: Image.Image, target_size: tuple) -> Image.Image:
        """Crop/resize to exactly fill target_size (CSS object-fit: cover)."""
        tw, th = target_size
        ow, oh = img.size
        scale = max(tw / ow, th / oh)
        nw, nh = int(ow * scale), int(oh * scale)
        img = img.resize((nw, nh), Image.LANCZOS)
        left = (nw - tw) // 2
        top = (nh - th) // 2
        return img.crop((left, top, left + tw, top + th))

    def _apply_zoom(self, img: Image.Image, progress: float) -> Image.Image:
        """Ken Burns zoom-in then zoom-out.  progress: 0.0 → 1.0."""
        t = 1.0 - abs(progress * 2 - 1.0)
        zoom = 1.0 + 0.15 * t
        w, h = img.size
        nw, nh = int(w / zoom), int(h / zoom)
        left = (w - nw) // 2
        top = (h - nh) // 2
        return img.crop((left, top, left + nw, top + nh)).resize((w, h), Image.LANCZOS)

    async def _generate_tts(self, text: str) -> Tuple[Optional[Path], list]:
        """Generate TTS audio + word timings for *text*.  Returns (path, timings)."""
        try:
            import edge_tts, re

            def strip_emojis(s: str) -> str:
                return re.sub(
                    r"[🌀-🪿😀-🙏🚀-🛿☀-⛿✀-➿🤀-🧿🇠-🇿‍︀-️]+",
                    "",
                    s,
                ).strip()

            tts_path = Path("temp_tts.mp3")
            word_timings = []
            clean_text = strip_emojis(text)
            subscribe_cta = "Remember to like and subscribe to the channel. Love you all!!"
            text_with_cta = f"{clean_text}. {subscribe_cta}"

            communicate = edge_tts.Communicate(text_with_cta, voice="en-SG-LunaNeural")
            with open(str(tts_path), "wb") as f:
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        f.write(chunk["data"])
                    elif chunk["type"] == "WordBoundary":
                        word_timings.append({
                            "word": chunk["text"],
                            "start": chunk["offset"] / 10_000_000,
                            "duration": chunk["duration"] / 10_000_000,
                        })

            raw_audio = mp.AudioFileClip(str(tts_path))
            real_duration = raw_audio.duration
            raw_audio.close()
            logger.info(f"TTS real duration: {real_duration:.2f}s")

            if not word_timings:
                words = text.split()
                per_word = (real_duration / 1.25) / max(len(words), 1)
                word_timings = [
                    {"word": w, "start": round(i * per_word, 3), "duration": round(per_word * 0.85, 3)}
                    for i, w in enumerate(words)
                ]

            return tts_path, word_timings
        except Exception as e:
            logger.error(f"TTS generation failed: {str(e)}")
            return None, []

    def _generate_caption_frame(self, highlight_word: str, img: Image.Image) -> np.ndarray:
        """Render the current spoken word centred in the lower third."""
        try:
            frame = img.copy().convert("RGBA")
            overlay = Image.new("RGBA", frame.size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay)
            img_w, img_h = frame.size
            font_bold = ImageFont.truetype(self.config.FONT_BOLD_PATH, 81)

            clean_word = "".join(
                c for c in highlight_word
                if not (
                    0x1F300 <= ord(c) <= 0x1FABF
                    or 0x1F600 <= ord(c) <= 0x1F64F
                    or 0x1F680 <= ord(c) <= 0x1F6FF
                    or 0x2600 <= ord(c) <= 0x26FF
                    or 0x2700 <= ord(c) <= 0x27BF
                    or 0x1F900 <= ord(c) <= 0x1F9FF
                    or 0x1F1E0 <= ord(c) <= 0x1F1FF
                    or ord(c) == 0x200D
                    or 0xFE00 <= ord(c) <= 0xFE0F
                )
            ).strip(".,!?:;\"'").strip()

            if clean_word:
                pad_x, pad_y = 40, 20
                bbox = draw.textbbox((0, 0), clean_word, font=font_bold)
                tw = bbox[2] - bbox[0]
                th = bbox[3] - bbox[1]
                x = (img_w - tw) // 2
                y = int(img_h * 0.75)

                draw.rounded_rectangle(
                    [x - pad_x, y - pad_y, x + tw + pad_x, y + th + pad_y],
                    radius=20,
                    fill=(80, 80, 80, 160),
                )
                for ox in range(-6, 7):
                    for oy in range(-6, 7):
                        if ox != 0 or oy != 0:
                            draw.text((x + ox, y + oy), clean_word, font=font_bold, fill=(0, 0, 0, 255))
                draw.text((x, y), clean_word, font=font_bold, fill=(255, 220, 0, 255))

            result = Image.alpha_composite(frame, overlay)
            if self._logo is not None:
                margin = 20
                lw, lh = self._logo.size
                result.paste(self._logo, (img_w - lw - margin, margin), self._logo)

            return np.array(result.convert("RGB"))
        except Exception as e:
            logger.error(f"Caption frame failed: {str(e)}")
            return np.array(img.convert("RGB"))

    # ------------------------------------------------------------------
    # Public API — build ONE compiled short from multiple posts
    # ------------------------------------------------------------------

    async def create_compiled_short(
        self,
        posts: List[Tuple[str, str, str]],
    ) -> Tuple[Optional[Path], List[str]]:  # (video_path, all_captions)
        """
        Build a single 9:16 vertical MP4 by compiling *posts* into a slide-show.

        Each post is one slide:
          • Image fills the frame (Ken Burns zoom).
          • Its caption is read aloud via TTS (word-by-word highlight overlay).
          • Slide duration = max(SLIDE_DURATION, tts_duration + 1.0 s).

        A single background-music track is mixed across the entire video.
        The final clip is written to ``output_short.mp4``.

        Parameters
        ----------
        posts : list of (image_url, caption, unique_key)

        Returns
        -------
        Tuple of (Path to the output MP4 or None on failure, list of all captions).
        """
        # Temp file paths
        tmp_tts_raw = Path("temp_tts.mp3")
        tmp_tts_fast = Path("temp_tts_fast.mp3")

        slide_clips: List[mp.VideoClip] = []
        slide_audios: List[Optional[mp.AudioClip]] = []
        all_captions: List[str] = []

        fps = 24

        try:
            for slide_idx, (image_url, caption, unique_key) in enumerate(posts):
                logger.info(f"[Slide {slide_idx + 1}/{len(posts)}] Processing {unique_key}")

                # ----------------------------------------------------------
                # 1. Download & prepare image
                # ----------------------------------------------------------
                try:
                    img_data = requests.get(image_url, timeout=30).content
                    tmp_img = Path(f"temp_slide_{slide_idx}.jpg")
                    tmp_img.write_bytes(img_data)
                    img = Image.open(tmp_img)
                    img = self._fit_image(img, self.config.OUTPUT_RESOLUTION)
                    tmp_img.unlink(missing_ok=True)
                except Exception as e:
                    logger.error(f"Could not download/process image for {unique_key}: {e}")
                    continue

                # ----------------------------------------------------------
                # 2. Generate TTS for this slide's caption
                # ----------------------------------------------------------
                tts_audio_clip: Optional[mp.AudioClip] = None
                word_timings: list = []

                if caption and caption != "No caption":
                    tts_path, word_timings = await self._generate_tts(caption)
                    if tts_path and tts_path.exists():
                        # Speed up TTS × 1.25
                        subprocess.run(
                            [
                                "ffmpeg", "-y", "-i", str(tts_path),
                                "-filter:a", "atempo=1.25",
                                str(tmp_tts_fast),
                            ],
                            check=True,
                            capture_output=True,
                        )
                        tts_audio_clip = mp.AudioFileClip(str(tmp_tts_fast))
                        tts_path.unlink(missing_ok=True)

                tts_duration = tts_audio_clip.duration if tts_audio_clip else 0.0
                slide_duration = max(float(self.config.SLIDE_DURATION), tts_duration + 1.0)

                # ----------------------------------------------------------
                # 3. Render frames for this slide
                # ----------------------------------------------------------
                total_frames = int(slide_duration * fps)
                frames: List[np.ndarray] = []

                for frame_idx in range(total_frames):
                    current_time = frame_idx / fps
                    progress = frame_idx / max(total_frames - 1, 1)

                    current_word = ""
                    for timing in word_timings:
                        if timing["start"] <= current_time <= timing["start"] + timing["duration"]:
                            current_word = timing["word"]
                            break

                    try:
                        zoomed = self._apply_zoom(img, progress)
                        frame = self._generate_caption_frame(current_word, zoomed)
                    except Exception as fe:
                        logger.warning(f"Frame {frame_idx} error: {fe}")
                        frame = np.array(img.convert("RGB"))
                    frames.append(frame)

                if not frames:
                    logger.warning(f"No frames for slide {slide_idx}, skipping")
                    continue

                slide_video = mp.ImageSequenceClip(frames, fps=fps)

                # Fade in/out only on first/last slide
                if slide_idx == 0:
                    slide_video = slide_video.fadein(0.5)
                if slide_idx == len(posts) - 1:
                    slide_video = slide_video.fadeout(0.5)

                slide_clips.append(slide_video)
                slide_audios.append(tts_audio_clip)
                all_captions.append(caption if caption != "No caption" else "")

                # Clean up per-slide TTS fast file
                if tmp_tts_fast.exists():
                    tmp_tts_fast.unlink(missing_ok=True)

            if not slide_clips:
                logger.error("No slides rendered; cannot produce output video.")
                return None, []

            # ------------------------------------------------------------------
            # 4. Concatenate slides into one video
            # ------------------------------------------------------------------
            logger.info(f"Concatenating {len(slide_clips)} slides...")
            final_video = mp.concatenate_videoclips(slide_clips, method="compose")

            total_duration = final_video.duration

            # Optional hard cap
            if self.config.MAX_DURATION > 0 and total_duration > self.config.MAX_DURATION:
                logger.info(
                    f"Trimming video from {total_duration:.1f}s → {self.config.MAX_DURATION}s"
                )
                final_video = final_video.subclip(0, self.config.MAX_DURATION)
                total_duration = self.config.MAX_DURATION

            # ------------------------------------------------------------------
            # 5. Build composite audio: TTS tracks + background music
            # ------------------------------------------------------------------
            audio_tracks: List[mp.AudioClip] = []

            # Place each slide's TTS at the correct time offset
            time_cursor = 0.0
            for clip, tts in zip(slide_clips, slide_audios):
                if tts is not None:
                    shifted = tts.set_start(time_cursor)
                    if self.config.MAX_DURATION > 0 and time_cursor >= self.config.MAX_DURATION:
                        break
                    audio_tracks.append(shifted)
                time_cursor += clip.duration

            # Background music
            if self.config.MUSIC_OPTION:
                music_path = (
                    self.download_music(self.config.MUSIC_OPTION)
                    if self.config.MUSIC_OPTION.startswith("http")
                    else Path(self.config.MUSIC_OPTION)
                )
                if music_path.exists():
                    bg = mp.AudioFileClip(str(music_path))
                    # Loop if needed
                    if bg.duration < total_duration:
                        loops = int(total_duration / bg.duration) + 1
                        bg = mp.concatenate_audioclips([bg] * loops)
                    bg = bg.subclip(0, total_duration)
                    bg = bg.audio_fadein(1.0).audio_fadeout(1.5)
                    bg_volume = 0.3 if audio_tracks else 0.8
                    bg = bg.volumex(bg_volume)
                    audio_tracks.insert(0, bg)

            if audio_tracks:
                final_audio = mp.CompositeAudioClip(audio_tracks)
                final_video = final_video.set_audio(final_audio)

            # ------------------------------------------------------------------
            # 6. Write output
            # ------------------------------------------------------------------
            output_path = Path("output_short.mp4")
            logger.info(f"Writing final video ({total_duration:.1f}s) → {output_path}")
            final_video.write_videofile(
                str(output_path),
                fps=fps,
                codec="libx264",
                audio_codec="aac",
                logger="bar",
            )
            logger.info(f"Compiled short saved: {output_path}")
            return output_path, all_captions

        except Exception as e:
            logger.error(f"Compiled short creation failed: {str(e)}")
            return None, []
        finally:
            # Clean up any stray temp files
            for p in [tmp_tts_raw, tmp_tts_fast]:
                if p.exists():
                    p.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# YouTube uploader  (unchanged logic)
# ---------------------------------------------------------------------------
class YouTubeUploader:
    def __init__(self, credentials: dict):
        self.credentials = credentials

    def upload_short(self, video_path: Path, config: Config, caption: str = "", all_captions: List[str] = None):
        try:
            creds = Credentials.from_authorized_user_info(self.credentials)
            youtube = build("youtube", "v3", credentials=creds)

            if all_captions is None:
                all_captions = [caption] if caption else []

            import re as _re

            def sanitize_tag(t: str) -> str:
                """Strip to ASCII-safe alphanumeric + spaces, max 30 chars."""
                t = t.strip("#.,!?:;\"'()[]{}").strip()
                # Remove characters YouTube rejects (keep letters, digits, spaces)
                t = _re.sub(r"[^\w\s]", "", t, flags=_re.UNICODE)
                t = t.strip()
                return t[:30] if t else ""

            # Extract tags from ALL captions
            caption_tags = []
            seen_tags: set = set()
            for cap in all_captions:
                for word in cap.split():
                    clean = sanitize_tag(word.lower())
                    if len(clean) > 2 and clean not in seen_tags:
                        caption_tags.append(clean)
                        seen_tags.add(clean)

            brand_tags = [sanitize_tag(t) for t in config.BRAND_HASHTAGS]
            brand_tags = [t for t in brand_tags if t]
            base_tags  = [sanitize_tag(t) for t in config.TAGS]
            base_tags  = [t for t in base_tags if t]

            # Deduplicate and cap: YouTube allows max 500 chars total across all tags
            raw_tags = base_tags + brand_tags + caption_tags
            all_tags: List[str] = []
            seen_final: set = set()
            total_chars = 0
            for t in raw_tags:
                if t in seen_final:
                    continue
                if total_chars + len(t) + 1 > 500:
                    break
                all_tags.append(t)
                seen_final.add(t)
                total_chars += len(t) + 1

            brand_hashtag_str = " ".join(f"#{t}" for t in brand_tags)
            caption_hashtag_str = " ".join(f"#{t}" for t in caption_tags[:10])
            hashtags = f"{brand_hashtag_str} {caption_hashtag_str}".strip()

            date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            primary_cap = all_captions[0] if all_captions else caption
            title = f"Video Short {primary_cap[:50]} {date_str}"

            # Build description with all captions
            caption_lines = "\n".join(
                f"📌 {cap.strip()}"
                for cap in all_captions
                if cap and cap != "No caption"
            )
            caption_section = f"\n\n{caption_lines}" if caption_lines else ""
            description = (
                f"{config.DESCRIPTION}{caption_section}\n\n{hashtags}\n#Shorts\n\ncryptohieu.com"
            )

            publish_at = (
                datetime.now(timezone.utc) + timedelta(hours=config.PUBLISH_DELAY_HOURS)
            ).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            logger.info(f"Scheduling publish at: {publish_at} UTC")

            body = {
                "snippet": {
                    "title": title,
                    "description": description,
                    "tags": all_tags,
                    "categoryId": "22",
                },
                "status": {
                    "privacyStatus": "private",
                    "publishAt": publish_at,
                    "selfDeclaredMadeForKids": False,
                },
            }

            media = MediaFileUpload(str(video_path), chunksize=-1, resumable=True)
            request = youtube.videos().insert(
                part=",".join(body.keys()), body=body, media_body=media
            )

            response = None
            while response is None:
                status, response = request.next_chunk()
                if status:
                    logger.info(f"Uploaded {int(status.progress() * 100)}%")

            video_id = response["id"]
            logger.info(f"Video uploaded: https://youtu.be/{video_id} (scheduled: {publish_at})")

            if config.PLAYLIST_ID:
                try:
                    youtube.playlistItems().insert(
                        part="snippet",
                        body={
                            "snippet": {
                                "playlistId": config.PLAYLIST_ID,
                                "resourceId": {"kind": "youtube#video", "videoId": video_id},
                            }
                        },
                    ).execute()
                    logger.info(f"Added to playlist: {config.PLAYLIST_ID}")
                except Exception as pe:
                    logger.error(f"Playlist insert failed: {pe}")

            return response
        except Exception as e:
            err_str = str(e)
            if "rateLimitExceeded" in err_str or "Video Uploads per day" in err_str:
                logger.error(
                    "YouTube daily upload quota exceeded. "
                    "Quota resets at midnight Pacific Time. "
                    "To increase limits: console.cloud.google.com → "
                    "APIs & Services → YouTube Data API v3 → Quotas."
                )
                raise SystemExit(1)
            logger.error(f"Upload failed: {err_str}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def _main():
    try:
        config = Config(
            TELEGRAM_TOKEN=os.getenv("TELEGRAM_TOKEN"),
            TELEGRAM_CHANNELS=get_env_json("TELEGRAM_CHANNELS", '["@xeonbitchannel"]'),
            YOUTUBE_CLIENT_SECRETS=get_env_json("YOUTUBE_CLIENT_SECRETS", "{}"),
            TITLE_TEMPLATE=os.getenv("TITLE_TEMPLATE", "Video Short - {date}"),
            DESCRIPTION=os.getenv("DESCRIPTION", "Automated YouTube Short"),
            TAGS=get_env_json("TAGS", '["Shorts", "Auto-generated"]'),
            PRIVACY_STATUS=os.getenv("PRIVACY_STATUS", "private"),
            PLAYLIST_ID=os.getenv("PLAYLIST_ID", "PLKfhqWP2rL8LS6mS4eJk0sx43sD4x8TeV"),
            PUBLISH_DELAY_HOURS=int(os.getenv("PUBLISH_DELAY_HOURS", 1)),
            BRAND_HASHTAGS=get_env_json("BRAND_HASHTAGS", '["xeonbit24", "xeonbit24.com"]'),
            SLIDE_DURATION=int(os.getenv("SLIDE_DURATION", 5)),
            MAX_DURATION=int(os.getenv("MAX_DURATION", 59)),
            MUSIC_OPTION=os.getenv("MUSIC_OPTION", "music.mp3"),
            FONT_PATH=os.getenv(
                "FONT_PATH", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
            ),
            FONT_BOLD_PATH=os.getenv(
                "FONT_BOLD_PATH", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            ),
        )

        if not config.TELEGRAM_TOKEN:
            raise ValueError("TELEGRAM_TOKEN is required")
        if not config.YOUTUBE_CLIENT_SECRETS:
            raise ValueError("YOUTUBE_CLIENT_SECRETS must be configured")
        if not config.TELEGRAM_CHANNELS:
            raise ValueError("At least one TELEGRAM_CHANNEL must be specified")

        # ----------------------------------------------------------------
        # Load published IDs
        # ----------------------------------------------------------------
        published_ids_file = Path(config.PUBLISHED_IDS_FILE)
        published_ids: list = []
        if published_ids_file.exists():
            try:
                published_ids = json.loads(published_ids_file.read_text())
                logger.info(f"Loaded {len(published_ids)} published IDs")
            except Exception as e:
                logger.warning(f"Could not load published IDs: {e}")

        # ----------------------------------------------------------------
        # Gather ALL new Telegram photos from ALL channels → one list
        # ----------------------------------------------------------------
        telegram = TelegramClient(config.TELEGRAM_TOKEN)
        max_per_channel = int(os.getenv("MAX_TELEGRAM_POSTS", 3))
        all_posts: List[Tuple[str, str, str]] = []

        for channel in config.TELEGRAM_CHANNELS:
            posts = telegram.get_latest_images(channel, published_ids, max_posts=max_per_channel)
            all_posts.extend(posts)

        if not all_posts:
            logger.error(
                "No new suitable content found "
                "(all recent posts already published or no photos)."
            )
            return

        logger.info(
            f"Collected {len(all_posts)} new image(s) across "
            f"{len(config.TELEGRAM_CHANNELS)} channel(s). "
            "Compiling into ONE short video..."
        )

        # ----------------------------------------------------------------
        # Build the single compiled short
        # ----------------------------------------------------------------
        creator = VideoCreator(config)

        # Use the first post's caption as the YouTube title/description seed
        primary_caption = all_posts[0][1] if all_posts else ""

        video_path, all_captions = await creator.create_compiled_short(all_posts)

        if not video_path or not video_path.exists():
            logger.error("Compiled video creation failed — nothing to upload.")
            return

        # ----------------------------------------------------------------
        # Upload the ONE video to YouTube
        # ----------------------------------------------------------------
        uploader = YouTubeUploader(config.YOUTUBE_CLIENT_SECRETS)
        upload_result = uploader.upload_short(
            video_path, config, caption=primary_caption, all_captions=all_captions
        )

        # ----------------------------------------------------------------
        # Mark ALL processed posts as published ONLY on success
        # ----------------------------------------------------------------
        if upload_result:
            for _, _, unique_key in all_posts:
                if unique_key not in published_ids:
                    published_ids.append(unique_key)
            MAX_PUBLISHED_IDS = 30
            pruned_ids = published_ids
            if len(published_ids) > MAX_PUBLISHED_IDS:
                pruned_ids = published_ids[-MAX_PUBLISHED_IDS:]
                logger.info(
                    f"Pruned published IDs from {len(published_ids)} → {MAX_PUBLISHED_IDS}."
                )
            try:
                published_ids_file.write_text(json.dumps(pruned_ids))
                logger.info(f"Saved {len(all_posts)} new published ID(s).")
            except Exception as save_err:
                logger.warning(f"Could not save published IDs: {save_err}")
        else:
            logger.warning("Upload did not succeed — published IDs NOT saved; posts will retry next run.")

        # Clean up output video
        if video_path.exists():
            video_path.unlink()
            logger.info("Cleaned up compiled video file.")

    except Exception:
        logger.exception("Fatal error in main process")
        sys.exit(1)


def main():
    asyncio.run(_main())


if __name__ == "__main__":
    main()
