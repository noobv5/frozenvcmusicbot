import os
import re
import sys
import time
import uuid
import json
import random
import logging
import tempfile
import threading
import subprocess
import psutil
from io import BytesIO
from datetime import datetime, timezone, timedelta
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import quote, urljoin
import aiohttp
import aiofiles
import asyncio
import requests
import isodate
import psutil
import pymongo
from pymongo import MongoClient, ASCENDING
from bson import ObjectId
from bson.binary import Binary
from dotenv import load_dotenv
from flask import Flask, request
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from pyrogram import Client, filters, errors
from pyrogram.enums import ChatType, ChatMemberStatus, ParseMode
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    ChatPermissions,
)
from pyrogram.errors import RPCError
from pytgcalls import PyTgCalls, idle
from pytgcalls.types import MediaStream
from pytgcalls import filters as fl
from pytgcalls.types import (
    ChatUpdate,
    UpdatedGroupCallParticipant,
    Update as TgUpdate,
)
from pytgcalls.types.stream import StreamEnded
from typing import Union
import urllib
from FrozenMusic.infra.concurrency.ci import deterministic_privilege_validator
from FrozenMusic.telegram_client.vector_transport import vector_transport_resolver
from FrozenMusic.infra.vector.yt_vector_orchestrator import yt_vector_orchestrator
from FrozenMusic.infra.vector.yt_backup_engine import yt_backup_engine
from FrozenMusic.infra.chrono.chrono_formatter import quantum_temporal_humanizer
from FrozenMusic.vector_text_tools import vectorized_unicode_boldifier

load_dotenv()


API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ASSISTANT_SESSION = os.environ.get("ASSISTANT_SESSION")
OWNER_ID = int(os.getenv("OWNER_ID", "6829790680")) # OWNER_ID আপডেট করা হয়েছে

# ——— Monkey-patch resolve_peer ——————————————
logging.getLogger("pyrogram").setLevel(logging.ERROR)
_original_resolve_peer = Client.resolve_peer
async def _safe_resolve_peer(self, peer_id):
    try:
        return await _original_resolve_peer(self, peer_id)
    except (KeyError, ValueError) as e:
        if "ID not found" in str(e) or "Peer id invalid" in str(e):
            return None
        raise
Client.resolve_peer = _safe_resolve_peer

# ——— Suppress un‐retrieved task warnings —————————
def _custom_exception_handler(loop, context):
    exc = context.get("exception")
    if isinstance(exc, (KeyError, ValueError)) and (
        "ID not found" in str(exc) or "Peer id invalid" in str(exc)
    ):
        return  

    if isinstance(exc, AttributeError) and "has no attribute 'write'" in str(exc):
        return

    loop.default_exception_handler(context)

asyncio.get_event_loop().set_exception_handler(_custom_exception_handler)

session_name = os.environ.get("SESSION_NAME", "music_bot1")
bot = Client(session_name, bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)
assistant = Client("assistant_account", session_string=ASSISTANT_SESSION)
call_py = PyTgCalls(assistant)


ASSISTANT_USERNAME = os.getenv("ASSISTANT_USERNAME")
ASSISTANT_CHAT_ID = os.getenv("ASSISTANT_CHAT_ID")
API_ASSISTANT_USERNAME = os.getenv("API_ASSISTANT_USERNAME")

if not ASSISTANT_USERNAME or not ASSISTANT_CHAT_ID or not API_ASSISTANT_USERNAME:
    print("অ্যাসিস্ট্যান্ট ইউজারনেম এবং চ্যাট আইডি সেট করা নেই") # Bengali: Assistant username and chat ID not set
else:
    # Convert chat ID to integer if needed
    try:
        ASSISTANT_CHAT_ID = int(ASSISTANT_CHAT_ID)
    except ValueError:
        print("অবৈধ ASSISTANT_CHAT_ID: পূর্ণসংখ্যা নয়") # Bengali: Invalid ASSISTANT_CHAT_ID: not an integer

# API Endpoints
API_URL = os.environ.get("API_URL")
DOWNLOAD_API_URL = os.environ.get("DOWNLOAD_API_URL")
BACKUP_SEARCH_API_URL= os.environ.get("BACKUP_SEARCH_API_URL")

# ─── MongoDB Setup ─────────────────────────────────────────
mongo_uri = os.environ.get("MongoDB_url")
mongo_client = MongoClient(mongo_uri)
db = mongo_client["music_bot"]


broadcast_collection  = db["broadcast"]


state_backup = db["state_backup"]


chat_containers = {}
playback_tasks = {}  
bot_start_time = time.time()
COOLDOWN = 10
chat_last_command = {}
chat_pending_commands = {}
QUEUE_LIMIT = 20
MAX_DURATION_SECONDS = 900  
LOCAL_VC_LIMIT = 10
playback_mode = {}



async def process_pending_command(chat_id, delay):
    await asyncio.sleep(delay)  
    if chat_id in chat_pending_commands:
        message, cooldown_reply = chat_pending_commands.pop(chat_id)
        await cooldown_reply.delete()  
        await play_handler(bot, message) 



async def skip_to_next_song(chat_id, message):
    """Skips to the next song in the queue and starts playback."""
    if chat_id not in chat_containers or not chat_containers[chat_id]:
        await message.edit("❌ কিউতে আর কোন গান নেই।") # Bengali: No more songs in the queue.
        await leave_voice_chat(chat_id)
        return

    await message.edit("⏭ পরবর্তী গানে স্কিপ করা হচ্ছে...") # Bengali: Skipping to the next song...

    # Pick next song from queue
    next_song_info = chat_containers[chat_id][0]
    try:
        await fallback_local_playback(chat_id, message, next_song_info)
    except Exception as e:
        print(f"পরবর্তী স্থানীয় প্লেব্যাক শুরু করতে ত্রুটি: {e}") # Bengali: Error starting next local playback: {e}
        await bot.send_message(chat_id, f"❌ পরবর্তী গান শুরু করতে ব্যর্থ: {e}") # Bengali: Failed to start next song: {e}



def safe_handler(func):
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            # Attempt to extract a chat ID (if available)
            chat_id = "Unknown"
            try:
                # If your function is a message handler, the second argument is typically the Message object.
                if len(args) >= 2:
                    chat_id = args[1].chat.id
                elif "message" in kwargs:
                    chat_id = kwargs["message"].chat.id
            except Exception:
                chat_id = "Unknown"
            error_text = (
                f"হ্যান্ডলার `{func.__name__}` এ ত্রুটি (চ্যাট আইডি: {chat_id}):\n\n{str(e)}" # Bengali: Error in handler `{func.__name__}` (chat id: {chat_id}):\n\n{str(e)}"
            )
            print(error_text)
            # Log the error to support
            await bot.send_message(6829790680, error_text) # OWNER_ID আপডেট করা হয়েছে
    return wrapper


async def extract_invite_link(client, chat_id):
    try:
        chat_info = await client.get_chat(chat_id)
        if chat_info.invite_link:
            return chat_info.invite_link
        elif chat_info.username:
            return f"https://t.me/{chat_info.username}"
        return None
    except ValueError as e:
        if "Peer id invalid" in str(e):
            print(f"চ্যাট {chat_id} এর জন্য অবৈধ পিয়ার আইডি। আমন্ত্রণ লিঙ্ক নিষ্কাশন এড়ানো হচ্ছে।") # Bengali: Invalid peer ID for chat {chat_id}. Skipping invite link extraction.
            return None
        else:
            raise e  # re-raise if it's another ValueError
    except Exception as e:
        print(f"চ্যাট {chat_id} এর জন্য আমন্ত্রণ লিঙ্ক নিষ্কাশন করতে ত্রুটি: {e}") # Bengali: Error extracting invite link for chat {chat_id}: {e}
        return None

async def extract_target_user(message: Message):
    # If the moderator replied to someone:
    if message.reply_to_message:
        return message.reply_to_message.from_user.id

    # Otherwise expect an argument like "/ban @user" or "/ban 123456"
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply("❌ আপনাকে একটি ব্যবহারকারীকে রিপ্লাই করতে হবে অথবা তাদের @ইউজারনেম/ইউজার_আইডি উল্লেখ করতে হবে।") # Bengali: You must reply to a user or specify their @username/user_id.
        return None

    target = parts[1]
    # Strip @
    if target.startswith("@"):
        target = target[1:]
    try:
        user = await message._client.get_users(target)
        return user.id
    except:
        await message.reply("❌ এই ব্যবহারকারীকে খুঁজে পাওয়া যায়নি।") # Bengali: Could not find that user.
        return None



async def is_assistant_in_chat(chat_id):
    try:
        member = await assistant.get_chat_member(chat_id, ASSISTANT_USERNAME)
        return member.status is not None
    except Exception as e:
        error_message = str(e)
        if "USER_BANNED" in error_message or "Banned" in error_message:
            return "banned"
        elif "USER_NOT_PARTICIPANT" in error_message or "Chat not found" in error_message:
            return False
        print(f"চ্যাটে অ্যাসিস্ট্যান্ট পরীক্ষা করতে ত্রুটি: {e}") # Bengali: Error checking assistant in chat: {e}
        return False

async def is_api_assistant_in_chat(chat_id):
    try:
        member = await bot.get_chat_member(chat_id, API_ASSISTANT_USERNAME)
        return member.status is not None
    except Exception as e:
        print(f"চ্যাটে API অ্যাসিস্ট্যান্ট পরীক্ষা করতে ত্রুটি: {e}") # Bengali: Error checking API assistant in chat: {e}
        return False
    
def iso8601_to_seconds(iso_duration):
    try:
        duration = isodate.parse_duration(iso_duration)
        return int(duration.total_seconds())
    except Exception as e:
        print(f"সময়কাল পার্স করতে ত্রুটি: {e}") # Bengali: Error parsing duration: {e}
        return 0


def iso8601_to_human_readable(iso_duration):
    try:
        duration = isodate.parse_duration(iso_duration)
        total_seconds = int(duration.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours > 0:
            return f"{hours}:{minutes:02}:{seconds:02}"
        return f"{minutes}:{seconds:02}"
    except Exception as e:
        return "অজানা সময়কাল" # Bengali: Unknown duration

async def fetch_youtube_link(query):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{API_URL}{query}") as response:
                if response.status == 200:
                    data = await response.json()
                    # Check if the API response contains a playlist
                    if "playlist" in data:
                        return data
                    else:
                        return (
                            data.get("link"),
                            data.get("title"),
                            data.get("duration"),
                            data.get("thumbnail")
                        )
                else:
                    raise Exception(f"API স্ট্যাটাস কোড {response.status} ফেরত দিয়েছে") # Bengali: API returned status code {response.status}
    except Exception as e:
        raise Exception(f"ইউটিউব লিঙ্ক আনতে ব্যর্থ: {str(e)}") # Bengali: Failed to fetch YouTube link: {str(e)}


    
async def fetch_youtube_link_backup(query):
    if not BACKUP_SEARCH_API_URL:
        raise Exception("ব্যাকআপ সার্চ API URL কনফিগার করা হয়নি") # Bengali: Backup Search API URL not configured
    # Build the correct URL:
    backup_url = (
        f"{BACKUP_SEARCH_API_URL.rstrip('/')}"
        f"/search?title={urllib.parse.quote(query)}"
    )
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(backup_url, timeout=30) as resp:
                if resp.status != 200:
                    raise Exception(f"ব্যাকআপ API স্ট্যাটাস {resp.status} ফেরত দিয়েছে") # Bengali: Backup API returned status {resp.status}
                data = await resp.json()
                # Mirror primary API’s return:
                if "playlist" in data:
                    return data
                return (
                    data.get("link"),
                    data.get("title"),
                    data.get("duration"),
                    data.get("thumbnail")
                )
    except Exception as e:
        raise Exception(f"ব্যাকআপ সার্চ API ত্রুটি: {e}") # Bengali: Backup Search API error: {e}
    
BOT_NAME = os.environ.get("BOT_NAME", "আপনার বট") # Updated BOT_NAME to a generic "আপনার বট"
BOT_LINK = os.environ.get("BOT_LINK", "https://t.me/YOUV2PLAYBOT") # Updated BOT_LINK

from pyrogram.errors import UserAlreadyParticipant, RPCError

async def invite_assistant(chat_id, invite_link, processing_message):
    """
    Internally invite the assistant to the chat by using the assistant client to join the chat.
    If the assistant is already in the chat, treat as success.
    On other errors, display and return False.
    """
    try:
        # Attempt to join via invite link
        await assistant.join_chat(invite_link)
        return True

    except UserAlreadyParticipant:
        # Assistant is already in the chat, no further action needed
        return True

    except RPCError as e:
        # Handle other Pyrogram RPC errors
        error_message = f"❌ অ্যাসিস্ট্যান্ট আমন্ত্রণ করার সময় ত্রুটি: টেলিগ্রাম বলছে: {e.code} {e.error_message}" # Bengali: Error while inviting assistant: Telegram says:
        await processing_message.edit(error_message)
        return False

    except Exception as e:
        # Catch-all for any unexpected exceptions
        error_message = f"❌ অ্যাসিস্ট্যান্ট আমন্ত্রণ করার সময় অপ্রত্যাশিত ত্রুটি: {str(e)}" # Bengali: Unexpected error while inviting assistant:
        await processing_message.edit(error_message)
        return False


# Helper to convert ASCII letters to Unicode bold
def to_bold_unicode(text: str) -> str:
    bold_text = ""
    for char in text:
        if 'A' <= char <= 'Z':
            bold_text += chr(ord('𝗔') + (ord(char) - ord('A')))
        elif 'a' <= char <= 'z':
            bold_text += chr(ord('𝗮') + (ord(char) - ord('a')))
        else:
            bold_text += char
    return bold_text

@bot.on_message(filters.command("start"))
async def start_handler(_, message):
    user_id = message.from_user.id
    raw_name = message.from_user.first_name or ""
    styled_name = to_bold_unicode(raw_name)
    user_link = f"[{styled_name}](tg://user?id={user_id})"

    add_me_text = to_bold_unicode("আমাকে যুক্ত করুন") # Bengali: Add Me
    updates_text = to_bold_unicode("আপডেটসমূহ") # Bengali: Updates
    support_text = to_bold_unicode("সহায়তা") # Bengali: Support
    help_text = to_bold_unicode("সাহায্য") # Bengali: Help

    caption = (
        f"👋 হে {user_link} 💠, 🥀\n\n"
        f">🎶 {BOT_NAME.upper()} এ স্বাগতম! 🎵\n" # Bengali: WELCOME TO YOUR BOT!
        f">🚀 সেরা ২৪x৭ আপটাইম ও সহায়তা\n" # Bengali: TOP-NOTCH 24x7 UPTIME & SUPPORT
        f">🔊 ক্রিস্টাল-ক্লিয়ার অডিও\n" # Bengali: CRYSTAL-CLEAR AUDIO
        f">🎧 সমর্থিত প্ল্যাটফর্ম: YouTube | Spotify | Resso | Apple Music | SoundCloud\n" # Bengali: SUPPORTED PLATFORMS
        f">✨ কিউ শেষ হলে স্বয়ংক্রিয় পরামর্শ\n" # Bengali: AUTO-SUGGESTIONS when queue ends
        f">🛠️ অ্যাডমিন কমান্ড: পজ, রেজ্যুম, স্কিপ, স্টপ, মিউট, আনমিউট, টিমিউট, কিক, ব্যান, আনব্যান, কাপল\n" # Bengali: ADMIN COMMANDS
        f">❤️ কাপল সাজেশন (গ্রুপে র্যান্ডম জোড়া নির্বাচন)\n" # Bengali: COUPLE SUGGESTION (pick random pair in group)
        f"๏ কমান্ড তালিকা দেখতে নিচে {help_text} ক্লিক করুন।" # Bengali: Click Help below for command list.
    )

    buttons = [
        [
            InlineKeyboardButton(f"➕ {add_me_text}", url=f"{BOT_LINK}?startgroup=true"), # BOT_LINK আপডেট করা হয়েছে
            InlineKeyboardButton(f"📢 {updates_text}", url="https://t.me/+yxoojuaOI0g4MWNl") # Updates link আপডেট করা হয়েছে
        ],
        [
            InlineKeyboardButton(f"💬 {support_text}", url="https://t.me/YOUV1"), # Support link আপডেট করা হয়েছে
            InlineKeyboardButton(f"❓ {help_text}", callback_data="show_help")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(buttons)

    await message.reply_animation(
        animation="https://frozen-imageapi.lagendplayersyt.workers.dev/file/2e483e17-05cb-45e2-b166-1ea476ce9521.mp4",
        caption=caption,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup
    )

    # Register chat ID for broadcasting silently
    chat_id = message.chat.id
    chat_type = message.chat.type
    if chat_type == ChatType.PRIVATE:
        if not broadcast_collection.find_one({"chat_id": chat_id}):
            broadcast_collection.insert_one({"chat_id": chat_id, "type": "private"})
    elif chat_type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        if not broadcast_collection.find_one({"chat_id": chat_id}):
            broadcast_collection.insert_one({"chat_id": chat_id, "type": "group"})



@bot.on_callback_query(filters.regex("^go_back$"))
async def go_back_callback(_, callback_query):
    user_id = callback_query.from_user.id
    raw_name = callback_query.from_user.first_name or ""
    styled_name = to_bold_unicode(raw_name)
    user_link = f"[{styled_name}](tg://user?id={user_id})"

    add_me_text = to_bold_unicode("আমাকে যুক্ত করুন") # Bengali: Add Me
    updates_text = to_bold_unicode("আপডেটসমূহ") # Bengali: Updates
    support_text = to_bold_unicode("সহায়তা") # Bengali: Support
    help_text = to_bold_unicode("সাহায্য") # Bengali: Help

    caption = (
        f"👋 হে {user_link} 💠, 🥀\n\n"
        f">🎶 {BOT_NAME.upper()} এ স্বাগতম! 🎵\n" # Bengali: WELCOME TO YOUR BOT!
        f">🚀 সেরা ২৪x৭ আপটাইম ও সহায়তা\n" # Bengali: TOP-NOTCH 24x7 UPTIME & SUPPORT
        f">🔊 ক্রিস্টাল-ক্লিয়ার অডিও\n" # Bengali: CRYSTAL-CLEAR AUDIO
        f">🎧 সমর্থিত প্ল্যাটফর্ম: YouTube | Spotify | Resso | Apple Music | SoundCloud\n" # Bengali: SUPPORTED PLATFORMS
        f">✨ কিউ শেষ হলে স্বয়ংক্রিয় পরামর্শ\n" # Bengali: AUTO-SUGGESTIONS when queue ends
        f">🛠️ অ্যাডমিন কমান্ড: পজ, রেজ্যুম, স্কিপ, স্টপ, মিউট, আনমিউট, টিমিউট, কিক, ব্যান, আনব্যান, কাপল\n" # Bengali: ADMIN COMMANDS
        f">❤️ কাপল (গ্রুপে র্যান্ডম জোড়া নির্বাচন)\n" # Bengali: COUPLE (pick random pair in group)
        f"๏ কমান্ড তালিকা দেখতে নিচে {help_text} ক্লিক করুন।" # Bengali: Click Help below for command list.
    )

    buttons = [
        [
            InlineKeyboardButton(f"➕ {add_me_text}", url=f"{BOT_LINK}?startgroup=true"), # BOT_LINK আপডেট করা হয়েছে
            InlineKeyboardButton(f"📢 {updates_text}", url="https://t.me/+yxoojuaOI0g4MWNl") # Updates link আপডেট করা হয়েছে
        ],
        [
            InlineKeyboardButton(f"💬 {support_text}", url="https://t.me/YOUV1"), # Support link আপডেট করা হয়েছে
            InlineKeyboardButton(f"❓ {help_text}", callback_data="show_help")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(buttons)

    await callback_query.message.edit_caption(
        caption=caption,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup
    )



@bot.on_callback_query(filters.regex("^show_help$"))
async def show_help_callback(_, callback_query):
    help_text = ">📜 *কমান্ডগুলি অন্বেষণ করতে একটি বিভাগ নির্বাচন করুন:*" # Bengali: Choose a category to explore commands:
    buttons = [
        [
            InlineKeyboardButton("🎵 সঙ্গীত নিয়ন্ত্রণ", callback_data="help_music"), # Bengali: Music Controls
            InlineKeyboardButton("🛡️ অ্যাডমিন টুলস", callback_data="help_admin") # Bengali: Admin Tools
        ],
        [
            InlineKeyboardButton("❤️ কাপল সাজেশন", callback_data="help_couple"), # Bengali: Couple Suggestion
            InlineKeyboardButton("🔍 ইউটিলিটি", callback_data="help_util") # Bengali: Utility
        ],
        [
            InlineKeyboardButton("🏠 হোম", callback_data="go_back") # Bengali: Home
        ]
    ]
    reply_markup = InlineKeyboardMarkup(buttons)
    await callback_query.message.edit_text(help_text, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)


@bot.on_callback_query(filters.regex("^help_music$"))
async def help_music_callback(_, callback_query):
    text = (
        ">🎵 *সঙ্গীত ও প্লেব্যাক কমান্ড*\n\n" # Bengali: Music & Playback Commands
        ">➜ `/play <গানের নাম বা URL>`\n" # Bengali: song name or URL
        "   • একটি গান চালান (YouTube/Spotify/Resso/Apple Music/SoundCloud)।\n" # Bengali: Play a song
        "   • যদি একটি অডিও/ভিডিওতে রিপ্লাই করা হয়, তবে এটি সরাসরি প্লে করে।\n\n" # Bengali: If replied to an audio/video, plays it directly.
        ">➜ `/playlist`\n"
        "   • আপনার সংরক্ষিত প্লেলিস্ট দেখুন বা পরিচালনা করুন।\n\n" # Bengali: View or manage your saved playlist.
        ">➜ `/skip`\n"
        "   • বর্তমানে বাজানো গানটি এড়িয়ে যান। (শুধুমাত্র অ্যাডমিন)\n\n" # Bengali: Skip the currently playing song. (Admins only)
        ">➜ `/pause`\n"
        "   • বর্তমান স্ট্রিম থামান। (শুধুমাত্র অ্যাডমিন)\n\n" # Bengali: Pause the current stream. (Admins only)
        ">➜ `/resume`\n"
        "   • একটি থামা স্ট্রিম আবার শুরু করুন। (শুধুমাত্র অ্যাডমিন)\n\n" # Bengali: Resume a paused stream. (Admins only)
        ">➜ `/stop` অথবা `/end`\n"
        "   • প্লেব্যাক বন্ধ করুন এবং কিউ পরিষ্কার করুন। (শুধুমাত্র অ্যাডমিন)" # Bengali: Stop playback and clear the queue. (Admins only)
    )
    buttons = [[InlineKeyboardButton("🔙 ফিরে যান", callback_data="show_help")]] # Bengali: Back
    await callback_query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))


@bot.on_callback_query(filters.regex("^help_admin$"))
async def help_admin_callback(_, callback_query):
    text = (
        "🛡️ *অ্যাডমিন ও মডারেশন কমান্ড*\n\n" # Bengali: Admin & Moderation Commands
        ">➜ `/mute @user`\n"
        "   • একজন ব্যবহারকারীকে অনির্দিষ্টকালের জন্য মিউট করুন। (শুধুমাত্র অ্যাডমিন)\n\n" # Bengali: Mute a user indefinitely. (Admins only)
        ">➜ `/unmute @user`\n"
        "   • পূর্বে মিউট করা ব্যবহারকারীকে আনমিউট করুন। (শুধুমাত্র অ্যাডমিন)\n\n" # Bengali: Unmute a previously muted user. (Admins only)
        ">➜ `/tmute @user <মিনিট>`\n" # Bengali: minutes
        "   • একটি নির্দিষ্ট সময়ের জন্য অস্থায়ীভাবে মিউট করুন। (শুধুমাত্র অ্যাডমিন)\n\n" # Bengali: Temporarily mute for a set duration. (Admins only)
        ">➜ `/kick @user`\n"
        "   • একজন ব্যবহারকারীকে অবিলম্বে কিক করুন (ব্যান + আনব্যান)। (শুধুমাত্র অ্যাডমিন)\n\n" # Bengali: Kick (ban + unban) a user immediately. (Admins only)
        ">➜ `/ban @user`\n"
        "   • একজন ব্যবহারকারীকে ব্যান করুন। (শুধুমাত্র অ্যাডমিন)\n\n" # Bengali: Ban a user. (Admins only)
        ">➜ `/unban @user`\n"
        "   • পূর্বে ব্যান করা ব্যবহারকারীকে আনব্যান করুন। (শুধুমাত্র অ্যাডমিন)" # Bengali: Unban a previously banned user. (Admins only)
    )
    buttons = [[InlineKeyboardButton("🔙 ফিরে যান", callback_data="show_help")]] # Bengali: Back
    await callback_query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))


@bot.on_callback_query(filters.regex("^help_couple$"))
async def help_couple_callback(_, callback_query):
    text = (
        "❤️ *কাপল সাজেশন কমান্ড*\n\n" # Bengali: Couple Suggestion Command
        ">➜ `/couple`\n"
        "   • দুটি এলোমেলো নন-বট সদস্যকে নির্বাচন করে এবং তাদের নাম সহ একটি “কাপল” ছবি পোস্ট করে।\n" # Bengali: Picks two random non-bot members and posts a “couple” image with their names.
        "   • প্রতিদিন ক্যাশ করা হয় যাতে মধ্যরাত UTC পর্যন্ত একই জোড়া প্রদর্শিত হয়।\n" # Bengali: Caches daily so the same pair appears until midnight UTC.
        "   • দ্রুততার জন্য প্রতি-গ্রুপ সদস্য ক্যাশ ব্যবহার করে।" # Bengali: Uses per-group member cache for speed.
    )
    buttons = [[InlineKeyboardButton("🔙 ফিরে যান", callback_data="show_help")]] # Bengali: Back
    await callback_query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))


@bot.on_callback_query(filters.regex("^help_util$"))
async def help_util_callback(_, callback_query):
    text = (
        "🔍 *ইউটিলিটি ও অতিরিক্ত কমান্ড*\n\n" # Bengali: Utility & Extra Commands
        ">➜ `/ping`\n"
        "   • বটের প্রতিক্রিয়া সময় এবং আপটাইম পরীক্ষা করুন।\n\n" # Bengali: Check bot’s response time and uptime.
        ">➜ `/clear`\n"
        "   • সম্পূর্ণ কিউ পরিষ্কার করুন। (শুধুমাত্র অ্যাডমিন)\n\n" # Bengali: Clear the entire queue. (Admins only)
        ">➜ স্বয়ংক্রিয় পরামর্শ:\n" # Bengali: Auto-Suggestions:
        "   • যখন কিউ শেষ হয়, বট স্বয়ংক্রিয়ভাবে ইনলাইন বাটনগুলির মাধ্যমে নতুন গান সুপারিশ করে।\n\n" # Bengali: When the queue ends, the bot automatically suggests new songs via inline buttons.
        ">➜ *অডিও গুণমান ও সীমা*\n" # Bengali: Audio Quality & Limits
        "   • ২ ঘন্টা ১০ মিনিট পর্যন্ত স্ট্রিম করে, তবে দীর্ঘ সময়ের জন্য স্বয়ংক্রিয় ফলব্যাক। (দেখুন `MAX_DURATION_SECONDS`)\n" # Bengali: Streams up to 2 hours 10 minutes, but auto-fallback for longer. (See `MAX_DURATION_SECONDS`)
    )
    buttons = [[InlineKeyboardButton("🔙 ফিরে যান", callback_data="show_help")]] # Bengali: Back
    await callback_query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))


@bot.on_message(filters.group & filters.regex(r'^/play(?:@\w+)?(?:\s+(?P<query>.+))?$'))
async def play_handler(_, message: Message):
    chat_id = message.chat.id

    # If replying to an audio/video message, handle local playback
    if message.reply_to_message and (message.reply_to_message.audio or message.reply_to_message.video):
        processing_message = await message.reply("❄️")

        # Fetch fresh media reference and download
        orig = message.reply_to_message
        fresh = await bot.get_messages(orig.chat.id, orig.id)
        media = fresh.video or fresh.audio
        if fresh.audio and getattr(fresh.audio, 'file_size', 0) > 100 * 1024 * 1024:
            await processing_message.edit("❌ অডিও ফাইল খুব বড়। সর্বাধিক অনুমোদিত আকার ১০০MB।") # Bengali: Audio file too large. Maximum allowed size is 100MB.
            return

        await processing_message.edit("⏳ অনুগ্রহ করে অপেক্ষা করুন, অডিও ডাউনলোড হচ্ছে...") # Bengali: Please wait, downloading audio…
        try:
            file_path = await bot.download_media(media)
        except Exception as e:
            await processing_message.edit(f"❌ মিডিয়া ডাউনলোড করতে ব্যর্থ: {e}") # Bengali: Failed to download media: {e}
            return

        # Download thumbnail if available
        thumb_path = None
        try:
            thumbs = fresh.video.thumbs if fresh.video else fresh.audio.thumbs
            thumb_path = await bot.download_media(thumbs[0])
        except Exception:
            pass

        # Prepare song_info and fallback to local playback
        duration = media.duration or 0
        title = getattr(media, 'file_name', 'শিরোনামহীন') # Bengali: Untitled
        song_info = {
            'url': file_path,
            'title': title,
            'duration': format_time(duration),
            'duration_seconds': duration,
            'requester': message.from_user.first_name,
            'thumbnail': thumb_path
        }
        await fallback_local_playback(chat_id, processing_message, song_info)
        return

    # Otherwise, process query-based search
    match = message.matches[0]
    query = (match.group('query') or "").strip()

    try:
        await message.delete()
    except Exception:
        pass

    # Enforce cooldown
    now_ts = time.time()
    if chat_id in chat_last_command and (now_ts - chat_last_command[chat_id]) < COOLDOWN:
        remaining = int(COOLDOWN - (now_ts - chat_last_command[chat_id]))
        if chat_id in chat_pending_commands:
            await bot.send_message(chat_id, f"⏳ এই চ্যাটের জন্য একটি কমান্ড ইতিমধ্যেই কিউতে আছে। অনুগ্রহ করে {remaining} সেকেন্ড অপেক্ষা করুন।") # Bengali: A command is already queued for this chat. Please wait {remaining}s.
        else:
            cooldown_reply = await bot.send_message(chat_id, f"⏳ কুলডাউন। {remaining} সেকেন্ডে প্রক্রিয়া করা হচ্ছে।") # Bengali: On cooldown. Processing in {remaining}s.
            chat_pending_commands[chat_id] = (message, cooldown_reply)
            asyncio.create_task(process_pending_command(chat_id, remaining))
        return
    chat_last_command[chat_id] = now_ts

    if not query:
        await bot.send_message(
            chat_id,
            "❌ আপনি কোনো গান উল্লেখ করেননি।\n\n" # Bengali: You did not specify a song.
            "সঠিক ব্যবহার: /play <গানের নাম>\nউদাহরণ: /play shape of you" # Bengali: Correct usage: /play <song name>\nExample: /play shape of you
        )
        return

    # Delegate to query processor
    await process_play_command(message, query)



async def process_play_command(message: Message, query: str):
    chat_id = message.chat.id
    processing_message = await message.reply("❄️")

    # --- ensure assistant is in the chat before we queue/play anything ----
    status = await is_assistant_in_chat(chat_id)
    if status == "banned":
        await processing_message.edit("❌ অ্যাসিস্ট্যান্ট এই চ্যাট থেকে নিষিদ্ধ।") # Bengali: Assistant is banned from this chat.
        return
    if status is False:
        # try to fetch an invite link to add the assistant
        invite_link = await extract_invite_link(bot, chat_id)
        if not invite_link:
            await processing_message.edit("❌ অ্যাসিস্ট্যান্ট যুক্ত করার জন্য একটি আমন্ত্রণ লিঙ্ক পাওয়া যায়নি।") # Bengali: Could not obtain an invite link to add the assistant.
            return
        invited = await invite_assistant(chat_id, invite_link, processing_message)
        if not invited:
            # invite_assistant handles error editing
            return

    # Convert short URLs to full YouTube URLs
    if "youtu.be" in query:
        m = re.search(r"youtu\.be/([^?&]+)", query)
        if m:
            query = f"https://www.youtube.com/watch?v={m.group(1)}"

    # Perform Youtube and handle results
    try:
        result = await fetch_youtube_link(query)
    except Exception as primary_err:
        await processing_message.edit(
            "⚠️ প্রাথমিক অনুসন্ধান ব্যর্থ হয়েছে। ব্যাকআপ API ব্যবহার করা হচ্ছে, এতে কয়েক সেকেন্ড সময় লাগতে পারে..." # Bengali: Primary search failed. Using backup API, this may take a few seconds…
        )
        try:
            result = await fetch_youtube_link_backup(query)
        except Exception as backup_err:
            await processing_message.edit(
                f"❌ উভয় অনুসন্ধান API ব্যর্থ হয়েছে:\n" # Bengali: Both search APIs failed:
                f"প্রাথমিক: {primary_err}\n" # Bengali: Primary:
                f"ব্যাকআপ:  {backup_err}" # Bengali: Backup:
            )
            return

    # Handle playlist vs single video
    if isinstance(result, dict) and "playlist" in result:
        playlist_items = result["playlist"]
        if not playlist_items:
            await processing_message.edit("❌ প্লেলিস্টে কোনো ভিডিও পাওয়া যায়নি।") # Bengali: No videos found in the playlist.
            return

        chat_containers.setdefault(chat_id, [])
        for item in playlist_items:
            secs = isodate.parse_duration(item["duration"]).total_seconds()
            chat_containers[chat_id].append({
                "url": item["link"],
                "title": item["title"],
                "duration": iso8601_to_human_readable(item["duration"]),
                "duration_seconds": secs,
                "requester": message.from_user.first_name if message.from_user else "অজানা", # Bengali: Unknown
                "thumbnail": item["thumbnail"]
            })

        total = len(playlist_items)
        reply_text = (
            f"✨ প্লেলিস্টে যুক্ত করা হয়েছে\n" # Bengali: Added to playlist
            f"কিউতে যুক্ত করা গানের মোট সংখ্যা: {total}\n" # Bengali: Total songs added to queue:
            f"#1 - {playlist_items[0]['title']}"
        )
        if total > 1:
            reply_text += f"\n#2 - {playlist_items[1]['title']}"
        await message.reply(reply_text)

        # If first playlist song, start playback
        if len(chat_containers[chat_id]) == total:
            first_song_info = chat_containers[chat_id][0]
            await fallback_local_playback(chat_id, processing_message, first_song_info)
        else:
            await processing_message.delete()

    else:
        video_url, title, duration_iso, thumb = result
        if not video_url:
            await processing_message.edit(
                "❌ গানটি খুঁজে পাওয়া যায়নি। অন্য একটি ক্যোয়ারী চেষ্টা করুন।\nসহায়তা: @YOUV1" # Bengali: Could not find the song. Try another query.\nSupport: @YOUV1
            )
            return

        secs = isodate.parse_duration(duration_iso).total_seconds()
        if secs > MAX_DURATION_SECONDS:
            await processing_message.edit(
                "❌ ১৫ মিনিটের বেশি স্ট্রিম অনুমতি নেই। আপনি যদি এই বটের মালিক হন তবে আপনার প্ল্যান আপগ্রেড করতে @HACKER_ONWER এর সাথে যোগাযোগ করুন।" # Bengali: Streams longer than 15 min are not allowed. If u are the owner of this bot contact @HACKER_ONWER to upgrade your plan
            )
            return

        readable = iso8601_to_human_readable(duration_iso)
        chat_containers.setdefault(chat_id, [])
        chat_containers[chat_id].append({
            "url": video_url,
            "title": title,
            "duration": readable,
            "duration_seconds": secs,
            "requester": message.from_user.first_name if message.from_user else "অজানা", # Bengali: Unknown
            "thumbnail": thumb
        })

        # If it's the first song, start playback immediately using fallback
        if len(chat_containers[chat_id]) == 1:
            await fallback_local_playback(chat_id, processing_message, chat_containers[chat_id][0])
        else:
            queue_buttons = InlineKeyboardMarkup([
                [InlineKeyboardButton("⏭ স্কিপ", callback_data="skip"), # Bengali: Skip
                 InlineKeyboardButton("🗑 পরিষ্কার করুন", callback_data="clear")] # Bengali: Clear
            ])
            await message.reply(
                f"✨ কিউতে যুক্ত করা হয়েছে :\n\n" # Bengali: Added to queue :
                f"**❍ শিরোনাম ➥** {title}\n" # Bengali: Title
                f"**❍ সময় ➥** {readable}\n" # Bengali: Time
                f"**❍ দ্বারা ➥ ** {message.from_user.first_name if message.from_user else 'অজানা'}\n" # Bengali: By
                f"**কিউ নম্বর:** {len(chat_containers[chat_id]) - 1}", # Bengali: Queue number:
                reply_markup=queue_buttons
            )
            await processing_message.delete()


# ─── Utility functions ──────────────────────────────────────────────────────────────

MAX_TITLE_LEN = 20

def _one_line_title(full_title: str) -> str:
    """
    Truncate `full_title` to at most MAX_TITLE_LEN chars.
    If truncated, append “…” so it still reads cleanly in one line.
    """
    if len(full_title) <= MAX_TITLE_LEN:
        return full_title
    else:
        return full_title[: (MAX_TITLE_LEN - 1) ] + "…"  # one char saved for the ellipsis

def parse_duration_str(duration_str: str) -> int:
    """
    Convert a duration string to total seconds.
    First, try ISO 8601 parsing (e.g. "PT3M9S"). If that fails,
    fall back to colon-separated formats like "3:09" or "1:02:30".
    """
    try:
        duration = isodate.parse_duration(duration_str)
        return int(duration.total_seconds())
    except Exception as e:
        if ':' in duration_str:
            try:
                parts = [int(x) for x in duration_str.split(':')]
                if len(parts) == 2:
                    minutes, seconds = parts
                    return minutes * 60 + seconds
                elif len(parts) == 3:
                    hours, minutes, seconds = parts
                    return hours * 3600 + minutes * 60 + seconds
            except Exception as e2:
                print(f"কোলন-বিভক্ত সময়কাল '{duration_str}' পার্স করতে ত্রুটি: {e2}") # Bengali: Error parsing colon-separated duration '{duration_str}': {e2}
                return 0
        else:
            print(f"সময়কাল '{duration_str}' পার্স করতে ত্রুটি: {e}") # Bengali: Error parsing duration '{duration_str}': {e}
            return 0

def format_time(seconds: float) -> str:
    """
    Given total seconds, return "H:MM:SS" or "M:SS" if hours=0.
    """
    secs = int(seconds)
    m, s = divmod(secs, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    else:
        return f"{m}:{s:02d}"

def get_progress_bar_styled(elapsed: float, total: float, bar_length: int = 14) -> str:
    """
    Build a progress bar string in the style:
      elapsed_time  <dashes>❄️<dashes>  total_time
    For example: 0:30 —❄️———— 3:09
    """
    if total <= 0:
        return "অগ্রগতি: N/A" # Bengali: Progress: N/A
    fraction = min(elapsed / total, 1)
    marker_index = int(fraction * bar_length)
    if marker_index >= bar_length:
        marker_index = bar_length - 1
    left = "━" * marker_index
    right = "─" * (bar_length - marker_index - 1)
    bar = left + "❄️" + right
    return f"{format_time(elapsed)} {bar} {format_time(total)}"


async def update_progress_caption(
    chat_id: int,
    progress_message: Message,
    start_time: float,
    total_duration: float,
    base_caption: str
):
    """
    Periodically update the inline keyboard so that the second row's button text
    shows the current progress bar. The caption remains `base_caption`.
    """
    while True:
        elapsed = time.time() - start_time
        if elapsed > total_duration:
            elapsed = total_duration
        progress_bar = get_progress_bar_styled(elapsed, total_duration)

        # Rebuild the keyboard with updated progress bar in the second row
        control_row = [
            InlineKeyboardButton(text="▷", callback_data="pause"),
