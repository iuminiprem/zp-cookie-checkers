# v2 - QRIS support - 07/10/2026
import os
import io
import asyncio
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
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

# Folder QRIS
QRIS_DIR = Path(__file__).parent / "qris"

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

# === BOT SETUP ===
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


# === HELPER ===
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

    embed = discord.Embed(
        title=f"🍪 Cookie Checker — {status_text}",
        color=color,
    )
    embed.add_field(
        name="📊 Progress",
        value=f"`{progress_bar(checked, total)}` **{checked}/{total}** ({pct}%)",
        inline=False,
    )
    for cat in CATEGORIES:
        count = data.get(f"{cat['key']}_count", 0)
        embed.add_field(
            name=f"{cat['emoji']} {cat['name']}",
            value=f"**{count}**",
            inline=True,
        )
    embed.add_field(name="\u200b", value="\u200b", inline=True)

    if status == "pending" and data.get("queue_position"):
        embed.add_field(
            name="📌 Posisi Antrian",
            value=f"**{data['queue_position']}**",
            inline=False,
        )
    embed.set_footer(text=f"Session: {session_id}")
    return embed


def is_allowed(user_id: int) -> bool:
    if not WHITELIST:
        return True
    return user_id in WHITELIST


def find_qris(name: str):
    """Cari file QRIS di folder qris/"""
    if not QRIS_DIR.exists():
        return None
    name_lower = name.lower().strip()
    for ext in ("png", "jpg", "jpeg", "webp", "gif"):
        p = QRIS_DIR / f"{name_lower}.{ext}"
        if p.exists():
            return p
    return None


def list_qris_available():
    """List semua QRIS yang ada di folder"""
    if not QRIS_DIR.exists():
        return []
    names = set()
    for f in QRIS_DIR.iterdir():
        if f.is_file() and f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            names.add(f.stem.lower())
    return sorted(names)


# === SLASH COMMAND: /check ===
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


# === SLASH COMMAND: /pay ===
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


# === SLASH COMMAND: /ping ===
@bot.tree.command(name="ping", description="Cek bot masih hidup")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"🏓 Pong! Latency: `{round(bot.latency * 1000)}ms`", ephemeral=True
    )


# === ON READY ===
@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"✅ Bot online: {bot.user}")
        print(f"✅ Synced {len(synced)} slash commands")
        print(f"💳 QRIS tersedia: {list_qris_available()}")
        if WHITELIST:
            print(f"🔒 Whitelist aktif: {WHITELIST}")
        else:
            print("🌐 Whitelist OFF")
    except Exception as e:
        print(f"❌ Sync error: {e}")


# === RUN ===
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("❌ DISCORD_TOKEN tidak ditemukan di environment!")
    bot.run(DISCORD_TOKEN)
