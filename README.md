# 🎬 Ultimate Reels AI Hunter

Production-grade zero-cost Instagram Reels harvesting platform running on GitHub Actions with live VNC debugging.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     GitHub Actions Runner                        │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                      agent.py                            │   │
│  │                                                          │   │
│  │  InstagramAgent                                          │   │
│  │    │                                                     │   │
│  │    ├─► DatabaseManager  ──► history.db (SQLite/WAL)      │   │
│  │    │      dedup + audit trail                            │   │
│  │    │                                                     │   │
│  │    ├─► VisionEvaluator                                   │   │
│  │    │      Stage 1: Pillow border pixel analysis          │   │
│  │    │      Stage 2: Gemini 1.5 Flash multimodal           │   │
│  │    │                                                     │   │
│  │    ├─► NotificationService ──► Telegram Bot API          │   │
│  │    │      MP4 delivery + crash alerts                    │   │
│  │    │                                                     │   │
│  │    └─► Playwright Chromium (stealth)                     │   │
│  │           │                                              │   │
│  │           └─► yt-dlp ──► CDN MP4 (watermark-free)        │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

---

## Files

| File | Purpose |
|------|---------|
| `agent.py` | Main Python agent (all classes) |
| `Dockerfile` | Ubuntu 22.04 container with noVNC desktop |
| `entrypoint.sh` | Container startup: Xvfb → Fluxbox → x11vnc → websockify → agent |
| `supervisord.conf` | Process supervision inside Docker |
| `requirements.txt` | Pinned Python dependencies |
| `.github/workflows/reels_agent.yml` | Cron-scheduled GitHub Actions workflow |

---

## GitHub Actions Setup

### 1. Fork / push this repo to GitHub

### 2. Configure Repository Secrets

Go to **Settings → Secrets and variables → Actions → New repository secret**:

| Secret Name | Description |
|---|---|
| `INSTAGRAM_SESSION_COOKIES` | Your Instagram session (see below) |
| `GEMINI_API_KEY` | Google AI Studio API key |
| `TELEGRAM_BOT_TOKEN` | Token from [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID` | Your Telegram chat/channel ID |

### 3. Get Instagram Session Cookies

**Option A — Semicolon-separated (simplest):**
1. Log into Instagram in Chrome
2. Open DevTools → Application → Cookies → `https://www.instagram.com`
3. Copy `sessionid`, `csrftoken`, `ds_user_id` values
4. Set secret as: `sessionid=AAA; csrftoken=BBB; ds_user_id=CCC`

**Option B — JSON export (full fidelity):**
1. Install the [EditThisCookie](https://chrome.google.com/webstore/detail/editthiscookie) extension
2. Visit instagram.com while logged in
3. Click the extension → Export → copy the JSON array
4. Paste the entire JSON array as the secret value

### 4. Enable the workflow

The workflow runs automatically every 4 hours once enabled. You can also trigger it manually from the **Actions** tab with custom parameters.

---

## Local Docker (with live VNC)

```bash
# Build
docker build -t reels-hunter .

# Run with live VNC on port 8080
docker run -it --rm \
  -p 8080:8080 \
  -e INSTAGRAM_SESSION_COOKIES="sessionid=XXX; csrftoken=YYY" \
  -e GEMINI_API_KEY="your-key" \
  -e TELEGRAM_BOT_TOKEN="your-token" \
  -e TELEGRAM_CHAT_ID="your-chat-id" \
  -v "$(pwd)/history.db:/app/history.db" \
  reels-hunter

# Open browser: http://localhost:8080/vnc.html
# You'll see the live Chromium window navigating Instagram in real time
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `MIN_VIEWS` | `50000` | Minimum view count to qualify |
| `MIN_LIKES` | `0` | Minimum like count (0 = disabled) |
| `TARGET_REELS_SCAN` | `35` | Reels to scroll through per run |
| `MAX_QUALIFIED_SEND` | `5` | Max reels to download+send per run |
| `MAX_RUNTIME_SECONDS` | `480` | Hard agent runtime ceiling (8 min) |
| `SHUTDOWN_BUFFER_SECONDS` | `45` | Pre-deadline graceful shutdown buffer |
| `PLAYWRIGHT_HEADLESS` | `false` | `true` for CI, `false` for VNC viewing |
| `GEMINI_MODEL` | `gemini-1.5-flash` | Gemini model name |
| `GEMINI_MAX_DIM` | `720` | Max pixel dimension before Gemini resize |
| `ENABLE_GEMINI_FALLBACK` | `true` | **NEW**: Use views/likes when Gemini quota exhausted |
| `FALLBACK_MIN_VIEWS` | `500000` | **NEW**: Min views needed for fallback approval |
| `FALLBACK_MIN_LIKES` | `250000` | **NEW**: Min likes needed for fallback approval |
| `TELEGRAM_MAX_VIDEO_MB` | `49` | Max video size for Telegram upload |
| `DB_PATH` | `history.db` | SQLite database file path |
| `DOWNLOAD_DIR` | `/tmp/reels_downloads` | Temp video download directory |
| `SCREENSHOT_DIR` | `/tmp/reels_screenshots` | Debug screenshot directory |

### 🆕 Gemini Fallback Mode

When `ENABLE_GEMINI_FALLBACK=true` (default), the system uses a **two-tier quality check**:

1. **Primary (Gemini Vision)**: AI checks for watermarks, quality, and content type
2. **Fallback (Engagement Metrics)**: If Gemini API hits quota/rate limits, high-engagement reels (views ≥ 500K AND likes ≥ 250K) still pass

This ensures you never miss viral content even during API quota exhaustion. Set `ENABLE_GEMINI_FALLBACK=false` to strictly enforce AI vision checks only.

---

## Processing Pipeline

```
For each discovered Reel URL:
  │
  ├─[1] Deduplication check (SQLite index)
  │       └── Already seen? → SKIP instantly (0 API cost)
  │
  ├─[2] Navigate to Reel + extract DOM metrics
  │       └── views < MIN_VIEWS? → SKIP
  │
  ├─[3] Screenshot the <video> element
  │
  ├─[4] Stage 1 vision: Pillow border pixel analysis
  │       └── Black bars detected? → SKIP (0 API cost)
  │
  ├─[5] Stage 2 vision: Gemini 1.5 Flash
  │       ├── PASSED → Continue
  │       ├── FAILED → SKIP
  │       └── Quota exhausted + ENABLE_GEMINI_FALLBACK=true?
  │             └── views ≥ 500K AND likes ≥ 250K? → PASS (fallback)
  │
  ├─[6] yt-dlp download from Instagram CDN
  │       Format: bestvideo[ext=mp4]+bestaudio[ext=m4a]
  │       (watermark-free raw stream)
  │
  └─[7] Send MP4 + metrics caption → Telegram
          Delete local file after successful send
```

---

## Zero-Cost Enforcement

- **Runtime cap:** Agent self-terminates after 8 minutes (configurable via `MAX_RUNTIME_SECONDS`)
- **Job timeout:** GitHub Actions job hard-kills at 12 minutes
- **Cron cadence:** Every 4 hours × 12 min max = ≤ 72 min/day < GitHub's 2,000 min/month free tier
- **history.db caching:** `actions/cache@v4` persists dedup state, preventing redundant Gemini API calls on already-seen reels
- **Gemini cost control:** Stage 1 Pillow check eliminates low-quality frames before they reach Gemini

---

## Crash Recovery

On any unhandled exception the agent:
1. Takes a full-viewport screenshot of the last browser state
2. Sends the screenshot + full Python traceback to Telegram
3. Persists the DB before exit
4. Saves crash screenshots as a GitHub Actions artifact (7-day retention)
