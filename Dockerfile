# ─────────────────────────────────────────────────────────────────────────────
# Ultimate Reels AI Hunter — Production Dockerfile
# Ubuntu 22.04 | Python 3.10 | Playwright Chromium | Xpra HTML5 | ffmpeg
#
# Xpra replaces the old 3-process chain:
#   ❌  Xvfb + x11vnc + websockify + noVNC  (fragile, 4 moving parts)
#   ✅  Xpra start-desktop               (one process, built-in HTML5 client)
# ─────────────────────────────────────────────────────────────────────────────

FROM ubuntu:22.04

LABEL maintainer="reels-hunter"
LABEL description="Reels AI Hunter with Xpra HTML5 live desktop (no VNC client needed)"

# ── Environment ───────────────────────────────────────────────────────────────
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=America/New_York
ENV DISPLAY=:99
ENV SCREEN_WIDTH=1280
ENV SCREEN_HEIGHT=1024
ENV SCREEN_DEPTH=24
ENV WEB_PORT=8080
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PIP_NO_CACHE_DIR=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# ── System packages ───────────────────────────────────────────────────────────
# Removed: x11vnc, novnc, websockify  (all replaced by Xpra in one shot)
# Kept:    fluxbox, xterm, x11-utils
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.10 python3-pip python3.10-venv python3.10-dev \
    fluxbox xterm x11-utils x11-xserver-utils dbus-x11 \
    ffmpeg \
    fonts-liberation fonts-liberation2 fonts-noto fonts-noto-core \
    fonts-noto-color-emoji fonts-noto-cjk fonts-dejavu-core \
    fonts-dejavu-extra fonts-freefont-ttf fonts-open-sans fonts-ubuntu \
    fontconfig \
    libnss3 libnss3-dev libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libdbus-1-3 libxcomposite1 libxdamage1 libxext6 \
    libxfixes3 libxrandr2 libgbm1 libxkbcommon0 libpango-1.0-0 \
    libpangocairo-1.0-0 libcairo2 libasound2 libgtk-3-0 libglib2.0-0 \
    libx11-6 libx11-xcb1 libxcb1 libxcb-dri3-0 libxss1 libxtst6 \
    libgdk-pixbuf2.0-0 \
    supervisor procps wget curl git ca-certificates locales tzdata gnupg \
    && locale-gen en_US.UTF-8 \
    && update-ca-certificates \
    && fc-cache -fv \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# ── Xpra from official repo ────────────────────────────────────────────────────
# Xpra bundles: virtual display + Fluxbox WM + HTML5 browser client.
# One install, one process, zero extra config — replaces 4 separate tools.
RUN wget -qO /usr/share/keyrings/xpra.gpg \
        https://xpra.org/repos/jammy/xpra.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/xpra.gpg] \
        https://xpra.org/repos/jammy jammy main" \
        > /etc/apt/sources.list.d/xpra.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        xpra \
        xpra-html5 \
    && rm -rf /var/lib/apt/lists/*

# ── Python symlink ─────────────────────────────────────────────────────────────
RUN ln -sf /usr/bin/python3.10 /usr/bin/python3 \
    && ln -sf /usr/bin/python3.10 /usr/bin/python \
    && python3 -m pip install --upgrade pip setuptools wheel

# ── Working directory ──────────────────────────────────────────────────────────
WORKDIR /app

# ── Python dependencies (layer cache) ─────────────────────────────────────────
COPY requirements.txt /app/requirements.txt
RUN pip3 install -r requirements.txt

# ── Chromium with proprietary codecs (H.264 / AAC for Instagram reels) ───────
# Playwright's bundled Chromium ships without proprietary codecs and cannot
# decode Instagram's H.264/AAC video streams ("Sorry, trouble playing video").
# Use system chromium-browser + chromium-codecs-ffmpeg-extra instead.
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium-browser \
        chromium-codecs-ffmpeg-extra \
    && rm -rf /var/lib/apt/lists/*

# Install only Playwright's system deps (skip downloading its codec-stripped binary)
RUN playwright install-deps chromium

ENV PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium-browser

# ── Application source ─────────────────────────────────────────────────────────
COPY agent.py       /app/agent.py
COPY entrypoint.sh  /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# ── Runtime directories ────────────────────────────────────────────────────────
RUN mkdir -p /var/log/supervisor /var/run/supervisor \
             /tmp/reels_downloads /tmp/reels_screenshots \
             /run/xpra /etc/xpra

COPY supervisord.conf /etc/supervisor/conf.d/reels-hunter.conf

# ── Ports ──────────────────────────────────────────────────────────────────────
# Single port now — Xpra serves the HTML5 UI and screen stream over one socket
EXPOSE 8080

# ── Health check ───────────────────────────────────────────────────────────────
HEALTHCHECK --interval=30s --timeout=10s --start-period=25s --retries=3 \
    CMD curl -f http://localhost:8080/ || exit 1

# ── Entrypoint ─────────────────────────────────────────────────────────────────
ENTRYPOINT ["/app/entrypoint.sh"]
