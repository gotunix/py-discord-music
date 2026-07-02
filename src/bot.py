"""
Discord Music Bot — Pandora Radio + YouTube.

Streams Pandora radio stations and YouTube audio into Discord voice.

Pandora Commands::

    !join           Join your current voice channel
    !leave          Disconnect from voice
    !stations       List your Pandora stations
    !station <name> Switch to a station and start playing
    !search <query> Search Pandora for artists/songs
    !addstation <#> Create a station from search results
    !thumbsup       👍 the current track
    !thumbsdown     👎 the current track

YouTube Commands::

    !yt <query>     Search YouTube
    !play <# or URL> Play from search results or a direct URL
    !playlist <URL> Load a YouTube playlist into the queue
    !queue          Show the current queue
    !clear          Clear the queue

Shared Commands::

    !playing        Show the current track
    !skip           Skip to the next track
    !pause          Pause playback
    !resume         Resume playback
    !volume <0-100> Set playback volume
    !move <from> <to> Move a track in the queue
    !remove <pos>   Remove a track from the queue
    !shuffle        Shuffle the queue
    !stop           Stop playback (stays in channel)
    !restartqueue   Restart the active queue from the beginning
    !savequeue <name> Save the current queue to a playlist
    !loadqueue <name> Load a saved playlist
    !savedplaylists List your saved playlists

Environment variables::

    DISCORD_BOT_TOKEN   — Discord bot token
    PANDORA_EMAIL       — Pandora account email
    PANDORA_PASSWORD    — Pandora account password

Run::

    python bot.py
"""

import asyncio
import json
import logging
import sys
import time
from typing import Dict

import discord
from discord.ext import commands

import config
from pandora_client import PandoraClient
from plex_client import PlexClient
from player import Player
from yt_client import YouTubeClient

# Load libopus — discord.py needs it for voice but can't always find it
_OPUS_PATHS = [
    '/opt/homebrew/lib/libopus.dylib',        # macOS ARM (Homebrew)
    '/usr/local/lib/libopus.dylib',           # macOS Intel (Homebrew)
    '/usr/lib/x86_64-linux-gnu/libopus.so.0', # Debian/Ubuntu
    'libopus',                                 # system default
]
if not discord.opus.is_loaded():
    for path in _OPUS_PATHS:
        try:
            discord.opus.load_opus(path)
            break
        except OSError:
            continue
    if not discord.opus.is_loaded():
        raise RuntimeError('Could not load libopus. Install it: brew install opus')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s — %(message)s',
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# Bot setup with required intents
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix=config.COMMAND_PREFIX, intents=intents, help_command=None)

# Shared clients (one login for all guilds)
pandora = PandoraClient()
youtube = YouTubeClient()
plex = PlexClient()

# Per-guild players
players: Dict[int, Player] = {}


def get_player(guild_id: int) -> Player:
    """Get or create a player for a guild."""
    if guild_id not in players:
        p = Player(pandora)
        p.on_track_start = _send_now_playing_hook
        players[guild_id] = p
    return players[guild_id]

async def _send_now_playing_hook(player: Player, track):
    if not player.text_channel:
        return
    is_yt = player.mode == 'youtube'
    is_plex = player.mode == 'plex'

    if is_yt: color = 0xFF0000
    elif is_plex: color = 0xE5A00D
    else: color = 0x224099

    embed = discord.Embed(title='🎵 Now Playing', description=track.display, color=color)

    if is_yt:
        if hasattr(track, 'uploader') and track.uploader:
            embed.add_field(name='Channel', value=track.uploader, inline=True)
        if player.queue_length > 0:
            embed.add_field(name='Queue', value=f'{player.queue_length} tracks', inline=True)
    elif is_plex:
        embed.add_field(name='Album', value=getattr(track, 'album', 'Unknown'), inline=True)
        embed.add_field(name='Artist', value=getattr(track, 'artist', 'Unknown'), inline=True)
        if player.plex_queue_length > 0:
            embed.add_field(name='Queue', value=f'{player.plex_queue_length} tracks', inline=True)
    else:
        embed.add_field(name='Album', value=getattr(track, 'album', 'Unknown'), inline=True)
        embed.add_field(name='Station', value=getattr(track, 'station_name', ''), inline=True)

    thumb = getattr(track, 'thumbnail', '') or getattr(track, 'art_url', '')
    if thumb: embed.set_thumbnail(url=thumb)

    if is_yt: source_icon = '▶ YouTube'
    elif is_plex: source_icon = '🟠 Plex'
    else: source_icon = '📻 Pandora'
    embed.set_footer(text=f'{source_icon} • Volume: {int(player.volume * 100)}%')

    try:
        await player.text_channel.send(embed=embed)
    except Exception as exc:
        log.error("Failed to auto-broadcast now playing message: %s", exc)

@bot.before_invoke
async def set_text_channel_before_invoke(ctx: commands.Context):
    if ctx.guild:
        player = get_player(ctx.guild.id)
        player.text_channel = ctx.channel


# ---------------------------------------------------------------------- events

@bot.event
async def on_ready():
    log.info('Bot ready: %s (ID: %s)', bot.user.name, bot.user.id)
    log.info('In %d guild(s)', len(bot.guilds))

    # Login to Pandora at startup
    try:
        pandora.login()
        pandora.get_stations()
        log.info('Pandora ready — %d stations loaded.',
                 len(pandora._stations))
    except Exception as exc:
        log.error('Pandora login failed: %s', exc)
        log.error('Set PANDORA_EMAIL and PANDORA_PASSWORD env vars.')

    # Connect to Plex at startup
    try:
        if config.PLEX_URL and config.PLEX_TOKEN:
            plex.connect()
            log.info('Plex ready.')
        else:
            log.warning('PLEX_URL / PLEX_TOKEN not set — Plex features disabled.')
    except Exception as exc:
        log.error('Plex connection failed: %s', exc)


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if before.channel and before.channel != after.channel:
        guild_id = before.channel.guild.id
        if guild_id in players:
            player = players[guild_id]
            if player.is_connected and player.voice_client and player.voice_client.channel.id == before.channel.id:
                non_bots = [m for m in before.channel.members if not m.bot]
                if len(non_bots) == 0:
                    log.info('All users left %s. Pausing and leaving.', before.channel.name)
                    if player.is_playing:
                        player.pause()
                    await player.leave(save_queue=True)

# ---------------------------------------------------------------------- commands

def save_queue_to_file(guild_id: int, name: str, queue_data: list):
    try:
        with open('playlists.json', 'r') as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    if str(guild_id) not in data:
        data[str(guild_id)] = {}
    data[str(guild_id)][name] = queue_data
    with open('playlists.json', 'w') as f:
        json.dump(data, f)

def load_queue_from_file(guild_id: int, name: str):
    try:
        with open('playlists.json', 'r') as f:
            data = json.load(f)
            return data.get(str(guild_id), {}).get(name)
    except (FileNotFoundError, json.JSONDecodeError):
        return None

@bot.command(name='help', help='Show this help message')
async def cmd_help(ctx: commands.Context, category: str = None):
    categories = {
        'youtube': ['yt', 'play', 'playlist'],
        'pandora': ['stations', 'station', 'search', 'addstation', 'deletestation', 'thumbsup', 'thumbsdown'],
        'plex': ['plex', 'plexplay', 'plexalbum', 'plexartist', 'plexplaylists', 'plexplaylist'],
        'general': [
            'join', 'leave', 'playing', 'skip', 'pause', 'resume', 'stop', 'volume', 
            'queue', 'clear', 'savequeue', 'loadqueue', 'savedplaylists', 'restartqueue', 
            'move', 'remove', 'shuffle'
        ]
    }
    
    if category:
        cat_lower = category.lower()
        if cat_lower in categories:
            cmds = categories[cat_lower]
            lines = [f"**{cat_lower.capitalize()} Commands:**\n"]
            for name in cmds:
                cmd = bot.get_command(name)
                if cmd:
                    aliases = f" (or `!{cmd.aliases[0]}`)" if cmd.aliases else ""
                    lines.append(f"• **!{name}**{aliases} - {cmd.help}")
            await ctx.send('\n'.join(lines))
        else:
            cmd = bot.get_command(category)
            if cmd:
                aliases = f" (Aliases: {', '.join(cmd.aliases)})" if cmd.aliases else ""
                await ctx.send(f"**!{cmd.name}**{aliases}\n{cmd.help}")
            else:
                await ctx.send(f"❌ Unknown command or category: `{category}`.\n**Categories:** {', '.join(categories.keys())}")
        return
        
    embed = discord.Embed(title="🎵 Music Bot Commands", description="Use `!help <category>` or `!help <command>` for details.", color=0x3498db)
    for cat, cmds in categories.items():
        cmd_list = []
        for name in cmds:
            cmd = bot.get_command(name)
            if cmd:
                # Adding the description to the main help menu!
                cmd_list.append(f"`!{name}` - {cmd.help}")
        if cmd_list:
            embed.add_field(name=f"📻 {cat.capitalize()}", value="\n".join(cmd_list), inline=False)
            
    await ctx.send(embed=embed)

@bot.command(name='join', help='Join your voice channel')
@commands.guild_only()
async def cmd_join(ctx: commands.Context):
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send('❌ You need to be in a voice channel first.')
        return

    player = get_player(ctx.guild.id)
    await player.join(ctx.author.voice.channel)
    await ctx.send(f'🔊 Joined **{ctx.author.voice.channel.name}**')


@bot.command(name='leave', help='Leave the voice channel')
@commands.guild_only()
async def cmd_leave(ctx: commands.Context):
    player = get_player(ctx.guild.id)
    if not player.is_connected:
        await ctx.send('❌ Not connected to any voice channel.')
        return

    await player.leave()
    await ctx.send('👋 Disconnected from voice.')


@bot.command(name='stations', help='List your Pandora stations')
@commands.guild_only()
async def cmd_stations(ctx: commands.Context):
    if not pandora.logged_in:
        await ctx.send('❌ Not logged into Pandora.')
        return

    stations = pandora.get_stations()
    if not stations:
        await ctx.send('No stations found on your Pandora account.')
        return

    lines = [f'📻 **Your Pandora Stations** ({len(stations)})\n']
    for i, s in enumerate(stations, 1):
        marker = ' 🎵' if pandora.current_station and s.id == pandora.current_station.id else ''
        lines.append(f'`{i:2d}.` {s.name}{marker}')

    # Discord has a 2000 char limit, split if needed
    msg = '\n'.join(lines)
    if len(msg) > 1900:
        # Send in chunks
        chunk = []
        for line in lines:
            chunk.append(line)
            if len('\n'.join(chunk)) > 1800:
                await ctx.send('\n'.join(chunk))
                chunk = []
        if chunk:
            await ctx.send('\n'.join(chunk))
    else:
        await ctx.send(msg)


@bot.command(name='station', help='Switch station: !station <name>')
@commands.guild_only()
async def cmd_station(ctx: commands.Context, *, name: str = ''):
    if not pandora.logged_in:
        await ctx.send('❌ Not logged into Pandora.')
        return

    if not name:
        await ctx.send('Usage: `!station <station name>`')
        return

    station = pandora.find_station(name)
    if not station:
        await ctx.send(f'❌ No station matching "**{name}**". Use `!stations` to see your list.')
        return

    pandora.set_station(station)
    await ctx.send(f'📻 Switched to **{station.name}**')

    # Auto-play if connected to voice
    player = get_player(ctx.guild.id)
    if player.is_connected:
        track = await player.play_pandora_next()
        if track:
            await _send_now_playing(ctx, track)
    else:
        await ctx.send('Use `!join` to connect to voice, then I\'ll start playing.')


@bot.command(name='playing', aliases=['np', 'nowplaying'], help='Show current track')
@commands.guild_only()
async def cmd_playing(ctx: commands.Context):
    player = get_player(ctx.guild.id)

    if not player.current_track:
        await ctx.send('🔇 Nothing playing right now.')
        return

    await _send_now_playing(ctx, player.current_track)


@bot.command(name='skip', aliases=['next', 's'], help='Skip to the next track')
@commands.guild_only()
async def cmd_skip(ctx: commands.Context):
    player = get_player(ctx.guild.id)

    if not player.is_playing:
        await ctx.send('❌ Nothing is playing.')
        return

    await ctx.send('⏭️ Skipping...')
    track = await player.skip()
    if track:
        await _send_now_playing(ctx, track)
    else:
        await ctx.send('❌ No more tracks available.')


@bot.command(name='thumbsup', aliases=['like', 'up'], help='👍 the current track')
@commands.guild_only()
async def cmd_thumbsup(ctx: commands.Context):
    player = get_player(ctx.guild.id)
    if not player.current_track:
        await ctx.send('❌ Nothing playing.')
        return

    ok = pandora.thumbs_up(player.current_track)
    if ok:
        await ctx.send(f'👍 Liked: {player.current_track.display}')
    else:
        await ctx.send('❌ Could not send feedback.')


@bot.command(name='thumbsdown', aliases=['dislike', 'down'], help='👎 the current track')
@commands.guild_only()
async def cmd_thumbsdown(ctx: commands.Context):
    player = get_player(ctx.guild.id)
    if not player.current_track:
        await ctx.send('❌ Nothing playing.')
        return

    ok = pandora.thumbs_down(player.current_track)
    if ok:
        await ctx.send(f'👎 Disliked: {player.current_track.display}')
        # Skip after thumbs down (Pandora behavior)
        track = await player.skip()
        if track:
            await _send_now_playing(ctx, track)


@bot.command(name='volume', aliases=['vol', 'v'], help='Set volume: !volume <0-100>')
@commands.guild_only()
async def cmd_volume(ctx: commands.Context, level: int = -1):
    if level < 0 or level > 100:
        player = get_player(ctx.guild.id)
        current = int(player.volume * 100)
        await ctx.send(f'🔊 Volume: **{current}%**. Use `!volume <0-100>` to change.')
        return

    player = get_player(ctx.guild.id)
    player.set_volume(level / 100.0)
    await ctx.send(f'🔊 Volume set to **{level}%**')


@bot.command(name='stop', help='Stop playback (stay in channel)')
@commands.guild_only()
async def cmd_stop(ctx: commands.Context):
    player = get_player(ctx.guild.id)
    if not player.is_playing:
        await ctx.send('❌ Nothing is playing.')
        return

    await player.stop()
    await ctx.send('⏹️ Stopped.')


@bot.command(name='search', aliases=['find'], help='Search Pandora: !search <query>')
@commands.guild_only()
async def cmd_search(ctx: commands.Context, *, query: str = ''):
    if not pandora.logged_in:
        await ctx.send('❌ Not logged into Pandora.')
        return
    if not query:
        await ctx.send('Usage: `!search <artist or song name>`')
        return

    await ctx.send(f'🔍 Searching for "**{query}**"...')

    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, pandora.search, query)

    if not results:
        await ctx.send(f'No results for "**{query}**".')
        return

    # Show top 10 results
    lines = [f'🔍 **Search Results** for "**{query}**"\n']
    for i, r in enumerate(results[:10], 1):
        lines.append(f'`{i:2d}.` {r.display}')
    lines.append(f'\nUse `!addstation <number>` to create a station.')

    await ctx.send('\n'.join(lines))


@bot.command(name='addstation', aliases=['add'], help='Create station from search: !addstation <number>')
@commands.guild_only()
async def cmd_addstation(ctx: commands.Context, number: int = 0):
    if not pandora.logged_in:
        await ctx.send('❌ Not logged into Pandora.')
        return
    if number < 1:
        await ctx.send('Usage: `!addstation <number>` (use `!search` first)')
        return

    if not pandora._last_search:
        await ctx.send('❌ No search results. Use `!search <query>` first.')
        return

    idx = number - 1
    if idx >= len(pandora._last_search):
        await ctx.send(f'❌ Invalid number. Choose 1-{len(pandora._last_search)}.')
        return

    result = pandora._last_search[idx]
    await ctx.send(f'📻 Creating station from {result.display}...')

    loop = asyncio.get_event_loop()
    station = await loop.run_in_executor(
        None, pandora.create_station_from_search, idx
    )

    if not station:
        await ctx.send('❌ Failed to create station.')
        return

    await ctx.send(f'✅ Created station: **{station.name}**')

    # Auto-switch and play
    pandora.set_station(station)
    player = get_player(ctx.guild.id)
    if player.is_connected:
        track = await player.play_pandora_next()
        if track:
            await _send_now_playing(ctx, track)
    else:
        await ctx.send('Use `!join` then `!station` to start playing.')


@bot.command(name='deletestation', aliases=['delstation', 'rmstation'],
             help='Delete a station: !deletestation <name>')
@commands.guild_only()
async def cmd_deletestation(ctx: commands.Context, *, name: str = ''):
    if not pandora.logged_in:
        await ctx.send('❌ Not logged into Pandora.')
        return
    if not name:
        await ctx.send('Usage: `!deletestation <station name>`')
        return

    station = pandora.find_station(name)
    if not station:
        await ctx.send(f'❌ No station matching "**{name}**".')
        return

    loop = asyncio.get_event_loop()
    ok = await loop.run_in_executor(None, pandora.delete_station, station)

    if ok:
        await ctx.send(f'🗑️ Deleted station: **{station.name}**')
    else:
        await ctx.send('❌ Failed to delete station.')

# --------------------------------------------------------- YouTube commands

@bot.command(name='yt', aliases=['ytsearch'], help='Search YouTube: !yt <query>')
@commands.guild_only()
async def cmd_yt(ctx: commands.Context, *, query: str = ''):
    if not query:
        await ctx.send('Usage: `!yt <search query>`')
        return

    await ctx.send(f'🔍 Searching YouTube for "**{query}**"...')

    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, youtube.search, query)

    if not results:
        await ctx.send(f'No results for "**{query}**".')
        return

    lines = [f'🔍 **YouTube Results** for "**{query}**"\n']
    for i, r in enumerate(results[:10], 1):
        lines.append(f'`{i:2d}.` {r.display}')
    lines.append(f'\nUse `!play <number>` to play, or `!play <YouTube URL>`.')

    await ctx.send('\n'.join(lines))


@bot.command(name='play', aliases=['p'], help='Play YouTube: !play <# or URL>')
@commands.guild_only()
async def cmd_play(ctx: commands.Context, *, query: str = ''):
    player = get_player(ctx.guild.id)

    if not query:
        if player.queue_length > 0 and not player.is_playing:
            if not player.is_connected:
                if ctx.author.voice and ctx.author.voice.channel:
                    await player.join(ctx.author.voice.channel)
                else:
                    await ctx.send('❌ Join a voice channel first.')
                    return
            await ctx.send('▶️ Starting queue...')
            track = await player.play_youtube_next()
            if track:
                await _send_now_playing(ctx, track)
            return
        await ctx.send('Usage: `!play <number>` (after `!yt`) or `!play <YouTube URL>`')
        return

    if not player.is_connected:
        # Auto-join if user is in a voice channel
        if ctx.author.voice and ctx.author.voice.channel:
            await player.join(ctx.author.voice.channel)
            await ctx.send(f'🔊 Joined **{ctx.author.voice.channel.name}**')
        else:
            await ctx.send('❌ Join a voice channel first, or use `!join`.')
            return

    # Check if it's a number (from search results)
    yt_track = None
    if query.strip().isdigit():
        idx = int(query) - 1
        if not youtube._last_search:
            await ctx.send('❌ No search results. Use `!yt <query>` first.')
            return
        if idx < 0 or idx >= len(youtube._last_search):
            await ctx.send(f'❌ Choose 1-{len(youtube._last_search)}.')
            return

        await ctx.send('⏳ Extracting audio...')
        loop = asyncio.get_event_loop()
        yt_track = await loop.run_in_executor(
            None, youtube.extract_from_search, idx
        )
    else:
        # It's a URL or search query
        url = query
        if not url.startswith('http'):
            # Treat as a direct search + play
            loop = asyncio.get_event_loop()
            results = await loop.run_in_executor(None, youtube.search, query, 1)
            if not results:
                await ctx.send(f'No results for "**{query}**".')
                return
            url = results[0].url

        # Detect playlist URLs and load all tracks
        if 'list=' in url and '/watch?' not in url or url.startswith(('https://www.youtube.com/playlist', 'https://youtube.com/playlist')):
            await ctx.send('⏳ Loading playlist... (this may take a moment)')
            loop = asyncio.get_event_loop()
            tracks = await loop.run_in_executor(None, youtube.extract_playlist, url)
            if not tracks:
                await ctx.send('❌ Could not load playlist.')
                return
            for t in tracks:
                player.add_to_queue(t)
            await ctx.send(f'🎶 Added **{len(tracks)}** tracks to the queue.')
            if not player.is_playing:
                track = await player.play_youtube_next()
                if track:
                    await _send_now_playing(ctx, track)
                else:
                    await ctx.send('❌ Could not start playback (tracks may be unavailable or blocked).')
            return

        await ctx.send('⏳ Extracting audio...')
        loop = asyncio.get_event_loop()
        yt_track = await loop.run_in_executor(None, youtube.extract, url)

    if not yt_track:
        await ctx.send('❌ Could not extract audio from that video.')
        return

    # If something is playing, add to queue; otherwise play immediately
    if player.is_playing:
        pos = player.add_to_queue(yt_track)
        await ctx.send(f'📝 Added to queue (#{pos}): {yt_track.display}')
    else:
        track = await player.play_youtube_now(yt_track)
        if track:
            await _send_now_playing(ctx, track)
        else:
            await ctx.send(f'❌ Failed to play **{yt_track.title}** (it may be blocked or unavailable).')


@bot.command(name='playlist', aliases=['pl'], help='Load YouTube playlist: !playlist <URL>')
@commands.guild_only()
async def cmd_playlist(ctx: commands.Context, url: str = ''):
    if not url:
        await ctx.send('Usage: `!playlist <YouTube playlist URL>`')
        return

    player = get_player(ctx.guild.id)
    if not player.is_connected:
        if ctx.author.voice and ctx.author.voice.channel:
            await player.join(ctx.author.voice.channel)
            await ctx.send(f'🔊 Joined **{ctx.author.voice.channel.name}**')
        else:
            await ctx.send('❌ Join a voice channel first.')
            return

    await ctx.send('⏳ Loading playlist... (this may take a moment)')

    loop = asyncio.get_event_loop()
    tracks = await loop.run_in_executor(None, youtube.extract_playlist, url)

    if not tracks:
        await ctx.send('❌ Could not load playlist.')
        return

    for t in tracks:
        player.add_to_queue(t)

    await ctx.send(f'🎶 Added **{len(tracks)}** tracks to the queue.')

    if not player.is_playing:
        track = await player.play_youtube_next()
        if track:
            await _send_now_playing(ctx, track)
        else:
            await ctx.send('❌ Could not start playback (tracks may be unavailable or blocked).')


@bot.command(name='queue', aliases=['q'], help='Show the current queue')
@commands.guild_only()
async def cmd_queue(ctx: commands.Context):
    player = get_player(ctx.guild.id)

    # Pick the active queue based on mode
    if player.mode == 'plex':
        queue = player.plex_queue
        q_len = player.plex_queue_length
    else:
        queue = player.queue
        q_len = player.queue_length

    if not player.current_track and q_len == 0:
        await ctx.send('💭 Queue is empty. Use `!play` or `!yt` to add tracks.')
        return

    lines = []
    if player.current_track:
        paused = ' ⏸️' if player.is_paused else ''
        lines.append(f'🎵 **Now Playing:** {player.current_track.display}{paused}')
        lines.append(f'   Mode: {player.mode.capitalize()}')
        lines.append('')

    if queue:
        lines.append(f'📝 **Queue** ({len(queue)} tracks)\n')
        for i, t in enumerate(queue[:15], 1):
            lines.append(f'`{i:2d}.` {t.display_short}')
        if len(queue) > 15:
            lines.append(f'   ... and {len(queue) - 15} more')
    elif player.mode != 'pandora':
        lines.append('Queue is empty.')

    await ctx.send('\n'.join(lines))


@bot.command(name='clear', help='Clear the active queue')
@commands.guild_only()
async def cmd_clear(ctx: commands.Context):
    player = get_player(ctx.guild.id)
    if player.mode == 'plex':
        count = player.clear_plex_queue()
    else:
        count = player.clear_queue()
    await ctx.send(f'🗑️ Cleared **{count}** tracks from the queue.')

@bot.command(name='savequeue', aliases=['saveplaylist', 'sq'], help='Save the current queue: !savequeue <name>')
@commands.guild_only()
async def cmd_savequeue(ctx: commands.Context, *, name: str = ''):
    if not name:
        await ctx.send('Usage: `!savequeue <name>`')
        return

    player = get_player(ctx.guild.id)
    
    if player.mode == 'plex':
        queue = [player.current_track] + list(player.plex_queue) if player.current_track else list(player.plex_queue)
        queue_data = [{
            'type': 'plex',
            'title': t.title,
            'artist': t.artist,
            'album': t.album,
            'audio_url': t.audio_url,
            'art_url': t.art_url,
            'duration': t.duration,
            'rating_key': t.rating_key
        } for t in queue if t]
    else:
        queue = [player.current_track] + list(player.queue) if player.current_track else list(player.queue)
        queue_data = [{
            'type': 'youtube',
            'title': t.title,
            'url': t.url,
            'webpage_url': getattr(t, 'webpage_url', ''),
            'duration': t.duration,
            'thumbnail': t.thumbnail,
            'uploader': t.uploader,
            'http_headers': t.http_headers
        } for t in queue if t]
        
    if not queue_data:
        await ctx.send('❌ Queue is empty. Nothing to save.')
        return
        
    save_queue_to_file(ctx.guild.id, name, queue_data)
    await ctx.send(f'💾 Saved queue as **{name}** with {len(queue_data)} tracks.')

@bot.command(name='loadqueue', aliases=['loadplaylist', 'lq'], help='Load a saved queue: !loadqueue <name>')
@commands.guild_only()
async def cmd_loadqueue(ctx: commands.Context, *, name: str = ''):
    if not name:
        await ctx.send('Usage: `!loadqueue <name>`. Or `!savedplaylists` to see them.')
        return

    queue_data = load_queue_from_file(ctx.guild.id, name)
    if not queue_data:
        await ctx.send(f'❌ No saved queue found with the name **{name}**.')
        return

    player = get_player(ctx.guild.id)
    if not player.is_connected:
        if ctx.author.voice and ctx.author.voice.channel:
            await player.join(ctx.author.voice.channel)
            await ctx.send(f'🔊 Joined **{ctx.author.voice.channel.name}**')
        else:
            await ctx.send('❌ Join a voice channel first.')
            return

    from yt_client import YTTrack
    from plex_client import PlexTrack

    youtube_tracks = 0
    plex_tracks = 0
    for t_data in queue_data:
        track_type = t_data.get('type')
        if track_type == 'plex':
            track = PlexTrack(
                title=t_data.get('title', ''),
                artist=t_data.get('artist', ''),
                album=t_data.get('album', ''),
                audio_url=t_data.get('audio_url', ''),
                art_url=t_data.get('art_url', ''),
                duration=t_data.get('duration', 0),
                rating_key=t_data.get('rating_key', '')
            )
            player.add_to_plex_queue(track)
            plex_tracks += 1
        elif track_type == 'youtube':
            track = YTTrack(
                title=t_data.get('title', ''),
                url=t_data.get('url', ''),
                webpage_url=t_data.get('webpage_url', ''),
                duration=t_data.get('duration', 0),
                thumbnail=t_data.get('thumbnail', ''),
                uploader=t_data.get('uploader', ''),
                http_headers=t_data.get('http_headers', {})
            )
            player.add_to_queue(track)
            youtube_tracks += 1
            
    total = youtube_tracks + plex_tracks
    await ctx.send(f'📂 Loaded **{total}** tracks from playlist **{name}**.')
    
    if plex_tracks > 0 and youtube_tracks == 0:
        if not player.is_playing:
            track = await player.play_plex_next()
            if track:
                await _send_now_playing(ctx, track)
    elif youtube_tracks > 0:
        if not player.is_playing:
            track = await player.play_youtube_next()
            if track:
                await _send_now_playing(ctx, track)

@bot.command(name='savedplaylists', aliases=['sqs'], help='List your saved playlists')
@commands.guild_only()
async def cmd_savedplaylists(ctx: commands.Context):
    try:
        with open('playlists.json', 'r') as f:
            data = json.load(f)
            guild_data = data.get(str(ctx.guild.id), {})
    except (FileNotFoundError, json.JSONDecodeError):
        guild_data = {}
        
    if not guild_data:
        await ctx.send('You have no saved playlists.')
        return
        
    lines = ['📂 **Your Saved Playlists**\n']
    for name, tracks in guild_data.items():
        lines.append(f'• **{name}** ({len(tracks)} tracks)')
        
    await ctx.send('\n'.join(lines))

@bot.command(name='restartqueue', aliases=['restart'], help='Restart the active queue from the beginning')
@commands.guild_only()
async def cmd_restartqueue(ctx: commands.Context):
    player = get_player(ctx.guild.id)
    if player.restart_queue():
        await ctx.send('🔄 Restarting playlist from the beginning...')
        
        # Start playing immediately
        if player.mode == 'plex':
            track = await player.play_plex_next()
        else:
            track = await player.play_youtube_next()
            
        if track:
            await _send_now_playing(ctx, track)
    else:
        await ctx.send('❌ No track history available to restart.')


@bot.command(name='pause', help='Pause playback')
@commands.guild_only()
async def cmd_pause(ctx: commands.Context):
    player = get_player(ctx.guild.id)
    if player.pause():
        await ctx.send('⏸️ Paused.')
    else:
        await ctx.send('❌ Nothing is playing.')


@bot.command(name='resume', aliases=['unpause'], help='Resume playback')
@commands.guild_only()
async def cmd_resume(ctx: commands.Context):
    player = get_player(ctx.guild.id)
    if player.resume():
        await ctx.send('▶️ Resumed.')
    elif player.queue_length > 0 and not player.is_playing:
        if not player.is_connected:
            if ctx.author.voice and ctx.author.voice.channel:
                await player.join(ctx.author.voice.channel)
            else:
                await ctx.send('❌ Join a voice channel first.')
                return
        await ctx.send('▶️ Starting YouTube queue...')
        track = await player.play_youtube_next()
        if track:
            await _send_now_playing(ctx, track)
    elif getattr(player, 'plex_queue_length', 0) > 0 and not player.is_playing:
        if not player.is_connected:
            if ctx.author.voice and ctx.author.voice.channel:
                await player.join(ctx.author.voice.channel)
            else:
                await ctx.send('❌ Join a voice channel first.')
                return
        await ctx.send('▶️ Starting Plex queue...')
        track = await player.play_plex_next()
        if track:
            await _send_now_playing(ctx, track)
    else:
        await ctx.send('❌ Nothing is paused or queued to resume.')


@bot.command(name='move', aliases=['mv'], help='Move a track in the queue: !move <from> <to>')
@commands.guild_only()
async def cmd_move(ctx: commands.Context, from_pos: int = 0, to_pos: int = 0):
    if from_pos < 1 or to_pos < 1:
        await ctx.send('Usage: `!move <from> <to>` (positions from `!queue`)')
        return

    player = get_player(ctx.guild.id)

    # Pick the active queue based on mode
    if player.mode == 'plex':
        q_len = player.plex_queue_length
        if from_pos > q_len or to_pos > q_len:
            await ctx.send(f'❌ Queue only has **{q_len}** tracks.')
            return
        track = player.move_in_plex_queue(from_pos, to_pos)
    else:
        q_len = player.queue_length
        if from_pos > q_len or to_pos > q_len:
            await ctx.send(f'❌ Queue only has **{q_len}** tracks.')
            return
        track = player.move_in_queue(from_pos, to_pos)

    await ctx.send(f'↕️ Moved **{track.display_short if hasattr(track, "display_short") else track.title}** from #{from_pos} → #{to_pos}')


@bot.command(name='remove', aliases=['rm'], help='Remove track(s): !remove <pos> or !remove <start>-<end>')
@commands.guild_only()
async def cmd_remove(ctx: commands.Context, *, pos_arg: str = ''):
    if not pos_arg:
        await ctx.send('Usage: `!remove <position>` or `!remove <start>-<end>`')
        return

    player = get_player(ctx.guild.id)

    # Determine active queue length
    if player.mode == 'plex':
        q_len = player.plex_queue_length
    else:
        q_len = player.queue_length

    # Parse range (e.g. "7-100") vs single position
    if '-' in pos_arg:
        parts = pos_arg.split('-', 1)
        try:
            start = int(parts[0])
            end = int(parts[1])
        except ValueError:
            await ctx.send('Usage: `!remove <start>-<end>` (e.g. `!rm 7-100`)')
            return
        if start < 1 or end < start:
            await ctx.send('❌ Invalid range. Start must be ≥ 1 and end ≥ start.')
            return
        if start > q_len:
            await ctx.send(f'❌ Queue only has **{q_len}** tracks.')
            return
        # Clamp end to queue length
        end = min(end, q_len)
        if player.mode == 'plex':
            count = player.remove_range_from_plex_queue(start, end)
        else:
            count = player.remove_range_from_queue(start, end)
        await ctx.send(f'🗑️ Removed **{count}** tracks (#{start}–#{end}).')
    else:
        try:
            pos = int(pos_arg)
        except ValueError:
            await ctx.send('Usage: `!remove <position>` or `!remove <start>-<end>`')
            return
        if pos < 1:
            await ctx.send('Usage: `!remove <position>` (positions from `!queue`)')
            return
        if pos > q_len:
            await ctx.send(f'❌ Queue only has **{q_len}** tracks.')
            return
        if player.mode == 'plex':
            track = player.remove_from_plex_queue(pos)
        else:
            track = player.remove_from_queue(pos)
        await ctx.send(f'🗑️ Removed #{pos}: **{track.display_short if hasattr(track, "display_short") else track.title}**')


@bot.command(name='shuffle', help='Shuffle the current queue')
@commands.guild_only()
async def cmd_shuffle(ctx: commands.Context):
    player = get_player(ctx.guild.id)

    if player.mode == 'plex':
        count = player.shuffle_plex_queue()
    else:
        count = player.shuffle_queue()

    if count == 0:
        await ctx.send('❌ Queue is empty — nothing to shuffle.')
    else:
        await ctx.send(f'🔀 Shuffled **{count}** tracks.')


# --------------------------------------------------------- Plex commands

@bot.command(name='plex', aliases=['plexsearch'], help='Search Plex music: !plex <query>')
@commands.guild_only()
async def cmd_plex(ctx: commands.Context, *, query: str = ''):
    if not query:
        await ctx.send('Usage: `!plex <search query>`')
        return

    if not plex.connected:
        await ctx.send('❌ Not connected to Plex. Set PLEX_URL and PLEX_TOKEN.')
        return

    await ctx.send(f'🔍 Searching Plex for "**{query}**"...')

    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, plex.search, query)

    if not results:
        await ctx.send(f'No results for "**{query}**" in your Plex library.')
        return

    lines = [f'🔍 **Plex Results** for "**{query}**"\n']
    for i, r in enumerate(results[:10], 1):
        lines.append(f'`{i:2d}.` {r.display}')
    lines.append(f'\nUse `!plexplay <number>` to play.')

    await ctx.send('\n'.join(lines))


@bot.command(name='plexplay', aliases=['pp'], help='Play from Plex: !plexplay <# or search>')
@commands.guild_only()
async def cmd_plexplay(ctx: commands.Context, *, query: str = ''):
    if not query:
        await ctx.send('Usage: `!plexplay <number>` (after `!plex`) or `!plexplay <song name>`')
        return

    if not plex.connected:
        await ctx.send('❌ Not connected to Plex.')
        return

    player = get_player(ctx.guild.id)
    if not player.is_connected:
        if ctx.author.voice and ctx.author.voice.channel:
            await player.join(ctx.author.voice.channel)
            await ctx.send(f'🔊 Joined **{ctx.author.voice.channel.name}**')
        else:
            await ctx.send('❌ Join a voice channel first, or use `!join`.')
            return

    plex_track = None
    if query.strip().isdigit():
        idx = int(query) - 1
        if not plex._last_search:
            await ctx.send('❌ No search results. Use `!plex <query>` first.')
            return
        if idx < 0 or idx >= len(plex._last_search):
            await ctx.send(f'❌ Choose 1-{len(plex._last_search)}.')
            return

        result = plex._last_search[idx]
        if result.result_type in ('album', 'artist'):
            # Queue all tracks from album/artist
            await ctx.send(f'⏳ Loading tracks from **{result.title}**...')
            loop = asyncio.get_event_loop()
            tracks = await loop.run_in_executor(None, plex.get_tracks_from_search, idx)
            if not tracks:
                await ctx.send('❌ Could not load tracks.')
                return
            for t in tracks:
                player.add_to_plex_queue(t)
            await ctx.send(f'🎶 Added **{len(tracks)}** tracks to the Plex queue.')
            if not player.is_playing:
                track = await player.play_plex_next()
                if track:
                    await _send_now_playing(ctx, track)
            return

        await ctx.send('⏳ Loading track...')
        loop = asyncio.get_event_loop()
        plex_track = await loop.run_in_executor(None, plex.get_track_from_search, idx)
    else:
        # Direct search + play
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, plex.search, query, 1)
        if not results:
            await ctx.send(f'No results for "**{query}**" in your Plex library.')
            return
        await ctx.send('⏳ Loading track...')
        plex_track = await loop.run_in_executor(None, plex.get_track_from_search, 0)

    if not plex_track:
        await ctx.send('❌ Could not load that track.')
        return

    if player.is_playing:
        pos = player.add_to_plex_queue(plex_track)
        await ctx.send(f'📝 Added to Plex queue (#{pos}): {plex_track.display}')
    else:
        track = await player.play_plex_now(plex_track)
        if track:
            await _send_now_playing(ctx, track)


@bot.command(name='plexalbum', aliases=['pa'], help='Queue a Plex album: !plexalbum <name>')
@commands.guild_only()
async def cmd_plexalbum(ctx: commands.Context, *, name: str = ''):
    if not name:
        await ctx.send('Usage: `!plexalbum <album name>`')
        return

    if not plex.connected:
        await ctx.send('❌ Not connected to Plex.')
        return

    player = get_player(ctx.guild.id)
    if not player.is_connected:
        if ctx.author.voice and ctx.author.voice.channel:
            await player.join(ctx.author.voice.channel)
            await ctx.send(f'🔊 Joined **{ctx.author.voice.channel.name}**')
        else:
            await ctx.send('❌ Join a voice channel first, or use `!join`.')
            return

    loop = asyncio.get_event_loop()

    # --- Pick from a previous disambiguation list ---
    if name.strip().isdigit():
        idx = int(name) - 1
        if not plex._last_album_search:
            await ctx.send('❌ No album search results. Use `!plexalbum <album name>` first.')
            return
        if idx < 0 or idx >= len(plex._last_album_search):
            await ctx.send(f'❌ Choose 1-{len(plex._last_album_search)}.')
            return

        info = plex._last_album_search[idx]
        await ctx.send(f'⏳ Loading **{info["title"]}** by **{info["artist"]}**...')
        tracks = await loop.run_in_executor(None, plex.get_album_tracks_by_index, idx)
        if not tracks:
            await ctx.send('❌ Could not load tracks from that album.')
            return

        for t in tracks:
            player.add_to_plex_queue(t)
        await ctx.send(
            f'💿 Queued **{len(tracks)}** tracks from '
            f'**{info["title"]}** by **{info["artist"]}**.'
        )
        if not player.is_playing:
            track = await player.play_plex_next()
            if track:
                await _send_now_playing(ctx, track)
        return

    # --- Search for albums by name ---
    await ctx.send(f'⏳ Searching for album "**{name}**"...')
    results = await loop.run_in_executor(None, plex.search_albums, name)

    if not results:
        await ctx.send(f'❌ No album matching "**{name}**" in your Plex library.')
        return

    if len(results) == 1:
        # Only one match — queue it immediately
        tracks = await loop.run_in_executor(None, plex.get_album_tracks_by_index, 0)
        if not tracks:
            await ctx.send('❌ Could not load tracks from that album.')
            return

        for t in tracks:
            player.add_to_plex_queue(t)
        await ctx.send(
            f'💿 Queued **{len(tracks)}** tracks from '
            f'**{results[0]["title"]}** by **{results[0]["artist"]}**.'
        )
        if not player.is_playing:
            track = await player.play_plex_next()
            if track:
                await _send_now_playing(ctx, track)
        return

    # Multiple matches — show disambiguation list
    lines = [f'💿 Multiple albums match "**{name}**":\n']
    for i, r in enumerate(results, 1):
        year = f' ({r["year"]})' if r["year"] else ''
        lines.append(f'`{i:2d}.` **{r["title"]}** — {r["artist"]}{year}  [{r["track_count"]} tracks]')
    lines.append(f'\nUse `!plexalbum <number>` to pick one.')
    await ctx.send('\n'.join(lines))


@bot.command(name='plexartist', aliases=['part'], help='Queue all tracks by artist: !plexartist <name>')
@commands.guild_only()
async def cmd_plexartist(ctx: commands.Context, *, name: str = ''):
    if not name:
        await ctx.send('Usage: `!plexartist <artist name>`')
        return

    if not plex.connected:
        await ctx.send('❌ Not connected to Plex.')
        return

    player = get_player(ctx.guild.id)
    if not player.is_connected:
        if ctx.author.voice and ctx.author.voice.channel:
            await player.join(ctx.author.voice.channel)
            await ctx.send(f'🔊 Joined **{ctx.author.voice.channel.name}**')
        else:
            await ctx.send('❌ Join a voice channel first, or use `!join`.')
            return

    await ctx.send(f'⏳ Loading tracks by "**{name}**"...')

    loop = asyncio.get_event_loop()
    tracks = await loop.run_in_executor(None, plex.get_artist_tracks, name)

    if not tracks:
        await ctx.send(f'❌ No tracks by "**{name}**" in your Plex library.')
        return

    for t in tracks:
        player.add_to_plex_queue(t)

    await ctx.send(f'🎤 Queued **{len(tracks)}** tracks by **{tracks[0].artist}** (shuffled).')

    if not player.is_playing:
        track = await player.play_plex_next()
        if track:
            await _send_now_playing(ctx, track)


@bot.command(name='plexplaylists', aliases=['plists'], help='List Plex playlists')
@commands.guild_only()
async def cmd_plexplaylists(ctx: commands.Context):
    if not plex.connected:
        await ctx.send('❌ Not connected to Plex.')
        return

    loop = asyncio.get_event_loop()
    playlists = await loop.run_in_executor(None, plex.list_playlists)

    if not playlists:
        await ctx.send('No audio playlists found on your Plex server.')
        return

    lines = [f'🎶 **Plex Playlists** ({len(playlists)})\n']
    for i, pl in enumerate(playlists, 1):
        lines.append(f'`{i:2d}.` **{pl["title"]}** — {pl["count"]} tracks')
    lines.append(f'\nUse `!plexplaylist <name>` to load one.')

    await ctx.send('\n'.join(lines))


@bot.command(name='plexplaylist', aliases=['ppl'], help='Shuffle a Plex playlist: !plexplaylist <name>')
@commands.guild_only()
async def cmd_plexplaylist(ctx: commands.Context, *, name: str = ''):
    if not name:
        await ctx.send('Usage: `!plexplaylist <playlist name>`. Use `!plexplaylists` to see them.')
        return

    if not plex.connected:
        await ctx.send('❌ Not connected to Plex.')
        return

    player = get_player(ctx.guild.id)
    if not player.is_connected:
        if ctx.author.voice and ctx.author.voice.channel:
            await player.join(ctx.author.voice.channel)
            await ctx.send(f'🔊 Joined **{ctx.author.voice.channel.name}**')
        else:
            await ctx.send('❌ Join a voice channel first, or use `!join`.')
            return

    await ctx.send(f'⏳ Loading playlist "**{name}**" (shuffled)...')

    loop = asyncio.get_event_loop()
    tracks = await loop.run_in_executor(None, plex.get_playlist_tracks, name)

    if not tracks:
        await ctx.send(f'❌ No playlist matching "**{name}**". Use `!plexplaylists` to see your playlists.')
        return

    for t in tracks:
        player.add_to_plex_queue(t)

    await ctx.send(f'🔀 Queued **{len(tracks)}** tracks from playlist (shuffled).')

    if not player.is_playing:
        track = await player.play_plex_next()
        if track:
            await _send_now_playing(ctx, track)


# ---------------------------------------------------------------------- helpers

async def _send_now_playing(ctx: commands.Context, track):
    """Send a rich embed for the currently playing track."""
    player = get_player(ctx.guild.id)
    is_yt = player.mode == 'youtube'
    is_plex = player.mode == 'plex'

    # Color per source
    if is_yt:
        color = 0xFF0000
    elif is_plex:
        color = 0xE5A00D  # Plex gold
    else:
        color = 0x224099  # Pandora blue

    embed = discord.Embed(
        title='🎵 Now Playing',
        description=track.display,
        color=color,
    )

    if is_yt:
        if hasattr(track, 'uploader') and track.uploader:
            embed.add_field(name='Channel', value=track.uploader, inline=True)
        if player.queue_length > 0:
            embed.add_field(name='Queue', value=f'{player.queue_length} tracks', inline=True)
    elif is_plex:
        embed.add_field(name='Album', value=getattr(track, 'album', 'Unknown'), inline=True)
        embed.add_field(name='Artist', value=getattr(track, 'artist', 'Unknown'), inline=True)
        if player.plex_queue_length > 0:
            embed.add_field(name='Queue', value=f'{player.plex_queue_length} tracks', inline=True)
    else:
        embed.add_field(name='Album', value=getattr(track, 'album', 'Unknown'), inline=True)
        embed.add_field(name='Station', value=getattr(track, 'station_name', ''), inline=True)

    thumb = getattr(track, 'thumbnail', '') or getattr(track, 'art_url', '')
    if thumb:
        embed.set_thumbnail(url=thumb)

    if is_yt:
        source_icon = '▶ YouTube'
    elif is_plex:
        source_icon = '🟠 Plex'
    else:
        source_icon = '📻 Pandora'
    embed.set_footer(text=f'{source_icon} • Volume: {int(player.volume * 100)}%')

    await ctx.send(embed=embed)


# ---------------------------------------------------------------------- main

if __name__ == '__main__':
    if not config.DISCORD_BOT_TOKEN:
        log.error('Set DISCORD_MUSIC_BOT_TOKEN environment variable.')
        sys.exit(1)

    if not config.PANDORA_EMAIL or not config.PANDORA_PASSWORD:
        log.warning('PANDORA_EMAIL / PANDORA_PASSWORD not set. '
                     'Pandora features will fail until configured.')

    MAX_RESTARTS = 50
    RESTART_DELAY = 5       # seconds between restart attempts
    UPTIME_RESET  = 60      # reset failure counter after this many seconds of uptime

    failures = 0

    while failures < MAX_RESTARTS:
        start_time = time.time()
        log.info('Starting Discord Music Bot (Pandora + YouTube)... '
                 '(attempt %d)', failures + 1)
        try:
            bot.run(config.DISCORD_BOT_TOKEN)
            # bot.run() returns cleanly on logout / KeyboardInterrupt
            log.info('Bot shut down cleanly.')
            break
        except KeyboardInterrupt:
            log.info('Interrupted by user — exiting.')
            break
        except Exception as exc:
            uptime = time.time() - start_time
            failures += 1
            log.error('Bot crashed after %.1fs: %s', uptime, exc,
                       exc_info=True)

            # Reset the failure counter if the bot ran for a while
            if uptime >= UPTIME_RESET:
                log.info('Bot had been up for %.0fs — resetting failure counter.', uptime)
                failures = 1

            if failures >= MAX_RESTARTS:
                log.critical('Reached %d consecutive failures — giving up.', MAX_RESTARTS)
                sys.exit(1)

            log.info('Restarting in %d seconds... (%d/%d failures)',
                     RESTART_DELAY, failures, MAX_RESTARTS)
            time.sleep(RESTART_DELAY)

            # Re-create the bot and player state for a clean restart
            bot = commands.Bot(command_prefix=config.COMMAND_PREFIX, intents=intents, help_command=None)
            players.clear()

            # Re-register all event handlers and commands
            @bot.event
            async def on_ready():
                log.info('Bot ready: %s (ID: %s)', bot.user.name, bot.user.id)
                log.info('In %d guild(s)', len(bot.guilds))
                try:
                    pandora.login()
                    pandora.get_stations()
                    log.info('Pandora ready — %d stations loaded.',
                             len(pandora._stations))
                except Exception as exc:
                    log.error('Pandora login failed: %s', exc)
                try:
                    if config.PLEX_URL and config.PLEX_TOKEN:
                        plex.connect()
                        log.info('Plex ready.')
                except Exception as exc:
                    log.error('Plex connection failed: %s', exc)

            @bot.event
            async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
                if before.channel and before.channel != after.channel:
                    guild_id = before.channel.guild.id
                    if guild_id in players:
                        player = players[guild_id]
                        if player.is_connected and player.voice_client and player.voice_client.channel.id == before.channel.id:
                            non_bots = [m for m in before.channel.members if not m.bot]
                            if len(non_bots) == 0:
                                log.info('All users left %s. Pausing and leaving.', before.channel.name)
                                if player.is_playing:
                                    player.pause()
                                await player.leave(save_queue=True)
            
            @bot.before_invoke
            async def set_text_channel_before_invoke(ctx: commands.Context):
                if ctx.guild:
                    player = get_player(ctx.guild.id)
                    player.text_channel = ctx.channel

            # Re-add all commands from this module
            for _cmd in list(bot.commands):
                bot.remove_command(_cmd.name)

            bot.add_command(cmd_help)
            bot.add_command(cmd_join)
            bot.add_command(cmd_leave)
            bot.add_command(cmd_stations)
            bot.add_command(cmd_station)
            bot.add_command(cmd_playing)
            bot.add_command(cmd_skip)
            bot.add_command(cmd_thumbsup)
            bot.add_command(cmd_thumbsdown)
            bot.add_command(cmd_volume)
            bot.add_command(cmd_stop)
            bot.add_command(cmd_search)
            bot.add_command(cmd_addstation)
            bot.add_command(cmd_deletestation)
            bot.add_command(cmd_yt)
            bot.add_command(cmd_play)
            bot.add_command(cmd_playlist)
            bot.add_command(cmd_queue)
            bot.add_command(cmd_clear)
            bot.add_command(cmd_savequeue)
            bot.add_command(cmd_loadqueue)
            bot.add_command(cmd_savedplaylists)
            bot.add_command(cmd_restartqueue)
            bot.add_command(cmd_plex)
            bot.add_command(cmd_plexplay)
            bot.add_command(cmd_plexalbum)
            bot.add_command(cmd_plexartist)
            bot.add_command(cmd_plexplaylists)
            bot.add_command(cmd_plexplaylist)
            bot.add_command(cmd_pause)
            bot.add_command(cmd_resume)
            bot.add_command(cmd_move)
            bot.add_command(cmd_remove)
            bot.add_command(cmd_shuffle)
