#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ខ្មែរClip (KhemClip) — Telegram bot
1) Download video/audio by link (yt-dlp)
2) Khmer TTS male / female (edge-tts)
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import telebot
from telebot import types
from flask import Flask

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("khemclip")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or 0)
DATA_DIR = os.environ.get("DATA_DIR", "/var/data")
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    DATA_DIR = "."
    os.makedirs(DATA_DIR, exist_ok=True)

BOT_NAME = "Techsor"
BOT_NAME_EN = "Techsor"

# Khmer neural voices (edge-tts)
VOICE_MALE = "km-KH-PisethNeural"
VOICE_FEMALE = "km-KH-SreymomNeural"

URL_RE = re.compile(
    r"https?://[^\s<>\"']+",
    re.I,
)

if not BOT_TOKEN:
    log.error("BOT_TOKEN missing")
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML") if BOT_TOKEN else None

# uid -> awaiting tts voice
waiting: dict = {}
# rate limit simple
_last: dict = {}


def rate_ok(uid: int, sec: float = 3.0) -> bool:
    now = time.time()
    if now - _last.get(uid, 0) < sec:
        return False
    _last[uid] = now
    return True


def main_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.row("📥 ទាញវីដេអូ", "🔊 TTS ខ្មែរ")
    kb.row("👨 សំឡេងប្រុស", "👩 សំឡេងស្រី")
    kb.row("ℹ️ ជួយ")
    return kb


def tts_kb():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("👨 ប្រុស (Piseth)", callback_data="tts:male"),
        types.InlineKeyboardButton("👩 ស្រី (Sreymom)", callback_data="tts:female"),
    )
    return kb


@bot.message_handler(commands=["start", "help"])
def cmd_start(message):
    waiting.pop(message.from_user.id, None)
    first = (message.from_user.first_name or "មិត្ត").strip()
    caption = (
        "សួស្តី <b>%s</b>!\n\n"
        "🎬 <b>%s</b> · <i>%s</i>\n\n"
        "📥 <b>ទាញវីដេអូ</b> — TikTok · YouTube · Facebook…\n"
        "🔊 <b>TTS ខ្មែរ</b> — ប្រុស (Piseth) · ស្រី (Sreymom)\n\n"
        "ផ្ញើ <b>link</b> ឬ ចុចម៉ឺនុយខាងក្រោម 👇"
    ) % (first, BOT_NAME, BOT_NAME_EN)
    banner = (os.environ.get("WELCOME_BANNER") or "").strip()
    try:
        if banner:
            bot.send_photo(message.chat.id, banner, caption=caption, reply_markup=main_kb())
        else:
            bot.send_message(message.chat.id, caption, reply_markup=main_kb())
    except Exception:
        bot.send_message(message.chat.id, caption, reply_markup=main_kb())


@bot.message_handler(func=lambda m: m.text in ("ℹ️ ជួយ", "ជួយ"))
def cmd_help(message):
    cmd_start(message)


@bot.message_handler(func=lambda m: m.text == "📥 ទាញវីដេអូ")
def ask_link(message):
    waiting[message.from_user.id] = {"mode": "dl"}
    bot.send_message(
        message.chat.id,
        "🔗 ផ្ញើ <b>link</b> វីដេអូមក (TikTok / YouTube / Facebook / Instagram…)",
        reply_markup=main_kb(),
    )


@bot.message_handler(func=lambda m: m.text == "🔊 TTS ខ្មែរ")
def ask_tts(message):
    waiting[message.from_user.id] = {"mode": "tts", "voice": "female"}
    bot.send_message(
        message.chat.id,
        "🔊 ជ្រើសសំឡេង រួចផ្ញើអត្ថបទខ្មែរ (≤500 តួ):",
        reply_markup=tts_kb(),
    )


@bot.message_handler(func=lambda m: m.text == "👨 សំឡេងប្រុស")
def tts_male_mode(message):
    waiting[message.from_user.id] = {"mode": "tts", "voice": "male"}
    bot.send_message(message.chat.id, "👨 សំឡេងប្រុស — ផ្ញើអត្ថបទខ្មែរមក:", reply_markup=main_kb())


@bot.message_handler(func=lambda m: m.text == "👩 សំឡេងស្រី")
def tts_female_mode(message):
    waiting[message.from_user.id] = {"mode": "tts", "voice": "female"}
    bot.send_message(message.chat.id, "👩 សំឡេងស្រី — ផ្ញើអត្ថបទខ្មែរមក:", reply_markup=main_kb())


@bot.callback_query_handler(func=lambda c: (c.data or "").startswith("tts:"))
def cb_tts_voice(call):
    uid = call.from_user.id
    voice = "male" if call.data.endswith("male") else "female"
    waiting[uid] = {"mode": "tts", "voice": voice}
    try:
        bot.answer_callback_query(call.id, "បានជ្រើស " + ("ប្រុស" if voice == "male" else "ស្រី"))
    except Exception:
        pass
    bot.send_message(
        call.message.chat.id,
        ("👨" if voice == "male" else "👩") + " ផ្ញើអត្ថបទខ្មែរមកឥឡូវ:",
        reply_markup=main_kb(),
    )


def _is_tiktok(url: str) -> bool:
    u = (url or "").lower()
    return any(x in u for x in ("tiktok.com", "vm.tiktok.", "vt.tiktok.", "tiktok."))


def _find_media(out_dir: str):
    try:
        files = sorted(Path(out_dir).iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        return None
    for f in files:
        if f.is_file() and f.suffix.lower() in (".mp4", ".mkv", ".webm", ".mp3", ".m4a", ".opus", ".mov"):
            if f.stat().st_size > 1000:
                return str(f)
    return None


def _download_tikwm(url: str, out_dir: str) -> tuple:
    """Fallback TikTok via tikwm public API (no watermark when available)."""
    import urllib.request
    import json as _json
    api = "https://www.tikwm.com/api/?url=" + urllib.parse.quote(url, safe="")
    req = urllib.request.Request(
        api,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "Referer": "https://www.tikwm.com/",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = _json.loads(resp.read().decode("utf-8", errors="ignore"))
    except Exception as e:
        return None, "tikwm: %s" % e
    if not isinstance(data, dict) or data.get("code") not in (0, "0", None):
        # some responses use code 0 for ok
        if not (isinstance(data, dict) and data.get("data")):
            return None, "tikwm: %s" % (data.get("msg") if isinstance(data, dict) else "bad response")
    d = data.get("data") or {}
    play = d.get("play") or d.get("wmplay") or d.get("hdplay") or ""
    if not play:
        # images slideshow?
        imgs = d.get("images") or []
        if imgs:
            return None, "tikwm: នេះជា slideshow រូប — មិនទាន់គាំទ្រ"
        return None, "tikwm: រក link video មិនឃើញ"
    # download file
    out = os.path.join(out_dir, "tikwm.mp4")
    try:
        req2 = urllib.request.Request(
            play,
            headers={
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
                "Referer": "https://www.tiktok.com/",
            },
        )
        with urllib.request.urlopen(req2, timeout=120) as r, open(out, "wb") as f:
            while True:
                chunk = r.read(1024 * 256)
                if not chunk:
                    break
                f.write(chunk)
        if os.path.getsize(out) < 1000:
            return None, "tikwm: file ទទេ"
        return out, ""
    except Exception as e:
        return None, "tikwm download: %s" % e


# ===== KHEMCLIP YT FIX v1 =====
# YouTube answers "Sign in to confirm you're not a bot" to datacenter IPs (Render). This block adds:
#   - cookies.txt support (admin sends the file to the bot, or env YTDLP_COOKIES_B64 / YTDLP_COOKIES)
#   - optional proxy (env YTDLP_PROXY, e.g. a residential proxy)
#   - a second YouTube attempt with another client, TikTok via tikwm first, friendly error messages
#   - one single download_media() (the file used to define it twice; the first copy was dead code)
import base64 as _b64
import html as _html


def _esc(s):
    return _html.escape(str(s or ""), quote=False)


COOKIES_FILE = os.path.join(DATA_DIR, "cookies.txt")


def _cookies_path():
    """cookies.txt for yt-dlp: sent by the admin (stored in DATA_DIR) or taken from the environment."""
    try:
        if os.path.isfile(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 50:
            return COOKIES_FILE
        raw = os.environ.get("YTDLP_COOKIES_B64", "").strip()
        txt = _b64.b64decode(raw).decode("utf-8", "ignore") if raw else os.environ.get("YTDLP_COOKIES", "")
        if txt and "\n" not in txt and "\\n" in txt:      # env var pasted with literal \n
            txt = txt.replace("\\n", "\n")
        if txt and txt.strip():
            p = os.path.join(DATA_DIR, "cookies_env.txt")
            with open(p, "w", encoding="utf-8") as f:
                f.write(txt if txt.endswith("\n") else txt + "\n")
            os.chmod(p, 0o600)
            return p
    except Exception as e:
        log.warning("cookies: %s", e)
    return None


def _is_youtube(url: str) -> bool:
    u = (url or "").lower()
    return any(x in u for x in ("youtube.com", "youtu.be", "youtube-nocookie.com"))


def _ytdlp_base(url: str, use_cookies: bool = True) -> list:
    args = [
        "yt-dlp", "--no-playlist", "--no-warnings", "--newline",
        "--max-filesize", "49M", "--retries", "3", "--fragment-retries", "3", "--socket-timeout", "30",
    ]
    proxy = os.environ.get("YTDLP_PROXY", "").strip()
    if proxy:
        args += ["--proxy", proxy]
    ck = _cookies_path()
    if use_cookies and ck and (_is_youtube(url) or os.environ.get("YTDLP_COOKIES_ALL") == "1"):
        args += ["--cookies", ck]
    return args


def _friendly_dl_error(stderr: str, url: str) -> str:
    s = (stderr or "").strip()
    low = s.lower()
    if "not a bot" in low or "sign in to confirm" in low:
        if _cookies_path():
            return ("⚠️ YouTube នៅតែបដិសេធ ទោះមាន cookies (អាចផុតកំណត់)។ "
                    "Admin សូម export cookies ថ្មី ឬប្រើ proxy (YTDLP_PROXY)។")
        return ("⚠️ YouTube មិនអនុញ្ញាតឲ្យ server នេះទាញវីដេអូ (bot check)។ "
                "សូមសាកល្បង link ពី TikTok / Facebook ឬប្រាប់ Admin ឲ្យដាក់ cookies។\n"
                "(Admin: ផ្ញើ file cookies.txt មក bot ឬកំណត់ env YTDLP_COOKIES_B64 / YTDLP_PROXY)")
    if "larger than max-filesize" in low:
        return "❌ ឯកសារធំពេកសម្រាប់ Telegram (>49MB)"
    if "private video" in low or "video unavailable" in low or "has been removed" in low:
        return "❌ វីដេអូនេះ private ឬត្រូវបានលុបហើយ"
    if "confirm your age" in low or "age-restricted" in low:
        return "❌ វីដេអូនេះត្រូវការគណនី (កំណត់អាយុ)"
    return s[-300:] or "yt-dlp failed"


def download_media(url: str, out_dir: str) -> tuple:
    """Return (filepath, error). Prefer mp4 under ~49MB for Telegram."""
    out_tmpl = os.path.join(out_dir, "dl.%(ext)s")
    ua = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
          "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
    errs = []

    if _is_tiktok(url):                                   # tikwm first (works when yt-dlp is challenged)
        path, err = _download_tikwm(url, out_dir)
        if path:
            return path, ""
        errs.append(err or "")

    attempts = []
    if _is_youtube(url):
        fmt = "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]/b[height<=720]/b"
        attempts.append(_ytdlp_base(url) + ["-o", out_tmpl, "-f", fmt, "--merge-output-format", "mp4", url])
        # second chance with another client (no cookies: that client does not use them)
        attempts.append(_ytdlp_base(url, use_cookies=False) + [
            "--extractor-args", "youtube:player_client=android_vr",
            "-o", out_tmpl, "-f", "b[height<=720]/b", url])
    elif _is_tiktok(url):
        b = _ytdlp_base(url) + ["--user-agent", ua, "--add-header", "Referer:https://www.tiktok.com/"]
        attempts.append(b + ["--extractor-args", "tiktok:api_hostname=api16-normal-c-useast1a.tiktokv.com",
                             "-o", out_tmpl, "-f", "b[ext=mp4]/best", url])
        attempts.append(b + ["--extractor-args", "tiktok:webpage_download=True", "-o", out_tmpl, "-f", "best", url])
        attempts.append(_ytdlp_base(url) + ["-o", out_tmpl, "-f", "best", url])
    else:
        attempts.append(_ytdlp_base(url) + [
            "-o", out_tmpl, "-f", "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]/b",
            "--merge-output-format", "mp4", url])
        attempts.append(_ytdlp_base(url) + ["-o", out_tmpl, "-f", "best[filesize<49M]/best", url])

    try:
        for cmd in attempts:
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=200)
            except FileNotFoundError:
                return None, "yt-dlp មិនមានលើ server — deploy ឡើងវិញ"
            found = _find_media(out_dir)
            if found:
                return found, ""
            errs.append((r.stderr or r.stdout or "")[-600:])
            if r.returncode == 0:
                break
        joined = "\n".join(e for e in errs if e)
        return None, _friendly_dl_error(joined, url)
    except subprocess.TimeoutExpired:
        return None, "timeout — តំណភ្ជាប់យូរពេក"
    except Exception as e:
        return None, str(e)


# ---- admin: manage cookies.txt from Telegram (registered BEFORE on_text, which would swallow commands) ----
@bot.message_handler(commands=["cookies"])
def cmd_cookies(message):
    if message.from_user.id != ADMIN_ID:
        return
    src = "ឯកសារ cookies.txt" if os.path.isfile(COOKIES_FILE) else ("env" if _cookies_path() else "")
    bot.reply_to(
        message,
        "🍪 cookies: " + ("✅ មាន (%s)" % src if src else "❌ គ្មាន") +
        "\nProxy: " + ("✅" if os.environ.get("YTDLP_PROXY") else "❌") +
        "\n\nផ្ញើ file <b>cookies.txt</b> (Netscape format) មកទីនេះ ដើម្បីដាក់។\n/delcookies — លុប",
    )


@bot.message_handler(commands=["delcookies"])
def cmd_delcookies(message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        if os.path.isfile(COOKIES_FILE):
            os.remove(COOKIES_FILE)
        bot.reply_to(message, "🗑 បានលុប cookies")
    except Exception as e:
        bot.reply_to(message, "❌ %s" % _esc(e))


@bot.message_handler(content_types=["document"])
def on_document(message):
    if message.from_user.id != ADMIN_ID:
        return
    doc = message.document
    name = (doc.file_name or "").lower()
    if not name.endswith(".txt") or (doc.file_size or 0) > 1024 * 1024:
        bot.reply_to(message, "ផ្ញើ file <b>cookies.txt</b> (≤1MB) ប៉ុណ្ណោះ")
        return
    try:
        data = bot.download_file(bot.get_file(doc.file_id).file_path).decode("utf-8", "ignore")
        rows = [l for l in data.splitlines() if l.strip() and not l.startswith("#") and len(l.split("\t")) >= 7]
        if not rows:
            bot.reply_to(message, "❌ មិនមែន cookies.txt (Netscape format) ទេ")
            return
        with open(COOKIES_FILE, "w", encoding="utf-8") as f:
            f.write(data if data.endswith("\n") else data + "\n")
        os.chmod(COOKIES_FILE, 0o600)
        yt = sum(1 for l in rows if "youtube" in l or "google" in l)
        try:
            bot.delete_message(message.chat.id, message.message_id)   # the file holds login secrets
        except Exception:
            pass
        bot.send_message(
            message.chat.id,
            "✅ បានរក្សាទុក cookies (%d ជួរ, YouTube/Google: %d)%s" % (
                len(rows), yt, "" if yt else "\n⚠️ គ្មាន cookies របស់ YouTube ទេ"),
        )
    except Exception as e:
        bot.reply_to(message, "❌ %s" % _esc(e))
# ===== END KHEMCLIP YT FIX =====


def run_tts(text: str, voice: str, out_path: str) -> str | None:
    """Generate mp3 with edge-tts. Return error or None."""
    v = VOICE_MALE if voice == "male" else VOICE_FEMALE
    text = (text or "").strip()[:500]
    if not text:
        return "អត្ថបទទទេ"

    async def _gen():
        import edge_tts
        communicate = edge_tts.Communicate(text, v)
        await communicate.save(out_path)

    try:
        asyncio.run(_gen())
        if not os.path.isfile(out_path) or os.path.getsize(out_path) < 100:
            return "TTS បរាជ័យ"
        return None
    except Exception as e:
        return str(e)


@bot.message_handler(func=lambda m: True, content_types=["text"])
def on_text(message):
    uid = message.from_user.id
    text = (message.text or "").strip()
    if not text:
        return
    if text.startswith("/"):
        return

    # URL anywhere → download
    m = URL_RE.search(text)
    st = waiting.get(uid) or {}
    if m and st.get("mode") != "tts":
        if not rate_ok(uid, 5):
            bot.reply_to(message, "⏳ រង់ចាំបន្តិច…")
            return
        url = m.group(0).rstrip(").,]}>\"'")
        status = bot.reply_to(message, "📥 កំពុងទាញ…")
        tmp = tempfile.mkdtemp(prefix="teanhsor_")
        try:
            path, err = download_media(url, tmp)
            if not path:
                bot.edit_message_text(f"❌ ទាញមិនបាន:\n<code>{_esc(err[:300])}</code>", message.chat.id, status.message_id)
                return
            size = os.path.getsize(path)
            if size > 49 * 1024 * 1024:
                bot.edit_message_text("❌ ឯកសារធំពេកសម្រាប់ Telegram", message.chat.id, status.message_id)
                return
            with open(path, "rb") as f:
                if path.lower().endswith((".mp3", ".m4a", ".opus")):
                    bot.send_audio(message.chat.id, f, caption=f"✅ {BOT_NAME}")
                else:
                    bot.send_video(message.chat.id, f, caption=f"✅ {BOT_NAME}", supports_streaming=True)
            try:
                bot.delete_message(message.chat.id, status.message_id)
            except Exception:
                pass
        except Exception as e:
            log.exception("dl")
            try:
                bot.edit_message_text(f"❌ {e}", message.chat.id, status.message_id)
            except Exception:
                bot.reply_to(message, f"❌ {e}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        waiting.pop(uid, None)
        return

    # TTS mode
    if st.get("mode") == "tts" or text.startswith("tts:"):
        if not rate_ok(uid, 2):
            bot.reply_to(message, "⏳ រង់ចាំបន្តិច…")
            return
        voice = st.get("voice") or "female"
        if text.startswith("tts:"):
            text = text[4:].strip()
        status = bot.reply_to(message, "🔊 កំពុងបង្កើតសំឡេង…")
        tmp = tempfile.mkdtemp(prefix="teanhsor_tts_")
        out = os.path.join(tmp, "speech.mp3")
        try:
            err = run_tts(text, voice, out)
            if err:
                bot.edit_message_text(f"❌ {err}", message.chat.id, status.message_id)
                return
            label = "ប្រុស · Piseth" if voice == "male" else "ស្រី · Sreymom"
            with open(out, "rb") as f:
                bot.send_audio(
                    message.chat.id,
                    f,
                    caption=f"🔊 <b>{BOT_NAME}</b>\nសំឡេង: {label}\n\n{text[:120]}",
                    title="Techsor TTS",
                    performer=label,
                )
            try:
                bot.delete_message(message.chat.id, status.message_id)
            except Exception:
                pass
        except Exception as e:
            log.exception("tts")
            try:
                bot.edit_message_text(f"❌ {e}", message.chat.id, status.message_id)
            except Exception:
                bot.reply_to(message, f"❌ {e}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return

    if m:
        # has url but was in tts - still allow
        waiting[uid] = {"mode": "dl"}
        on_text(message)
        return

    bot.send_message(
        message.chat.id,
        "ផ្ញើ <b>link</b> ដើម្បីទាញវីដេអូ\n"
        "ឬ ចុច 🔊 TTS / 👨 សំឡេងប្រុស / 👩 សំឡេងស្រី",
        reply_markup=main_kb(),
    )


# Flask health for Render
app = Flask(__name__)


@app.get("/health")
def health():
    return {"ok": True, "bot": BOT_NAME_EN}, 200


def run_flask():
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    if not BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN")
    log.info("Starting %s", BOT_NAME)
    threading.Thread(target=run_flask, daemon=True).start()
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=40)
        except Exception as e:
            log.error("polling: %s", e)
            time.sleep(3)
