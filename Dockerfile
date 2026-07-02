FROM python:3.11-slim

# Install system dependencies
# ffmpeg is required to encode audio, and libopus is required by discord.py
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg libopus0 && \
    rm -rf /var/lib/apt/lists/*

# Set the application directory inside the container
WORKDIR /app

# Install pip requirements first (this allows Docker to cache the installation step and makes rebuilding instantly fast)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the python source code over from the src directory
COPY src/ .

# Start the bot
CMD ["python", "bot.py"]
