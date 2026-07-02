"""
Audio player — manages Discord voice connection and audio streaming.

Supports two playback modes:

* **Pandora** — auto-advances through a radio station
* **YouTube** — plays from a user-managed queue

Handles joining/leaving voice channels, streaming audio via ffmpeg,
auto-advancing, queue management, and volume control.
"""

import asyncio
import logging
import os
import random
import subprocess
from collections import deque
from typing import Any, Optional, Union

import discord

import config
from pandora_client import PandoraClient, Track

log = logging.getLogger(__name__)

# ffmpeg options for streaming HTTP audio into Discord voice
# -err_detect ignore_err  → skip corrupted frames instead of aborting
# -fflags +discardcorrupt → drop corrupt packets rather than dying
FFMPEG_OPTIONS = {
    'before_options': (
        '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5'
        ' -err_detect ignore_err'
    ),
    'options': '-vn -f s16le -acodec pcm_s16le -ar 48000 -ac 2 -fflags +discardcorrupt',
}


class YTAudioSource(discord.AudioSource):
    """
    Custom audio source for YouTube.

    Downloads audio to a temp file via yt-dlp, then streams the
    local file through ffmpeg to Discord voice.

    This is the only reliable approach because:
    - YouTube's CDN blocks direct ffmpeg access (403)
    - yt-dlp's stdout pipe buffers the entire download
    """

    # Cache directory relative to this script
    _CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cache')

    def __init__(self, url: str, executable: str = 'ffmpeg'):
        import glob
        import uuid
        import yt_dlp

        self._ffmpeg = None
        self._filepath = None

        # Ensure cache directory exists
        os.makedirs(self._CACHE_DIR, exist_ok=True)

        # Unique filename (yt-dlp adds the extension)
        base = os.path.join(self._CACHE_DIR, uuid.uuid4().hex[:12])

        log.info('Downloading YouTube audio to %s ...', base)

        opts = {
            'format': 'bestaudio/best',
            'outtmpl': base + '.%(ext)s',
            'quiet': True,
            'no_warnings': True,
            'no_part': True,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'opus',
            }],
        }

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
        except yt_dlp.utils.DownloadError as e:
            log.error('yt-dlp failed to download %s: %s', url, str(e))
            raise RuntimeError(f'Download failed: {str(e)}')

        # Find the downloaded file (extension varies)
        files = glob.glob(base + '.*')
        if not files:
            raise RuntimeError('yt-dlp download produced no output file')
        self._filepath = files[0]

        log.info('Download complete: %s. Starting ffmpeg playback.', self._filepath)

        # Stream the local file through ffmpeg → PCM
        self._ffmpeg = subprocess.Popen(
            [
                executable,
                '-err_detect', 'ignore_err',   # skip bad frames
                '-i', self._filepath,
                '-f', 's16le',       # raw PCM
                '-ar', '48000',      # 48kHz (Discord requirement)
                '-ac', '2',          # stereo
                '-fflags', '+discardcorrupt',   # drop corrupt packets
                '-loglevel', 'warning',
                'pipe:1',
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def read(self) -> bytes:
        """Read a 20ms PCM audio frame for Discord."""
        # Discord expects exactly 3840 bytes (20ms at 48kHz, stereo, s16le).
        # A short read can crash the Opus SILK resampler, so pad with silence.
        FRAME_SIZE = 3840
        data = self._ffmpeg.stdout.read(FRAME_SIZE)
        if not data or len(data) == 0:
            return b''
        if len(data) < FRAME_SIZE:
            data += b'\x00' * (FRAME_SIZE - len(data))
        return data

    def is_opus(self) -> bool:
        return False

    def cleanup(self):
        """Clean up ffmpeg process and delete cached file."""
        if self._ffmpeg and self._ffmpeg.poll() is None:
            self._ffmpeg.kill()
            self._ffmpeg.wait()
        if self._filepath:
            try:
                os.unlink(self._filepath)
                log.info('Cleaned up cache file: %s', self._filepath)
            except OSError:
                pass


class Player:
    """
    Manages voice playback for a single guild.

    Supports Pandora radio, YouTube queue, and Plex library playback.
    """

    # Playback modes
    MODE_IDLE = 'idle'
    MODE_PANDORA = 'pandora'
    MODE_YOUTUBE = 'youtube'
    MODE_PLEX = 'plex'

    def __init__(self, pandora: PandoraClient):
        self.pandora = pandora
        self.voice_client: Optional[discord.VoiceClient] = None
        self.current_track: Optional[Any] = None  # Track or YTTrack
        self.volume: float = config.DEFAULT_VOLUME
        self.mode: str = self.MODE_IDLE
        self._playing = False
        self._paused = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.text_channel: Optional[Any] = None
        self.on_track_start: Optional[Any] = None

        # YouTube queue
        self._yt_queue: deque = deque()
        self._plex_queue: deque = deque()
        self._yt_history: deque = deque()
        self._plex_history: deque = deque()

    @property
    def is_connected(self) -> bool:
        return self.voice_client is not None and self.voice_client.is_connected()

    @property
    def is_playing(self) -> bool:
        return self.is_connected and self._playing

    @property
    def is_paused(self) -> bool:
        return self.is_connected and self._paused

    @property
    def queue(self) -> list:
        """Return a copy of the YouTube queue."""
        return list(self._yt_queue)

    @property
    def queue_length(self) -> int:
        return len(self._yt_queue)

    # ------------------------------------------------------------------
    # Voice connection
    # ------------------------------------------------------------------

    async def join(self, channel: discord.VoiceChannel) -> None:
        """Join a voice channel (or move if already connected)."""
        if self.voice_client and self.voice_client.is_connected():
            await self.voice_client.move_to(channel)
            log.info('Moved to voice channel: %s', channel.name)
        else:
            self.voice_client = await channel.connect()
            log.info('Joined voice channel: %s', channel.name)

        self._loop = asyncio.get_event_loop()

    async def leave(self, save_queue: bool = False) -> None:
        """Disconnect from voice."""
        if self.voice_client:
            self._playing = False
            
            if not save_queue:
                self.mode = self.MODE_IDLE
                
            if self.voice_client.is_playing() or self.voice_client.is_paused():
                self.voice_client.stop()
                
            await self.voice_client.disconnect()
            self.voice_client = None
            
            if save_queue and self.current_track:
                if self.mode == self.MODE_YOUTUBE:
                    self._yt_queue.appendleft(self.current_track)
                elif self.mode == self.MODE_PLEX:
                    self._plex_queue.appendleft(self.current_track)
            elif not save_queue:
                self._yt_queue.clear()
                self._plex_queue.clear()
                self._yt_history.clear()
                self._plex_history.clear()
                
            self.current_track = None
            log.info('Left voice channel.')

    # ------------------------------------------------------------------
    # Shared playback engine
    # ------------------------------------------------------------------

    def _start_playback(self, audio_url: str, track: Any,
                        after_callback, webpage_url: str = None) -> bool:
        """
        Start streaming audio through ffmpeg → Discord voice.

        For YouTube tracks, uses yt-dlp subprocess piped to ffmpeg to
        avoid CDN 403 errors.  For Pandora / direct URLs, streams
        directly via ffmpeg.

        Returns True on success.
        """
        log.info('Audio URL: %s', audio_url[:80])

        try:
            if webpage_url:
                # YouTube: yt-dlp handles auth → pipes to ffmpeg → PCM output
                source = YTAudioSource(webpage_url, executable=config.FFMPEG_PATH)
            else:
                # Pandora / direct URL: stream directly
                source = discord.FFmpegPCMAudio(
                    audio_url,
                    executable=config.FFMPEG_PATH,
                    **FFMPEG_OPTIONS,
                )
        except Exception as exc:
            log.error('Failed to create audio source: %s', exc)
            self._playing = False
            return False

        source = discord.PCMVolumeTransformer(source, volume=self.volume)
        self.current_track = track
        self._playing = True

        if self.voice_client.is_playing():
            self.voice_client.stop()

        self.voice_client.play(source, after=after_callback)
        return True

    # ------------------------------------------------------------------
    # Pandora playback
    # ------------------------------------------------------------------

    async def play_pandora_next(self, notify: bool = False) -> Optional[Track]:
        """
        Fetch the next track from Pandora and start streaming.

        Automatically advances when the track ends.
        """
        if not self.is_connected:
            log.warning('Cannot play — not connected to voice.')
            return None

        if not self.pandora.current_station:
            log.warning('Cannot play — no station selected.')
            return None

        self.mode = self.MODE_PANDORA

        # Stop current playback
        if self.voice_client.is_playing():
            self.voice_client.stop()

        # Fetch next track
        loop = asyncio.get_event_loop()
        track = await loop.run_in_executor(None, self.pandora.get_next_track)

        if not track:
            self._playing = False
            self.current_track = None
            return None

        log.info('Now playing (Pandora): %s — %s', track.title, track.artist)

        def after_playback(error):
            if error:
                log.error('Playback error (Pandora): %s', error)
            if not self._playing or not self._loop or self.mode != self.MODE_PANDORA:
                return
            if not self.is_connected:
                log.warning('Voice disconnected after error — not advancing.')
                self._playing = False
                return
            asyncio.run_coroutine_threadsafe(
                self.play_pandora_next(notify=True), self._loop
            )

        ok = self._start_playback(track.audio_url, track, after_playback)
        if ok and notify and self.on_track_start:
            asyncio.create_task(self.on_track_start(self, track))
        return track if ok else None

    # ------------------------------------------------------------------
    # YouTube playback
    # ------------------------------------------------------------------

    def add_to_queue(self, yt_track) -> int:
        """Add a YTTrack to the queue. Returns the new queue length."""
        self._yt_queue.append(yt_track)
        log.info('Queued (YouTube): %s', yt_track.title)
        return len(self._yt_queue)

    def clear_queue(self) -> int:
        """Clear the YouTube queue. Returns number of items cleared."""
        count = len(self._yt_queue)
        self._yt_queue.clear()
        self._yt_history.clear()
        return count

    def move_in_queue(self, from_pos: int, to_pos: int):
        """Move a track in the YouTube queue. Positions are 1-indexed."""
        items = list(self._yt_queue)
        track = items.pop(from_pos - 1)
        items.insert(to_pos - 1, track)
        self._yt_queue = deque(items)
        log.info('Moved queue item %d -> %d: %s', from_pos, to_pos, track.title)
        return track

    def remove_from_queue(self, pos: int):
        """Remove a track at the given 1-indexed position from the YouTube queue."""
        items = list(self._yt_queue)
        track = items.pop(pos - 1)
        self._yt_queue = deque(items)
        log.info('Removed from queue position %d: %s', pos, track.title)
        return track

    def remove_range_from_queue(self, start: int, end: int) -> int:
        """Remove tracks from position start to end (1-indexed, inclusive)."""
        items = list(self._yt_queue)
        del items[start - 1:end]
        count = len(self._yt_queue) - len(items)
        self._yt_queue = deque(items)
        log.info('Removed %d tracks from queue positions %d-%d.', count, start, end)
        return count

    def shuffle_queue(self) -> int:
        """Shuffle the YouTube queue in place. Returns queue length."""
        items = list(self._yt_queue)
        random.shuffle(items)
        self._yt_queue = deque(items)
        log.info('Shuffled YouTube queue (%d tracks).', len(items))
        return len(items)

    async def play_youtube_next(self, notify: bool = False) -> Optional[Any]:
        """
        Play the next track from the YouTube queue.

        Automatically advances when the track ends.  Stops when the
        queue is empty.
        """
        if not self.is_connected:
            return None

        self.mode = self.MODE_YOUTUBE

        while self._yt_queue:
            track = self._yt_queue.popleft()
            self._yt_history.append(track)
            log.info('Now playing (YouTube): %s', track.title)

            def after_playback(error):
                if error:
                    log.error('Playback error (YouTube queue): %s', error)
                if not self._playing or not self._loop or self.mode != self.MODE_YOUTUBE:
                    return
                if not self.is_connected:
                    log.warning('Voice disconnected after error — not advancing.')
                    self._playing = False
                    return
                asyncio.run_coroutine_threadsafe(
                    self.play_youtube_next(notify=True), self._loop
                )

            ok = self._start_playback(track.url, track, after_playback,
                                      webpage_url=getattr(track, 'webpage_url', None))
            
            if ok:
                if notify and self.on_track_start:
                    asyncio.create_task(self.on_track_start(self, track))
                return track
            else:
                log.warning('Skipping track due to playback init failure: %s', track.title)

        # Queue is empty
        self._playing = False
        self.current_track = None
        log.info('YouTube queue empty.')
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.stop()
        return None


    async def play_youtube_now(self, yt_track) -> Optional[Any]:
        """Play a single YouTube track immediately (skips queue)."""
        if not self.is_connected:
            return None

        self.mode = self.MODE_YOUTUBE

        log.info('Now playing (YouTube): %s', yt_track.title)

        def after_playback(error):
            if error:
                log.error('Playback error (YouTube immediate): %s', error)
            if not self._playing or not self._loop:
                self._playing = False
                self.current_track = None
                return
            if not self.is_connected:
                log.warning('Voice disconnected after error — not advancing.')
                self._playing = False
                self.current_track = None
                return
            # After the immediate track, continue with queue if any
            if self._yt_queue:
                asyncio.run_coroutine_threadsafe(
                    self.play_youtube_next(notify=True), self._loop
                )
            else:
                self._playing = False
                self.current_track = None

        ok = self._start_playback(yt_track.url, yt_track, after_playback,
                                  webpage_url=getattr(yt_track, 'webpage_url', None))
        if ok:
            return yt_track
        else:
            log.warning('Playback failed for immediate track (e.g. copyright blocked): %s', yt_track.title)
            # If the immediate track failed, but we still have a queue, auto-play the queue
            if self._yt_queue:
                return await self.play_youtube_next()
            else:
                self._playing = False
                self.current_track = None
                return None

    # ------------------------------------------------------------------
    # Plex playback
    # ------------------------------------------------------------------

    def add_to_plex_queue(self, plex_track) -> int:
        """Add a PlexTrack to the Plex queue. Returns new queue length."""
        self._plex_queue.append(plex_track)
        log.info('Queued (Plex): %s — %s', plex_track.title, plex_track.artist)
        return len(self._plex_queue)

    def clear_plex_queue(self) -> int:
        """Clear the Plex queue. Returns number of items cleared."""
        count = len(self._plex_queue)
        self._plex_queue.clear()
        self._plex_history.clear()
        return count

    def move_in_plex_queue(self, from_pos: int, to_pos: int):
        """Move a track in the Plex queue. Positions are 1-indexed."""
        items = list(self._plex_queue)
        track = items.pop(from_pos - 1)
        items.insert(to_pos - 1, track)
        self._plex_queue = deque(items)
        log.info('Moved Plex queue item %d -> %d: %s', from_pos, to_pos, track.title)
        return track

    def remove_from_plex_queue(self, pos: int):
        """Remove a track at the given 1-indexed position from the Plex queue."""
        items = list(self._plex_queue)
        track = items.pop(pos - 1)
        self._plex_queue = deque(items)
        log.info('Removed from Plex queue position %d: %s', pos, track.title)
        return track

    def remove_range_from_plex_queue(self, start: int, end: int) -> int:
        """Remove tracks from position start to end (1-indexed, inclusive)."""
        items = list(self._plex_queue)
        del items[start - 1:end]
        count = len(self._plex_queue) - len(items)
        self._plex_queue = deque(items)
        log.info('Removed %d tracks from Plex queue positions %d-%d.', count, start, end)
        return count

    def shuffle_plex_queue(self) -> int:
        """Shuffle the Plex queue in place. Returns queue length."""
        items = list(self._plex_queue)
        random.shuffle(items)
        self._plex_queue = deque(items)
        log.info('Shuffled Plex queue (%d tracks).', len(items))
        return len(items)

    @property
    def plex_queue(self) -> list:
        """Return a copy of the Plex queue."""
        return list(self._plex_queue)

    @property
    def plex_queue_length(self) -> int:
        return len(self._plex_queue)

    async def play_plex_next(self, notify: bool = False) -> Optional[Any]:
        """
        Play the next track from the Plex queue.

        Automatically advances when the track ends.  Stops when the
        queue is empty.
        """
        if not self.is_connected:
            return None

        self.mode = self.MODE_PLEX

        if not self._plex_queue:
            self._playing = False
            self.current_track = None
            log.info('Plex queue empty.')
            if self.voice_client and self.voice_client.is_playing():
                self.voice_client.stop()
            return None

        track = self._plex_queue.popleft()
        self._plex_history.append(track)
        log.info('Now playing (Plex): %s — %s', track.title, track.artist)

        def after_playback(error):
            if error:
                log.error('Playback error (Plex): %s', error)
            if not self._playing or not self._loop or self.mode != self.MODE_PLEX:
                return
            if not self.is_connected:
                log.warning('Voice disconnected after error — not advancing.')
                self._playing = False
                return
            asyncio.run_coroutine_threadsafe(
                self.play_plex_next(notify=True), self._loop
            )

        ok = self._start_playback(track.audio_url, track, after_playback)
        if ok and notify and self.on_track_start:
            asyncio.create_task(self.on_track_start(self, track))
        return track if ok else None

    async def play_plex_now(self, plex_track) -> Optional[Any]:
        """Play a single Plex track immediately (skips queue)."""
        if not self.is_connected:
            return None

        self.mode = self.MODE_PLEX

        log.info('Now playing (Plex): %s — %s', plex_track.title, plex_track.artist)

        def after_playback(error):
            if error:
                log.error('Playback error (Plex immediate): %s', error)
            if not self._playing or not self._loop:
                self._playing = False
                self.current_track = None
                return
            if not self.is_connected:
                log.warning('Voice disconnected after error — not advancing.')
                self._playing = False
                self.current_track = None
                return
            # After the immediate track, continue with queue if any
            if self._plex_queue:
                asyncio.run_coroutine_threadsafe(
                    self.play_plex_next(notify=True), self._loop
                )
            else:
                self._playing = False
                self.current_track = None

        ok = self._start_playback(plex_track.audio_url, plex_track, after_playback)
        return plex_track if ok else None

    # ------------------------------------------------------------------
    # Shared controls
    # ------------------------------------------------------------------

    async def skip(self) -> Optional[Any]:
        """Skip to the next track (works for all modes)."""
        log.info('Skipping...')
        # Disable auto-advance so the after_playback callback doesn't
        # race with the explicit play call below.
        self._playing = False
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.stop()
        if self.mode == self.MODE_PLEX:
            return await self.play_plex_next()
        if self.mode == self.MODE_YOUTUBE:
            return await self.play_youtube_next()
        return await self.play_pandora_next()

    def set_volume(self, vol: float) -> None:
        """Set volume (0.0 to 1.0)."""
        self.volume = max(0.0, min(1.0, vol))
        if (self.voice_client
                and self.voice_client.source
                and isinstance(self.voice_client.source, discord.PCMVolumeTransformer)):
            self.voice_client.source.volume = self.volume
        log.info('Volume set to %.0f%%', self.volume * 100)

    def pause(self) -> bool:
        """Pause playback. Returns True if paused successfully."""
        if not self.is_connected or not self._playing:
            return False
        if self.voice_client.is_playing():
            self.voice_client.pause()
            self._paused = True
            log.info('Playback paused.')
            return True
        return False

    def resume(self) -> bool:
        """Resume playback. Returns True if resumed successfully."""
        if not self.is_connected or not self._paused:
            return False
        if self.voice_client.is_paused():
            self.voice_client.resume()
            self._paused = False
            log.info('Playback resumed.')
            return True
        return False

    async def stop(self) -> None:
        """Stop playback without disconnecting."""
        self._playing = False
        self._paused = False
        self.mode = self.MODE_IDLE
        self._yt_queue.clear()
        self._plex_queue.clear()
        self._yt_history.clear()
        self._plex_history.clear()
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.stop()
        self.current_track = None

    def restart_queue(self) -> bool:
        """Restart the active queue by prepending history."""
        if self.mode == self.MODE_IDLE:
            return False

        has_history = False
        if self.mode == self.MODE_YOUTUBE and (self._yt_history or self.current_track):
            has_history = True
            items = list(self._yt_history) + ([self.current_track] if self.current_track else []) + list(self._yt_queue)
            self._yt_queue = deque(items)
            self._yt_history.clear()
        elif self.mode == self.MODE_PLEX and (self._plex_history or self.current_track):
            has_history = True
            items = list(self._plex_history) + ([self.current_track] if self.current_track else []) + list(self._plex_queue)
            self._plex_queue = deque(items)
            self._plex_history.clear()

        if not has_history:
            return False

        self._playing = False
        self.current_track = None
        if self.voice_client and (self.voice_client.is_playing() or self.voice_client.is_paused()):
            self.voice_client.stop()

        return True
