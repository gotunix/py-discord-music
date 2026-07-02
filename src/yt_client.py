"""
YouTube client — search and extract audio URLs via yt-dlp.

Supports searching by query, extracting single videos, and loading
playlists.  Audio is streamed via ffmpeg (no download to disk).
"""

import logging
from dataclasses import dataclass, field
from typing import List, Optional

import yt_dlp

log = logging.getLogger(__name__)

# yt-dlp options: extract audio info without downloading
_YDL_SEARCH_OPTS = {
    'quiet': True,
    'no_warnings': True,
    'extract_flat': 'in_playlist',   # get metadata without full extraction
}

_YDL_EXTRACT_OPTS = {
    'quiet': True,
    'no_warnings': True,
    'format': 'bestaudio/best',
    'noplaylist': False,        # allow playlists
    'extract_flat': False,
}


@dataclass
class YTTrack:
    """A YouTube audio track."""
    title: str
    url: str           # direct audio stream URL (for ffmpeg)
    webpage_url: str   # the YouTube page URL
    duration: int = 0  # seconds
    thumbnail: str = ''
    uploader: str = ''
    http_headers: dict = field(default_factory=dict)  # headers ffmpeg needs

    @property
    def display(self) -> str:
        mins = self.duration // 60
        secs = self.duration % 60
        return f'**{self.title}** [{mins}:{secs:02d}]'

    @property
    def display_short(self) -> str:
        return f'{self.title} ({self.duration // 60}:{self.duration % 60:02d})'


@dataclass
class YTSearchResult:
    """A YouTube search result (not yet extracted for streaming)."""
    title: str
    video_id: str
    url: str
    duration: int = 0
    uploader: str = ''

    @property
    def display(self) -> str:
        mins = self.duration // 60
        secs = self.duration % 60
        return f'**{self.title}** — *{self.uploader}* [{mins}:{secs:02d}]'


class YouTubeClient:
    """
    Search YouTube and extract audio stream URLs via yt-dlp.

    Usage::

        yt = YouTubeClient()
        results = yt.search('lofi chill beats')
        track = yt.extract(results[0].url)
        # track.url is the direct audio stream URL for ffmpeg
    """

    def __init__(self):
        self._last_search: List[YTSearchResult] = []

    def search(self, query: str, limit: int = 10) -> List[YTSearchResult]:
        """
        Search YouTube and return a list of results.

        Results are cached in ``_last_search`` for the ``!play <number>``
        flow.
        """
        search_query = f'ytsearch{limit}:{query}'

        with yt_dlp.YoutubeDL(_YDL_SEARCH_OPTS) as ydl:
            info = ydl.extract_info(search_query, download=False)

        results = []
        entries = info.get('entries', []) if info else []

        for entry in entries:
            if not entry:
                continue
            results.append(YTSearchResult(
                title=entry.get('title', 'Unknown'),
                video_id=entry.get('id', ''),
                url=entry.get('url', entry.get('webpage_url', f"https://www.youtube.com/watch?v={entry.get('id', '')}")),
                duration=int(entry.get('duration', 0) or 0),
                uploader=entry.get('uploader', entry.get('channel', '')),
            ))

        self._last_search = results
        log.info('YouTube search "%s": %d results.', query, len(results))
        return results

    def _make_track(self, info: dict) -> YTTrack:
        """Build a YTTrack from yt-dlp info dict, including HTTP headers."""
        return YTTrack(
            title=info.get('title', 'Unknown'),
            url=info.get('url', ''),
            webpage_url=info.get('webpage_url', ''),
            duration=int(info.get('duration', 0) or 0),
            thumbnail=info.get('thumbnail', ''),
            uploader=info.get('uploader', ''),
            http_headers=info.get('http_headers', {}),
        )

    def extract(self, url: str) -> Optional[YTTrack]:
        """
        Extract the audio stream URL for a single video.

        Returns a YTTrack with a direct stream URL suitable for ffmpeg.
        """
        opts = {**_YDL_EXTRACT_OPTS, 'noplaylist': True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info:
            return None

        # If it's a playlist, grab just the first entry
        if 'entries' in info:
            entries = list(info['entries'])
            if not entries:
                return None
            info = entries[0]

        return self._make_track(info)

    def extract_playlist(self, url: str) -> List[YTTrack]:
        """
        Extract all tracks from a YouTube playlist URL.

        Uses flat extraction (metadata only) for speed — actual audio
        is downloaded at play time by YTAudioSource.
        """
        opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': 'in_playlist',
            'noplaylist': False,
        }

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        tracks = []
        if not info:
            return tracks

        entries = info.get('entries', [])
        for entry in entries:
            if not entry:
                continue
            video_id = entry.get('id', '')
            webpage_url = entry.get(
                'webpage_url',
                entry.get('url', f'https://www.youtube.com/watch?v={video_id}')
                if video_id else '',
            )
            if not webpage_url:
                continue
            tracks.append(YTTrack(
                title=entry.get('title', 'Unknown'),
                url='',   # resolved at play time by YTAudioSource
                webpage_url=webpage_url,
                duration=int(entry.get('duration', 0) or 0),
                thumbnail=entry.get('thumbnail', ''),
                uploader=entry.get('uploader', entry.get('channel', '')),
            ))

        log.info('Extracted %d tracks from playlist.', len(tracks))
        return tracks

    def extract_from_search(self, index: int) -> Optional[YTTrack]:
        """Extract audio from a cached search result by index (0-based)."""
        if index < 0 or index >= len(self._last_search):
            return None
        result = self._last_search[index]
        return self.extract(result.url)
