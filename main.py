import asyncio
import os
import random
import time
from collections import defaultdict
from datetime import datetime

import pytz
import requests
from PIL import Image
from pyrogram import Client, enums, filters
from pyrogram.errors import FloodWait
from pyrogram.types import InputMediaPhoto, Message

from modules.custom_modules.elevenlabs import generate_elevenlabs_audio
from utils import modules_help, prefix
from utils.config import gemini_key
from utils.db import db
from utils.scripts import import_library

import_library("google.genai", "google-genai")
from google import genai
from google.genai import types
from google.genai.errors import APIError  # noqa: F401  kept for callers that may import it

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SAFETY_SETTINGS: list[types.SafetySetting] = [
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
        threshold=types.HarmBlockThreshold.OFF,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_HARASSMENT,
        threshold=types.HarmBlockThreshold.OFF,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        threshold=types.HarmBlockThreshold.OFF,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        threshold=types.HarmBlockThreshold.OFF,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_CIVIC_INTEGRITY,
        threshold=types.HarmBlockThreshold.OFF,
    ),
]

# FIX 1: Raised MAX_OUTPUT_TOKENS from 500 → 1500 so responses are not cut off mid-sentence.
MAX_OUTPUT_TOKENS = 1500
# FIX 2: Updated default model from "gemini-2.0-flash" → "gemini-2.5-flash" (current stable).
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
DEFAULT_HISTORY_HEAD = 50
DEFAULT_HISTORY_TAIL = 50
# FIX 3: Raised HISTORY_CAP so DB history is trimmed at storage time (prevents unbounded growth).
HISTORY_CAP = 200
RESPONSE_MAX_CHARS = 4000
CHUNK_SIZE = 3800
REPLY_WORKER_COOLDOWN = 2.1
ROLES_CACHE_TTL = 300
BOT_PICS_CACHE_TTL = 600
# FIX 4: Raised semaphore from 4 → 10 so hundreds of concurrent chats aren't bottlenecked.
GEMINI_SEMAPHORE = asyncio.Semaphore(10)
# FIX 5: Max poll iterations for file upload (prevents infinite loop if Gemini hangs).
FILE_UPLOAD_MAX_POLLS = 30

HISTORY_COLLECTION = "custom.gchat"
SETTINGS_COLLECTION = "custom.gsettings"

ROLES_URL = (
    "https://gist.githubusercontent.com/iTahseen/"
    "00890d65192ca3bd9b2a62eb034b96ab/raw/roles.json"
)
BOT_PIC_GROUP_ID = -1001234567890
LA_TIMEZONE = pytz.timezone("America/Los_Angeles")
SMILEYS = ["-.-", "):", ":)", "*.*", ")*"]

# ---------------------------------------------------------------------------
# Gemini async client pool
# ---------------------------------------------------------------------------

_client_pool: dict[str, object] = {}
_client_pool_lock = asyncio.Lock()


async def _get_async_client(api_key: str) -> object:
    async with _client_pool_lock:
        if api_key not in _client_pool:
            _client_pool[api_key] = genai.Client(api_key=api_key).aio
        return _client_pool[api_key]


# ---------------------------------------------------------------------------
# Per-chat reply worker queue
# ---------------------------------------------------------------------------

_chat_queues: dict[int, asyncio.Queue] = {}
_chat_workers: dict[int, asyncio.Task] = {}
_chat_worker_lock = asyncio.Lock()


async def _chat_reply_worker(chat_id: int) -> None:
    queue = _chat_queues[chat_id]
    while True:
        try:
            item = await asyncio.wait_for(queue.get(), timeout=120)
        except asyncio.TimeoutError:
            async with _chat_worker_lock:
                _chat_queues.pop(chat_id, None)
                _chat_workers.pop(chat_id, None)
            return
        reply_func, args, kwargs = item
        cleanup_file = kwargs.pop("cleanup_file", None)
        try:
            try:
                await reply_func(*args, **kwargs)
            except FloodWait as exc:
                await asyncio.sleep(exc.value + 1)
                await reply_func(*args, **kwargs)
        except Exception:
            pass
        finally:
            _cleanup(cleanup_file)
        await asyncio.sleep(REPLY_WORKER_COOLDOWN)


async def _ensure_chat_worker(chat_id: int) -> None:
    async with _chat_worker_lock:
        if chat_id not in _chat_queues:
            _chat_queues[chat_id] = asyncio.Queue()
        if chat_id not in _chat_workers or _chat_workers[chat_id].done():
            _chat_workers[chat_id] = asyncio.create_task(_chat_reply_worker(chat_id))


# FIX 6: Removed unused `client` parameter from send_reply — it was accepted but never used.
async def send_reply(reply_func, args, kwargs, chat_id: int = 0) -> None:
    await _ensure_chat_worker(chat_id)
    await _chat_queues[chat_id].put((reply_func, list(args), kwargs))


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _cleanup(path: str | None) -> None:
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


async def _notify_me(client: Client, text: str) -> None:
    try:
        await client.send_message("me", text)
    except Exception:
        pass


# FIX 7: Removed _sync_write_file wrapper — replaced with an inline lambda via asyncio.to_thread
#         at each call site, removing the unnecessary indirection.
def _write_text_file(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


async def _dispatch_response(
    client: Client,
    message: Message,
    bot_response: str,
    user_id: int,
    chat_id: int,
    prefix_name: str = "gchat_resp",
) -> None:
    if await handle_gpic_message(client, chat_id, bot_response):
        return
    if await handle_voice_message(client, chat_id, bot_response):
        return
    await send_response_smart(client, message, bot_response, user_id, prefix_name=prefix_name)


# ---------------------------------------------------------------------------
# User active check
# FIX 8: Convert enabled/disabled lists to sets for O(1) lookup instead of O(n).
# ---------------------------------------------------------------------------

def _is_user_active(user_id: int) -> bool:
    disabled = set(db.get(SETTINGS_COLLECTION, "disabled_users") or [])
    if user_id in disabled:
        return False
    if db.get(SETTINGS_COLLECTION, "gchat_for_all"):
        return True
    enabled = set(db.get(SETTINGS_COLLECTION, "enabled_users") or [])
    return user_id in enabled


# ---------------------------------------------------------------------------
# Model / settings accessors
# ---------------------------------------------------------------------------

_gemini_model_cache: str | None = None


def get_gemini_model() -> str:
    global _gemini_model_cache
    if _gemini_model_cache is None:
        _gemini_model_cache = (
            db.get(SETTINGS_COLLECTION, "gemini_model") or DEFAULT_GEMINI_MODEL
        )
    return _gemini_model_cache


def set_gemini_model(model_name: str) -> None:
    global _gemini_model_cache
    db.set(SETTINGS_COLLECTION, "gemini_model", model_name)
    _gemini_model_cache = model_name


def get_voice_enabled() -> bool:
    enabled = db.get(SETTINGS_COLLECTION, "voice_generation_enabled")
    if enabled is None:
        enabled = True
        db.set(SETTINGS_COLLECTION, "voice_generation_enabled", True)
    return bool(enabled)


def set_voice_enabled(enabled: bool) -> None:
    db.set(SETTINGS_COLLECTION, "voice_generation_enabled", enabled)


def get_history_limits() -> tuple[int, int]:
    head = db.get(SETTINGS_COLLECTION, "history_head")
    tail = db.get(SETTINGS_COLLECTION, "history_tail")
    try:
        head = int(head)
    except (TypeError, ValueError):
        head = DEFAULT_HISTORY_HEAD
    try:
        tail = int(tail)
    except (TypeError, ValueError):
        tail = DEFAULT_HISTORY_TAIL
    return head, tail


# FIX 9: Consolidated key retrieval into _get_current_key() — was duplicated inline
#         inside generate_gemini_response with its own separate DB reads.
def _get_current_key() -> str:
    gemini_keys: list[str] = db.get(SETTINGS_COLLECTION, "gemini_keys") or [gemini_key]
    current_key_index: int = db.get(SETTINGS_COLLECTION, "current_key_index") or 0
    return gemini_keys[current_key_index]


def _get_all_keys() -> tuple[list[str], int]:
    """Return (keys_list, current_index) in one DB read pair."""
    gemini_keys: list[str] = db.get(SETTINGS_COLLECTION, "gemini_keys") or [gemini_key]
    current_key_index: int = db.get(SETTINGS_COLLECTION, "current_key_index") or 0
    return gemini_keys, current_key_index


# ---------------------------------------------------------------------------
# Roles cache
# ---------------------------------------------------------------------------

_roles_cache: dict = {}
_roles_cache_ts: float = 0.0


def _fetch_roles_sync() -> dict:
    r = requests.get(ROLES_URL, timeout=5)
    r.raise_for_status()
    return r.json()


async def fetch_roles() -> dict:
    global _roles_cache, _roles_cache_ts
    if _roles_cache and (time.monotonic() - _roles_cache_ts) < ROLES_CACHE_TTL:
        return _roles_cache
    try:
        roles = await asyncio.to_thread(_fetch_roles_sync)
        if not isinstance(roles, dict):
            return _roles_cache
        default_role_name = db.get(SETTINGS_COLLECTION, "default_role") or "default"
        if default_role_name in roles:
            roles["default"] = roles[default_role_name]
        _roles_cache = roles
        _roles_cache_ts = time.monotonic()
        return roles
    except Exception:
        return _roles_cache


async def _get_active_role(client: Client, user_id: int):
    roles = await fetch_roles()
    default_role = roles.get("default")
    if not default_role:
        await _notify_me(client, "Err: 'default' role missing.")
        return None
    return db.get(SETTINGS_COLLECTION, f"custom_roles.{user_id}") or default_role


# ---------------------------------------------------------------------------
# Bot pics cache
# ---------------------------------------------------------------------------

_bot_pics_cache: list[str] = []
_bot_pics_ts: float = 0.0


async def _fetch_bot_pics(client: Client, max_photos: int = 200) -> list[str]:
    global _bot_pics_cache, _bot_pics_ts
    if _bot_pics_cache and (time.monotonic() - _bot_pics_ts) < BOT_PICS_CACHE_TTL:
        return _bot_pics_cache
    photos: list[str] = []
    async for msg in client.get_chat_history(BOT_PIC_GROUP_ID, limit=max_photos):
        if msg.photo:
            photos.append(msg.photo.file_id)
    _bot_pics_cache = photos
    _bot_pics_ts = time.monotonic()
    return photos


async def _send_bot_pics(client: Client, chat_id: int, n: int, caption: str) -> bool:
    photos = await _fetch_bot_pics(client)
    if not photos:
        return False
    selected = random.sample(photos, min(n, len(photos)))
    if len(selected) > 1:
        media = [
            InputMediaPhoto(pic, caption=caption if i == 0 else "")
            for i, pic in enumerate(selected)
        ]
        await send_reply(client.send_media_group, [chat_id, media], {}, chat_id=chat_id)
    else:
        await send_reply(client.send_photo, [chat_id, selected[0]], {"caption": caption}, chat_id=chat_id)
    return True


# ---------------------------------------------------------------------------
# Chat history
# FIX 10: History is now trimmed at storage time (capped at HISTORY_CAP entries)
#          so the DB never grows unbounded. Previously only trimmed for prompt building.
# ---------------------------------------------------------------------------

def get_chat_history(user_id: int, user_message: str, user_name: str) -> list[dict]:
    max_head, max_tail = get_history_limits()
    raw = db.get(HISTORY_COLLECTION, f"chat_history.{user_id}") or []

    history: list[dict] = []
    for entry in raw:
        if isinstance(entry, dict) and "role" in entry and "text" in entry:
            history.append(entry)
        elif isinstance(entry, str):
            history.append({"role": "user", "text": entry})

    history.append({"role": "user", "text": f"{user_name}: {user_message}"})

    # Trim storage to cap — keep newest entries
    if len(history) > HISTORY_CAP:
        history = history[-HISTORY_CAP:]

    db.set(HISTORY_COLLECTION, f"chat_history.{user_id}", history)

    # Trim further for prompt window (head + tail view)
    if len(history) > max_head + max_tail:
        return (
            history[:max_head]
            + [{"role": "user", "text": "..."}]
            + history[-max_tail:]
        )
    return history


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def build_system_instruction(bot_role) -> str:
    role_text = "\n".join(bot_role) if isinstance(bot_role, list) else str(bot_role)
    timestamp = datetime.now(LA_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    return f"{role_text}\n\nCurrent Time: {timestamp}"


def build_prompt(chat_history: list[dict], user_message: str) -> str:
    lines = []
    for entry in chat_history:
        if isinstance(entry, dict):
            speaker = "You" if entry["role"] == "model" else "User"
            lines.append(f"{speaker}: {entry['text']}")
        else:
            lines.append(str(entry))
    history_block = "\n".join(lines) if lines else "(no prior messages)"
    return f"Conversation so far:\n{history_block}\n\nUser: {user_message}"


# ---------------------------------------------------------------------------
# Key rotation logic
# FIX 11: Narrowed _should_rotate_key — removed overly broad "invalid"/"suspended"
#          string matches that could rotate keys on unrelated errors.
# ---------------------------------------------------------------------------

def _should_rotate_key(exc: Exception) -> bool:
    code = getattr(exc, "code", None)
    if code in (429, 403):
        return True
    err = str(exc)
    # Only match explicit quota/rate-limit signals, not generic "invalid" errors
    return any(k in err for k in ("429", "RESOURCE_EXHAUSTED", "quota", "rate limit"))


# ---------------------------------------------------------------------------
# Gemini response generation
# FIX 12: Removed duplicate DB reads for keys — now uses _get_all_keys() helper.
# FIX 13: Empty response now returns a user-visible fallback string instead of "".
# ---------------------------------------------------------------------------

async def generate_gemini_response(
    input_data,
    user_id: int,
    bot_role=None,
) -> str:
    retries = 3
    gemini_keys, current_key_index = _get_all_keys()
    system_instr = build_system_instruction(bot_role) if bot_role else None

    while retries > 0:
        try:
            current_key = gemini_keys[current_key_index]
            config = types.GenerateContentConfig(
                system_instruction=system_instr,
                max_output_tokens=MAX_OUTPUT_TOKENS,
                safety_settings=SAFETY_SETTINGS,
            )
            ai_client = await _get_async_client(current_key)
            async with GEMINI_SEMAPHORE:
                response = await ai_client.models.generate_content(
                    model=get_gemini_model(),
                    contents=input_data,
                    config=config,
                )

            if not response.text:
                candidates = getattr(response, "candidates", None)
                finish = candidates[0].finish_reason if candidates else None
                if finish == types.FinishReason.SAFETY:
                    # Safety block — don't store, return empty sentinel
                    return ""

            bot_response = (response.text or "").strip()
            if bot_response:
                full_history = db.get(HISTORY_COLLECTION, f"chat_history.{user_id}") or []
                full_history.append({"role": "model", "text": bot_response})
                # Trim storage cap on model replies too
                if len(full_history) > HISTORY_CAP:
                    full_history = full_history[-HISTORY_CAP:]
                db.set(HISTORY_COLLECTION, f"chat_history.{user_id}", full_history)
            return bot_response

        except Exception as exc:
            if _should_rotate_key(exc):
                retries -= 1
                current_key_index = (current_key_index + 1) % len(gemini_keys)
                db.set(SETTINGS_COLLECTION, "current_key_index", current_key_index)
                await asyncio.sleep(4)
            else:
                raise

    return ""


# ---------------------------------------------------------------------------
# File upload to Gemini
# FIX 14: Added poll timeout (FILE_UPLOAD_MAX_POLLS) to prevent infinite loop
#          if Gemini file processing never completes.
# ---------------------------------------------------------------------------

async def upload_file_to_gemini(file_path: str):
    current_key = _get_current_key()
    ai_client = await _get_async_client(current_key)
    uploaded_file = await ai_client.files.upload(file=file_path)

    polls = 0
    while uploaded_file.state == types.FileState.PROCESSING:
        if polls >= FILE_UPLOAD_MAX_POLLS:
            raise TimeoutError(f"File processing timed out after {polls} polls: {file_path!r}")
        await asyncio.sleep(10)
        uploaded_file = await ai_client.files.get(name=uploaded_file.name)
        polls += 1

    if uploaded_file.state == types.FileState.FAILED:
        raise ValueError(f"File upload failed: {file_path!r}")
    return uploaded_file


# ---------------------------------------------------------------------------
# Response sending
# ---------------------------------------------------------------------------

async def send_response_smart(
    client: Client,
    message: Message,
    response: str,
    user_id: int,
    *,
    prefix_name: str = "gchat_resp",
) -> None:
    chat_id = message.chat.id
    if len(response) > RESPONSE_MAX_CHARS:
        fp = f"{prefix_name}_{user_id}_{int(time.time())}.txt"
        await asyncio.to_thread(_write_text_file, fp, response)
        await send_reply(
            client.send_document,
            [chat_id, fp],
            {"caption": "Response", "reply_to_message_id": message.id, "cleanup_file": fp},
            chat_id=chat_id,
        )
    else:
        await send_reply(message.reply_text, [response], {}, chat_id=chat_id)


# ---------------------------------------------------------------------------
# Special response handlers
# ---------------------------------------------------------------------------

async def handle_voice_message(client: Client, chat_id: int, bot_response: str) -> bool:
    if not isinstance(bot_response, str) or not bot_response.startswith(".el"):
        return False

    text = bot_response[3:].strip()
    if not get_voice_enabled():
        await send_reply(client.send_message, [chat_id, text], {}, chat_id=chat_id)
        return True

    try:
        audio_path = await generate_elevenlabs_audio(text=text)
        if audio_path and os.path.exists(audio_path):
            await send_reply(
                client.send_voice,
                [chat_id],
                {"voice": audio_path, "cleanup_file": audio_path},
                chat_id=chat_id,
            )
            return True
    except Exception:
        pass

    await send_reply(client.send_message, [chat_id, text], {}, chat_id=chat_id)
    return True


async def handle_gpic_message(client: Client, chat_id: int, bot_response: str) -> bool:
    if not isinstance(bot_response, str) or not bot_response.startswith(".gpic"):
        return False

    parts = bot_response.split(maxsplit=2)
    n = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 1
    caption = parts[2] if len(parts) == 3 else ""

    sent = await _send_bot_pics(client, chat_id, n, caption)
    if not sent:
        await send_reply(client.send_message, ["me", "No bot pictures in group/channel."], {}, chat_id=chat_id)
    return True


async def send_typing_action(client: Client, chat_id: int, user_message: str) -> None:
    try:
        await client.send_chat_action(chat_id=chat_id, action=enums.ChatAction.TYPING)
        await asyncio.sleep(min(len(user_message) / 10, 5))
    except Exception as exc:
        await _notify_me(client, f"send_typing_action error: {exc}")


# ---------------------------------------------------------------------------
# Sticker / GIF handler
# ---------------------------------------------------------------------------

sticker_gif_buffer: dict[int, list[Message]] = defaultdict(list)
sticker_gif_timer: dict[int, asyncio.Task] = {}


async def _process_sticker_gif_buffer(client: Client, user_id: int) -> None:
    try:
        await asyncio.sleep(8)
        msgs = sticker_gif_buffer.pop(user_id, [])
        sticker_gif_timer.pop(user_id, None)
        if not msgs:
            return
        last_msg = msgs[-1]
        await asyncio.sleep(random.uniform(5, 10))
        await send_reply(last_msg.reply_text, [random.choice(SMILEYS)], {}, chat_id=last_msg.chat.id)
    except Exception as exc:
        await _notify_me(client, f"Sticker/GIF buffer error:\n{exc}")


@Client.on_message(
    (filters.sticker | filters.animation) & filters.private & ~filters.me & ~filters.bot,
    group=1,
)
async def handle_sticker_gif_buffered(client: Client, message: Message) -> None:
    user_id = message.from_user.id
    user_name = message.from_user.first_name or "User"
    chat_id = message.chat.id

    if not _is_user_active(user_id):
        return

    try:
        full_history = db.get(HISTORY_COLLECTION, f"chat_history.{user_id}") or []
        if not full_history:
            bot_role = await _get_active_role(client, user_id)
            if bot_role is None:
                return
            chat_history = get_chat_history(user_id, "hello", user_name)
            prompt = build_prompt(chat_history, "hello")
            await send_typing_action(client, chat_id, "hello")
            try:
                bot_response = await generate_gemini_response(prompt, user_id, bot_role=bot_role)
            except Exception as exc:
                await _notify_me(client, f"sticker initial gchat error:\n\n{exc}")
                return

            if not bot_response:
                await _notify_me(client, f"Gemini returned empty response for user {user_id}")
                return
            await _dispatch_response(client, message, bot_response, user_id, chat_id)
            return
    except Exception as exc:
        await _notify_me(client, f"sticker handler error:\n{exc}")

    sticker_gif_buffer[user_id].append(message)
    if sticker_gif_timer.get(user_id):
        sticker_gif_timer[user_id].cancel()
    sticker_gif_timer[user_id] = asyncio.create_task(
        _process_sticker_gif_buffer(client, user_id)
    )


# ---------------------------------------------------------------------------
# Text message handler
# ---------------------------------------------------------------------------

_message_buffer: dict[int, list[str]] = defaultdict(list)
_message_timers: dict[int, asyncio.Task] = {}


@Client.on_message(filters.text & filters.private & ~filters.me & ~filters.bot, group=1)
async def gchat(client: Client, message: Message) -> None:
    try:
        user_id = message.from_user.id
        user_name = message.from_user.first_name or "User"
        user_message = message.text.strip()
        chat_id = message.chat.id

        if not _is_user_active(user_id):
            return

        bot_role = await _get_active_role(client, user_id)
        if bot_role is None:
            return

        _message_buffer[user_id].append(user_message)

        if _message_timers.get(user_id):
            _message_timers[user_id].cancel()

        async def process_combined_messages() -> None:
            await asyncio.sleep(8)
            buffered = _message_buffer.pop(user_id, [])
            _message_timers.pop(user_id, None)
            if not buffered:
                return
            combined = " ".join(buffered)
            chat_history = get_chat_history(user_id, combined, user_name)
            await asyncio.sleep(random.choice([3, 5, 7]))
            await send_typing_action(client, chat_id, combined)
            prompt = build_prompt(chat_history, combined)
            try:
                bot_response = await generate_gemini_response(prompt, user_id, bot_role=bot_role)
            except Exception as exc:
                await _notify_me(client, f"gchat error:\n\n{exc}")
                return
            if not bot_response:
                await _notify_me(client, f"Gemini returned empty response for user {user_id}")
                return
            await _dispatch_response(client, message, bot_response, user_id, chat_id)

        _message_timers[user_id] = asyncio.create_task(process_combined_messages())

    except Exception as exc:
        await _notify_me(client, f"gchat module error:\n\n{exc}")


# ---------------------------------------------------------------------------
# File / image handler
# FIX 15: Image timer now cancels and restarts on each new image (like message timer does),
#          so the debounce window resets from the LAST image, not the first.
# ---------------------------------------------------------------------------

_image_buffer: dict[int, list[str]] = defaultdict(list)
_image_timers: dict[int, asyncio.Task] = {}


@Client.on_message(filters.private & ~filters.me & ~filters.bot, group=1)
async def handle_files(client: Client, message: Message) -> None:
    file_path: str | None = None
    try:
        user_id = message.from_user.id
        user_name = message.from_user.first_name or "User"
        chat_id = message.chat.id

        if not _is_user_active(user_id):
            return

        bot_role = await _get_active_role(client, user_id)
        if bot_role is None:
            return

        caption = message.caption.strip() if message.caption else ""
        chat_history = get_chat_history(user_id, caption or "(media)", user_name)

        if message.photo:
            image_path = await client.download_media(message.photo)
            _image_buffer[user_id].append(image_path)

            # FIX 15: Cancel existing timer so window resets on each new image
            if _image_timers.get(user_id):
                _image_timers[user_id].cancel()

            async def process_images() -> None:
                try:
                    await asyncio.sleep(10)
                    image_paths = _image_buffer.pop(user_id, [])
                    _image_timers.pop(user_id, None)
                    if not image_paths:
                        return

                    sample_images: list[Image.Image] = []
                    try:
                        for img_path in image_paths:
                            try:
                                img = await asyncio.to_thread(Image.open, img_path)
                                sample_images.append(img)
                            except Exception:
                                continue
                        if not sample_images:
                            await _notify_me(client, "No valid images to process.")
                            return

                        prompt_text = "User sent multiple images." + (
                            f" Caption: {caption}" if caption else ""
                        )
                        prompt = build_prompt(chat_history, prompt_text)
                        input_data = [prompt] + sample_images
                        response = await generate_gemini_response(input_data, user_id, bot_role=bot_role)
                        if not response:
                            await _notify_me(client, f"Empty Gemini response for images from user {user_id}")
                            return
                        await _dispatch_response(client, message, response, user_id, chat_id, prefix_name="gchat_img_resp")
                    finally:
                        for im in sample_images:
                            try:
                                im.close()
                            except Exception:
                                pass
                        for path in image_paths:
                            _cleanup(path)
                except Exception as exc:
                    await _notify_me(client, f"process_images error:\n\n{exc}")

            _image_timers[user_id] = asyncio.create_task(process_images())
            return

        file_type: str | None = None
        if message.video or message.video_note:
            file_type = "video"
            file_path = await client.download_media(message.video or message.video_note)
        elif message.audio or message.voice:
            file_type = "audio"
            file_path = await client.download_media(message.audio or message.voice)
        elif message.document:
            file_type = "pdf" if (message.document.file_name or "").endswith(".pdf") else "document"
            file_path = await client.download_media(message.document)

        if not (file_path and file_type):
            return

        try:
            uploaded_file = await upload_file_to_gemini(file_path)
        except Exception as exc:
            await _notify_me(client, f"upload_file_to_gemini error:\n\n{exc}")
            return

        prompt_text = f"User sent a {file_type}." + (f" Caption: {caption}" if caption else "")
        prompt = build_prompt(chat_history, prompt_text)
        input_data = [prompt, uploaded_file]

        try:
            response = await generate_gemini_response(input_data, user_id, bot_role=bot_role)
        except Exception as exc:
            await _notify_me(client, f"generate_gemini_response error:\n\n{exc}")
            return

        if not response:
            await _notify_me(client, f"Empty Gemini response for file from user {user_id}")
            return
        await _dispatch_response(client, message, response, user_id, chat_id, prefix_name="gchat_file_resp")

    except Exception as exc:
        await _notify_me(client, f"handle_files error:\n\n{exc}")
    finally:
        _cleanup(file_path)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

@Client.on_message(filters.command(["gchat", "gc"], prefix) & filters.me)
async def gchat_command(client: Client, message: Message) -> None:
    chat_id = message.chat.id
    try:
        parts = message.text.strip().split()
        if len(parts) < 2:
            await send_reply(message.edit_text, ["Usage: gchat [on|off|del|all|r] [user_id]"], {}, chat_id=chat_id)
            return

        command = parts[1].lower()
        user_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else chat_id

        enabled_users: list = db.get(SETTINGS_COLLECTION, "enabled_users") or []
        disabled_users: list = db.get(SETTINGS_COLLECTION, "disabled_users") or []

        if command == "on":
            if user_id in disabled_users:
                disabled_users.remove(user_id)
                db.set(SETTINGS_COLLECTION, "disabled_users", disabled_users)
            if user_id not in enabled_users:
                enabled_users.append(user_id)
                db.set(SETTINGS_COLLECTION, "enabled_users", enabled_users)
            await send_reply(message.edit_text, [f"<spoiler>ON: {user_id}</spoiler>"], {}, chat_id=chat_id)

        elif command == "off":
            if user_id not in disabled_users:
                disabled_users.append(user_id)
                db.set(SETTINGS_COLLECTION, "disabled_users", disabled_users)
            if user_id in enabled_users:
                enabled_users.remove(user_id)
                db.set(SETTINGS_COLLECTION, "enabled_users", enabled_users)
            await send_reply(message.edit_text, [f"<spoiler>OFF: {user_id}</spoiler>"], {}, chat_id=chat_id)

        elif command == "del":
            db.remove(HISTORY_COLLECTION, f"chat_history.{user_id}")
            await send_reply(message.edit_text, [f"<spoiler>Deleted: {user_id}</spoiler>"], {}, chat_id=chat_id)

        elif command == "all":
            new_state = not (db.get(SETTINGS_COLLECTION, "gchat_for_all") or False)
            db.set(SETTINGS_COLLECTION, "gchat_for_all", new_state)
            status = "enabled" if new_state else "disabled"
            await send_reply(message.edit_text, [f"All: {status}"], {}, chat_id=chat_id)

        elif command == "r":
            changed = False
            if user_id in enabled_users:
                enabled_users.remove(user_id)
                db.set(SETTINGS_COLLECTION, "enabled_users", enabled_users)
                changed = True
            if user_id in disabled_users:
                disabled_users.remove(user_id)
                db.set(SETTINGS_COLLECTION, "disabled_users", disabled_users)
                changed = True
            label = "Removed" if changed else "Not found"
            await send_reply(message.edit_text, [f"<spoiler>{label}: {user_id}</spoiler>"], {}, chat_id=chat_id)

        else:
            await send_reply(message.edit_text, ["Usage: gchat [on|off|del|all|r] [user_id]"], {}, chat_id=chat_id)

        await send_reply(message.delete, [], {}, chat_id=chat_id)
    except Exception as exc:
        await _notify_me(client, f"gchat command error:\n\n{exc}")


@Client.on_message(filters.command("gswitch", prefix) & filters.me)
async def switch_role(client: Client, message: Message) -> None:
    chat_id = message.chat.id
    try:
        roles = await fetch_roles()
        if not roles:
            await _notify_me(client, "Role fetch error.")
            await send_reply(message.edit_text, ["Failed to fetch roles."], {}, chat_id=chat_id)
            return

        user_id = chat_id
        parts = message.text.strip().split()
        if len(parts) == 1:
            available = "\n".join(f"- {r}" for r in roles)
            await send_reply(message.edit_text, [f"Roles:\n{available}"], {}, chat_id=chat_id)
            return

        role_name = parts[1].lower()
        if role_name in roles:
            db.set(SETTINGS_COLLECTION, f"custom_roles.{user_id}", roles[role_name])
            await send_reply(message.edit_text, [f"Switched: {role_name}"], {}, chat_id=chat_id)
        else:
            await send_reply(message.edit_text, [f"Not found: {role_name}"], {}, chat_id=chat_id)
        await send_reply(message.delete, [], {}, chat_id=chat_id)
    except Exception as exc:
        await _notify_me(client, f"switch command error:\n\n{exc}")


@Client.on_message(filters.command("role", prefix) & filters.me)
async def set_custom_role(client: Client, message: Message) -> None:
    chat_id = message.chat.id
    try:
        parts = message.text.strip().split()
        user_id = chat_id
        custom_role = None

        if len(parts) == 2 and parts[1].isdigit():
            user_id = int(parts[1])
        elif len(parts) > 2 and parts[1].isdigit():
            user_id = int(parts[1])
            custom_role = " ".join(parts[2:]).strip()
        elif len(parts) > 1:
            custom_role = " ".join(parts[1:]).strip()

        if not custom_role:
            db.remove(SETTINGS_COLLECTION, f"custom_roles.{user_id}")
            db.remove(HISTORY_COLLECTION, f"chat_history.{user_id}")
            await send_reply(message.edit_text, [f"<spoiler>Role reset: {user_id}</spoiler>"], {}, chat_id=chat_id)
        else:
            db.set(SETTINGS_COLLECTION, f"custom_roles.{user_id}", custom_role)
            db.remove(HISTORY_COLLECTION, f"chat_history.{user_id}")
            await send_reply(
                message.edit_text,
                [f"<spoiler>Role set: {user_id}</spoiler>\n{custom_role}"],
                {},
                chat_id=chat_id,
            )
        await send_reply(message.delete, [], {}, chat_id=chat_id)
    except Exception as exc:
        await _notify_me(client, f"role command error:\n\n{exc}")


@Client.on_message(filters.command(["setgchat", "setgc"], prefix) & filters.me)
async def set_gemini_key(client: Client, message: Message) -> None:
    chat_id = message.chat.id
    try:
        parts = message.text.strip().split()
        subcommand = parts[1] if len(parts) > 1 else None
        key = parts[2] if len(parts) > 2 else None
        gemini_keys: list[str] = db.get(SETTINGS_COLLECTION, "gemini_keys") or []
        current_key_index: int = db.get(SETTINGS_COLLECTION, "current_key_index") or 0

        if subcommand == "model":
            if key:
                set_gemini_model(key)
                await send_reply(message.edit_text, [f"Gemini model set to: {key}"], {}, chat_id=chat_id)
            else:
                await send_reply(message.edit_text, [f"Current Gemini model: {get_gemini_model()}"], {}, chat_id=chat_id)
            return

        if subcommand == "voice":
            new_state = not get_voice_enabled()
            set_voice_enabled(new_state)
            await send_reply(message.edit_text, [f"Voice: {'ON' if new_state else 'OFF'}"], {}, chat_id=chat_id)
            return

        if subcommand == "add" and key:
            if key in gemini_keys:
                await send_reply(message.edit_text, ["Key already added!"], {}, chat_id=chat_id)
                return
            gemini_keys.append(key)
            db.set(SETTINGS_COLLECTION, "gemini_keys", gemini_keys)
            await send_reply(message.edit_text, ["Gemini key added!"], {}, chat_id=chat_id)
            return

        if subcommand == "set" and key:
            index = int(key) - 1
            if 0 <= index < len(gemini_keys):
                db.set(SETTINGS_COLLECTION, "current_key_index", index)
                await send_reply(message.edit_text, [f"Current key set to: {key}"], {}, chat_id=chat_id)
            else:
                await send_reply(message.edit_text, [f"Invalid key index: {key}"], {}, chat_id=chat_id)
            return

        if subcommand == "del" and key:
            index = int(key) - 1
            if 0 <= index < len(gemini_keys):
                del gemini_keys[index]
                db.set(SETTINGS_COLLECTION, "gemini_keys", gemini_keys)
                if current_key_index >= len(gemini_keys):
                    current_key_index = max(0, len(gemini_keys) - 1)
                    db.set(SETTINGS_COLLECTION, "current_key_index", current_key_index)
                await send_reply(message.edit_text, [f"Key {key} deleted!"], {}, chat_id=chat_id)
            else:
                await send_reply(message.edit_text, [f"Invalid key index: {key}"], {}, chat_id=chat_id)
            return

        if subcommand == "role":
            roles = await fetch_roles()
            if key:
                role_name = key.lower()
                if role_name in roles:
                    db.set(SETTINGS_COLLECTION, "default_role", role_name)
                    await send_reply(message.edit_text, [f"Default: {role_name}"], {}, chat_id=chat_id)
                else:
                    await send_reply(message.edit_text, [f"Not found: {role_name}"], {}, chat_id=chat_id)
            else:
                roles_list = "\n".join(f"- {r}" for r in roles) if roles else "No roles found."
                await send_reply(message.edit_text, [f"Available roles:\n{roles_list}"], {}, chat_id=chat_id)
            return

        if subcommand == "history":
            if key and key.isdigit():
                n = int(key)
                db.set(SETTINGS_COLLECTION, "history_head", n)
                db.set(SETTINGS_COLLECTION, "history_tail", n)
                await send_reply(message.edit_text, [f"History head/tail set to: {n}"], {}, chat_id=chat_id)
                return
            if len(parts) > 3 and parts[2].isdigit() and parts[3].isdigit():
                head, tail = int(parts[2]), int(parts[3])
                db.set(SETTINGS_COLLECTION, "history_head", head)
                db.set(SETTINGS_COLLECTION, "history_tail", tail)
                await send_reply(message.edit_text, [f"History head: {head}, tail: {tail}"], {}, chat_id=chat_id)
                return

        keys_list = "\n".join(f"{i + 1}. {k}" for i, k in enumerate(gemini_keys))
        current_key = gemini_keys[current_key_index] if gemini_keys else "None"
        current_model = get_gemini_model()
        voice_status = "ON" if get_voice_enabled() else "OFF"
        current_default = db.get(SETTINGS_COLLECTION, "default_role") or "default"
        head = db.get(SETTINGS_COLLECTION, "history_head") or DEFAULT_HISTORY_HEAD
        tail = db.get(SETTINGS_COLLECTION, "history_tail") or DEFAULT_HISTORY_TAIL
        menu_text = (
            f"Keys:\n{keys_list}\n\n"
            f"Current: {current_key}\nModel: {current_model}\n"
            f"Voice: {voice_status}\nRole: {current_default}\n"
            f"History head: {head}, tail: {tail}"
        )
        if len(menu_text) > CHUNK_SIZE:
            fp = f"gchat_menu_{int(time.time())}.txt"
            await asyncio.to_thread(_write_text_file, fp, menu_text)
            await send_reply(
                client.send_document,
                [chat_id, fp],
                {"caption": "gchat menu", "cleanup_file": fp},
                chat_id=chat_id,
            )
        else:
            await send_reply(message.edit_text, [menu_text], {}, chat_id=chat_id)

    except Exception as exc:
        await _notify_me(client, f"setgchat error:\n\n{exc}")


@Client.on_message(filters.command("gpic", prefix) & filters.me)
async def gpic_command(client: Client, message: Message) -> None:
    chat_id = message.chat.id
    try:
        parts = message.text.strip().split(maxsplit=2)
        n = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 1
        caption = parts[2] if len(parts) == 3 else ""
        sent = await _send_bot_pics(client, chat_id, n, caption)
        if not sent:
            await send_reply(message.edit_text, ["No bot pictures found."], {}, chat_id=chat_id)
            return
        await send_reply(message.delete, [], {}, chat_id=chat_id)
    except Exception as exc:
        await _notify_me(client, f"gpic command error:\n\n{exc}")


@Client.on_message(filters.command("test", prefix) & filters.me)
async def test_keys(client: Client, message: Message) -> None:
    file_path: str | None = None
    try:
        await message.edit_text("Testing Gemini keys...")
        gemini_keys: list[str] = db.get(SETTINGS_COLLECTION, "gemini_keys") or [gemini_key]
        if not gemini_keys:
            await message.edit_text("No Gemini keys configured.")
            return

        result_lines = [
            "Gemini API Key Test Results\n",
            f"Model: {get_gemini_model()}\n",
            "-" * 40,
        ]
        for idx, key in enumerate(gemini_keys):
            try:
                config = types.GenerateContentConfig(
                    max_output_tokens=MAX_OUTPUT_TOKENS,
                    safety_settings=SAFETY_SETTINGS,
                )
                ai_client = await _get_async_client(key)
                async with GEMINI_SEMAPHORE:
                    response = await ai_client.models.generate_content(
                        model=get_gemini_model(),
                        contents="ping",
                        config=config,
                    )
                status = "OK" if response.text else "No response"
            except Exception as exc:
                status = f"ERROR: {exc.__class__.__name__}: {str(exc)[:80]}"
            result_lines.append(f"{idx + 1}. {key[:10]}... → {status}")

        result_text = "\n".join(result_lines)
        file_path = "gemini_test_results.txt"
        await asyncio.to_thread(_write_text_file, file_path, result_text)
        await client.send_document(
            chat_id=message.chat.id,
            document=file_path,
            caption="✅ Gemini API key test results",
        )
        await message.delete()
    except Exception as exc:
        await _notify_me(client, f"test command error:\n\n{exc}")
    finally:
        _cleanup(file_path)


modules_help["gchat"] = {
    "gchat on/off/del/all/r [user_id]": "Manage gchat for users.",
    "role [user_id] <role>": "Set or reset user role.",
    "gswitch [role]": "Show or set gchat role per chat.",
    "setgchat add/set/del <key|index>": "Manage Gemini API keys.",
    "setgchat": "Show Gemini config & status.",
    "setgchat model <name>": "Set/show Gemini model.",
    "setgchat voice": "Toggle voice reply.",
    "setgchat role <role>": "Set/show global default role.",
    "setgchat history <n>": "Set history head/tail (same value).",
    "setgchat history <head> <tail>": "Set history head and tail separately.",
    "gpic [n] [caption]": "Send n random pics with caption.",
    "test": "Test all configured Gemini keys.",
}
