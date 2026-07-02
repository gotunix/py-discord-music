"""
Plex client — connects to a Plex Media Server and streams music.

Uses python-plexapi to search, browse, and get audio stream URLs
from your Plex music library.  Stream URLs are direct HTTP, so
ffmpeg can play them the same way as Pandora tracks.

Requires::

    PLEX_URL            — Plex server URL (e.g. http://192.168.1.100:32400)
    PLEX_TOKEN          — X-Plex-Token for authentication
    PLEX_MUSIC_LIBRARY  — Name of the music library (default: 'Music')
"""

import logging
import random
from dataclasses import dataclass, field
from typing import List, Optional

from plexapi.server import PlexServer

import config

log = logging.getLogger(__name__)


@dataclass
class PlexTrack:
    """A single audio track from Plex."""
    title: str
    artist: str
    album: str
    audio_url: str          # direct HTTP stream URL (from getStreamURL())
    art_url: str = ''
    duration: int = 0       # seconds
    rating_key: str = ''    # Plex internal ID

    @property
    def display(self) -> str:
        mins = self.duration // 60
        secs = self.duration % 60
        return f'**{self.title}** — *{self.artist}* [{mins}:{secs:02d}]'

    @property
    def display_short(self) -> str:
        return f'{self.title} — {self.artist} ({self.duration // 60}:{self.duration % 60:02d})'


@dataclass
class PlexSearchResult:
    """A Plex search result for display before playing."""
    title: str
    artist: str
    album: str
    result_type: str        # 'track', 'album', 'artist'
    rating_key: str = ''

    @property
    def display(self) -> str:
        if self.result_type == 'track':
            return f'🎵 **{self.title}** — *{self.artist}* (_{self.album}_)'
        elif self.result_type == 'album':
            return f'💿 **{self.title}** — *{self.artist}*'
        else:
            return f'🎤 **{self.title}**'


class PlexClient:
    """
    Manages Plex server connection and music library access.

    Usage::

        client = PlexClient()
        client.connect()
        results = client.search('Beatles')
        track = client.get_track_from_search(0)
        # track.audio_url is a direct HTTP URL for ffmpeg
    """

    def __init__(self):
        self._server: Optional[PlexServer] = None
        self._music_library = None
        self._last_search: List[PlexSearchResult] = []
        self._last_search_raw = []     # raw plexapi objects for extraction
        self._last_album_search = []   # cached album search results (dicts)
        self._last_album_search_raw = []  # raw plexapi album objects

    @property
    def connected(self) -> bool:
        return self._server is not None

    def connect(self) -> None:
        """Connect to the Plex server using credentials from config."""
        if not config.PLEX_URL or not config.PLEX_TOKEN:
            raise ValueError('PLEX_URL and PLEX_TOKEN must be set.')

        log.info('Connecting to Plex server at %s ...', config.PLEX_URL)
        self._server = PlexServer(config.PLEX_URL, config.PLEX_TOKEN)
        log.info('Connected to Plex: %s', self._server.friendlyName)

        # Locate the music library
        try:
            self._music_library = self._server.library.section(config.PLEX_MUSIC_LIBRARY)
            log.info('Plex music library: %s (%d items)',
                     self._music_library.title, self._music_library.totalSize)
        except Exception as exc:
            log.error('Could not find music library "%s": %s',
                      config.PLEX_MUSIC_LIBRARY, exc)
            raise

    def _plex_to_track(self, plex_track) -> PlexTrack:
        """Convert a plexapi.audio.Track to a PlexTrack with stream URL."""
        stream_url = plex_track.getStreamURL()

        # Build thumbnail URL (full URL needs server base)
        art_url = ''
        thumb = plex_track.thumbUrl
        if thumb:
            art_url = thumb

        return PlexTrack(
            title=plex_track.title or 'Unknown',
            artist=(plex_track.grandparentTitle
                    or getattr(plex_track, 'originalTitle', '')
                    or 'Unknown'),
            album=plex_track.parentTitle or 'Unknown',
            audio_url=stream_url,
            art_url=art_url,
            duration=(plex_track.duration or 0) // 1000,  # ms → seconds
            rating_key=str(plex_track.ratingKey),
        )

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(self, query: str, limit: int = 10) -> List[PlexSearchResult]:
        """
        Search the Plex music library for tracks, albums, and artists.

        Results are cached for the ``!plexplay <number>`` flow.
        """
        if not self._music_library:
            log.warning('Cannot search — music library not loaded.')
            return []

        results = []
        raw_objects = []

        # Search for tracks first (most useful)
        try:
            tracks = self._music_library.searchTracks(title=query, limit=limit)
            for t in tracks[:limit]:
                results.append(PlexSearchResult(
                    title=t.title,
                    artist=(t.grandparentTitle
                            or getattr(t, 'originalTitle', '')
                            or 'Unknown'),
                    album=t.parentTitle or 'Unknown',
                    result_type='track',
                    rating_key=str(t.ratingKey),
                ))
                raw_objects.append(t)
        except Exception as exc:
            log.error('Plex track search error: %s', exc)

        # Also search for albums and artists if we have room
        remaining = limit - len(results)
        if remaining > 0:
            try:
                albums = self._music_library.searchAlbums(title=query, limit=remaining)
                for a in albums[:remaining]:
                    results.append(PlexSearchResult(
                        title=a.title,
                        artist=a.parentTitle or 'Unknown',
                        album=a.title,
                        result_type='album',
                        rating_key=str(a.ratingKey),
                    ))
                    raw_objects.append(a)
            except Exception as exc:
                log.error('Plex album search error: %s', exc)

        remaining = limit - len(results)
        if remaining > 0:
            try:
                artists = self._music_library.searchArtists(title=query, limit=remaining)
                for ar in artists[:remaining]:
                    results.append(PlexSearchResult(
                        title=ar.title,
                        artist=ar.title,
                        album='',
                        result_type='artist',
                        rating_key=str(ar.ratingKey),
                    ))
                    raw_objects.append(ar)
            except Exception as exc:
                log.error('Plex artist search error: %s', exc)

        self._last_search = results
        self._last_search_raw = raw_objects
        log.info('Plex search "%s": %d results.', query, len(results))
        return results

    # ------------------------------------------------------------------
    # Track extraction
    # ------------------------------------------------------------------

    def get_track_from_search(self, index: int) -> Optional[PlexTrack]:
        """
        Get a playable track from search results by index (0-based).

        If the result is an album, returns the first track.
        If the result is an artist, returns a random track.
        """
        if index < 0 or index >= len(self._last_search_raw):
            return None

        obj = self._last_search_raw[index]
        result = self._last_search[index]

        if result.result_type == 'track':
            return self._plex_to_track(obj)
        elif result.result_type == 'album':
            tracks = obj.tracks()
            if tracks:
                return self._plex_to_track(tracks[0])
        elif result.result_type == 'artist':
            tracks = obj.tracks()
            if tracks:
                return self._plex_to_track(random.choice(tracks))

        return None

    def get_tracks_from_search(self, index: int) -> List[PlexTrack]:
        """
        Get all playable tracks from a search result (0-based).

        If the result is a track, returns just that track.
        If the result is an album, returns all album tracks.
        If the result is an artist, returns all artist tracks (shuffled).
        """
        if index < 0 or index >= len(self._last_search_raw):
            return []

        obj = self._last_search_raw[index]
        result = self._last_search[index]

        if result.result_type == 'track':
            return [self._plex_to_track(obj)]
        elif result.result_type == 'album':
            return [self._plex_to_track(t) for t in obj.tracks()]
        elif result.result_type == 'artist':
            tracks = [self._plex_to_track(t) for t in obj.tracks()]
            random.shuffle(tracks)
            return tracks

        return []

    # ------------------------------------------------------------------
    # Direct lookups
    # ------------------------------------------------------------------

    def get_album_tracks(self, album_name: str) -> List[PlexTrack]:
        """
        Get all tracks from an album by name.

        Returns tracks in album order.
        """
        if not self._music_library:
            return []

        try:
            albums = self._music_library.searchAlbums(title=album_name, limit=5)
        except Exception as exc:
            log.error('Plex album lookup error: %s', exc)
            return []

        if not albums:
            return []

        # Use the best match (first result)
        album = albums[0]
        tracks = [self._plex_to_track(t) for t in album.tracks()]
        log.info('Found album "%s" with %d tracks.', album.title, len(tracks))
        return tracks

    def search_albums(self, album_name: str, limit: int = 10) -> list:
        """
        Search for albums by name and cache results for disambiguation.

        Returns a list of dicts with 'title', 'artist', 'year', and
        'track_count' keys.  Raw plexapi objects are cached so
        ``get_album_tracks_by_index`` can retrieve the actual tracks.
        """
        self._last_album_search = []
        self._last_album_search_raw = []

        if not self._music_library:
            return []

        try:
            albums = self._music_library.searchAlbums(
                title=album_name, limit=limit,
            )
        except Exception as exc:
            log.error('Plex album search error: %s', exc)
            return []

        if not albums:
            return []

        results = []
        for a in albums:
            results.append({
                'title': a.title,
                'artist': a.parentTitle or 'Unknown',
                'year': getattr(a, 'year', None) or '',
                'track_count': len(a.tracks()) if hasattr(a, 'tracks') else 0,
            })
        self._last_album_search = results
        self._last_album_search_raw = albums
        log.info(
            'Album search "%s": %d results.', album_name, len(results),
        )
        return results

    def get_album_tracks_by_index(self, index: int) -> List[PlexTrack]:
        """
        Get tracks from a cached album search result by index (0-based).
        """
        if index < 0 or index >= len(self._last_album_search_raw):
            return []

        album = self._last_album_search_raw[index]
        tracks = [self._plex_to_track(t) for t in album.tracks()]
        log.info(
            'Loaded %d tracks from album "%s".',
            len(tracks), album.title,
        )
        return tracks

    def get_artist_tracks(self, artist_name: str, shuffle: bool = True) -> List[PlexTrack]:
        """
        Get all tracks by an artist.

        Returns tracks shuffled by default.
        """
        if not self._music_library:
            return []

        try:
            artists = self._music_library.searchArtists(title=artist_name, limit=5)
        except Exception as exc:
            log.error('Plex artist lookup error: %s', exc)
            return []

        if not artists:
            return []

        artist = artists[0]
        tracks = [self._plex_to_track(t) for t in artist.tracks()]
        log.info('Found artist "%s" with %d tracks.', artist.title, len(tracks))
        if shuffle:
            random.shuffle(tracks)
        return tracks

    # ------------------------------------------------------------------
    # Playlists
    # ------------------------------------------------------------------

    def list_playlists(self) -> List[dict]:
        """
        List all audio playlists on the Plex server.

        Returns a list of dicts with 'title' and 'count' keys.
        """
        if not self._server:
            return []

        try:
            playlists = self._server.playlists(playlistType='audio')
            return [
                {'title': pl.title, 'count': pl.leafCount}
                for pl in playlists
            ]
        except Exception as exc:
            log.error('Plex playlist list error: %s', exc)
            return []

    def get_playlist_tracks(self, name: str, shuffle: bool = True) -> List[PlexTrack]:
        """
        Get all tracks from a Plex playlist by name.

        Returns tracks shuffled by default.
        """
        if not self._server:
            return []

        try:
            playlists = self._server.playlists(playlistType='audio')
        except Exception as exc:
            log.error('Plex playlist lookup error: %s', exc)
            return []

        # Find by case-insensitive partial match
        match = None
        name_lower = name.lower()
        for pl in playlists:
            if pl.title.lower() == name_lower:
                match = pl
                break
            if name_lower in pl.title.lower() and match is None:
                match = pl

        if not match:
            return []

        tracks = [self._plex_to_track(t) for t in match.items()
                   if t.type == 'track']
        log.info('Found playlist "%s" with %d tracks.', match.title, len(tracks))
        if shuffle:
            random.shuffle(tracks)
        return tracks

