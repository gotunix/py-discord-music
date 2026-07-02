# Discord Music Bot

A feature-rich Discord bot that seamlessly streams audio from **YouTube**, **Plex**, and **Pandora**. Complete with fully persistent playlist queuing, intelligent context-aware help categorization, and robust local caching!

---

## 🚀 Running via Docker (Recommended)

This project is completely containerized with `docker-compose` to eliminate all dependency headaches (like `ffmpeg` and `libopus`). This ensures the bot instantly works on any Linux, Mac, or Windows machine perfectly natively.

### 1. Configuration
To start, you need to provide your authentication tokens cleanly using environment variables.
```bash
cp .env.example .env
```
Open the `.env` file with your favorite editor and paste in your `DISCORD_MUSIC_BOT_TOKEN`, alongside your Plex or Pandora details.

*(Note: Ensure an empty `playlists.json` file exists before your very first boot so Docker doesn't map it as a folder! If you just downloaded the repo, simply run `echo "{}" > playlists.json`)*

### 2. Launching the Bot
Because the system is fully abstracted via `docker-compose`, downloading all dependencies and starting the bot takes just one command:
```bash
docker compose up --build -d
```
The `-d` flag runs the bot in "detached" mode so you can comfortably close your terminal. The `restart: unless-stopped` flag guarantees your bot will instantly and automatically reboot if your host machine ever restarts or crashes!

### 3. Upgrading & Modifying Code
If you happen to edit `bot.py` or any player files and want to instantly restart the bot with the new code, simply rebuild the Docker image:
```bash
docker compose build
docker compose up -d
```
Because both the `playlists.json` file and the `/cache` directories are tightly **bind-mounted** to the host, upgrading your Python code using Docker will **never** wipe out your saved playlists or accidentally delete your gigabytes of downloaded music offline cache!

---

## 🎵 Features
- **Graphic Help Menu**: Type `!help` natively in Discord to view beautifully categorized control lists. (Use `!help plex` specifically to filter your view).
- **Persistent Queues**: Save your current 500-track queue permanently using `!savequeue <name>` and instantly recall it weeks later with `!loadqueue <name>`.
- **YouTube Parsing**: Automatically resolve, cache, and play YouTube links directly into the discord voice channel.
- **Plex Streaming**: Stream albums, artists, or playlists securely from your local home Plex ecosystem.
- **Pandora Radio**: Authenticate and perfectly broadcast personalized automated Pandora algorithmic stations.
