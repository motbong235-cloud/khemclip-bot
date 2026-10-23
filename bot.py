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
    return any(x in u for x in ("tiktok.com", "vm.tiktok.com", "vt.tiktok.com", "tiktok."))


def download_media(url: str, out_dir: str) -> tuple[str | None, str]:
    """Return (filepath, error). Prefer mp4 under ~49MB for Telegram."""
    out_tmpl = os.path.join(out_dir, "dl.%(ext)s")
    ua = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
        "Mobile/15E148 Safari/604.1"
    )
    base = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--newline",
        "-o", out_tmpl,
        "--max-filesize", "49M",
        "--retries", "3",
        "--fragment-retries", "3",
        "--socket-timeout", "30",
        "--user-agent", ua,
        "--add-header", "Referer:https://www.tiktok.com/",
        "--add-header", "Accept-Language:en-US,en;q=0.9",
    ]

    attempts = []
    if _is_tiktok(url):
        # TikTok: try extractor args + mobile UA variants
        attempts.append(base + [
            "--extractor-args", "tiktok:api_hostname=api16-normal-c-useast1a.tiktokv.com",
            "-f", "b[ext=mp4]/best",
            url,
        ])
        attempts.append(base + [
            "--extractor-args", "tiktok:webpage_download=True",
            "-f", "best",
            url,
        ])
        attempts.append([
            "yt-dlp", "--no-playlist", "--no-warnings",
            "-o", out_tmpl, "--max-filesize", "49M",
            "--user-agent", ua,
            "-f", "download",
            url,
        ])
        attempts.append([
            "yt-dlp", "--no-playlist", "--no-warnings",
            "-o", out_tmpl, "--max-filesize", "49M",
            "-f", "best",
            url,
        ])
    else:
        attempts.append(base + [
            "-f", "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]/b",
            "--merge-output-format", "mp4",
            url,
        ])
        attempts.append([
            "yt-dlp", "--no-playlist", "--no-warnings",
            "-o", out_tmpl, "--max-filesize", "49M",
            "-f", "best[filesize<49M]/best",
            url,
        ])

    last_err = "yt-dlp failed"
    try:
        for cmd in attempts:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=200)
            files = sorted(Path(out_dir).glob("dl.*"), key=lambda p: p.stat().st_mtime, reverse=True)
            for f in files:
                if f.is_file() and f.suffix.lower() in (".mp4", ".mkv", ".webm", ".mp3", ".m4a", ".opus", ".mov"):
                    if f.stat().st_size > 1000:
                        return str(f), ""
            # also any media file created
            files2 = sorted(Path(out_dir).iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
            for f in files2:
                if f.is_file() and f.suffix.lower() in (".mp4", ".mkv", ".webm", ".mp3", ".m4a", ".opus", ".mov"):
                    if f.stat().st_size > 1000:
                        return str(f), ""
            last_err = (r.stderr or r.stdout or last_err)[-500:]
            if r.returncode == 0:
                break
        return None, last_err
    except subprocess.TimeoutExpired:
        return None, "timeout — តំណភ្ជាប់យូរពេក"
    except Exception as e:
        return None, str(e)


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
                bot.edit_message_text(f"❌ ទាញមិនបាន:\n<code>{err[:300]}</code>", message.chat.id, status.message_id)
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
