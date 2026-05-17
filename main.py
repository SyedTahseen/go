import os
import uuid
import json
import aiohttp
import asyncio
import tempfile

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
)
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()

mongo = AsyncIOMotorClient(MONGO_URI)
db    = mongo["koyeb_panel"]
users = db["users"]


# ─────────────────────────────────────────────────────────────────────────────
# FSM
# ─────────────────────────────────────────────────────────────────────────────

class AddAccount(StatesGroup):
    waiting_token = State()
    waiting_name  = State()

class UpdateEnv(StatesGroup):
    waiting_key   = State()
    waiting_value = State()

class CreateService(StatesGroup):
    waiting_app_id      = State()
    waiting_name        = State()
    waiting_source_type = State()   # "docker" or "git"
    waiting_image       = State()   # docker image
    waiting_git_repo    = State()   # git repo URL
    waiting_git_branch  = State()
    waiting_region      = State()
    waiting_instance    = State()
    waiting_ports       = State()   # e.g. "8000:http" or "skip"
    waiting_env         = State()   # KEY=VALUE lines or "skip"


# ─────────────────────────────────────────────────────────────────────────────
# Koyeb API helper
# ─────────────────────────────────────────────────────────────────────────────

async def koyeb_request(token, method, endpoint, payload=None, params=None):
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
    }
    url = f"https://app.koyeb.com{endpoint}"

    async with aiohttp.ClientSession() as session:
        async with session.request(
            method, url, headers=headers, json=payload, params=params
        ) as resp:
            try:
                data = await resp.json()
            except Exception:
                data = await resp.text()
            return resp.status, data


# ─────────────────────────────────────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────────────────────────────────────

async def get_user(user_id):
    return await users.find_one({"telegram_id": user_id})


# ─────────────────────────────────────────────────────────────────────────────
# Static keyboards
# ─────────────────────────────────────────────────────────────────────────────

home_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="➕ Add Account", callback_data="add_account")],
        [InlineKeyboardButton(text="📦 Accounts",   callback_data="accounts")],
    ]
)


# ─────────────────────────────────────────────────────────────────────────────
# /start  &  home
# ─────────────────────────────────────────────────────────────────────────────

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer("<b>Koyeb Telegram Panel</b>", reply_markup=home_keyboard)


@dp.callback_query(F.data == "home")
async def cb_home(callback: CallbackQuery):
    await callback.message.edit_text(
        "<b>Koyeb Telegram Panel</b>", reply_markup=home_keyboard
    )


# ─────────────────────────────────────────────────────────────────────────────
# Add account – step 1: ask for token
# ─────────────────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "add_account")
async def cb_add_account(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddAccount.waiting_token)
    await callback.message.edit_text(
        "Send your Koyeb API token.\n\n"
        "<i>Create one at: Koyeb dashboard → Account Settings → API</i>"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Add account – step 2: validate token then ask for name
# ─────────────────────────────────────────────────────────────────────────────

@dp.message(AddAccount.waiting_token)
async def msg_receive_token(message: Message, state: FSMContext):
    token = message.text.strip()

    await message.answer("⏳ Verifying token...")

    status, data = await koyeb_request(token, "GET", "/v1/apps")

    if status == 401:
        return await message.answer(
            "❌ <b>Invalid API token</b> — Koyeb rejected it (401).\n\n"
            "Make sure you copied the full token and try again."
        )

    if status not in (200, 404):
        return await message.answer(
            f"❌ Unexpected response from Koyeb (HTTP {status}).\n"
            f"<code>{data}</code>"
        )

    user = await get_user(message.from_user.id)
    if user:
        for acc in user.get("accounts", []):
            if acc["token"] == token:
                await state.clear()
                return await message.answer(
                    "⚠️ This token is already saved.", reply_markup=home_keyboard
                )

    await state.update_data(token=token)
    await state.set_state(AddAccount.waiting_name)

    await message.answer(
        "✅ Token is valid!\n\n"
        "Now send a <b>name</b> for this account.\n"
        "<i>Example: My Koyeb, Production, Side Project…</i>"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Add account – step 3: save name
# ─────────────────────────────────────────────────────────────────────────────

@dp.message(AddAccount.waiting_name)
async def msg_receive_name(message: Message, state: FSMContext):
    account_name = message.text.strip()

    if not account_name:
        return await message.answer("Please send a non-empty name.")

    fsm_data = await state.get_data()
    token    = fsm_data["token"]

    account = {
        "id":    str(uuid.uuid4()),
        "token": token,
        "name":  account_name,
    }

    user = await get_user(message.from_user.id)

    if not user:
        await users.insert_one({
            "telegram_id": message.from_user.id,
            "accounts":    [account],
        })
    else:
        await users.update_one(
            {"telegram_id": message.from_user.id},
            {"$push": {"accounts": account}},
        )

    await state.clear()
    await message.answer(
        f"✅ Account <b>{account_name}</b> added!",
        reply_markup=home_keyboard,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Accounts list
# ─────────────────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "accounts")
async def cb_accounts(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)

    if not user or not user.get("accounts"):
        return await callback.message.edit_text(
            "No accounts found.", reply_markup=home_keyboard
        )

    keyboard = [
        [InlineKeyboardButton(text=f"📁 {acc['name']}", callback_data=f"account:{i}")]
        for i, acc in enumerate(user["accounts"])
    ]
    keyboard.append([InlineKeyboardButton(text="⬅ Back", callback_data="home")])

    await callback.message.edit_text(
        "<b>Your Accounts</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Account panel
# ─────────────────────────────────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("account:"))
async def cb_account_panel(callback: CallbackQuery):
    index = int(callback.data.split(":")[1])
    user  = await get_user(callback.from_user.id)
    accs  = user.get("accounts", [])

    if index >= len(accs):
        return

    await users.update_one(
        {"telegram_id": callback.from_user.id},
        {"$set": {"temp_account_index": index}},
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📦 Apps",          callback_data="apps")],
            [InlineKeyboardButton(text="⚙ Services",       callback_data="services")],
            [InlineKeyboardButton(text="➕ Create Service", callback_data="create_service_start")],
            [InlineKeyboardButton(text="🗑 Delete Account", callback_data=f"delete_account:{index}")],
            [InlineKeyboardButton(text="⬅ Back",           callback_data="accounts")],
        ]
    )

    await callback.message.edit_text(
        f"<b>{accs[index]['name']}</b>", reply_markup=keyboard
    )


# ─────────────────────────────────────────────────────────────────────────────
# Apps  –  GET /v1/apps
#
# AppListItem has a "domains" array; each domain has a "name" field.
# We show the first domain name as the URL (*.koyeb.app auto-assigned domain).
# ─────────────────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "apps")
async def cb_apps(callback: CallbackQuery):
    user          = await get_user(callback.from_user.id)
    account_index = user.get("temp_account_index")
    token         = user["accounts"][account_index]["token"]

    status, data = await koyeb_request(token, "GET", "/v1/apps")

    if status != 200:
        return await callback.message.edit_text(
            f"❌ Failed to fetch apps (HTTP {status})\n<code>{data}</code>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⬅ Back", callback_data=f"account:{account_index}")
            ]])
        )

    apps_list = data.get("apps", [])

    if not apps_list:
        text = "<b>Apps</b>\n\nNo apps found."
        keyboard = [[InlineKeyboardButton(text="⬅ Back", callback_data=f"account:{account_index}")]]
        return await callback.message.edit_text(
            text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
        )

    lines = ["<b>Apps</b>\n"]
    for app in apps_list:
        # Pull first domain name as URL
        domains = app.get("domains", [])
        url_str = ""
        if domains:
            first_domain = domains[0].get("name", "")
            if first_domain:
                url_str = f"\n    🔗 https://{first_domain}"

        lines.append(
            f"📦 <b>{app['name']}</b>\n"
            f"    Status: <code>{app.get('status', '?')}</code>"
            f"{url_str}"
        )

    keyboard = [[InlineKeyboardButton(text="⬅ Back", callback_data=f"account:{account_index}")]]
    await callback.message.edit_text(
        "\n\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        disable_web_page_preview=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Services  –  GET /v1/services
# ─────────────────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "services")
async def cb_services(callback: CallbackQuery):
    user          = await get_user(callback.from_user.id)
    account_index = user.get("temp_account_index")
    token         = user["accounts"][account_index]["token"]

    status, data = await koyeb_request(token, "GET", "/v1/services")

    if status != 200:
        return await callback.message.edit_text(
            f"❌ Failed to fetch services (HTTP {status})\n<code>{data}</code>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⬅ Back", callback_data=f"account:{account_index}")
            ]])
        )

    services_list = data.get("services", [])

    await users.update_one(
        {"telegram_id": callback.from_user.id},
        {"$set": {"temp_services": services_list}},
    )

    keyboard = [
        [InlineKeyboardButton(
            text=f"⚙ {svc['name']} — {svc.get('status', '?')}",
            callback_data=f"service:{i}",
        )]
        for i, svc in enumerate(services_list)
    ]
    keyboard.append([
        InlineKeyboardButton(text="⬅ Back", callback_data=f"account:{account_index}")
    ])

    text = f"<b>Services</b> ({len(services_list)} total)" if services_list else "<b>Services</b>\n\nNo services found."
    await callback.message.edit_text(
        text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )


# ─────────────────────────────────────────────────────────────────────────────
# Service panel
#
# To get the URL: we fetch the app using svc["app_id"] → domains array.
# ─────────────────────────────────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("service:"))
async def cb_service_panel(callback: CallbackQuery):
    index         = int(callback.data.split(":")[1])
    user          = await get_user(callback.from_user.id)
    services_list = user.get("temp_services", [])

    if index >= len(services_list):
        return

    svc           = services_list[index]
    account_index = user.get("temp_account_index")
    token         = user["accounts"][account_index]["token"]

    # Fetch app to get domains/URL
    url_line = ""
    app_id = svc.get("app_id")
    if app_id:
        app_status, app_data = await koyeb_request(token, "GET", f"/v1/apps/{app_id}")
        if app_status == 200:
            app_obj = app_data.get("app", {})
            domains = app_obj.get("domains", [])
            if domains:
                first_domain = domains[0].get("name", "")
                if first_domain:
                    url_line = f"\nURL:    <code>https://{first_domain}</code>"

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🚀 Redeploy", callback_data=f"redeploy:{index}"),
                InlineKeyboardButton(text="⏸ Pause",    callback_data=f"pause:{index}"),
            ],
            [
                InlineKeyboardButton(text="▶ Resume",   callback_data=f"resume:{index}"),
                InlineKeyboardButton(text="📜 Logs",    callback_data=f"logs:{index}"),
            ],
            [
                InlineKeyboardButton(text="🌐 Env Vars",   callback_data=f"env_view:{index}"),
                InlineKeyboardButton(text="💾 Backup Env", callback_data=f"env_backup:{index}"),
            ],
            [InlineKeyboardButton(text="🗑 Delete",     callback_data=f"delete_service:{index}")],
            [InlineKeyboardButton(text="⬅ Back",        callback_data="services")],
        ]
    )

    await callback.message.edit_text(
        f"<b>{svc['name']}</b>\n\n"
        f"Status: <code>{svc.get('status', '?')}</code>\n"
        f"Type:   <code>{svc.get('type', '?')}</code>\n"
        f"ID:     <code>{svc['id']}</code>"
        f"{url_line}",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Shared helper – fetch service object + token from DB
# ─────────────────────────────────────────────────────────────────────────────

async def get_service_and_token(callback, index):
    user          = await get_user(callback.from_user.id)
    services_list = user.get("temp_services", [])
    account_index = user.get("temp_account_index")
    token         = user["accounts"][account_index]["token"]
    return services_list[index], token


async def get_deployment_definition(token, service):
    """Fetch the latest deployment definition for a service (contains env vars)."""
    deployment_id = service.get("active_deployment_id") or service.get("latest_deployment_id")
    if not deployment_id:
        return None, None
    dep_status, dep_data = await koyeb_request(token, "GET", f"/v1/deployments/{deployment_id}")
    if dep_status != 200:
        return None, None
    deployment = dep_data.get("deployment", {})
    definition = deployment.get("definition", {})
    return deployment, definition


# ─────────────────────────────────────────────────────────────────────────────
# View Env Vars  –  fetched from latest deployment definition
# ─────────────────────────────────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("env_view:"))
async def cb_env_view(callback: CallbackQuery):
    index      = int(callback.data.split(":")[1])
    svc, token = await get_service_and_token(callback, index)

    await callback.answer()

    deployment, definition = await get_deployment_definition(token, svc)
    if definition is None:
        return await callback.message.edit_text(
            "❌ Could not fetch deployment definition.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⬅ Back", callback_data=f"service:{index}")
            ]])
        )

    env_vars = definition.get("env", [])

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Add / Update Env", callback_data=f"env_update:{index}")],
            [InlineKeyboardButton(text="💾 Backup Env JSON",  callback_data=f"env_backup:{index}")],
            [InlineKeyboardButton(text="⬅ Back",             callback_data=f"service:{index}")],
        ]
    )

    if not env_vars:
        return await callback.message.edit_text(
            f"<b>Env Vars — {svc['name']}</b>\n\nNo environment variables set.",
            reply_markup=keyboard,
        )

    lines = [f"<b>Env Vars — {svc['name']}</b>\n"]
    for ev in env_vars:
        key   = ev.get("key", "")
        value = ev.get("value")
        secret = ev.get("secret")
        if secret:
            lines.append(f"🔒 <code>{key}</code> = <i>[secret: {secret}]</i>")
        else:
            lines.append(f"📝 <code>{key}</code> = <code>{value}</code>")

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=keyboard,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Backup Env – send env vars as a downloadable JSON file
# ─────────────────────────────────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("env_backup:"))
async def cb_env_backup(callback: CallbackQuery):
    index      = int(callback.data.split(":")[1])
    svc, token = await get_service_and_token(callback, index)

    await callback.answer()

    deployment, definition = await get_deployment_definition(token, svc)
    if definition is None:
        return await callback.answer("❌ Could not fetch deployment definition.", show_alert=True)

    env_vars = definition.get("env", [])

    backup = {
        "service_id":   svc["id"],
        "service_name": svc["name"],
        "env":          env_vars,
    }

    filename = f"{svc['name']}_env_backup.json"
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(backup, f, indent=2, ensure_ascii=False)

    await callback.message.answer_document(
        FSInputFile(filename),
        caption=(
            f"💾 Env backup for <b>{svc['name']}</b>\n\n"
            "You can restore by uploading this file back (feature coming soon) "
            "or use it to re-create the env vars manually."
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Update Env – FSM
#
# Flow: ask key → ask value → PATCH service with merged env list
# ─────────────────────────────────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("env_update:"))
async def cb_env_update_start(callback: CallbackQuery, state: FSMContext):
    index = int(callback.data.split(":")[1])
    await state.update_data(service_index=index)
    await state.set_state(UpdateEnv.waiting_key)
    await callback.message.edit_text(
        "✏️ <b>Update / Add Env Var</b>\n\n"
        "Send the <b>key</b> (variable name) you want to set.\n"
        "<i>Example: DATABASE_URL</i>"
    )


@dp.message(UpdateEnv.waiting_key)
async def msg_env_key(message: Message, state: FSMContext):
    key = message.text.strip()
    if not key:
        return await message.answer("Key cannot be empty. Please send a valid key name.")
    await state.update_data(env_key=key)
    await state.set_state(UpdateEnv.waiting_value)
    await message.answer(
        f"Now send the <b>value</b> for <code>{key}</code>.\n"
        "<i>Send <code>DELETE</code> to remove this key.</i>"
    )


@dp.message(UpdateEnv.waiting_value)
async def msg_env_value(message: Message, state: FSMContext):
    value    = message.text.strip()
    fsm_data = await state.get_data()
    key      = fsm_data["env_key"]
    index    = fsm_data["service_index"]

    await state.clear()

    user          = await get_user(message.from_user.id)
    services_list = user.get("temp_services", [])
    account_index = user.get("temp_account_index")
    token         = user["accounts"][account_index]["token"]
    svc           = services_list[index]

    await message.answer("⏳ Updating environment variable...")

    # Fetch current full deployment definition
    deployment, definition = await get_deployment_definition(token, svc)
    if definition is None:
        return await message.answer("❌ Could not fetch current deployment definition.")

    env_vars = definition.get("env", [])

    if value.upper() == "DELETE":
        env_vars = [ev for ev in env_vars if ev.get("key") != key]
        action = f"🗑 Removed <code>{key}</code>"
    else:
        # Merge: update existing key or append
        found = False
        for ev in env_vars:
            if ev.get("key") == key:
                ev["value"] = value
                ev.pop("secret", None)
                found = True
                break
        if not found:
            env_vars.append({"key": key, "value": value})
        action = f"✅ Set <code>{key}</code> = <code>{value}</code>"

    definition["env"] = env_vars

    # PATCH /v1/services/{id}  with full updated definition
    payload = {
        "definition": definition,
        "save_only":  False,  # trigger a redeploy so the new env takes effect
    }
    status, data = await koyeb_request(
        token, "PUT", f"/v1/services/{svc['id']}", payload=payload
    )

    if status not in (200, 201, 202):
        err = data.get("message", str(data)) if isinstance(data, dict) else str(data)
        return await message.answer(f"❌ Update failed (HTTP {status}): {err}")

    back_kbd = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅ Back to Service", callback_data=f"service:{index}")
    ]])
    await message.answer(
        f"{action}\n\n<i>A new deployment has been triggered to apply the changes.</i>",
        reply_markup=back_kbd,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Redeploy  –  POST /v1/services/{id}/redeploy
# ─────────────────────────────────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("redeploy:"))
async def cb_redeploy(callback: CallbackQuery):
    index      = int(callback.data.split(":")[1])
    svc, token = await get_service_and_token(callback, index)

    status, data = await koyeb_request(
        token,
        "POST",
        f"/v1/services/{svc['id']}/redeploy",
        payload={},
    )

    if status not in (200, 201, 202):
        return await callback.answer(
            f"❌ Redeploy failed (HTTP {status}): {data.get('message', '') if isinstance(data, dict) else data}",
            show_alert=True,
        )

    await callback.answer("🚀 Redeploy triggered!", show_alert=False)


# ─────────────────────────────────────────────────────────────────────────────
# Pause  –  POST /v1/services/{id}/pause
# ─────────────────────────────────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("pause:"))
async def cb_pause(callback: CallbackQuery):
    index      = int(callback.data.split(":")[1])
    svc, token = await get_service_and_token(callback, index)

    status, data = await koyeb_request(
        token,
        "POST",
        f"/v1/services/{svc['id']}/pause",
    )

    if status not in (200, 201, 202):
        return await callback.answer(
            f"❌ Pause failed (HTTP {status}): {data.get('message', '') if isinstance(data, dict) else data}",
            show_alert=True,
        )

    await callback.answer("⏸ Service paused", show_alert=False)


# ─────────────────────────────────────────────────────────────────────────────
# Resume  –  POST /v1/services/{id}/resume
# ─────────────────────────────────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("resume:"))
async def cb_resume(callback: CallbackQuery):
    index      = int(callback.data.split(":")[1])
    svc, token = await get_service_and_token(callback, index)

    status, data = await koyeb_request(
        token,
        "POST",
        f"/v1/services/{svc['id']}/resume",
        params={"skip_build": "false"},
    )

    if status not in (200, 201, 202):
        return await callback.answer(
            f"❌ Resume failed (HTTP {status}): {data.get('message', '') if isinstance(data, dict) else data}",
            show_alert=True,
        )

    await callback.answer("▶ Service resumed", show_alert=False)


# ─────────────────────────────────────────────────────────────────────────────
# Logs  –  GET /v1/streams/logs/query
# ─────────────────────────────────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("logs:"))
async def cb_logs(callback: CallbackQuery):
    index      = int(callback.data.split(":")[1])
    svc, token = await get_service_and_token(callback, index)

    params = {
        "service_id": svc["id"],
        "type":       "runtime",
        "order":      "desc",
        "limit":      "100",
    }

    status, data = await koyeb_request(
        token, "GET", "/v1/streams/logs/query", params=params
    )

    if status != 200:
        return await callback.answer(
            f"❌ Failed to fetch logs (HTTP {status}): {data.get('message', '') if isinstance(data, dict) else data}",
            show_alert=True,
        )

    log_entries = data.get("data", []) if isinstance(data, dict) else []

    if not log_entries:
        return await callback.answer("No logs found for this service.", show_alert=True)

    lines = []
    for entry in log_entries:
        ts     = entry.get("created_at", "")
        labels = entry.get("labels", {})
        stream = labels.get("stream", "") if isinstance(labels, dict) else ""
        msg    = entry.get("msg", "")
        lines.append(f"[{ts}] [{stream}] {msg}" if ts else msg)

    lines.reverse()

    filename = f"{svc['name']}_logs.txt"
    with open(filename, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    await callback.message.answer_document(
        FSInputFile(filename),
        caption=f"📜 Logs for <b>{svc['name']}</b>",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Delete service  –  DELETE /v1/services/{id}
# ─────────────────────────────────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("delete_service:"))
async def cb_delete_service(callback: CallbackQuery):
    index      = int(callback.data.split(":")[1])
    svc, token = await get_service_and_token(callback, index)

    status, data = await koyeb_request(
        token, "DELETE", f"/v1/services/{svc['id']}"
    )

    if status not in (200, 202, 204):
        return await callback.answer(
            f"❌ Delete failed (HTTP {status}): {data.get('message', '') if isinstance(data, dict) else data}",
            show_alert=True,
        )

    await callback.message.edit_text(
        f"✅ Service <b>{svc['name']}</b> deleted.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⬅ Back to Services", callback_data="services")
        ]])
    )


# ─────────────────────────────────────────────────────────────────────────────
# Delete account
# ─────────────────────────────────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("delete_account:"))
async def cb_delete_account(callback: CallbackQuery):
    index = int(callback.data.split(":")[1])
    user  = await get_user(callback.from_user.id)
    accs  = user.get("accounts", [])

    if index >= len(accs):
        return

    await users.update_one(
        {"telegram_id": callback.from_user.id},
        {"$pull": {"accounts": {"id": accs[index]["id"]}}},
    )

    await callback.message.edit_text("✅ Account deleted.", reply_markup=home_keyboard)


# ─────────────────────────────────────────────────────────────────────────────
# Create Service – FSM wizard
#
# Steps:
#   1. Choose app (list from API) or enter app ID manually
#   2. Service name
#   3. Source type: Docker or Git
#   4a. Docker image  (if docker)
#   4b. Git repo URL + branch  (if git)
#   5. Region (fra, was, sin, tyo, syd …)
#   6. Instance type (nano, micro, small, medium, large …)
#   7. Port + protocol (e.g. "8000:http" or "skip")
#   8. Env vars (KEY=VALUE lines or "skip")
#   → POST /v1/services
# ─────────────────────────────────────────────────────────────────────────────

KOYEB_REGIONS   = ["fra", "was", "sin", "tyo", "syd"]
KOYEB_INSTANCES = ["nano", "micro", "small", "medium", "large", "xlarge"]


@dp.callback_query(F.data == "create_service_start")
async def cb_create_service_start(callback: CallbackQuery, state: FSMContext):
    user          = await get_user(callback.from_user.id)
    account_index = user.get("temp_account_index")
    token         = user["accounts"][account_index]["token"]

    # Fetch apps so user can pick one
    app_status, app_data = await koyeb_request(token, "GET", "/v1/apps")
    apps_list = app_data.get("apps", []) if app_status == 200 else []

    await state.update_data(apps_list=apps_list)
    await state.set_state(CreateService.waiting_app_id)

    if apps_list:
        lines = ["<b>Create New Service</b>\n\nStep 1/8 — Choose an <b>App</b>:\n"]
        for i, app in enumerate(apps_list):
            lines.append(f"  <b>{i + 1}.</b> {app['name']} (<code>{app['id']}</code>)")
        lines.append("\nSend the <b>number</b> from the list, or paste an App ID directly.")
        await callback.message.edit_text("\n".join(lines))
    else:
        await callback.message.edit_text(
            "<b>Create New Service</b>\n\n"
            "Step 1/8 — No apps found. Send your <b>App ID</b> directly."
        )


@dp.message(CreateService.waiting_app_id)
async def msg_create_app_id(message: Message, state: FSMContext):
    text     = message.text.strip()
    fsm_data = await state.get_data()
    apps_list = fsm_data.get("apps_list", [])

    # Allow selecting by number
    if text.isdigit():
        idx = int(text) - 1
        if 0 <= idx < len(apps_list):
            app_id = apps_list[idx]["id"]
        else:
            return await message.answer("Invalid number. Please try again.")
    else:
        app_id = text

    await state.update_data(app_id=app_id)
    await state.set_state(CreateService.waiting_name)
    await message.answer(
        "Step 2/8 — Enter a <b>name</b> for the service.\n"
        "<i>Lowercase letters, numbers and hyphens only. Example: my-api</i>"
    )


@dp.message(CreateService.waiting_name)
async def msg_create_name(message: Message, state: FSMContext):
    name = message.text.strip().lower()
    if not name:
        return await message.answer("Name cannot be empty.")
    await state.update_data(service_name=name)
    await state.set_state(CreateService.waiting_source_type)

    kbd = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🐳 Docker Image", callback_data="cs_source:docker")],
        [InlineKeyboardButton(text="🐙 Git Repository", callback_data="cs_source:git")],
    ])
    await message.answer("Step 3/8 — Choose the <b>source type</b>:", reply_markup=kbd)


@dp.callback_query(F.data.startswith("cs_source:"), CreateService.waiting_source_type)
async def cb_create_source(callback: CallbackQuery, state: FSMContext):
    source = callback.data.split(":")[1]
    await state.update_data(source_type=source)

    if source == "docker":
        await state.set_state(CreateService.waiting_image)
        await callback.message.edit_text(
            "Step 4/8 — Enter the <b>Docker image</b>.\n"
            "<i>Example: nginx:latest  or  ghcr.io/myorg/myapp:v1.2</i>"
        )
    else:
        await state.set_state(CreateService.waiting_git_repo)
        await callback.message.edit_text(
            "Step 4/8 — Enter the <b>Git repository URL</b>.\n"
            "<i>Example: github.com/myorg/myapp</i>"
        )


@dp.message(CreateService.waiting_image)
async def msg_create_image(message: Message, state: FSMContext):
    await state.update_data(docker_image=message.text.strip())
    await state.set_state(CreateService.waiting_region)
    await _ask_region(message)


@dp.message(CreateService.waiting_git_repo)
async def msg_create_git_repo(message: Message, state: FSMContext):
    await state.update_data(git_repo=message.text.strip())
    await state.set_state(CreateService.waiting_git_branch)
    await message.answer(
        "Step 4b/8 — Enter the <b>branch</b> to deploy.\n"
        "<i>Example: main  or  production</i>"
    )


@dp.message(CreateService.waiting_git_branch)
async def msg_create_git_branch(message: Message, state: FSMContext):
    await state.update_data(git_branch=message.text.strip())
    await state.set_state(CreateService.waiting_region)
    await _ask_region(message)


async def _ask_region(message: Message):
    kbd = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=r, callback_data=f"cs_region:{r}")]
        for r in KOYEB_REGIONS
    ])
    await message.answer("Step 5/8 — Choose a <b>region</b>:", reply_markup=kbd)


@dp.callback_query(F.data.startswith("cs_region:"), CreateService.waiting_region)
async def cb_create_region(callback: CallbackQuery, state: FSMContext):
    region = callback.data.split(":")[1]
    await state.update_data(region=region)
    await state.set_state(CreateService.waiting_instance)

    kbd = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=inst, callback_data=f"cs_instance:{inst}")]
        for inst in KOYEB_INSTANCES
    ])
    await callback.message.edit_text(
        "Step 6/8 — Choose an <b>instance type</b>:", reply_markup=kbd
    )


@dp.callback_query(F.data.startswith("cs_instance:"), CreateService.waiting_instance)
async def cb_create_instance(callback: CallbackQuery, state: FSMContext):
    instance = callback.data.split(":")[1]
    await state.update_data(instance_type=instance)
    await state.set_state(CreateService.waiting_ports)
    await callback.message.edit_text(
        "Step 7/8 — Enter <b>port and protocol</b>.\n\n"
        "Format: <code>PORT:PROTOCOL</code>\n"
        "<i>Example: <code>8000:http</code>  or  <code>3000:http</code></i>\n\n"
        "Send <code>skip</code> to create a worker with no exposed ports."
    )


@dp.message(CreateService.waiting_ports)
async def msg_create_ports(message: Message, state: FSMContext):
    text = message.text.strip()
    ports = None
    svc_type = "WORKER"

    if text.lower() != "skip":
        parts = text.split(":")
        if len(parts) != 2 or not parts[0].isdigit():
            return await message.answer(
                "Invalid format. Use <code>PORT:PROTOCOL</code> (e.g. <code>8000:http</code>) "
                "or send <code>skip</code>."
            )
        ports    = {"port": int(parts[0]), "protocol": parts[1].lower()}
        svc_type = "WEB"

    await state.update_data(ports=ports, svc_type=svc_type)
    await state.set_state(CreateService.waiting_env)
    await message.answer(
        "Step 8/8 — Enter <b>environment variables</b>.\n\n"
        "One per line in <code>KEY=VALUE</code> format:\n"
        "<pre>DATABASE_URL=postgres://...\nDEBUG=false</pre>\n\n"
        "Send <code>skip</code> to create the service without any env vars."
    )


@dp.message(CreateService.waiting_env)
async def msg_create_env(message: Message, state: FSMContext):
    text     = message.text.strip()
    fsm_data = await state.get_data()

    env_vars = []
    if text.lower() != "skip":
        for line in text.splitlines():
            line = line.strip()
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            env_vars.append({"key": k.strip(), "value": v.strip()})

    await state.clear()

    user          = await get_user(message.from_user.id)
    account_index = user.get("temp_account_index")
    token         = user["accounts"][account_index]["token"]

    await message.answer("⏳ Creating service...")

    # Build DeploymentDefinition
    source_type   = fsm_data["source_type"]
    instance_type = fsm_data["instance_type"]
    region        = fsm_data["region"]
    svc_type      = fsm_data["svc_type"]
    ports_cfg     = fsm_data.get("ports")

    definition = {
        "name":    fsm_data["service_name"],
        "type":    svc_type,
        "regions": [region],
        "instance_types": [{"type": instance_type}],
        "scalings": [{"min": 1, "max": 1}],
        "env": env_vars,
    }

    if ports_cfg:
        definition["ports"]  = [ports_cfg]
        definition["routes"] = [{"port": ports_cfg["port"], "path": "/"}]

    if source_type == "docker":
        definition["docker"] = {"image": fsm_data["docker_image"]}
    else:
        definition["git"] = {
            "repository": fsm_data["git_repo"],
            "branch":     fsm_data["git_branch"],
        }

    payload = {
        "app_id":     fsm_data["app_id"],
        "definition": definition,
    }

    status, data = await koyeb_request(token, "POST", "/v1/services", payload=payload)

    if status not in (200, 201, 202):
        err = data.get("message", str(data)) if isinstance(data, dict) else str(data)
        return await message.answer(
            f"❌ Service creation failed (HTTP {status}):\n<code>{err}</code>",
            reply_markup=home_keyboard,
        )

    new_svc = data.get("service", {})
    svc_id  = new_svc.get("id", "?")

    await message.answer(
        f"✅ <b>Service created successfully!</b>\n\n"
        f"Name: <b>{fsm_data['service_name']}</b>\n"
        f"ID:   <code>{svc_id}</code>\n\n"
        f"<i>Deployment is now starting. Check status in Services.</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⚙ View Services", callback_data="services")
        ]])
    )


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

async def main():
    print("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
