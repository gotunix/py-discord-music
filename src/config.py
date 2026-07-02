"""
Configuration for the Discord Pandora Music Bot.

All settings loaded from environment variables.
"""

import os

# Discord
DISCORD_BOT_TOKEN = os.environ.get('DISCORD_MUSIC_BOT_TOKEN', '')
COMMAND_PREFIX = os.environ.get('BOT_PREFIX', '!')

# Pandora
PANDORA_EMAIL = os.environ.get('PANDORA_EMAIL', '')
PANDORA_PASSWORD = os.environ.get('PANDORA_PASSWORD', '')

# Plex
PLEX_URL = os.environ.get('PLEX_URL', '')                          # e.g. http://192.168.1.100:32400
PLEX_TOKEN = os.environ.get('PLEX_TOKEN', '')                      # X-Plex-Token
PLEX_MUSIC_LIBRARY = os.environ.get('PLEX_MUSIC_LIBRARY', 'Music') # Library name

# Audio
AUDIO_QUALITY = os.environ.get('AUDIO_QUALITY', 'highQuality')  # lowQuality, mediumQuality, highQuality
FFMPEG_PATH = os.environ.get('FFMPEG_PATH', 'ffmpeg')
DEFAULT_VOLUME = float(os.environ.get('DEFAULT_VOLUME', '0.03'))  # 0.0 - 1.0
