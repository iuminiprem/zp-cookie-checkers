# v12 - reaction role (button) + all features
import os
import io
import json
import time
import asyncio
import aiohttp
import discord
from datetime import datetime, timezone, timedelta
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
from pathlib import Path

load_dotenv()

# === KONFIG ===
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DEFAULT_API_KEY = os.getenv("DEFAULT_API_KEY", "")
ZP_API_BASE = "https://zeropoint.to/api/cookie-checker-api"

WHITELIST_RAW = os.getenv("WHITELIST_USERS", "")
WHITELIST = [int(x.strip()) for x in WHITELIST_RAW.split(",") if x.strip().isdigit()]

POLL_INTERVAL = 2
MAX_POLLS = 600

QRIS_DIR = Path(__file__).parent / "qris"
ANON_DB = Path(__file__).parent / "anon_channels.json"
STATUS_DB = Path(__file__).parent / "status_auto.json"
GETROLE_DB = Path(__file__).parent / "getrole.json"

BUMP_COOLDOWN = 5
AUTO_STATUS_INTERVAL = 60

WIB = timezone(timedelta(hours=7))

CATEGORIES = [
    {"key": "alive",        "name": "Alive",        "emoji": "🟢"},
    {"key": "dead",         "name": "Dead",         "emoji": "🔴"},
    {"key": "face_lock",    "name": "Face Lock",    "emoji": "🟡"},
    {"key": "captcha_lock", "name": "Captcha Lock", "emoji": "🟣"},
    {"key": "ban_warn",     "name": "Ban Warn",     "emoji": "🟠"},
]

COLOR_CHECKING = 0x22d3ee
COLOR_DONE     = 0x22c55e
COLOR_ERROR    = 0xff4d6d
COLOR_ANON     = 0x9b59b6
COLOR_STATUS   = 0x5865f2
COLOR_GETROLE  = 0x9b59b6

# Button styles rotation
BUTTON_STYLES = [
    discord.ButtonStyle.primary,    # Blue
    discord.ButtonStyle.success,    # Green
    discord.ButtonStyle.danger,     # Red
    discord.ButtonStyle.secondary,  # Gray
]

# === BOT SETUP ===
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.presences = True
bot = commands.Bot(command_prefix="!", intents=intents)

_last_bump = {}
_last_bump_msg = {}


# ============================================================
# HELPERS
# ============================================================
def now_wib() -> str:
    return datetime.now(WIB).strftime("%H:%M:%S")


def progress_bar(current, total, length=20):
    if total <= 0:
        return "░" * length
    filled = int(length * current / total)
    return "█" * filled + "░" * (length - filled)


def build_check_embed(data, session_id):
    status = data.get("status", "unknown")
    total = data.get("total", 0)
    checked = data.get("checked", 0)
    pct = int(100 * checked / total) if total > 0 else 0

    status_map = {
        "pending":   "⏳ Menunggu antrian",
        "checking":  "🔄 Sedang memeriksa",
        "completed": "✅ Selesai!",
        "error":     "❌ Error",
    }
    status_text = status_map.get(status, status)

    color = COLOR_CHECKING
    if status == "completed":
        color = COLOR_DONE
    elif status == "error":
        color = COLOR_ERROR

    embed = discord.Embed(title=f"🍪 Cookie Checker — {status_text}", color=color)
    embed.add_field(
        name="📊 Progress",
        value=f"`{progress_bar(checked, total)}` **{checked}/{total}** ({pct}%)",
        inline=False,
    )
    for cat in CATEGORIES:
        count = data.get(f"{cat['key']}_count", 0)
        embed.add_field(name=f"{cat['emoji']} {cat['name']}", value=f"**{count}**", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)

    if status == "pending" and data.get("queue_position"):
        embed.add_field(name="📌 Posisi Antrian", value=f"**{data['queue_position']}**", inline=False)
    embed.set_footer(text=f"Session: {session_id}")
    return embed


def is_allowed(user_id: int) -> bool:
    if not WHITELIST:
        return True
    return user_id in WHITELIST


def find_qris(name: str):
    if not QRIS_DIR.exists():
        return None
    name_lower = name.lower().strip()
    for ext in ("png", "jpg", "jpeg", "webp", "gif"):
        p = QRIS_DIR / f"{name_lower}.{ext}"
        if p.exists():
            return p
    return None


def list_qris_available():
    if not QRIS_DIR.exists():
        return []
    names = set()
    for f in QRIS_DIR.iterdir():
        if f.is_file() and f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            names.add(f.stem.lower())
    return sorted(names)


def check_user_status(member: discord.Member) -> bool:
    if not member:
        return False
    return member.status != discord.Status.offline


def get_status_emoji(member: discord.Member) -> str:
    if not member:
        return "⚫"
    status = member.status
    if status == discord.Status.online:
        return "🟢"
    elif status == discord.Status.idle:
        return "🟡"
    elif status == discord.Status.dnd:
        return "🔴"
    return "⚫"


def get_status_text(member: discord.Member) -> str:
    if not member:
        return "OFF"
    return "ON" if member.status != discord.Status.offline else "OFF"


# ============================================================
# ANON DB
# ============================================================
def load_anon_db() -> dict:
    if not ANON_DB.exists():
        return {}
    try:
        with open(ANON_DB, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_anon_db(data: dict):
    try:
        with open(ANON_DB, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ Gagal save anon db: {e}")


def is_anon_channel(channel_id: int) -> bool:
    return str(channel_id) in load_anon_db()


def get_anon_config(channel_id: int) -> dict:
    return load_anon_db().get(str(channel_id), {})


# ============================================================
# STATUS DB
# ============================================================
def load_status_db() -> dict:
    if not STATUS_DB.exists():
        return {}
    try:
        with open(STATUS_DB, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_status_db(data: dict):
    try:
        with open(STATUS_DB, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ Gagal save status db: {e}")


# ============================================================
# GETROLE DB
# ============================================================
def load_getrole_db() -> dict:
    if not GETROLE_DB.exists():
        return {}
    try:
        with open(GETROLE_DB, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_getrole_db(data: dict):
    try:
        with open(GETROLE_DB, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ Gagal save getrole db: {e}")


# ============================================================
# BUILD STATUS EMBED
# ============================================================
def build_status_embed(members: list, title: str) -> discord.Embed:
    embed = discord.Embed(title=title, color=COLOR_STATUS)

    if not members:
        embed.description = "*Belum ada user yang di-set.*"
    else:
        lines = []
        for m in members:
            if m is None:
                lines.append("⚫ *User tidak ditemukan*")
                continue
            emoji = get_status_emoji(m)
            status_text = get_status_text(m)
            lines.append(f"{emoji} **{m.display_name}** — `{status_text}`")
        embed.description = "\n\n".join(lines)

    embed.set_footer(text=f"🔄 Auto-refresh tiap 1 menit • Update: {now_wib()} WIB")
    return embed


# ============================================================
# GETROLE VIEW (Persistent)
# ============================================================
class GetRoleButton(discord.ui.Button):
    def __init__(self, role_id: int, role_name: str, emoji: str, style: discord.ButtonStyle, row: int):
        super().__init__(
            label=role_name[:80],  # Max 80 char
            emoji=emoji,
            style=style,
            custom_id=f"getrole_{role_id}",
            row=row,
        )
        self.role_id = role_id

    async def callback(self, interaction: discord.Interaction):
        role = interaction.guild.get_role(self.role_id)
        if not role:
            await interaction.response.send_message(
                "❌ Role udah gak ada di server.", ephemeral=True
            )
            return

        member = interaction.user

        # Cek apakah user udah punya role ini
        if role in member.roles:
            # Hapus role
            try:
                await member.remove_roles(role, reason="GetRole toggle")
                await interaction.response.send_message(
                    f"🗑️ Role **{role.name}** dihapus dari kamu.",
                    ephemeral=True,
                )
            except discord.Forbidden:
                await interaction.response.send_message(
                    "❌ Bot gak punya izin buat hapus role ini.",
                    ephemeral=True,
                )
        else:
            # Kasih role
            try:
                await member.add_roles(role, reason="GetRole toggle")
                await interaction.response.send_message(
                    f"✅ Role **{role.name}** dikasih ke kamu!",
                    ephemeral=True,
                )
            except discord.Forbidden:
                await interaction.response.send_message(
                    "❌ Bot gak punya izin buat kasih role ini.",
                    ephemeral=True,
                )


class GetRoleView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)  # Persistent

        # Load roles dari DB
        db = load_getrole_db()
        cfg = db.get(str(guild_id), {})
        roles = cfg.get("roles", [])

        # Batas 25 role (5x5 grid)
        roles = roles[:25]

        # Bikin button per role
        for i, r in enumerate(roles):
            role_id = int(r["role_id"])
            role_name = r.get("role_name", "Role")
            emoji = r.get("emoji", "🔹")
            style = BUTTON_STYLES[i % len(BUTTON_STYLES)]
            row = i // 5  # Max 5 button per row

            self.add_item(GetRoleButton(
                role_id=role_id,
                role_name=role_name,
                emoji=emoji,
                style=style,
                row=row,
            ))


# ============================================================
# SLASH COMMAND: /check
# ============================================================
@bot.tree.command(name="check", description="Cek cookie Roblox (upload .txt atau paste)")
@app_commands.describe(
    file="File .txt berisi cookies",
    text="Atau paste cookie langsung",
    api_key="API Key ZeroPoint (opsional)",
)
async def check(
    interaction: discord.Interaction,
    file: discord.Attachment = None,
    text: str = None,
    api_key: str = None,
):
    if not is_allowed(interaction.user.id):
        await interaction.response.send_message("🚫 Kamu gak ada di whitelist bro!", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)

    cookies_raw = ""
    if file:
        if not file.filename.lower().endswith(".txt"):
            await interaction.followup.send("❌ File harus `.txt` bro!")
            return
        if file.size > 5 * 1024 * 1024:
            await interaction.followup.send("❌ File max 5MB!")
            return
        cookies_raw = (await file.read()).decode("utf-8", errors="ignore").strip()
    elif text:
        cookies_raw = text.strip()
    else:
        await interaction.followup.send("❌ Kasih file `.txt` atau paste cookie pakai parameter `text`!")
        return

    if not cookies_raw:
        await interaction.followup.send("❌ Cookie kosong!")
        return

    used_key = api_key or DEFAULT_API_KEY
    if not used_key:
        await interaction.followup.send("❌ API Key gak ada!")
        return

    headers = {"X-API-Key": used_key, "Content-Type": "application/json"}

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(
                f"{ZP_API_BASE}/submit",
                json={"cookies": cookies_raw},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                submit_data = await r.json(content_type=None)
                if r.status != 200:
                    err_map = {
                        401: "❌ API Key tidak valid (401)",
                        403: "❌ API Key dinonaktifkan (403)",
                        400: "❌ Tidak ada cookie valid (400)",
                        503: "❌ Service ZeroPoint lagi down (503)",
                    }
                    msg = err_map.get(r.status, f"❌ HTTP {r.status}")
                    if r.status == 429:
                        cd = submit_data.get("cooldown", "?")
                        msg = f"⏰ Rate limit! Tunggu **{cd} detik**"
                    await interaction.followup.send(msg)
                    return
                session_id = submit_data.get("session_id")
                total = submit_data.get("total", 0)
                if not session_id:
                    await interaction.followup.send("❌ Response gak ada session_id!")
                    return
        except Exception as e:
            await interaction.followup.send(f"❌ Gagal submit: `{e}`")
            return

    msg = await interaction.followup.send(
        embed=build_check_embed({"status": "pending", "total": total, "checked": 0}, session_id)
    )

    last_status = None
    poll_count = 0
    while poll_count < MAX_POLLS:
        await asyncio.sleep(POLL_INTERVAL)
        poll_count += 1
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{ZP_API_BASE}/status/{session_id}",
                    headers={"X-API-Key": used_key},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    if r.status == 404:
                        await msg.edit(content="❌ Session hilang / expired.", embed=None)
                        return
                    data = await r.json(content_type=None)
                    status = data.get("status")
                    if (status != last_status or poll_count % 3 == 0
                            or status in ("completed", "error")):
                        try:
                            await msg.edit(embed=build_check_embed(data, session_id))
                        except discord.HTTPException:
                            pass
                        last_status = status
                    if status == "completed":
                        await send_check_results(msg, session_id, data, used_key)
                        return
                    if status == "error":
                        await msg.edit(content="❌ Server error pas checking.", embed=None)
                        return
        except Exception as e:
            print(f"[poll error] {e}")
            continue

    await msg.edit(content="⏰ Timeout, server terlalu lama.", embed=None)


async def send_check_results(msg, session_id, data, api_key):
    files_to_send = []
    async with aiohttp.ClientSession() as session:
        for cat in CATEGORIES:
            count = data.get(f"{cat['key']}_count", 0)
            if count == 0:
                continue
            try:
                async with session.get(
                    f"{ZP_API_BASE}/download/{session_id}/{cat['key']}",
                    headers={"X-API-Key": api_key},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    if r.status == 200:
                        content = await r.read()
                        files_to_send.append(
                            discord.File(io.BytesIO(content), filename=f"{cat['key']}_{session_id}.txt")
                        )
            except Exception as e:
                print(f"[download {cat['key']} error] {e}")

    lines = [f"✅ **Selesai!** — Total **{data.get('total', 0)}** cookies\n"]
    for cat in CATEGORIES:
        count = data.get(f"{cat['key']}_count", 0)
        lines.append(f"{cat['emoji']} **{cat['name']}**: {count}")
    summary = "\n".join(lines)

    if files_to_send:
        if len(files_to_send) > 10:
            await msg.edit(content=summary + "\n*(kebanyakan file, kirim sebagian)*", embed=None,
                           attachments=files_to_send[:10])
            await msg.channel.send(files=files_to_send[10:])
        else:
            await msg.edit(content=summary, embed=None, attachments=files_to_send)
    else:
        await msg.edit(content=summary + "\n*(tidak ada file hasil)*", embed=None)


# ============================================================
# SLASH COMMAND: /pay
# ============================================================
@bot.tree.command(name="pay", description="Tampilkan QRIS pembayaran")
@app_commands.describe(target="Nama target (contoh: msky, bx, ame)")
async def pay(interaction: discord.Interaction, target: str):
    await interaction.response.defer(thinking=True)

    target_lower = target.lower().strip()
    qris_path = find_qris(target_lower)

    if not qris_path:
        available = list_qris_available()
        desc = f"❌ QRIS untuk **{target}** gak ditemukan."
        if available:
            desc += f"\n\nYang tersedia: {', '.join(f'`{n}`' for n in available)}"
        embed = discord.Embed(title="❌ Tidak Ditemukan", description=desc, color=COLOR_ERROR)
        await interaction.followup.send(embed=embed)
        return

    embed = discord.Embed(
        title=f"💳 QRIS — {target.upper()}",
        description=f"Silakan scan QRIS di bawah untuk pembayaran **{target.upper()}**.",
        color=0x5865f2,
    )

    file = discord.File(str(qris_path), filename=qris_path.name)
    embed.set_image(url=f"attachment://{qris_path.name}")
    embed.set_footer(text=f"Requested by {interaction.user.display_name}")

    await interaction.followup.send(embed=embed, file=file)


# ============================================================
# SLASH COMMAND: /statusauto
# ============================================================
@bot.tree.command(name="statusauto", description="Setup auto-status di channel ini")
@app_commands.describe(
    title="Judul embed (contoh: WORKER YANG ONLINE)",
    user1="User pertama",
    user2="User kedua (opsional)",
    user3="User ketiga (opsional)",
    user4="User keempat (opsional)",
    user5="User kelima (opsional)",
)
async def statusauto(
    interaction: discord.Interaction,
    title: str,
    user1: discord.Member,
    user2: discord.Member = None,
    user3: discord.Member = None,
    user4: discord.Member = None,
    user5: discord.Member = None,
):
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message("🚫 Butuh `Manage Messages`!", ephemeral=True)
        return

    ch_id = str(interaction.channel_id)
    db = load_status_db()

    if ch_id in db:
        await interaction.response.send_message(
            f"⚠️ Channel udah di-set! Stop dulu pakai `/statusautostop`.", ephemeral=True
        )
        return

    members = [u for u in [user1, user2, user3, user4, user5] if u is not None]
    user_ids = [str(u.id) for u in members]

    await interaction.response.defer(thinking=True)

    db[ch_id] = {
        "channel_id": ch_id,
        "guild_id": str(interaction.guild_id),
        "title": title,
        "user_ids": user_ids,
        "message_id": None,
    }
    save_status_db(db)

    embed = build_status_embed(members, title)
    try:
        msg = await interaction.channel.send(embed=embed)
        db[ch_id]["message_id"] = str(msg.id)
        save_status_db(db)
    except discord.HTTPException as e:
        await interaction.followup.send(f"❌ Gagal kirim: `{e}`")
        return

    await interaction.followup.send(
        f"✅ Auto-status di-set di {interaction.channel.mention}!\n"
        f"Tambah/hapus user: `/statusautoadd` / `/statusautoremove`",
        ephemeral=True,
    )


@bot.tree.command(name="statusautoadd", description="Tambah user ke auto-status")
async def statusautoadd(interaction: discord.Interaction, user: discord.Member):
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message("🚫 Butuh `Manage Messages`!", ephemeral=True)
        return
    ch_id = str(interaction.channel_id)
    db = load_status_db()
    if ch_id not in db:
        await interaction.response.send_message("❌ Channel belum di-set.", ephemeral=True)
        return
    uid = str(user.id)
    if uid in db[ch_id]["user_ids"]:
        await interaction.response.send_message(f"⚠️ {user.mention} udah ada!", ephemeral=True)
        return
    if len(db[ch_id]["user_ids"]) >= 20:
        await interaction.response.send_message("⚠️ Max 20 user!", ephemeral=True)
        return
    db[ch_id]["user_ids"].append(uid)
    save_status_db(db)
    await refresh_status_message(ch_id)
    await interaction.response.send_message(f"✅ {user.mention} ditambah!", ephemeral=True)


@bot.tree.command(name="statusautoremove", description="Hapus user dari auto-status")
async def statusautoremove(interaction: discord.Interaction, user: discord.Member):
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message("🚫 Butuh `Manage Messages`!", ephemeral=True)
        return
    ch_id = str(interaction.channel_id)
    db = load_status_db()
    if ch_id not in db:
        await interaction.response.send_message("❌ Channel belum di-set.", ephemeral=True)
        return
    uid = str(user.id)
    if uid not in db[ch_id]["user_ids"]:
        await interaction.response.send_message(f"⚠️ {user.mention} gak ada!", ephemeral=True)
        return
    db[ch_id]["user_ids"].remove(uid)
    save_status_db(db)
    await refresh_status_message(ch_id)
    await interaction.response.send_message(f"🗑️ {user.mention} dihapus!", ephemeral=True)


@bot.tree.command(name="statusautostop", description="Stop auto-status di channel ini")
async def statusautostop(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message("🚫 Butuh `Manage Messages`!", ephemeral=True)
        return
    ch_id = str(interaction.channel_id)
    db = load_status_db()
    if ch_id not in db:
        await interaction.response.send_message("❌ Bukan auto-status channel!", ephemeral=True)
        return
    msg_id = db[ch_id].get("message_id")
    if msg_id:
        try:
            msg = await interaction.channel.fetch_message(int(msg_id))
            await msg.delete()
        except Exception:
            pass
    del db[ch_id]
    save_status_db(db)
    await interaction.response.send_message(f"🗑️ Auto-status dimatiin.", ephemeral=True)


# ============================================================
# SLASH COMMAND: /getrole (reaction role)
# ============================================================
getrole_group = app_commands.Group(name="getrole", description="Setup reaction role")


@getrole_group.command(name="add", description="Tambah role ke panel")
@app_commands.describe(
    role="Role yang mau dikasih",
    emoji="Emoji buat tombol (opsional)",
)
async def getrole_add(
    interaction: discord.Interaction,
    role: discord.Role,
    emoji: str = "🔹",
):
    if not interaction.user.guild_permissions.manage_roles:
        await interaction.response.send_message("🚫 Butuh `Manage Roles`!", ephemeral=True)
        return

    # Cek bot bisa kasih role ini
    if role >= interaction.guild.me.top_role:
        await interaction.response.send_message(
            "❌ Role ini lebih tinggi dari role bot. Pindahin role bot ke atas dulu.",
            ephemeral=True,
        )
        return

    if role.is_default():
        await interaction.response.send_message("❌ Gak bisa pakai role @everyone!", ephemeral=True)
        return

    guild_id = str(interaction.guild_id)
    db = load_getrole_db()

    if guild_id not in db:
        db[guild_id] = {"roles": []}

    # Cek udah ada?
    for r in db[guild_id]["roles"]:
        if r["role_id"] == str(role.id):
            await interaction.response.send_message(
                f"⚠️ Role {role.mention} udah ada di panel!", ephemeral=True
            )
            return

    # Cek limit
    if len(db[guild_id]["roles"]) >= 25:
        await interaction.response.send_message(
            "⚠️ Max 25 role per panel!", ephemeral=True
        )
        return

    # Tambah
    db[guild_id]["roles"].append({
        "role_id": str(role.id),
        "role_name": role.name,
        "emoji": emoji,
    })
    save_getrole_db(db)

    await interaction.response.send_message(
        f"✅ Role {role.mention} + {emoji} ditambah!\n"
        f"Total: {len(db[guild_id]['roles'])} role\n\n"
        f"Kirim panel pakai `/getrole send`",
        ephemeral=True,
    )


@getrole_group.command(name="remove", description="Hapus role dari panel")
@app_commands.describe(role="Role yang mau dihapus")
async def getrole_remove(interaction: discord.Interaction, role: discord.Role):
    if not interaction.user.guild_permissions.manage_roles:
        await interaction.response.send_message("🚫 Butuh `Manage Roles`!", ephemeral=True)
        return

    guild_id = str(interaction.guild_id)
    db = load_getrole_db()

    if guild_id not in db:
        await interaction.response.send_message("❌ Belum ada role di panel!", ephemeral=True)
        return

    original = len(db[guild_id]["roles"])
    db[guild_id]["roles"] = [
        r for r in db[guild_id]["roles"] if r["role_id"] != str(role.id)
    ]

    if len(db[guild_id]["roles"]) == original:
        await interaction.response.send_message(
            f"⚠️ Role {role.mention} gak ada di panel!", ephemeral=True
        )
        return

    save_getrole_db(db)
    await interaction.response.send_message(
        f"🗑️ Role {role.mention} dihapus dari panel!", ephemeral=True
    )


@getrole_group.command(name="list", description="Lihat semua role di panel")
async def getrole_list(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    db = load_getrole_db()
    roles = db.get(guild_id, {}).get("roles", [])

    if not roles:
        await interaction.response.send_message(
            "📭 Belum ada role di panel. Tambah pakai `/getrole add`.",
            ephemeral=True,
        )
        return

    lines = []
    for i, r in enumerate(roles, 1):
        lines.append(f"{i}. {r['emoji']} → <@&{r['role_id']}>")

    embed = discord.Embed(
        title=f"📋 Get Role List ({len(roles)} role)",
        description="\n".join(lines),
        color=COLOR_GETROLE,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@getrole_group.command(name="send", description="Kirim panel get-role ke channel")
@app_commands.describe(
    channel="Channel tujuan",
    title="Judul panel (opsional)",
    description="Deskripsi panel (opsional)",
)
async def getrole_send(
    interaction: discord.Interaction,
    channel: discord.TextChannel = None,
    title: str = "🎭 Ambil Role Kamu!",
    description: str = "Klik tombol di bawah untuk ambil / lepas role.\nKlik lagi untuk lepas role.",
):
    if not interaction.user.guild_permissions.manage_roles:
        await interaction.response.send_message("🚫 Butuh `Manage Roles`!", ephemeral=True)
        return

    target = channel or interaction.channel
    guild_id = str(interaction.guild_id)
    db = load_getrole_db()
    roles = db.get(guild_id, {}).get("roles", [])

    if not roles:
        await interaction.response.send_message(
            "❌ Belum ada role. Tambah dulu pakai `/getrole add`.",
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title=title,
        description=description,
        color=COLOR_GETROLE,
    )

    # List role di deskripsi
    role_lines = "\n".join(f"{r['emoji']} **{r['role_name']}**" for r in roles)
    embed.add_field(name="📋 Role Tersedia", value=role_lines, inline=False)
    embed.set_footer(text="Powered by Cek Zzz Solve")

    view = GetRoleView(interaction.guild_id)

    try:
        msg = await target.send(embed=embed, view=view)
    except discord.Forbidden:
        await interaction.response.send_message(
            f"❌ Bot gak punya izin kirim ke {target.mention}!", ephemeral=True
        )
        return

    # Simpan message_id
    db.setdefault(guild_id, {})["panel_message_id"] = str(msg.id)
    db[guild_id]["panel_channel_id"] = str(target.id)
    save_getrole_db(db)

    await interaction.response.send_message(
        f"✅ Panel terkirim di {target.mention}! ({len(roles)} role)",
        ephemeral=True,
    )


@getrole_group.command(name="reset", description="Hapus semua role dari panel")
async def getrole_reset(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_roles:
        await interaction.response.send_message("🚫 Butuh `Manage Roles`!", ephemeral=True)
        return

    guild_id = str(interaction.guild_id)
    db = load_getrole_db()

    if guild_id in db:
        del db[guild_id]
        save_getrole_db(db)

    await interaction.response.send_message("🗑️ Panel di-reset!", ephemeral=True)


# Daftar group ke bot
bot.tree.add_command(getrole_group)


# ============================================================
# MODAL ANON
# ============================================================
class AnonTemplateModal(discord.ui.Modal, title="Set Pesan Template"):
    template_input = discord.ui.TextInput(
        label="Pesan Template",
        style=discord.TextStyle.paragraph,
        placeholder="Contoh:\n🔧 OPEN JASA JOKI ROBLOX\n\n✅ Joki Level\n✅ Joki Rank\n\n📩 DM untuk order!",
        required=True,
        max_length=2000,
    )

    def __init__(self, channel_id: int, mode: str = "set"):
        super().__init__()
        self.channel_id = channel_id
        self.mode = mode
        if mode == "edit":
            cfg = get_anon_config(channel_id)
            if cfg.get("template"):
                self.template_input.default = cfg["template"]

    async def on_submit(self, interaction: discord.Interaction):
        db = load_anon_db()
        ch_id = str(self.channel_id)

        if self.mode == "set":
            db[ch_id] = {
                "channel_id": ch_id,
                "guild_id": str(interaction.guild_id),
                "template": self.template_input.value,
            }
            save_anon_db(db)
            await interaction.response.send_message("✅ Announcement di-set!", ephemeral=True)
        elif self.mode == "edit":
            if ch_id not in db:
                await interaction.response.send_message("❌ Belum di-set.", ephemeral=True)
                return
            db[ch_id]["template"] = self.template_input.value
            save_anon_db(db)
            await interaction.response.send_message("✅ Template diupdate!", ephemeral=True)


@bot.tree.command(name="setanon", description="Set channel jadi announcement (auto-bump)")
async def setanon(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message("🚫 Butuh `Manage Messages`!", ephemeral=True)
        return
    if str(interaction.channel_id) in load_anon_db():
        await interaction.response.send_message("⚠️ Udah di-set!", ephemeral=True)
        return
    await interaction.response.send_modal(AnonTemplateModal(channel_id=interaction.channel_id, mode="set"))


@bot.tree.command(name="editanon", description="Edit template announcement")
async def editanon(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message("🚫 Butuh `Manage Messages`!", ephemeral=True)
        return
    if str(interaction.channel_id) not in load_anon_db():
        await interaction.response.send_message("❌ Belum di-set.", ephemeral=True)
        return
    await interaction.response.send_modal(AnonTemplateModal(channel_id=interaction.channel_id, mode="edit"))


@bot.tree.command(name="unsetanon", description="Hapus setting announcement")
async def unsetanon(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message("🚫 Butuh `Manage Messages`!", ephemeral=True)
        return
    ch_id = str(interaction.channel_id)
    db = load_anon_db()
    if ch_id not in db:
        await interaction.response.send_message("❌ Bukan announcement channel!", ephemeral=True)
        return
    del db[ch_id]
    save_anon_db(db)
    await interaction.response.send_message("🗑️ Setting dihapus.", ephemeral=True)


@bot.tree.command(name="ping", description="Cek bot masih hidup")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"🏓 Pong! Latency: `{round(bot.latency * 1000)}ms`", ephemeral=True
    )


# ============================================================
# EVENT: ON MESSAGE — AUTO BUMP
# ============================================================
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        await bot.process_commands(message)
        return
    if not message.guild:
        await bot.process_commands(message)
        return
    if not is_anon_channel(message.channel.id):
        await bot.process_commands(message)
        return
    if not message.content.strip():
        await bot.process_commands(message)
        return

    ch_id = message.channel.id
    now = time.time()
    last = _last_bump.get(ch_id, 0)
    if now - last < BUMP_COOLDOWN:
        await bot.process_commands(message)
        return

    cfg = get_anon_config(ch_id)
    template = cfg.get("template", "")
    if not template:
        await bot.process_commands(message)
        return

    old_msg = _last_bump_msg.get(ch_id)
    if old_msg:
        try:
            await old_msg.delete()
        except discord.HTTPException:
            pass

    try:
        new_msg = await message.channel.send(template)
        _last_bump[ch_id] = now
        _last_bump_msg[ch_id] = new_msg
    except discord.HTTPException as e:
        print(f"[anon bump error] {e}")

    await bot.process_commands(message)


# ============================================================
# REFRESH STATUS MESSAGE
# ============================================================
async def refresh_status_message(ch_id: str):
    db = load_status_db()
    cfg = db.get(ch_id)
    if not cfg:
        return
    channel = bot.get_channel(int(ch_id))
    if not channel:
        return

    members = []
    for uid in cfg.get("user_ids", []):
        try:
            m = channel.guild.get_member(int(uid))
            if m is None:
                m = await channel.guild.fetch_member(int(uid))
            members.append(m)
        except Exception:
            members.append(None)

    embed = build_status_embed(members, cfg.get("title", "Status"))

    msg_id = cfg.get("message_id")
    if msg_id:
        try:
            msg = await channel.fetch_message(int(msg_id))
            await msg.edit(embed=embed)
            return
        except discord.NotFound:
            pass
        except discord.HTTPException as e:
            print(f"[status refresh error] {e}")
            return

    try:
        new_msg = await channel.send(embed=embed)
        db[ch_id]["message_id"] = str(new_msg.id)
        save_status_db(db)
    except discord.HTTPException as e:
        print(f"[status send error] {e}")


# ============================================================
# TASK: AUTO-REFRESH STATUS
# ============================================================
@tasks.loop(seconds=AUTO_STATUS_INTERVAL)
async def auto_status_refresh():
    db = load_status_db()
    if not db:
        return
    for ch_id in list(db.keys()):
        try:
            await refresh_status_message(ch_id)
        except Exception as e:
            print(f"[auto_status error] {e}")


# ============================================================
# EVENT: PRESENCE UPDATE — INSTANT REFRESH
# ============================================================
@bot.event
async def on_presence_update(before: discord.Member, after: discord.Member):
    db = load_status_db()
    if not db:
        return
    uid = str(after.id)
    for ch_id, cfg in db.items():
        if uid in cfg.get("user_ids", []):
            try:
                await refresh_status_message(ch_id)
            except Exception as e:
                print(f"[presence refresh error] {e}")


# ============================================================
# ON READY
# ============================================================
@bot.event
async def on_ready():
    print("=" * 60)
    print(f"🤖 Bot online: {bot.user}")
    print(f"🏠 Guilds: {[g.name for g in bot.guilds]}")

    # Register persistent views buat get-role
    for guild in bot.guilds:
        try:
            bot.add_view(GetRoleView(guild.id))
            print(f"✅ GetRole view registered for '{guild.name}'")
        except Exception as e:
            print(f"⚠️ Gagal register GetRole view: {e}")

    try:
        synced = await bot.tree.sync()
        print(f"✅ Global sync: {len(synced)} commands")
    except Exception as e:
        print(f"❌ Global sync error: {e}")

    for guild in bot.guilds:
        try:
            bot.tree.copy_global_to(guild=guild)
            gs = await bot.tree.sync(guild=guild)
            print(f"✅ Guild sync '{guild.name}': {len(gs)} commands")
        except Exception as e:
            print(f"⚠️ Gagal sync ke '{guild.name}': {e}")

    if not auto_status_refresh.is_running():
        auto_status_refresh.start()
        print("🔄 Auto-status task STARTED")

    print("=" * 60)
    print("🎉 Bot READY!")


# ============================================================
# RUN
# ============================================================
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("❌ DISCORD_TOKEN tidak ditemukan!")
    bot.run(DISCORD_TOKEN)
