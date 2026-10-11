# v8 - status list + auto-refresh
import os
import io
import json
import time
import asyncio
import aiohttp
import discord
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

BUMP_COOLDOWN = 5
AUTO_STATUS_INTERVAL = 60  # detik

# Role yang dianggap "ON" (case-insensitive)
ON_ROLES = ["WORKER ON", "OWNER ON", "WORKER", "OWNER"]

# === KATEGORI ===
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

# === BOT SETUP ===
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True  # WAJIB: aktifin Server Members Intent di Developer Portal
bot = commands.Bot(command_prefix="!", intents=intents)

_last_bump = {}
_last_bump_msg = {}


# ============================================================
# HELPERS
# ============================================================
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
    """Return True kalau user punya role ON_ROLES."""
    if not member or not hasattr(member, "roles"):
        return False
    user_roles = [r.name.upper() for r in member.roles]
    return any(role.upper() in user_roles for role in ON_ROLES)


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
# STATUS DB (auto-refresh)
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
# BUILD EMBED STATUS (multi-user)
# ============================================================
def build_status_embed(members: list, title: str = "📊 Status Worker") -> discord.Embed:
    embed = discord.Embed(title=title, color=COLOR_STATUS)

    if not members:
        embed.description = "*Belum ada user yang di-set.*"
        return embed

    lines = []
    for m in members:
        if m is None:
            lines.append("❔ *User tidak ditemukan*")
            continue

        is_on = check_user_status(m)
        indicator = "🟢" if is_on else "🔴"
        status_text = "ON" if is_on else "OFF"
        lines.append(f"{indicator} {m.mention} — **{status_text}**")

    embed.description = "\n".join(lines)
    embed.set_footer(text=f"🔄 Auto-refresh tiap 1 menit • Update: {discord.utils.utcnow().strftime('%H:%M:%S')} UTC")
    return embed


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
# SLASH COMMAND: /status
# ============================================================
@bot.tree.command(name="status", description="Cek status worker (single user)")
@app_commands.describe(user="User yang mau dicek (opsional)")
async def status(interaction: discord.Interaction, user: discord.Member = None):
    target = user or interaction.user

    is_on = check_user_status(target)
    if is_on:
        status_text = "🟢 **ON**"
        status_desc = f"{target.mention} sedang **ON** — siap menerima order!"
        color = COLOR_DONE
    else:
        status_text = "🔴 **OFF**"
        status_desc = f"{target.mention} sedang **OFF** — lagi gak available."
        color = COLOR_ERROR

    embed = discord.Embed(
        title=f"📊 Status — {target.display_name}",
        description=status_desc,
        color=color,
    )
    embed.add_field(name="Status", value=status_text, inline=True)
    embed.add_field(name="User", value=target.mention, inline=True)
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.set_footer(text="Status berdasarkan role server")

    await interaction.response.send_message(embed=embed)


# ============================================================
# SLASH COMMAND: /statuslist (multi-user, 1 embed)
# ============================================================
@bot.tree.command(name="statuslist", description="Cek status beberapa worker sekaligus (1 embed)")
@app_commands.describe(
    user1="User pertama",
    user2="User kedua (opsional)",
    user3="User ketiga (opsional)",
    user4="User keempat (opsional)",
    user5="User kelima (opsional)",
)
async def statuslist(
    interaction: discord.Interaction,
    user1: discord.Member,
    user2: discord.Member = None,
    user3: discord.Member = None,
    user4: discord.Member = None,
    user5: discord.Member = None,
):
    members = [u for u in [user1, user2, user3, user4, user5] if u is not None]

    embed = build_status_embed(members, title="📊 Status Worker")
    await interaction.response.send_message(embed=embed)


# ============================================================
# SLASH COMMAND: /statusauto (set auto-refresh)
# ============================================================
@bot.tree.command(name="statusauto", description="Set auto-refresh status di channel (tiap 1 menit)")
@app_commands.describe(
    channel="Channel buat nampilin status",
    user1="User pertama",
    user2="User kedua (opsional)",
    user3="User ketiga (opsional)",
    user4="User keempat (opsional)",
    user5="User kelima (opsional)",
)
async def statusauto(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    user1: discord.Member,
    user2: discord.Member = None,
    user3: discord.Member = None,
    user4: discord.Member = None,
    user5: discord.Member = None,
):
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message(
            "🚫 Kamu butuh permission `Manage Messages`!", ephemeral=True
        )
        return

    members = [u for u in [user1, user2, user3, user4, user5] if u is not None]
    user_ids = [str(u.id) for u in members]

    await interaction.response.defer(thinking=True)

    db = load_status_db()
    ch_id = str(channel.id)

    # Kirim pesan pertama
    embed = build_status_embed(members, title="📊 Status Worker")
    try:
        msg = await channel.send(embed=embed)
    except discord.HTTPException as e:
        await interaction.followup.send(f"❌ Gagal kirim ke channel: `{e}`")
        return

    # Simpan config
    db[ch_id] = {
        "channel_id": ch_id,
        "guild_id": str(interaction.guild_id),
        "message_id": str(msg.id),
        "user_ids": user_ids,
        "set_by": str(interaction.user.id),
        "set_at": str(discord.utils.utcnow()),
    }
    save_status_db(db)

    embed_reply = discord.Embed(
        title="✅ Auto-Status Di-set!",
        description=(
            f"**Channel:** {channel.mention}\n"
            f"**Users:** {len(user_ids)} orang\n"
            f"**Interval:** 1 menit\n\n"
            f"Bot bakal update pesan di channel itu tiap 1 menit.\n"
            f"Stop pakai `/statusautostop` di channel itu."
        ),
        color=COLOR_DONE,
    )
    await interaction.followup.send(embed=embed_reply, ephemeral=True)


# ============================================================
# SLASH COMMAND: /statusautostop
# ============================================================
@bot.tree.command(name="statusautostop", description="Stop auto-refresh status di channel ini")
async def statusautostop(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message(
            "🚫 Kamu butuh permission `Manage Messages`!", ephemeral=True
        )
        return

    ch_id = str(interaction.channel_id)
    db = load_status_db()

    if ch_id not in db:
        await interaction.response.send_message(
            f"❌ Channel {interaction.channel.mention} bukan auto-status channel!",
            ephemeral=True
        )
        return

    del db[ch_id]
    save_status_db(db)

    await interaction.response.send_message(
        f"🗑️ Auto-status di {interaction.channel.mention} udah dimatiin.",
        ephemeral=True
    )


# ============================================================
# SLASH COMMAND: /statusautolist
# ============================================================
@bot.tree.command(name="statusautolist", description="Lihat semua auto-status channel")
async def statusautolist(interaction: discord.Interaction):
    db = load_status_db()
    guild_id = str(interaction.guild_id)

    lines = []
    for ch_id, cfg in db.items():
        if cfg.get("guild_id") == guild_id:
            ch = interaction.guild.get_channel(int(ch_id))
            if ch:
                lines.append(f"• {ch.mention} — {len(cfg.get('user_ids', []))} user")

    if not lines:
        await interaction.response.send_message(
            "📭 Belum ada auto-status channel di server ini.\n\n"
            "Set pakai `/statusauto`.",
            ephemeral=True
        )
        return

    embed = discord.Embed(
        title="📊 Auto-Status Channels",
        description="\n".join(lines),
        color=COLOR_STATUS,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


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
                "set_by": str(interaction.user.id),
                "set_at": str(discord.utils.utcnow()),
                "template": self.template_input.value,
            }
            save_anon_db(db)

            embed = discord.Embed(
                title="✅ Announcement Di-set!",
                description=(
                    f"Channel {interaction.channel.mention} sekarang **aktif**.\n\n"
                    f"**Template:**\n```\n{self.template_input.value}\n```\n"
                    f"Setiap ada chat, bot bakal **bump** template ini.\n"
                    f"Cooldown: **{BUMP_COOLDOWN} detik**.\n"
                    f"Bump lama bakal dihapus otomatis."
                ),
                color=COLOR_ANON,
            )
            embed.set_footer(text=f"Di-set oleh {interaction.user.display_name}")
            await interaction.response.send_message(embed=embed)

        elif self.mode == "edit":
            if ch_id not in db:
                await interaction.response.send_message(
                    "❌ Channel ini belum di-set. Pakai `/setanon` dulu.", ephemeral=True
                )
                return

            db[ch_id]["template"] = self.template_input.value
            save_anon_db(db)

            embed = discord.Embed(
                title="✅ Template Diupdate!",
                description=f"**Template baru:**\n```\n{self.template_input.value}\n```",
                color=COLOR_DONE,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)


# === /setanon ===
@bot.tree.command(name="setanon", description="Set channel ini jadi announcement (auto-bump)")
async def setanon(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message(
            "🚫 Kamu butuh permission `Manage Messages`!", ephemeral=True
        )
        return

    ch_id = interaction.channel_id
    if str(ch_id) in load_anon_db():
        await interaction.response.send_message(
            f"⚠️ Channel {interaction.channel.mention} udah di-set!\n"
            f"Pakai `/editanon` buat edit, `/unsetanon` buat hapus.",
            ephemeral=True
        )
        return

    await interaction.response.send_modal(AnonTemplateModal(channel_id=ch_id, mode="set"))


# === /editanon ===
@bot.tree.command(name="editanon", description="Edit template announcement")
async def editanon(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message(
            "🚫 Kamu butuh permission `Manage Messages`!", ephemeral=True
        )
        return

    if str(interaction.channel_id) not in load_anon_db():
        await interaction.response.send_message(
            f"❌ Channel {interaction.channel.mention} belum di-set.", ephemeral=True
        )
        return

    await interaction.response.send_modal(AnonTemplateModal(channel_id=interaction.channel_id, mode="edit"))


# === /unsetanon ===
@bot.tree.command(name="unsetanon", description="Hapus setting announcement")
async def unsetanon(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message(
            "🚫 Kamu butuh permission `Manage Messages`!", ephemeral=True
        )
        return

    ch_id = str(interaction.channel_id)
    db = load_anon_db()

    if ch_id not in db:
        await interaction.response.send_message(
            f"❌ Channel {interaction.channel.mention} bukan announcement channel!",
            ephemeral=True
        )
        return

    del db[ch_id]
    save_anon_db(db)

    await interaction.response.send_message(
        f"🗑️ Setting dihapus dari {interaction.channel.mention}.", ephemeral=True
    )


# === /listanon ===
@bot.tree.command(name="listanon", description="Lihat semua announcement channel")
async def listanon(interaction: discord.Interaction):
    db = load_anon_db()
    guild_id = str(interaction.guild_id)

    lines = []
    for ch_id, cfg in db.items():
        if cfg.get("guild_id") == guild_id:
            ch = interaction.guild.get_channel(int(ch_id))
            if ch:
                lines.append(f"• {ch.mention}")

    if not lines:
        await interaction.response.send_message(
            "📭 Belum ada announcement channel.\nSet pakai `/setanon`.",
            ephemeral=True
        )
        return

    embed = discord.Embed(
        title="📢 Announcement Channels",
        description="\n".join(lines),
        color=COLOR_ANON,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# === /ping ===
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

    # Hapus bump lama
    old_msg = _last_bump_msg.get(ch_id)
    if old_msg:
        try:
            await old_msg.delete()
        except discord.HTTPException:
            pass

    # Kirim bump baru
    try:
        new_msg = await message.channel.send(template)
        _last_bump[ch_id] = now
        _last_bump_msg[ch_id] = new_msg
    except discord.HTTPException as e:
        print(f"[anon bump error] {e}")

    await bot.process_commands(message)


# ============================================================
# TASK: AUTO-REFRESH STATUS (tiap 1 menit)
# ============================================================
@tasks.loop(seconds=AUTO_STATUS_INTERVAL)
async def auto_status_refresh():
    db = load_status_db()
    if not db:
        return

    for ch_id, cfg in list(db.items()):
        try:
            channel = bot.get_channel(int(ch_id))
            if not channel:
                continue

            # Ambil member
            members = []
            for uid in cfg.get("user_ids", []):
                try:
                    m = await channel.guild.fetch_member(int(uid))
                    members.append(m)
                except Exception:
                    members.append(None)

            # Build embed
            embed = build_status_embed(members, title="📊 Status Worker")

            # Edit pesan lama
            msg_id = cfg.get("message_id")
            if msg_id:
                try:
                    msg = await channel.fetch_message(int(msg_id))
                    await msg.edit(embed=embed)
                    continue
                except discord.NotFound:
                    pass  # pesan kehapus, kirim ulang
                except discord.HTTPException as e:
                    print(f"[status refresh error] {e}")
                    continue

            # Kalau gak ada pesan, kirim baru
            new_msg = await channel.send(embed=embed)
            db[ch_id]["message_id"] = str(new_msg.id)
            save_status_db(db)

        except Exception as e:
            print(f"[auto_status error] {e}")
            continue


# ============================================================
# ON READY
# ============================================================
@bot.event
async def on_ready():
    print("=" * 60)
    print(f"🤖 Bot online: {bot.user}")
    print(f"🆔 Bot ID: {bot.user.id}")
    print(f"🏠 Guilds: {[g.name for g in bot.guilds]}")
    print(f"📁 QRIS tersedia: {list_qris_available()}")
    print(f"📢 Anon channels: {list(load_anon_db().keys())}")
    print(f"📊 Auto-status channels: {list(load_status_db().keys())}")

    # Sync global
    try:
        synced = await bot.tree.sync()
        print(f"✅ Global sync: {len(synced)} commands")
    except Exception as e:
        print(f"❌ Global sync error: {e}")

    # Sync ke setiap guild (INSTAN)
    for guild in bot.guilds:
        try:
            bot.tree.copy_global_to(guild=guild)
            guild_synced = await bot.tree.sync(guild=guild)
            print(f"✅ Guild sync '{guild.name}': {len(guild_synced)} commands")
        except Exception as e:
            print(f"⚠️ Gagal sync ke guild '{guild.name}': {e}")

    # Start auto-refresh task
    if not auto_status_refresh.is_running():
        auto_status_refresh.start()
        print("🔄 Auto-status refresh task STARTED")

    print("=" * 60)
    print("🎉 Bot READY!")


# ============================================================
# RUN
# ============================================================
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("❌ DISCORD_TOKEN tidak ditemukan di environment!")
    bot.run(DISCORD_TOKEN)
