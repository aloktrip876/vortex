FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install ffmpeg, Node.js (for PO Token provider), and git
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        nodejs \
        npm \
        git \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Clone and build the bgutil PO Token provider (script mode)
RUN git clone --single-branch --branch 1.3.1 \
    https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git \
    /bgutil && \
    cd /bgutil/server && \
    npm ci && \
    npx tsc

# Install the yt-dlp plugin for PO Token support
RUN pip install --no-cache-dir bgutil-ytdlp-pot-provider

COPY . /app

ENV PORT=5000
EXPOSE 5000

CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:5000", "--workers", "2", "--threads", "4", "--timeout", "120"]
