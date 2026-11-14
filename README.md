# Remote Media, Cloud & VPN Telegram Controller Bot

High-performance Telegram controller bot for managing media downloads (JioHotstar, Amazon Prime, and Vinetrimmer), batch multi-audio MKV muxing, ExpressVPN, and direct Google Drive cloud uploads via Rclone & Robocopy.

---

## 🚀 Features

- **Interactive Telegram UI:**
  - Inline keyboard buttons for **Video Quality** (`2160p`, `1080p`, `720p`, `480p`, `Best`).
  - Inline multi-toggle buttons for **Audio Languages** (Hindi, Tamil, Telugu, English, Malayalam, Kannada, Bengali, Marathi, All, Original).
  - Video Codec selection (`H.264` / `H.265`).
  - One-tap task cancellation directly on live progress messages.
- **Direct Link Recognition:** Paste any Hotstar or Amazon Prime link in chat to automatically pop up the interactive setup menu.
- **Live Progress Tracking:** Visual real-time progress bar with percentage, download speed, and ETA.
- **Auto-Pipeline:** Single-command or one-tap workflow: **Download ➔ Multi-Audio Batch Mux ➔ Google Drive Upload**.
- **Cloud Upload Support:**
  - Rclone (Direct Google Drive Cloud API with chunking & multi-threading).
  - Robocopy (Local mounted desktop Google Drive mirror).
- **ExpressVPN Controls:** Check public IP, ISP details, status, connect, and disconnect directly from Telegram.

---

## 📋 Setup & Installation

### 1. Clone the Repository
```bash
git clone https://github.com/PARASGOR905/BOT.git
cd BOT
```

### 2. Environment Configuration
Copy `.env.example` to `.env` and fill in your credentials:
```bash
cp .env.example .env
```
Edit `.env`:
```env
BOT_TOKEN=your_telegram_bot_token_here
ADMIN_ID=your_telegram_user_id_here
```

### 3. Vinetrimmer Configuration
Copy `vinetrimmer/vinetrimmer.yml.example` to `vinetrimmer/vinetrimmer.yml`:
```bash
cp vinetrimmer/vinetrimmer.yml.example vinetrimmer/vinetrimmer.yml
```

### 4. Install Dependencies
```bash
poetry install
```

### 5. Run the Bot
```bash
python bot.py
```

---

## 🤖 Bot Commands

| Command | Description |
|---|---|
| `/start`, `/help` | Show bot help and overview |
| `/jhs <url>` | Download JioHotstar title (opens interactive buttons) |
| `/prime <url>` | Download Amazon Prime title (opens interactive buttons) |
| `/quality` | Set default download quality with buttons |
| `/audio` | Toggle default audio languages with buttons |
| `/settings` | Open interactive settings & preferences panel |
| `/auto <args>` | Pipeline: Download ➔ Mux ➔ Google Drive Upload |
| `/mux` | Batch mux multi-audio tracks into `.MULTi.mkv` |
| `/upload [target]` | Upload file/folder from downloads to Google Drive |
| `/status` | Check running task status and progress |
| `/cancel` | Stop or terminate running task |
| `/list` | List items in the downloads folder |
| `/disk` | Check local and cloud storage disk space |
| `/clean` | Clean temporary cache directory |
| `/vpn status` | Check ExpressVPN status & Public IP |
| `/vpn ip` | Display Public IP, Location, ISP info |
| `/vpn connect [loc]` | Connect ExpressVPN to target location |
| `/vpn disconnect` | Disconnect ExpressVPN |
| `/engine [rclone/robocopy]` | Switch cloud upload backend |
| `/autoupload [on/off]` | Toggle auto-upload after download completion |

---

## 🔒 Security Note
Never commit your real `.env`, `bot_settings.json`, CDM devices, or `vinetrimmer.yml` to public version control.
All sensitive credentials are protected via `.gitignore`.
