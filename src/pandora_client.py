"""
Pandora API client — wraps pydora for station listing and track fetching.

Uses the same unofficial API that the popular pianobar CLI uses.
Requires a Pandora account (free tier works).
"""

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from pandora.clientbuilder import SettingsDictBuilder

import config

log = logging.getLogger(__name__)


@dataclass
class Track:
    """Simplified track info for the player."""
    title: str
    artist: str
    album: str
    audio_url: str
    art_url: str = ''
    duration: int = 0  # seconds
    track_token: str = ''
    station_name: str = ''

    @property
    def display(self) -> str:
        return f'**{self.title}** by *{self.artist}*'


@dataclass
class Station:
    """Simplified station info."""
    id: str
    name: str

    def __str__(self):
        return self.name


@dataclass
class SearchResult:
    """A single search result (artist or song)."""
    token: str
    name: str
    artist: str
    kind: str          # 'artist' or 'song'
    score: int = 0
    _raw: object = field(default=None, repr=False)

    @property
    def display(self) -> str:
        if self.kind == 'song':
            return f'🎵 {self.name} — *{self.artist}*'
        return f'🎤 {self.artist}'


class PandoraClient:
    """
    Manages Pandora authentication, station listing, and track fetching.

    Usage::

        client = PandoraClient()
        client.login()
        stations = client.get_stations()
        tracks = client.get_playlist(stations[0])
    """

    def __init__(self):
        self._api = None
        self._stations: List[Station] = []
        self._current_station = None
        self._playlist: List[Track] = []
        self._logged_in = False
        self._last_search: List[SearchResult] = []

    @property
    def logged_in(self) -> bool:
        return self._logged_in

    @property
    def current_station(self) -> Optional[Station]:
        return self._current_station

    def login(self) -> None:
        """Authenticate with Pandora using credentials from config."""
        if not config.PANDORA_EMAIL or not config.PANDORA_PASSWORD:
            raise RuntimeError(
                'Set PANDORA_EMAIL and PANDORA_PASSWORD environment variables.'
            )

        log.info('Logging into Pandora as %s...', config.PANDORA_EMAIL)

        client = SettingsDictBuilder({
            'DECRYPTION_KEY': 'R=U!LH$O2B#',
            'ENCRYPTION_KEY': '6#26FRL$ZWD',
            'PARTNER_USER': 'android',
            'PARTNER_PASSWORD': 'AC7IBG09A3DTSYM4R41UJWL07VLN8JI7',
            'DEVICE': 'android-generic',
            'API_HOST': 'tuner.pandora.com/services/json/',
        }).build()

        client.login(config.PANDORA_EMAIL, config.PANDORA_PASSWORD)
        self._api = client
        self._logged_in = True
        log.info('Pandora login successful.')

    def get_stations(self) -> List[Station]:
        """Fetch the user's station list from Pandora."""
        if not self._api:
            raise RuntimeError('Not logged in. Call login() first.')

        raw_stations = self._api.get_station_list()
        self._stations = [
            Station(id=s.id, name=s.name)
            for s in raw_stations
        ]
        log.info('Loaded %d stations.', len(self._stations))
        return self._stations

    def find_station(self, query: str) -> Optional[Station]:
        """
        Find a station by name (case-insensitive partial match) or by number.

        Supports:
            find_station('Jazz')     — partial name match
            find_station('3')        — station #3 from the list
        """
        if not self._stations:
            self.get_stations()

        log.debug('Searching for station: "%s" among %d stations',
                  query, len(self._stations))

        # Try numeric selection first (e.g. "3" = third station)
        try:
            idx = int(query) - 1
            if 0 <= idx < len(self._stations):
                return self._stations[idx]
        except ValueError:
            pass

        query_lower = query.strip().lower()

        # Exact match first
        for s in self._stations:
            if s.name.lower() == query_lower:
                return s

        # Partial match
        for s in self._stations:
            if query_lower in s.name.lower():
                return s

        # Log available names when not found to aid debugging
        log.warning('No station matched "%s". Available: %s',
                    query, [s.name for s in self._stations[:10]])
        return None

    def set_station(self, station: Station) -> None:
        """Set the active station and clear the playlist buffer."""
        self._current_station = station
        self._playlist.clear()
        log.info('Station set to: %s', station.name)

    def get_next_track(self) -> Optional[Track]:
        """
        Get the next track from the current station.

        Automatically fetches a new batch from Pandora when the local
        buffer is empty.
        """
        if not self._current_station:
            return None

        if not self._playlist:
            self._fetch_playlist()

        if not self._playlist:
            log.warning('No tracks available from Pandora.')
            return None

        return self._playlist.pop(0)

    def _fetch_playlist(self) -> None:
        """Fetch a batch of tracks from the current station."""
        if not self._api or not self._current_station:
            return

        try:
            # Get the raw station object from Pandora
            raw_stations = self._api.get_station_list()
            raw_station = None
            for s in raw_stations:
                if s.id == self._current_station.id:
                    raw_station = s
                    break

            if not raw_station:
                log.error('Station %s not found.', self._current_station.name)
                return

            playlist = list(raw_station.get_playlist())
            log.info('Raw playlist: %d items from %s',
                     len(playlist), self._current_station.name)

            for item in playlist:
                # Skip ads
                if getattr(item, 'is_ad', False):
                    log.debug('Skipping ad item.')
                    continue

                # Prepare playback (some pydora versions need this)
                try:
                    if hasattr(item, 'prepare_playback'):
                        item.prepare_playback()
                except Exception:
                    pass

                audio_url = getattr(item, 'audio_url', '')
                if not audio_url:
                    log.debug('Skipping item with no audio URL.')
                    continue

                track = Track(
                    title=getattr(item, 'song_name', 'Unknown'),
                    artist=getattr(item, 'artist_name', 'Unknown'),
                    album=getattr(item, 'album_name', 'Unknown'),
                    audio_url=audio_url,
                    art_url=getattr(item, 'album_art_url', ''),
                    duration=getattr(item, 'track_length', 0) or 0,
                    track_token=getattr(item, 'track_token', ''),
                    station_name=self._current_station.name,
                )
                log.info('Queued: %s by %s [%s]',
                         track.title, track.artist, audio_url[:60])
                self._playlist.append(track)

            log.info('Fetched %d playable tracks from %s.',
                     len(self._playlist), self._current_station.name)

        except Exception as exc:
            log.error('Failed to fetch playlist: %s', exc, exc_info=True)

    def thumbs_up(self, track: Track) -> bool:
        """Give a thumbs up to a track."""
        if not self._api or not track.track_token:
            return False
        try:
            self._api.add_feedback(track.track_token, True)
            log.info('Thumbs up: %s', track.display)
            return True
        except Exception as exc:
            log.error('Thumbs up failed: %s', exc)
            return False

    def thumbs_down(self, track: Track) -> bool:
        """Give a thumbs down (also skips the track in Pandora's algorithm)."""
        if not self._api or not track.track_token:
            return False
        try:
            self._api.add_feedback(track.track_token, False)
            log.info('Thumbs down: %s', track.display)
            return True
        except Exception as exc:
            log.error('Thumbs down failed: %s', exc)
            return False

    # ------------------------------------------------------------------
    # Search & station management
    # ------------------------------------------------------------------

    def search(self, query: str) -> List[SearchResult]:
        """
        Search Pandora for artists and songs.

        Returns a combined list of SearchResult items (artists first,
        then songs).  The results are also cached in ``_last_search``
        for use by ``create_station_from_search()``.
        """
        if not self._api:
            raise RuntimeError('Not logged in.')

        raw = self._api.search(query)
        results: List[SearchResult] = []

        # Artists
        for item in getattr(raw, 'artists', []):
            results.append(SearchResult(
                token=getattr(item, 'token', ''),
                name=getattr(item, 'artist', 'Unknown Artist'),
                artist=getattr(item, 'artist', 'Unknown Artist'),
                kind='artist',
                score=getattr(item, 'score', 0),
                _raw=item,
            ))

        # Songs
        for item in getattr(raw, 'songs', []):
            results.append(SearchResult(
                token=getattr(item, 'token', ''),
                name=getattr(item, 'song_name', 'Unknown Song'),
                artist=getattr(item, 'artist', 'Unknown Artist'),
                kind='song',
                score=getattr(item, 'score', 0),
                _raw=item,
            ))

        self._last_search = results
        log.info('Search "%s": %d results.', query, len(results))
        return results

    def create_station_from_search(self, index: int) -> Optional[Station]:
        """
        Create a new station from a previous search result (by index).

        The station is added to the user's Pandora account and the
        local station list is refreshed.
        """
        if not self._api:
            raise RuntimeError('Not logged in.')
        if index < 0 or index >= len(self._last_search):
            return None

        result = self._last_search[index]
        if not result._raw or not hasattr(result._raw, 'create_station'):
            log.error('Search result has no create_station method.')
            return None

        try:
            new_station = result._raw.create_station()
            log.info('Created station: %s (from %s — %s)',
                     new_station.name, result.kind, result.name)
            # Refresh station list
            self.get_stations()
            return Station(id=new_station.id, name=new_station.name)
        except Exception as exc:
            log.error('Failed to create station: %s', exc)
            return None

    def delete_station(self, station: Station) -> bool:
        """Delete a station from the user's Pandora account."""
        if not self._api:
            return False
        try:
            self._api.delete_station(station.id)
            log.info('Deleted station: %s', station.name)
            self.get_stations()
            return True
        except Exception as exc:
            log.error('Failed to delete station: %s', exc)
            return False

