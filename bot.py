#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ខ្មែរClip (KhemClip) — Telegram bot
1) Download video/audio by link (yt-dlp)
2) Khmer TTS male / female (edge-tts)
3) OCR — ចម្លងអត្ថបទពីរូបភាព
"""
from __future__ import annotations

import asyncio
import html
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

# uid -> awaiting mode (dl / dl_audio / tts / admin_broadcast / admin_emoji)
waiting: dict = {}
# rate limit simple
_last: dict = {}

USERS_FILE = os.path.join(DATA_DIR, "users.json")
EMOJI_FILE = os.path.join(DATA_DIR, "emoji_premium.json")
BANNER_FILE = os.path.join(DATA_DIR, "welcome_banner.json")

# fallback unicode → optional Telegram custom emoji id (premium)
DEFAULT_EMOJI = {
    "video": "📥",
    "audio": "🎵",
    "tts": "🔊",
    "help": "ℹ️",
    "male": "👨",
    "female": "👩",
    "ok": "✅",
    "fail": "❌",
    "wait": "⏳",
    "link": "🔗",
    "bot": "🎬",
    "admin": "🛠",
    "broadcast": "📣",
    "emoji": "✨",
    "choice": "⬇️",
    "ocr": "📝",
}


def _load_json(path, default):
    try:
        import json
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default


def _save_json(path, data):
    try:
        import json
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("save %s: %s", path, e)


def emoji_map():
    m = dict(DEFAULT_EMOJI)
    custom = _load_json(EMOJI_FILE, {})
    if isinstance(custom, dict):
        for k, v in custom.items():
            if v:
                m[k] = v
    return m


def _esc(s) -> str:
    """Escape text for Telegram HTML parse_mode."""
    return html.escape(str(s or ""), quote=False)


def e(key: str) -> str:
    """HTML for *message text only*. Premium id → <tg-emoji>; else unicode.
    NEVER put this string into keyboard button text (shows raw HTML).
    """
    val = str(emoji_map().get(key) or DEFAULT_EMOJI.get(key) or "")
    if val.isdigit() and len(val) >= 8:
        fb = DEFAULT_EMOJI.get(key, "•")
        return f'<tg-emoji emoji-id="{val}">{fb}</tg-emoji>'
    return val


def e_plain(key: str) -> str:
    """Unicode only for keyboard button text — never HTML / never numeric id."""
    val = str(emoji_map().get(key) or DEFAULT_EMOJI.get(key) or "")
    # strip accidental <tg-emoji> if stored wrong
    if "<tg-emoji" in val or "emoji-id" in val:
        m = re.search(r">([^<]+)<", val)
        if m:
            return m.group(1).strip() or DEFAULT_EMOJI.get(key, "•")
        return DEFAULT_EMOJI.get(key, "•")
    if val.isdigit():
        return DEFAULT_EMOJI.get(key, "•")
    # only keep short unicode glyph
    if len(val) > 8:
        return DEFAULT_EMOJI.get(key, "•")
    return val or DEFAULT_EMOJI.get(key, "•")


def e_icon_id(key: str) -> str | None:
    """Custom emoji id (for message HTML only helper / future use)."""
    val = str(emoji_map().get(key) or "")
    if val.isdigit() and len(val) >= 8:
        return val
    return None


def kb_text(icon_key: str, label: str) -> str:
    """Always plain string for ReplyKeyboard — Telegram does not parse HTML here."""
    return f"{e_plain(icon_key)} {label}".strip()


def make_kb_button(text: str, icon_key: str | None = None):
    """Reply button. Premium icon only if ENABLE_BUTTON_ICONS=1.
    Default: plain unicode (safe). Never put <tg-emoji> HTML in text.
    """
    text = str(text)
    use_icon = os.environ.get("ENABLE_BUTTON_ICONS", "").strip() == "1"
    icon = e_icon_id(icon_key) if icon_key and use_icon else None
    if icon:
        return {"text": text, "icon_custom_emoji_id": icon}
    return text


def make_inline_button(text: str, callback_data: str, icon_key: str | None = None, style: str | None = None):
    text = str(text)
    use_icon = os.environ.get("ENABLE_BUTTON_ICONS", "").strip() == "1"
    icon = e_icon_id(icon_key) if icon_key and use_icon else None
    if icon:
        try:
            return types.InlineKeyboardButton(text=text, callback_data=callback_data, icon_custom_emoji_id=icon)
        except TypeError:
            btn = types.InlineKeyboardButton(text=text, callback_data=callback_data)
            try:
                btn.icon_custom_emoji_id = icon
            except Exception:
                pass
            return btn
    return types.InlineKeyboardButton(text=text, callback_data=callback_data)


def track_user(uid: int):
    """Remember every user who interacts (for broadcast count)."""
    if not uid:
        return
    try:
        users = _load_json(USERS_FILE, [])
        if not isinstance(users, list):
            users = []
        s = str(int(uid))
        if s not in users:
            users.append(s)
            _save_json(USERS_FILE, users[-50000:])
    except Exception as ex:
        log.warning("track_user: %s", ex)


def user_count() -> int:
    try:
        users = _load_json(USERS_FILE, [])
        return len(users) if isinstance(users, list) else 0
    except Exception:
        return 0


def is_admin(uid: int) -> bool:
    return bool(ADMIN_ID) and int(uid) == int(ADMIN_ID)


def get_welcome_banner() -> str:
    """file_id or https URL for /start photo. Env WELCOME_BANNER overrides if set."""
    env = (os.environ.get("WELCOME_BANNER") or "").strip()
    if env:
        return env
    data = _load_json(BANNER_FILE, {})
    if isinstance(data, dict):
        return str(data.get("file_id") or data.get("url") or "").strip()
    return ""


def set_welcome_banner(file_id: str = "", url: str = ""):
    _save_json(BANNER_FILE, {"file_id": file_id or "", "url": url or ""})



def rate_ok(uid: int, sec: float = 3.0) -> bool:
    now = time.time()
    if now - _last.get(uid, 0) < sec:
        return False
    _last[uid] = now
    return True


def main_kb(uid: int | None = None):
    """Reply keyboard — plain text labels + icon_custom_emoji_id when set.
    Button text must NEVER contain <tg-emoji> HTML.
    """
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.row(
        make_kb_button(kb_text("video", "ទាញ Media"), "video"),
        make_kb_button(kb_text("tts", "TTS ខ្មែរ"), "tts"),
    )
    kb.row(
        make_kb_button(kb_text("male", "សំឡេងប្រុស"), "male"),
        make_kb_button(kb_text("female", "សំឡេងស្រី"), "female"),
    )
    kb.row(make_kb_button(kb_text("help", "ជួយ"), "help"))
    if uid and is_admin(uid):
        kb.row(make_kb_button(kb_text("admin", "Admin"), "admin"))
    return kb


def tts_kb():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("👨  ប្រុស · Piseth", callback_data="tts:male"),
        types.InlineKeyboardButton("👩  ស្រី · Sreymom", callback_data="tts:female"),
    )
    return kb


def after_dl_audio_kb():
    """Shown after successful video/photo download — optional audio extract."""
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("🎧 ទាញ Audio MP3 ពី link នេះ", callback_data="fmt:audio_again"))
    kb.add(types.InlineKeyboardButton("✅ រួចរាល់ · មិនយក Audio", callback_data="fmt:done"))
    return kb


def format_choice_kb():
    """After user sends a link: pick video / audio / photo."""
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        make_inline_button("Video MP4", "fmt:video", "video"),
        make_inline_button("Audio MP3", "fmt:audio", "audio"),
    )
    kb.add(
        make_inline_button("Photo / Live", "fmt:photo", "ok"),
        make_inline_button("បោះបង់", "fmt:cancel", "fail"),
    )
    return kb


def admin_kb():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(make_inline_button("Broadcast សារទាំងអស់", "adm:broadcast", "broadcast"))
    kb.add(make_inline_button("Setup emoji premium", "adm:emoji", "emoji"))
    kb.add(make_inline_button("Welcome Banner", "adm:banner", "bot"))
    kb.add(make_inline_button("មើល emoji បច្ចុប្បន្ន", "adm:emoji_list", "help"))
    kb.add(make_inline_button("បិទ", "adm:close", "fail"))
    return kb


# Human labels for emoji keys (admin UI)
EMOJI_LABELS = {
    "video": "Video",
    "audio": "Audio",
    "tts": "TTS",
    "help": "Help",
    "male": "Male",
    "female": "Female",
    "ok": "OK",
    "fail": "Fail",
    "wait": "Wait",
    "link": "Link",
    "bot": "Bot",
    "admin": "Admin",
    "broadcast": "Broadcast",
    "emoji": "Emoji",
    "choice": "Choice",
}


def emoji_key_kb():
    """Admin picks which UI key to assign a premium emoji to."""
    kb = types.InlineKeyboardMarkup(row_width=2)
    custom = _load_json(EMOJI_FILE, {})
    if not isinstance(custom, dict):
        custom = {}
    row = []
    for k in DEFAULT_EMOJI.keys():
        label = EMOJI_LABELS.get(k, k)
        mark = " ✓" if k in custom and custom.get(k) else ""
        # show default glyph for readability on button
        glyph = DEFAULT_EMOJI.get(k, "")
        row.append(
            types.InlineKeyboardButton(
                f"{glyph} {label}{mark}",
                callback_data=f"adm:ek:{k}",
            )
        )
        if len(row) == 2:
            kb.row(*row)
            row = []
    if row:
        kb.row(*row)
    kb.add(types.InlineKeyboardButton("📋 មើលទាំងអស់", callback_data="adm:emoji_list"))
    kb.add(types.InlineKeyboardButton("🗑 Reset ទាំងអស់", callback_data="adm:emoji_reset"))
    kb.add(types.InlineKeyboardButton("✖️ បិទ", callback_data="adm:close"))
    return kb


def emoji_after_save_kb(key: str):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("➡️ Key បន្ទាប់", callback_data="adm:emoji"),
        types.InlineKeyboardButton(f"🔁 កែ {key} ម្ដងទៀត", callback_data=f"adm:ek:{key}"),
    )
    kb.add(types.InlineKeyboardButton("📋 មើលទាំងអស់", callback_data="adm:emoji_list"))
    kb.add(types.InlineKeyboardButton("✖️ រួចរាល់", callback_data="adm:close"))
    return kb


def extract_premium_emoji(message) -> str | None:
    """Auto-capture custom_emoji_id from a message (premium emoji)."""
    for ents in (message.entities, getattr(message, "caption_entities", None)):
        if not ents:
            continue
        for ent in ents:
            if getattr(ent, "type", None) == "custom_emoji":
                eid = getattr(ent, "custom_emoji_id", None)
                if eid:
                    return str(eid)
    return None


def format_emoji_list() -> str:
    custom = _load_json(EMOJI_FILE, {})
    if not isinstance(custom, dict):
        custom = {}
    lines = [f'{e("emoji")} <b>Emoji បច្ចុប្បន្ន</b>', "━━━━━━━━━━━━━━━━", ""]
    for k, default in DEFAULT_EMOJI.items():
        label = EMOJI_LABELS.get(k, k)
        cur = custom.get(k)
        preview = e(k)
        if cur:
            kind = "premium" if str(cur).isdigit() else "unicode"
            lines.append(f"{preview} <b>{label}</b> <code>{k}</code>")
            lines.append(f"   → {kind}: <code>{cur}</code>")
        else:
            lines.append(f"{preview} <b>{label}</b> <code>{k}</code> · default")
    lines.append("")
    lines.append(f"✓ = បានកំណត់  ·  {sum(1 for k in DEFAULT_EMOJI if k in custom)}/{len(DEFAULT_EMOJI)}")
    return "\n".join(lines)


def card(title: str, body: str) -> str:
    """Pretty HTML card block for bot messages."""
    return f"<b>{title}</b>\n━━━━━━━━━━━━━━━━\n{body}"


def safe_send(chat_id, html_text, reply_markup=None, plain_fallback=None):
    """Send HTML; on Telegram entity/premium failure retry plain text (never silent)."""
    try:
        return bot.send_message(chat_id, html_text, reply_markup=reply_markup)
    except Exception as ex1:
        log.warning("safe_send html failed: %s", ex1)
        plain = plain_fallback
        if plain is None:
            # strip simple tags
            plain = re.sub(r"<[^>]+>", "", str(html_text))
        try:
            return bot.send_message(chat_id, plain, reply_markup=reply_markup)
        except Exception as ex2:
            log.warning("safe_send plain+markup failed: %s", ex2)
            try:
                return bot.send_message(chat_id, plain)
            except Exception as ex3:
                log.exception("safe_send total fail: %s", ex3)
                return None



def run_ocr(image_path: str) -> tuple[str, str]:
    """Extract text from image via OCR.space (free). Returns (text, error)."""
    import requests
    api_key = (os.environ.get("OCR_SPACE_API_KEY") or "helloworld").strip()
    # try eng first, then auto
    for lang in ("eng", "auto"):
        try:
            with open(image_path, "rb") as f:
                r = requests.post(
                    "https://api.ocr.space/parse/image",
                    files={"file": ("image.jpg", f, "image/jpeg")},
                    data={
                        "apikey": api_key,
                        "language": lang,
                        "isOverlayRequired": False,
                        "OCREngine": 2,
                        "scale": True,
                        "detectOrientation": True,
                    },
                    timeout=60,
                )
            data = r.json()
            if data.get("IsErroredOnProcessing"):
                err = data.get("ErrorMessage") or data.get("ErrorDetails") or "ocr_error"
                if isinstance(err, list):
                    err = "; ".join(str(x) for x in err)
                continue
            parsed = data.get("ParsedResults") or []
            texts = []
            for pr in parsed:
                tx = (pr.get("ParsedText") or "").strip()
                if tx:
                    texts.append(tx)
            text = "\n".join(texts).strip()
            if text:
                return text, ""
        except Exception as ex:
            log.exception("ocr")
            return "", str(ex)
    return "", "រកអត្ថបទមិនឃើញក្នុងរូប (ឬ OCR limit)"


def download_tg_file(file_id: str, dest: str) -> str:
    """Download Telegram file by file_id → dest path. Returns error or empty."""
    try:
        fi = bot.get_file(file_id)
        url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{fi.file_path}"
        import urllib.request
        urllib.request.urlretrieve(url, dest)
        if os.path.isfile(dest) and os.path.getsize(dest) > 50:
            return ""
        return "empty_file"
    except Exception as ex:
        return str(ex)


@bot.message_handler(commands=["start", "help"])
def cmd_start(message):
    uid = message.from_user.id
    waiting.pop(uid, None)
    track_user(uid)
    first = (message.from_user.first_name or "មិត្ត").strip()
    # Premium HTML caption (needs Premium owner or Fragment)
    caption_rich = (
        f"សួស្តី <b>{first}</b> 👋\n\n"
        f"{e('bot')} <b>{BOT_NAME}</b>  ·  <i>{BOT_NAME_EN}</i>\n"
        f"━━━━━━━━━━━━━━━━\n\n"
        f"{e('video')} <b>ទាញ Media</b>\n"
        f"   TikTok · YouTube · Facebook\n"
        f"   ជ្រើស Video / Audio / Photo · បន្ទាប់ Video/Photo អាចទាញ Audio បន្ត\n\n"
        f"{e('tts')} <b>TTS ខ្មែរ</b>\n"
        f"   👨 Piseth  ·  👩 Sreymom\n\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"💡 ផ្ញើ <b>link</b> មកភ្លាម ឬចុចម៉ឺនុយខាងក្រោម"
    )
    # Always-works plain caption
    caption_plain = (
        f"សួស្តី <b>{first}</b> 👋\n\n"
        f"{e_plain('bot')} <b>{BOT_NAME}</b>  ·  <i>{BOT_NAME_EN}</i>\n"
        f"━━━━━━━━━━━━━━━━\n\n"
        f"{e_plain('video')} <b>ទាញ Media</b>\n"
        f"   TikTok · YouTube · Facebook\n"
        f"   ជ្រើស Video / Audio / Photo · បន្ទាប់ Video/Photo អាចទាញ Audio បន្ត\n\n"
        f"{e_plain('tts')} <b>TTS ខ្មែរ</b>\n"
        f"   👨 Piseth  ·  👩 Sreymom\n\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"💡 ផ្ញើ <b>link</b> មកភ្លាម ឬចុចម៉ឺនុយខាងក្រោម"
    )
    kb = main_kb(uid)
    banner = get_welcome_banner()
    for caption in (caption_rich, caption_plain):
        try:
            if banner:
                bot.send_photo(message.chat.id, banner, caption=caption, reply_markup=kb)
            else:
                bot.send_message(message.chat.id, caption, reply_markup=kb)
            return
        except Exception as ex:
            log.warning("start send: %s", ex)
            banner = ""  # retry without bad banner
    try:
        bot.send_message(message.chat.id, caption_plain, reply_markup=types.ReplyKeyboardRemove())
    except Exception as ex:
        log.exception("start total fail: %s", ex)


@bot.message_handler(func=lambda m: m.text and ("ជួយ" in m.text))
def cmd_help(message):
    cmd_start(message)


@bot.message_handler(func=lambda m: m.text and ("ទាញ Media" in m.text or m.text in ("📥 ទាញវីដេអូ", "🎵 ទាញសំឡេង", "ទាញសំឡេង", "ទាញសូរ")))
def ask_link(message):
    waiting[message.from_user.id] = {"mode": "dl_choose"}
    safe_send(
        message.chat.id,
        card(
            f'{e_plain("link")} ទាញ Media',
            "ផ្ញើ <b>link</b> មកឥឡូវ\n"
            "TikTok · YouTube · Facebook · Instagram\n\n"
            "រួចជ្រើស 🎬 <b>Video</b> · 🎧 <b>Audio</b> · 🖼 <b>Photo</b>",
        ),
        reply_markup=main_kb(message.from_user.id),
        plain_fallback="ទាញ Media — ផ្ញើ link មកឥឡូវ",
    )


@bot.message_handler(func=lambda m: m.text and ("TTS" in m.text or "TTS ខ្មែរ" in (m.text or "")))
def ask_tts(message):
    waiting[message.from_user.id] = {"mode": "tts", "voice": "female"}
    bot.send_message(
        message.chat.id,
        card(
            f'{e("tts")} TTS ខ្មែរ',
            "ជ្រើសសំឡេងខាងក្រោម\n"
            "រួចផ្ញើអត្ថបទខ្មែរ (អត់លើស 500 តួ)",
        ),
        reply_markup=tts_kb(),
    )


@bot.message_handler(func=lambda m: m.text and "Admin" in (m.text or "") and is_admin(m.from_user.id))
def ask_admin(message):
    n = user_count()
    bot.send_message(
        message.chat.id,
        card(
            f'{e("admin")} Admin panel',
            f"👥 អ្នកប្រើ (track): <b>{n}</b>\n(រាប់ពីពេលមានមុខងារនេះ)\n\nជ្រើសមុខងារខាងក្រោម",
        ),
        reply_markup=admin_kb(),
    )


@bot.message_handler(func=lambda m: m.text and "សំឡេងប្រុស" in m.text)
def tts_male_mode(message):
    waiting[message.from_user.id] = {"mode": "tts", "voice": "male"}
    bot.send_message(message.chat.id, card(f'{e("male")} សំឡេងប្រុស · Piseth', "ផ្ញើអត្ថបទខ្មែរមកឥឡូវ ✍️"), reply_markup=main_kb(message.from_user.id))


@bot.message_handler(func=lambda m: m.text and "សំឡេងស្រី" in m.text)
def tts_female_mode(message):
    waiting[message.from_user.id] = {"mode": "tts", "voice": "female"}
    bot.send_message(message.chat.id, card(f'{e("female")} សំឡេងស្រី · Sreymom', "ផ្ញើអត្ថបទខ្មែរមកឥឡូវ ✍️"), reply_markup=main_kb(message.from_user.id))



@bot.message_handler(func=lambda m: m.text and ("ចម្លងអត្ថបទ" in (m.text or "") or "OCR" in (m.text or "").upper()))
def ask_ocr(message):
    waiting[message.from_user.id] = {"mode": "ocr"}
    track_user(message.from_user.id)
    safe_send(
        message.chat.id,
        card(
            f'{e("ocr")} ចម្លងអត្ថបទពីរូប',
            "ផ្ញើ <b>រូបភាព</b> (photo) ឬ file រូប (jpg/png/webp)\n"
            "Bot នឹងអានអក្សរក្នុងរូប រួចផ្ញើអត្ថបទឲ្យចម្លង។",
        ),
        reply_markup=main_kb(message.from_user.id),
        plain_fallback="ផ្ញើរូបភាពដើម្បី OCR",
    )


@bot.message_handler(content_types=["photo", "document"])
def on_image(message):
    uid = message.from_user.id
    track_user(uid)
    st = waiting.get(uid) or {}
    # Always allow OCR when mode=ocr, or when user sends photo without link mode
    is_ocr = st.get("mode") == "ocr" or (message.content_type == "photo" and st.get("mode") not in ("dl_choose", "tts", "admin_broadcast", "admin_emoji", "admin_banner"))
    # If not explicitly ocr and not idle-ish, skip for admin banner etc
    if st.get("mode") in ("admin_banner", "admin_emoji", "admin_broadcast", "tts"):
        return
    if not is_ocr and st.get("mode") == "dl_choose":
        return

    file_id = None
    if message.content_type == "photo" and message.photo:
        file_id = message.photo[-1].file_id
    elif message.content_type == "document" and message.document:
        mime = (message.document.mime_type or "")
        name = (message.document.file_name or "").lower()
        if mime.startswith("image/") or name.endswith((".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif")):
            file_id = message.document.file_id
        else:
            if st.get("mode") == "ocr":
                bot.reply_to(message, f'{e("fail")} សូមផ្ញើ file រូបភាព')
            return
    if not file_id:
        return

    if st.get("mode") != "ocr" and message.content_type == "photo":
        # soft prompt: treat photo as OCR
        waiting[uid] = {"mode": "ocr"}

    status = bot.reply_to(message, f'{e("wait")} កំពុងអានអក្សរពីរូប…')
    tmp = tempfile.mkdtemp(prefix="ocr_")
    dest = os.path.join(tmp, "img.jpg")
    try:
        err = download_tg_file(file_id, dest)
        if err:
            bot.edit_message_text(f'{e("fail")} ទាញរូបមិនបាន: {err}', message.chat.id, status.message_id)
            return
        text, oerr = run_ocr(dest)
        if oerr or not text:
            bot.edit_message_text(
                f'{e("fail")} {oerr or "រកអក្សរមិនឃើញ"}',
                message.chat.id,
                status.message_id,
            )
            return
        # send as plain + HTML code block for easy copy
        body = html.escape(text)
        if len(body) > 3500:
            body = body[:3500] + "\n…"
        try:
            bot.edit_message_text(
                f'{e("ocr")} <b>អត្ថបទពីរូប</b>\n\n<pre>{body}</pre>',
                message.chat.id,
                status.message_id,
            )
        except Exception:
            bot.send_message(message.chat.id, f'{e("ocr")} អត្ថបទ:\n\n{text[:4000]}')
        # also send as document-less copy-friendly plain
        if len(text) > 500:
            bot.send_message(message.chat.id, text[:4000])
        waiting.pop(uid, None)
    except Exception as ex:
        log.exception("on_image")
        try:
            bot.edit_message_text(f'{e("fail")} {ex}', message.chat.id, status.message_id)
        except Exception:
            bot.reply_to(message, f'{e("fail")} {ex}')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@bot.callback_query_handler(func=lambda c: (c.data or "").startswith("tts:"))
def cb_tts_voice(call):
    uid = call.from_user.id
    track_user(uid)
    voice = "male" if call.data.endswith("male") else "female"
    waiting[uid] = {"mode": "tts", "voice": voice}
    try:
        bot.answer_callback_query(call.id, "បានជ្រើស " + ("ប្រុស" if voice == "male" else "ស្រី"))
    except Exception:
        pass
    label = "ប្រុស · Piseth" if voice == "male" else "ស្រី · Sreymom"
    bot.send_message(
        call.message.chat.id,
        card(
            f'{(e("male") if voice == "male" else e("female"))} {label}',
            "ផ្ញើអត្ថបទខ្មែរមកឥឡូវ ✍️",
        ),
        reply_markup=main_kb(uid),
    )


@bot.callback_query_handler(func=lambda c: (c.data or "").startswith("fmt:"))
def cb_format(call):
    """User chose Video or Audio for a pending URL."""
    uid = call.from_user.id
    track_user(uid)
    data = call.data or ""
    if data.endswith("cancel") or data.endswith("done"):
        waiting.pop(uid, None)
        try:
            bot.answer_callback_query(call.id, "រួចរាល់" if data.endswith("done") else "បោះបង់")
            bot.edit_message_text(
                f'{e_plain("ok")} {"រួចរាល់" if data.endswith("done") else "បានបោះបង់"}',
                call.message.chat.id,
                call.message.message_id,
                reply_markup=None,
            )
        except Exception:
            try:
                bot.send_message(call.message.chat.id, "រួចរាល់", reply_markup=main_kb(uid))
            except Exception:
                pass
        return
    st = waiting.get(uid) or {}
    url = st.get("url") or ""
    audio_only = data.endswith("audio") or data.endswith("audio_again")
    photo_mode = data.endswith("photo")
    try:
        if audio_only:
            bot.answer_callback_query(call.id, "🎧 Audio")
        elif photo_mode:
            bot.answer_callback_query(call.id, "🖼 Photo / Live")
        else:
            bot.answer_callback_query(call.id, "🎬 Video")
    except Exception:
        pass
    if not url:
        bot.send_message(call.message.chat.id, f'{e("fail")} មិនមាន link — ផ្ញើ link ថ្មី', reply_markup=main_kb(uid))
        return
    try:
        bot.edit_message_text(
            card(
                f'{e("wait")} កំពុងទាញ…',
                ("🎧 សំឡេង MP3" if audio_only else ("🖼 Photo / Live" if photo_mode else "🎬 វីដេអូ MP4")) + "\nសូមរង់ចាំបន្តិច",
            ),
            call.message.chat.id,
            call.message.message_id,
        )
    except Exception:
        pass
    tmp = tempfile.mkdtemp(prefix="teanhsor_")
    try:
        path, err = download_media(url, tmp, audio_only=audio_only, photo_mode=photo_mode)
        if not path:
            bot.send_message(
                call.message.chat.id,
                f'{e("fail")} ទាញមិនបាន:\n<code>{_esc((err or "")[:300])}</code>',
                reply_markup=main_kb(uid),
            )
            return
        result = _normalize_dl_result(path)
        try:
            _send_dl_result(call.message.chat.id, result)
        except Exception as send_ex:
            waiting.pop(uid, None)
            bot.send_message(
                call.message.chat.id,
                f'{e_plain("fail")} {send_ex}',
                reply_markup=main_kb(uid),
            )
            return
        # After Video / Photo / Live success → always offer Audio from same link
        if not audio_only:
            waiting[uid] = {"mode": "dl_choose", "url": url}  # keep URL for Audio button
            safe_send(
                call.message.chat.id,
                f"{e_plain('ok')} <b>ទាញបានហើយ!</b>\n\n"
                f"ចង់ទាញ <b>Audio MP3</b> ពី link នេះបន្តទេ?\n"
                f"ចុចប៊ូតុងខាងក្រោម 👇",
                reply_markup=after_dl_audio_kb(),
                plain_fallback="ទាញបានហើយ! ចង់បាន Audio MP3 ផងទេ?",
            )
        else:
            waiting.pop(uid, None)
            try:
                bot.send_message(
                    call.message.chat.id,
                    f'{e_plain("ok")} រួចរាល់ · Audio',
                    reply_markup=main_kb(uid),
                )
            except Exception:
                pass
    except Exception as ex:
        log.exception("fmt")
        bot.send_message(call.message.chat.id, f'{e("fail")} {ex}', reply_markup=main_kb(uid))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@bot.callback_query_handler(func=lambda c: (c.data or "").startswith("adm:"))
def cb_admin(call):
    uid = call.from_user.id
    track_user(uid)
    if not is_admin(uid):
        try:
            bot.answer_callback_query(call.id, "Admin only", show_alert=True)
        except Exception:
            pass
        return
    data = call.data or ""
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    if data == "adm:close":
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        waiting.pop(uid, None)
        return
    if data == "adm:broadcast":
        waiting[uid] = {"mode": "admin_broadcast"}
        bot.send_message(
            call.message.chat.id,
            f'{e("broadcast")} ផ្ញើ <b>សារ</b> មកឥឡូវ — នឹងផ្ញើទៅអ្នកប្រើទាំងអស់ដែល track\n'
            f'(អត្ថបទ)\n\nបោះបង់: /start',
            reply_markup=main_kb(uid),
        )
        return
    if data == "adm:emoji":
        waiting.pop(uid, None)
        bot.send_message(
            call.message.chat.id,
            f'{e("emoji")} <b>Setup emoji premium</b>\n'
            f'━━━━━━━━━━━━━━━━\n'
            f'1️⃣ ជ្រើសកន្លែង (key) ខាងក្រោម\n'
            f'2️⃣ ផ្ញើ <b>emoji premium</b> មកមួយសារ\n'
            f'3️⃣ Bot <b>ចាប់ auto</b> — មិនបាច់ copy id\n\n'
            f'✓ = បានកំណត់រួច',
            reply_markup=emoji_key_kb(),
        )
        return
    if data.startswith("adm:ek:"):
        key = data.split(":", 2)[-1].strip().lower()
        if key not in DEFAULT_EMOJI:
            bot.send_message(call.message.chat.id, f'{e("fail")} key មិនត្រឹមត្រូវ')
            return
        waiting[uid] = {"mode": "admin_emoji", "key": key}
        label = EMOJI_LABELS.get(key, key)
        cur = _load_json(EMOJI_FILE, {})
        cur_v = cur.get(key) if isinstance(cur, dict) else None
        status = f"ឥឡូវ: {e(key)}" + (f" (<code>{cur_v}</code>)" if cur_v else " · default")
        bot.send_message(
            call.message.chat.id,
            f'{e("emoji")} <b>{label}</b> <code>{key}</code>\n'
            f'━━━━━━━━━━━━━━━━\n'
            f'{status}\n\n'
            f'👉 ផ្ញើ <b>emoji premium</b> មកឥឡូវ\n'
            f'   (ឬ emoji ធម្មតា)\n\n'
            f'🗑 លុប: ផ្ញើ <code>clear</code>\n'
            f'✖️ បោះបង់: /start',
            reply_markup=main_kb(uid),
        )
        return
    if data == "adm:emoji_list":
        bot.send_message(
            call.message.chat.id,
            format_emoji_list(),
            reply_markup=emoji_key_kb(),
        )
        return
    if data == "adm:emoji_reset":
        _save_json(EMOJI_FILE, {})
        waiting.pop(uid, None)
        bot.send_message(
            call.message.chat.id,
            f'{e("ok")} Reset emoji ទាំងអស់ → default\n\nជ្រើស key ដើម្បីកំណត់ថ្មី:',
            reply_markup=emoji_key_kb(),
        )
        return
    if data == "adm:banner":
        cur = get_welcome_banner()
        status = "មាន banner រួច" if cur else "មិនទាន់មាន"
        waiting[uid] = {"mode": "admin_banner"}
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(types.InlineKeyboardButton("🗑 លុប banner", callback_data="adm:banner_clear"))
        kb.add(types.InlineKeyboardButton("✖️ បោះបង់", callback_data="adm:close"))
        bot.send_message(
            call.message.chat.id,
            f'{e("bot")} <b>Welcome Banner</b>\n'
            f'━━━━━━━━━━━━━━━━\n'
            f'ស្ថានភាព: <b>{status}</b>\n\n'
            f'👉 ផ្ញើ <b>រូប</b> មកឥឡូវ (photo)\n'
            f'ឬផ្ញើ <b>link URL</b> រូប\n\n'
            f'ទំហំណែនាំ: 1280×720 ឬ 1920×1080',
            reply_markup=kb,
        )
        return
    if data == "adm:banner_clear":
        set_welcome_banner("", "")
        waiting.pop(uid, None)
        bot.send_message(
            call.message.chat.id,
            f'{e("ok")} លុប welcome banner ហើយ',
            reply_markup=main_kb(uid),
        )
        return


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


def _download_tikwm(url: str, out_dir: str, audio_only: bool = False, photo_mode: bool = False) -> tuple:
    """TikTok via tikwm. Returns (dict|None, err).
    dict: {"kind": "video"|"audio"|"photos"|"live", "paths": [...], "live_path": optional}
    """
    import urllib.request
    import urllib.parse
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
        if not (isinstance(data, dict) and data.get("data")):
            return None, "tikwm: %s" % (data.get("msg") if isinstance(data, dict) else "bad response")
    d = data.get("data") or {}

    def _fetch(file_url: str, dest: str):
        try:
            req2 = urllib.request.Request(
                file_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
                    "Referer": "https://www.tiktok.com/",
                },
            )
            with urllib.request.urlopen(req2, timeout=120) as r, open(dest, "wb") as f:
                while True:
                    chunk = r.read(1024 * 256)
                    if not chunk:
                        break
                    f.write(chunk)
            if os.path.getsize(dest) < 500:
                return "file empty"
            return None
        except Exception as e:
            return str(e)

    imgs = d.get("images") or []
    if isinstance(imgs, list):
        imgs = [x for x in imgs if isinstance(x, str) and x.startswith("http")]
    else:
        imgs = []
    play = d.get("play") or d.get("wmplay") or d.get("hdplay") or ""
    try:
        duration = float(d.get("duration") or 0)
    except Exception:
        duration = 0
    is_liveish = bool(play) and 0 < duration <= 3.5

    def _save_images(urls):
        paths = []
        for i, iu in enumerate(urls[:12]):
            ext = "jpg"
            low = iu.lower()
            if ".png" in low:
                ext = "png"
            elif ".webp" in low:
                ext = "webp"
            dest = os.path.join(out_dir, "tikwm_img_%02d.%s" % (i, ext))
            if not _fetch(iu, dest):
                paths.append(dest)
        return paths

    # Photo / slideshow
    if photo_mode or (imgs and (not play or photo_mode)):
        use = imgs
        if not use:
            cover = d.get("origin_cover") or d.get("cover") or d.get("ai_dynamic_cover") or ""
            if cover:
                use = [cover]
        if not use and play and photo_mode:
            # no stills — treat short video as live
            pass
        elif use:
            paths = _save_images(use)
            if not paths:
                return None, "tikwm: ទាញរូបមិនបាន"
            if is_liveish and play:
                live_path = os.path.join(out_dir, "tikwm_live.mp4")
                if not _fetch(play, live_path):
                    return {"kind": "live", "paths": paths, "live_path": live_path}, ""
            return {"kind": "photos", "paths": paths}, ""

    if audio_only:
        music = ""
        md = d.get("music_info") if isinstance(d.get("music_info"), dict) else {}
        if not md and isinstance(d.get("music"), dict):
            md = d.get("music") or {}
        if isinstance(md, dict):
            music = md.get("play") or md.get("url") or md.get("music") or ""
        if not music and isinstance(d.get("music"), str):
            music = d.get("music") or ""
        if music:
            out_m = os.path.join(out_dir, "tikwm_music.mp3")
            if not _fetch(music, out_m):
                return {"kind": "audio", "paths": [out_m]}, ""
        if not play:
            return None, "tikwm: រក music/video មិនឃើញ"
        vid = os.path.join(out_dir, "tikwm_tmp.mp4")
        err = _fetch(play, vid)
        if err:
            return None, "tikwm video: %s" % err
        out_a = os.path.join(out_dir, "tikwm_audio.mp3")
        try:
            r = subprocess.run(
                ["ffmpeg", "-y", "-i", vid, "-vn", "-acodec", "libmp3lame", "-q:a", "2", out_a],
                capture_output=True, timeout=120,
            )
            if r.returncode == 0 and os.path.isfile(out_a) and os.path.getsize(out_a) > 500:
                return {"kind": "audio", "paths": [out_a]}, ""
            out_m4a = os.path.join(out_dir, "tikwm_audio.m4a")
            r2 = subprocess.run(
                ["ffmpeg", "-y", "-i", vid, "-vn", "-acodec", "copy", out_m4a],
                capture_output=True, timeout=120,
            )
            if r2.returncode == 0 and os.path.isfile(out_m4a):
                return {"kind": "audio", "paths": [out_m4a]}, ""
            return None, "ffmpeg extract audio failed"
        except Exception as e:
            return None, "extract audio: %s" % e

    if not play:
        if imgs:
            paths = _save_images(imgs)
            if paths:
                return {"kind": "photos", "paths": paths}, ""
        return None, "tikwm: រក link video/រូប មិនឃើញ"
    out = os.path.join(out_dir, "tikwm.mp4")
    err = _fetch(play, out)
    if err:
        return None, "tikwm download: %s" % err
    if is_liveish:
        cover = d.get("origin_cover") or d.get("cover") or ""
        cover_paths = []
        if cover:
            cp = os.path.join(out_dir, "tikwm_cover.jpg")
            if not _fetch(cover, cp):
                cover_paths = [cp]
        return {"kind": "live", "paths": cover_paths, "live_path": out}, ""
    return {"kind": "video", "paths": [out]}, ""




def _normalize_dl_result(path) -> dict:
    if isinstance(path, dict) and path.get("kind"):
        return path
    if isinstance(path, str) and path:
        low = path.lower()
        if low.endswith((".mp3", ".m4a", ".opus", ".ogg", ".wav")):
            return {"kind": "audio", "paths": [path]}
        if low.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
            return {"kind": "photos", "paths": [path]}
        return {"kind": "video", "paths": [path]}
    return {"kind": "video", "paths": []}


def _send_dl_result(chat_id, result: dict):
    kind = result.get("kind") or "video"
    paths = [p for p in (result.get("paths") or []) if p and os.path.isfile(p)]
    live_path = result.get("live_path")
    cap = f'{e("ok")} <b>{BOT_NAME}</b>'

    def _sz_ok(fp):
        try:
            return os.path.getsize(fp) <= 49 * 1024 * 1024
        except Exception:
            return False

    if kind == "audio" and paths:
        if not _sz_ok(paths[0]):
            raise RuntimeError("file too large")
        with open(paths[0], "rb") as f:
            bot.send_audio(chat_id, f, caption=cap + "\n🎧 Audio")
        return
    if kind == "photos" and paths:
        paths = [p for p in paths if _sz_ok(p)]
        if not paths:
            raise RuntimeError("file too large")
        if len(paths) == 1:
            with open(paths[0], "rb") as f:
                bot.send_photo(chat_id, f, caption=cap + "\n🖼 Photo")
        else:
            media, files = [], []
            try:
                for i, pth in enumerate(paths[:10]):
                    fh = open(pth, "rb")
                    files.append(fh)
                    if i == 0:
                        media.append(types.InputMediaPhoto(fh, caption=cap + f"\n🖼 Slideshow · {len(paths)} រូប", parse_mode="HTML"))
                    else:
                        media.append(types.InputMediaPhoto(fh))
                bot.send_media_group(chat_id, media)
            finally:
                for fh in files:
                    try:
                        fh.close()
                    except Exception:
                        pass
        return
    if kind == "live":
        if paths and _sz_ok(paths[0]):
            with open(paths[0], "rb") as f:
                bot.send_photo(chat_id, f, caption=cap + "\n📷 Live Photo · រូប")
        if live_path and os.path.isfile(live_path) and _sz_ok(live_path):
            with open(live_path, "rb") as f:
                bot.send_video(chat_id, f, caption=cap + "\n✨ Live · ចលនា", supports_streaming=True)
        elif not paths:
            raise RuntimeError("live media missing")
        return
    vpath = live_path if live_path and os.path.isfile(live_path) else (paths[0] if paths else None)
    if not vpath or not _sz_ok(vpath):
        raise RuntimeError("no file or too large")
    with open(vpath, "rb") as f:
        bot.send_video(chat_id, f, caption=cap + "\n🎬 Video · MP4", supports_streaming=True)


def download_media(url: str, out_dir: str, audio_only: bool = False, photo_mode: bool = False) -> tuple:
    """Return (result, error). result = filepath str OR dict {kind, paths, live_path?}."""
    out_tmpl = os.path.join(out_dir, "dl.%(ext)s")
    ua_mobile = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    )
    ua_desk = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
    errs = []

    # TikTok: tikwm first (avoids yt-dlp player_response failures)
    if _is_tiktok(url):
        path, err = _download_tikwm(url, out_dir, audio_only=audio_only, photo_mode=photo_mode)
        if path:
            return path, ""
        errs.append(err or "tikwm failed")

    # Direct image URL
    lowu = url.lower().split("?")[0]
    if photo_mode or lowu.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
        try:
            import urllib.request
            ext = "jpg"
            for e_ in ("png", "webp", "gif", "jpeg", "jpg"):
                if lowu.endswith("." + e_):
                    ext = "jpg" if e_ == "jpeg" else e_
                    break
            dest = os.path.join(out_dir, "direct_img." + ext)
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
                f.write(r.read())
            if os.path.getsize(dest) > 500:
                return {"kind": "photos", "paths": [dest]}, ""
        except Exception as e:
            errs.append("direct image: %s" % e)

    attempts = []
    if audio_only:
        attempts.append(
            _ytdlp_base(url) + [
                "-o", out_tmpl, "-x", "--audio-format", "mp3", "--audio-quality", "0",
                "--prefer-ffmpeg", url,
            ]
        )
        attempts.append(
            _ytdlp_base(url) + ["-o", out_tmpl, "-f", "bestaudio/best", "-x", "--audio-format", "mp3", url]
        )
        if _is_tiktok(url):
            b = _ytdlp_base(url) + ["--user-agent", ua_mobile, "--add-header", "Referer:https://www.tiktok.com/"]
            attempts.append(b + ["-o", out_tmpl, "-x", "--audio-format", "mp3", "-f", "bestaudio/best", url])
    elif _is_youtube(url):
        fmt = "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]/b[height<=720]/b"
        attempts.append(_ytdlp_base(url) + ["-o", out_tmpl, "-f", fmt, "--merge-output-format", "mp4", url])
        attempts.append(
            _ytdlp_base(url, use_cookies=False)
            + ["--extractor-args", "youtube:player_client=android_vr",
               "-o", out_tmpl, "-f", "best[height<=720]/best", "--merge-output-format", "mp4", url]
        )
    elif _is_tiktok(url):
        b = _ytdlp_base(url) + ["--user-agent", ua_mobile, "--add-header", "Referer:https://www.tiktok.com/"]
        attempts.append(
            b + ["--extractor-args", "tiktok:api_hostname=api16-normal-c-useast1a.tiktokv.com",
                 "-o", out_tmpl, "-f", "best", url]
        )
        attempts.append(
            b + ["--extractor-args", "tiktok:webpage_download=True", "-o", out_tmpl, "-f", "best", url]
        )
        attempts.append(_ytdlp_base(url) + ["-o", out_tmpl, "-f", "best", url])
    elif _is_facebook(url):
        # Facebook pfbid / watch often 404 — try mobile + desktop clients
        for ua in (ua_mobile, ua_desk):
            b = _ytdlp_base(url) + ["--user-agent", ua, "--add-header", "Referer:https://www.facebook.com/"]
            attempts.append(
                b + ["-o", out_tmpl, "-f", "best[height<=720]/best", "--merge-output-format", "mp4", url]
            )
        # cookies help private/login walls
        attempts.append(
            _ytdlp_base(url) + [
                "--user-agent", ua_desk,
                "-o", out_tmpl, "-f", "best", "--merge-output-format", "mp4", url,
            ]
        )
    else:
        attempts.append(
            _ytdlp_base(url)
            + ["-o", out_tmpl, "-f", "bv*[height<=720]+ba/b[height<=720]/best",
               "--merge-output-format", "mp4", url]
        )
        attempts.append(_ytdlp_base(url) + ["-o", out_tmpl, "-f", "best[filesize<49M]/best", url])

    for cmd in attempts:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except FileNotFoundError:
            return None, "yt-dlp មិនមានលើ server — deploy ឡើងវិញ"
        except subprocess.TimeoutExpired:
            errs.append("timeout")
            continue
        except Exception as e:
            errs.append(str(e))
            continue
        if r.returncode == 0:
            path = _find_media(out_dir)
            if path:
                if audio_only and path.lower().endswith((".mp4", ".mkv", ".webm", ".mov")):
                    out_a = os.path.join(out_dir, "audio_extract.mp3")
                    try:
                        rr = subprocess.run(
                            ["ffmpeg", "-y", "-i", path, "-vn", "-acodec", "libmp3lame", "-q:a", "2", out_a],
                            capture_output=True, text=True, timeout=120,
                        )
                        if rr.returncode == 0 and os.path.isfile(out_a) and os.path.getsize(out_a) > 500:
                            return out_a, ""
                    except Exception as e:
                        errs.append("ffmpeg: %s" % e)
                else:
                    return path, ""
        friendly = _friendly_dl_error((r.stderr or "") + (r.stdout or ""), url)
        errs.append(friendly)

    if audio_only and _is_tiktok(url):
        path, err = _download_tikwm(url, out_dir, audio_only=False)
        if path:
            out_a = os.path.join(out_dir, "from_video.mp3")
            try:
                rr = subprocess.run(
                    ["ffmpeg", "-y", "-i", path, "-vn", "-acodec", "libmp3lame", "-q:a", "2", out_a],
                    capture_output=True, text=True, timeout=120,
                )
                if rr.returncode == 0 and os.path.isfile(out_a) and os.path.getsize(out_a) > 500:
                    return out_a, ""
            except Exception as e:
                errs.append(str(e))
        if err:
            errs.append(err)

    # unique short errors
    seen = []
    for e in errs:
        e = (e or "").strip()
        if e and e not in seen:
            seen.append(e)
    return None, " | ".join(seen[:3]) or "download failed"


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



@bot.message_handler(content_types=["photo"])
def on_photo(message):
    uid = message.from_user.id
    track_user(uid)
    st = waiting.get(uid) or {}
    if st.get("mode") != "admin_banner" or not is_admin(uid):
        return
    try:
        # largest size
        ph = message.photo[-1]
        file_id = ph.file_id
        set_welcome_banner(file_id=file_id, url="")
        waiting.pop(uid, None)
        bot.send_photo(
            message.chat.id,
            file_id,
            caption=f'{e("ok")} <b>Welcome banner</b> រក្សាទុកហើយ\n'
            f'សាក /start ដើម្បីមើល',
            reply_markup=main_kb(uid),
        )
    except Exception as ex:
        bot.reply_to(message, f'{e("fail")} {ex}')


@bot.message_handler(func=lambda m: True, content_types=["text"])
def on_text(message):
    try:
        _on_text_inner(message)
    except Exception as ex:
        log.exception("on_text")
        try:
            bot.reply_to(message, f"មានបញ្ហា: {ex}\nសូម /start រួចផ្ញើ link ម្តងទៀត")
        except Exception:
            pass


def _on_text_inner(message):
    uid = message.from_user.id
    text = (message.text or "").strip()
    if not text:
        return
    if text.startswith("/"):
        return
    track_user(uid)
    st = waiting.get(uid) or {}

    # Admin set banner via URL
    if st.get("mode") == "admin_banner" and is_admin(uid):
        if text.lower() in ("clear", "លុប"):
            set_welcome_banner("", "")
            waiting.pop(uid, None)
            bot.reply_to(message, f'{e("ok")} លុប banner', reply_markup=main_kb(uid))
            return
        if text.startswith("http://") or text.startswith("https://"):
            set_welcome_banner(file_id="", url=text.strip())
            waiting.pop(uid, None)
            try:
                bot.send_photo(
                    message.chat.id,
                    text.strip(),
                    caption=f'{e("ok")} Banner URL រក្សាទុកហើយ\nសាក /start',
                    reply_markup=main_kb(uid),
                )
            except Exception as ex:
                bot.reply_to(message, f'{e("ok")} រក្សា URL ហើយ (preview បរាជ័យ: {ex})', reply_markup=main_kb(uid))
            return
        bot.reply_to(message, "ផ្ញើ <b>រូប</b> ឬ <b>URL</b> មក")
        return

    # Admin broadcast
    if st.get("mode") == "admin_broadcast" and is_admin(uid):
        users = _load_json(USERS_FILE, []) or []
        ok = fail = 0
        status = bot.reply_to(message, f'{e("broadcast")} កំពុងផ្ញើទៅ {len(users)} នាក់…')
        for u in users:
            try:
                bot.send_message(int(u), text, parse_mode="HTML")
                ok += 1
            except Exception:
                fail += 1
        waiting.pop(uid, None)
        try:
            bot.edit_message_text(
                f'{e("ok")} Broadcast រួច: success={ok} fail={fail}',
                message.chat.id,
                status.message_id,
            )
        except Exception:
            bot.reply_to(message, f'{e("ok")} success={ok} fail={fail}')
        return

    # Admin emoji setup — auto-capture premium emoji from message
    if st.get("mode") == "admin_emoji" and is_admin(uid):
        key = (st.get("key") or "").strip().lower()
        custom = _load_json(EMOJI_FILE, {})
        if not isinstance(custom, dict):
            custom = {}

        # clear
        if text.lower() in ("clear", "លុប", "reset"):
            if key:
                custom.pop(key, None)
                _save_json(EMOJI_FILE, custom)
                bot.reply_to(
                    message,
                    f'{e("ok")} លុប <code>{key}</code> → default {DEFAULT_EMOJI.get(key)}',
                    reply_markup=emoji_after_save_kb(key),
                )
            else:
                bot.reply_to(message, "មិនមាន key")
            return

        # optional legacy: key = value
        if "=" in text and not extract_premium_emoji(message):
            k, _, val = text.partition("=")
            k, val = k.strip().lower(), val.strip()
            if k in DEFAULT_EMOJI:
                key = k
                if val:
                    custom[key] = val
                else:
                    custom.pop(key, None)
                _save_json(EMOJI_FILE, custom)
                bot.reply_to(message, f'{e("ok")} save <code>{key}</code>\nឥឡូវ: {e(key)}')
                waiting.pop(uid, None)
                return

        if not key or key not in DEFAULT_EMOJI:
            bot.reply_to(message, "សូមជ្រើស key ពី Admin → Setup emoji សិន")
            return

        eid = extract_premium_emoji(message)
        if eid:
            custom[key] = eid
            _save_json(EMOJI_FILE, custom)
            waiting.pop(uid, None)
            label = EMOJI_LABELS.get(key, key)
            bot.reply_to(
                message,
                f'{e("ok")} <b>ចាប់ premium auto!</b>\n'
                f'━━━━━━━━━━━━━━━━\n'
                f'{e(key)} <b>{label}</b> <code>{key}</code>\n'
                f'id: <code>{eid}</code>',
                reply_markup=emoji_after_save_kb(key),
            )
            return

        # plain unicode emoji / text as fallback
        val = text.strip()
        if val and len(val) <= 8:
            custom[key] = val
            _save_json(EMOJI_FILE, custom)
            waiting.pop(uid, None)
            label = EMOJI_LABELS.get(key, key)
            bot.reply_to(
                message,
                f'{e("ok")} រក្សាទុក\n'
                f'{e(key)} <b>{label}</b> = {val}',
                reply_markup=emoji_after_save_kb(key),
            )
            return

        bot.reply_to(
            message,
            f'{e("fail")} មិនឃើញ emoji\n\n'
            f'សូមផ្ញើ <b>emoji premium</b> មួយសារ\n'
            f'Bot នឹងចាប់ id ស្វ័យប្រវត្តិ',
        )
        return

    # URL → choose Video / Audio
    m = URL_RE.search(text)
    if m and st.get("mode") != "tts":
        if not rate_ok(uid, 5):
            bot.reply_to(message, f'{e("wait")} រង់ចាំបន្តិច…')
            return
        url = m.group(0).rstrip(").,]}>\"'")
        # Legacy modes still work
        if st.get("mode") == "dl_audio":
            audio_only = True
            status = bot.reply_to(message, f'{e("wait")} កំពុងទាញសំឡេង…')
            tmp = tempfile.mkdtemp(prefix="teanhsor_")
            try:
                path, err = download_media(url, tmp, audio_only=True)
                if not path:
                    try:
                        bot.edit_message_text(f'{e_plain("fail")} ទាញមិនបាន:\n<code>{_esc((err or "")[:300])}</code>', message.chat.id, status.message_id)
                    except Exception:
                        bot.reply_to(message, f'ទាញមិនបាន: {(err or "")[:200]}')
                    return
                waiting.pop(uid, None)
                try:
                    _send_dl_result(message.chat.id, _normalize_dl_result(path))
                except Exception as send_ex:
                    bot.reply_to(message, f'ទាញមិនបាន: {send_ex}')
                    return
                try:
                    bot.delete_message(message.chat.id, status.message_id)
                except Exception:
                    pass
            except Exception as ex:
                bot.reply_to(message, f'{e("fail")} {ex}')
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            return
        if st.get("mode") == "dl":
            status = bot.reply_to(message, f'{e("wait")} កំពុងទាញវីដេអូ…')
            tmp = tempfile.mkdtemp(prefix="teanhsor_")
            try:
                path, err = download_media(url, tmp, audio_only=False)
                if not path:
                    try:
                        bot.edit_message_text(f'{e_plain("fail")} ទាញមិនបាន:\n<code>{_esc((err or "")[:300])}</code>', message.chat.id, status.message_id)
                    except Exception:
                        bot.reply_to(message, f'ទាញមិនបាន: {(err or "")[:200]}')
                    return
                waiting.pop(uid, None)
                try:
                    _send_dl_result(message.chat.id, _normalize_dl_result(path))
                except Exception as send_ex:
                    bot.reply_to(message, f'ទាញមិនបាន: {send_ex}')
                    return
                try:
                    bot.delete_message(message.chat.id, status.message_id)
                except Exception:
                    pass
            except Exception as ex:
                bot.reply_to(message, f'{e("fail")} {ex}')
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            return

        # Default: ask Video / Audio / Photo
        waiting[uid] = {"mode": "dl_choose", "url": url}
        short = _esc(url[:72] + ("…" if len(url) > 72 else ""))
        body = (
            f"{e_plain('choice')} <b>ជ្រើសទម្រង់</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"<code>{short}</code>\n\n"
            f"🎬 <b>Video</b> — វីដេអូ MP4\n"
            f"🎧 <b>Audio</b> — សំឡេង MP3\n"
            f"🖼 <b>Photo / Live</b> — រូប / Live Photo\n\n"
            f"<i>បន្ទាប់ពី Video/រូប អាចជ្រើសទាញ Audio បន្ត</i>"
        )
        plain = (
            f"ជ្រើសទម្រង់\n{url[:80]}\n\n"
            f"Video / Audio / Photo"
        )
        safe_send(message.chat.id, body, reply_markup=format_choice_kb(), plain_fallback=plain)
        return

    # TTS mode
    if st.get("mode") == "tts" or text.startswith("tts:"):
        if not rate_ok(uid, 2):
            bot.reply_to(message, f'{e("wait")} រង់ចាំបន្តិច…')
            return
        voice = st.get("voice") or "female"
        if text.startswith("tts:"):
            text = text[4:].strip()
        status = bot.reply_to(message, f'{e("tts")} កំពុងបង្កើតសំឡេង…')
        tmp = tempfile.mkdtemp(prefix="teanhsor_tts_")
        out = os.path.join(tmp, "speech.mp3")
        try:
            err = run_tts(text, voice, out)
            if err:
                bot.edit_message_text(f'{e("fail")} {err}', message.chat.id, status.message_id)
                return
            label = "ប្រុស · Piseth" if voice == "male" else "ស្រី · Sreymom"
            with open(out, "rb") as f:
                bot.send_audio(
                    message.chat.id,
                    f,
                    caption=f'{e("tts")} <b>{BOT_NAME}</b>\nសំឡេង: {label}\n\n{text[:120]}',
                    title="Techsor TTS",
                    performer=label,
                )
            try:
                bot.delete_message(message.chat.id, status.message_id)
            except Exception:
                pass
        except Exception as ex:
            log.exception("tts")
            try:
                bot.edit_message_text(f'{e("fail")} {ex}', message.chat.id, status.message_id)
            except Exception:
                bot.reply_to(message, f'{e("fail")} {ex}')
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return

    if m:
        waiting[uid] = {"mode": "dl_choose", "url": m.group(0).rstrip(").,]}>\"'")}
        safe_send(
            message.chat.id,
            f"{e_plain('choice')} <b>ជ្រើសទម្រង់</b>\n🎬 Video · 🎧 Audio · 🖼 Photo",
            reply_markup=format_choice_kb(),
            plain_fallback="ជ្រើសទម្រង់: Video / Audio / Photo",
        )
        return

    bot.send_message(
        message.chat.id,
        card(
            f'{e("help")} តើចង់ធ្វើអ្វី?',
            f"• ផ្ញើ <b>link</b> → ជ្រើស Video / Audio / Photo\n"
            f"• ចុច {e('tts')} <b>TTS ខ្មែរ</b> → បញ្ចូលអត្ថបទ\n• ចុច {e('ocr')} <b>ចម្លងអត្ថបទ</b> → ផ្ញើរូប",
        ),
        reply_markup=main_kb(uid),
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
