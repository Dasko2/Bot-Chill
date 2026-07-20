import os
import sqlite3
import asyncio
import time as time_module
from datetime import time as dt_time
from threading import Thread

import discord
from discord import app_commands
from discord.ext import commands, tasks
from flask import Flask
from google import genai

# ==========================================
# 1. KEEP ALIVE (Serveur Flask pour Render)
# ==========================================
app = Flask('')

@app.route('/')
def home():
    return "Bot Chill est en ligne !"

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_web_server)
    t.daemon = True
    t.start()

# ==========================================
# 2. BASE DE DONNÉES SQLITE
# ==========================================
DB_NAME = "flavibot_data.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            username TEXT,
            points INTEGER DEFAULT 0,
            xp INTEGER DEFAULT 0,
            level INTEGER DEFAULT 1,
            last_daily INTEGER DEFAULT 0
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tickets (
            ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT UNIQUE,
            user_id TEXT,
            reason TEXT,
            status TEXT DEFAULT 'OPEN',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS authorized_roles (
            role_id TEXT PRIMARY KEY
        )
    ''')

    defaults = {
        'WELCOME_CHANNEL_ID': '0',
        'MSG_WELCOME_TITLE': '👋 Bienvenue {member} !',
        'MSG_WELCOME_DESC': 'Ravi de te voir parmi nous sur {guild} !',
        'TICKET_CATEGORY_ID': '0',
        'DEVINETTE_CHANNEL_ID': '0',
        'DEVINETTE_POINTS': '20',
        'DAILY_POINTS': '20',
        'DAILY_COOLDOWN': '86400'
    }

    for k, v in defaults.items():
        cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", (k, v))

    conn.commit()
    conn.close()

def get_config_val(key: str, default=""):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM config WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else default

def set_config_val(key: str, value):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()

def check_admin(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator or interaction.user.id == interaction.guild.owner_id:
        return True
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT role_id FROM authorized_roles")
    authorized_role_ids = [int(row[0]) for row in cursor.fetchall()]
    conn.close()
    user_role_ids = [role.id for role in interaction.user.roles]
    return any(r_id in authorized_role_ids for r_id in user_role_ids)

# ==========================================
# 3. INITIALISATION DU BOT
# ==========================================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

CURRENT_DEVINETTE = {
    "reponse": None,
    "active": False
}

# ==========================================
# 4. SYSTÈME DE TICKETS
# ==========================================
class TicketReasonModal(discord.ui.Modal, title="Ouvrir un Ticket"):
    reason = discord.ui.TextInput(
        label="Raison du ticket",
        style=discord.TextStyle.paragraph,
        placeholder="Décris ta demande...",
        required=True,
        max_length=500
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        category_id = int(get_config_val("TICKET_CATEGORY_ID", "0"))
        category = guild.get_channel(category_id) if category_id != 0 else None

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True)
        }

        channel_name = f"ticket-{interaction.user.name.lower()}"
        ticket_channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites
        )

        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO tickets (channel_id, user_id, reason) VALUES (?, ?, ?)",
            (str(ticket_channel.id), str(interaction.user.id), self.reason.value)
        )
        conn.commit()
        conn.close()

        embed = discord.Embed(
            title=f"🎫 Ticket de {interaction.user.display_name}",
            description=f"Raison : {self.reason.value}\n\nUn membre du staff va prendre en charge ta demande.",
            color=discord.Color.green()
        )
        await ticket_channel.send(content=f"{interaction.user.mention}", embed=embed, view=CloseTicketView())
        await interaction.followup.send(f"✅ Ton ticket a été créé : {ticket_channel.mention}", ephemeral=True)


class TicketSystemView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📩 Ouvrir un ticket", style=discord.ButtonStyle.primary, custom_id="open_ticket_btn")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TicketReasonModal())


class CloseTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔒 Fermer le ticket", style=discord.ButtonStyle.danger, custom_id="close_ticket_btn")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("🔒 Fermeture du ticket dans 5 secondes...")
        await asyncio.sleep(5)

        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("UPDATE tickets SET status = 'CLOSED' WHERE channel_id = ?", (str(interaction.channel.id),))
        conn.commit()
        conn.close()

        await interaction.channel.delete(reason="Ticket fermé")

# ==========================================
# 5. DEVINETTES GEMINI IA & ANIMATION
# ==========================================
def generer_devinette_gemini():
    """Retourne (question, reponse) ou (None, message_erreur) en cas d'échec."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        msg = "GEMINI_API_KEY manquante dans les variables d'environnement."
        print(f"❌ {msg}")
        return None, msg

    # Modèles à essayer dans l'ordre
    MODELS = ["gemini-2.0-flash", "gemini-1.5-flash", "gemini-2.5-flash"]

    prompt = (
        "Génère une devinette originale, courte et amusante en français.\n"
        "Format de réponse OBLIGATOIRE sur exactement 2 lignes :\n"
        "Devinette: [Question]\n"
        "Réponse: [Un seul mot précis en minuscules]"
    )

    last_error = "Aucun modèle n'a répondu."
    for model_name in MODELS:
        try:
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
            )

            lines = response.text.strip().split('\n')
            question, reponse = "", ""
            for line in lines:
                if line.startswith("Devinette:"):
                    question = line.replace("Devinette:", "").strip()
                elif line.startswith("Réponse:"):
                    reponse = line.replace("Réponse:", "").strip().lower()

            if question and reponse:
                return question, reponse

            last_error = f"Format invalide reçu de {model_name} : {response.text[:100]!r}"
            print(f"⚠️ {last_error}")

        except Exception as e:
            last_error = f"Erreur avec {model_name} : {e}"
            print(f"❌ {last_error}")

    return None, last_error


# Génère une liste de tous les horaires :00 et :30 de la journée
HALF_HOURS = [dt_time(hour=h, minute=m) for h in range(24) for m in (0, 30)]


async def envoyer_devinette():
    """Logique principale d'envoi d'une devinette (réutilisable par la boucle et force_devinette)."""
    dev_channel_id = int(get_config_val("DEVINETTE_CHANNEL_ID", "0"))
    if dev_channel_id == 0:
        print("⚠️ Aucun salon de devinette n'est configuré (/config_devinette_salon).")
        return

    channel = bot.get_channel(dev_channel_id)
    if not channel:
        print(f"⚠️ Salon de devinette introuvable (ID: {dev_channel_id}).")
        return

    # --- ANIMATION D'APPARITION DU MESSAGE ---
    msg = await channel.send("⏳ L'IA prépare une nouvelle devinette...\n[▱▱▱▱▱▱▱▱▱▱]")
    await asyncio.sleep(1.2)
    await msg.edit(content="🧩 Génération de la question...\n[██████▱▱▱▱]")

    question, reponse_ou_erreur = await asyncio.to_thread(generer_devinette_gemini)

    if not question:
        await msg.edit(content=f"❌ Impossible de générer une devinette.\n> {reponse_ou_erreur}")
        return

    reponse = reponse_ou_erreur

    CURRENT_DEVINETTE["reponse"] = reponse
    CURRENT_DEVINETTE["active"] = True

    pts = get_config_val("DEVINETTE_POINTS", "20")

    await msg.edit(content="✨ Devinette prête !\n[██████████]")
    await asyncio.sleep(1)

    embed = discord.Embed(
        title="🧩 Devinette du moment !",
        description=f"{question}\n\nÉcris ta réponse dans ce salon. Le premier à trouver gagne {pts} points !",
        color=discord.Color.gold()
    )
    await channel.send(embed=embed)


@tasks.loop(time=HALF_HOURS)
async def devinette_loop():
    await envoyer_devinette()


# ==========================================
# 6. ÉVÉNEMENTS DISCORD
# ==========================================
@bot.event
async def on_ready():
    init_db()
    bot.add_view(TicketSystemView())
    bot.add_view(CloseTicketView())

    if not devinette_loop.is_running():
        devinette_loop.start()
        print("⏰ Boucle de devinettes démarrée (Programmé à :00 et :30 de chaque heure).")

    try:
        synced = await bot.tree.sync()
        print(f"✅ Synchronisé {len(synced)} commandes slash.")
    except Exception as e:
        print(f"❌ Erreur synchro: {e}")

    print(f"🤖 Bot connecté sous le nom : {bot.user}")


@bot.event
async def on_member_join(member: discord.Member):
    welcome_channel_id = int(get_config_val("WELCOME_CHANNEL_ID", "0"))
    if welcome_channel_id == 0:
        return

    channel = member.guild.get_channel(welcome_channel_id)
    if not channel:
        return

    title = get_config_val("MSG_WELCOME_TITLE", "👋 Bienvenue {member} !")
    desc = get_config_val("MSG_WELCOME_DESC", "Ravi de te voir parmi nous sur {guild} !")

    title = title.replace("{member}", member.display_name).replace("{guild}", member.guild.name)
    desc = (
        desc
        .replace("{member}", member.display_name)
        .replace("{member_mention}", member.mention)
        .replace("{guild}", member.guild.name)
    )

    embed = discord.Embed(title=title, description=desc, color=discord.Color.blue())
    if member.avatar:
        embed.set_thumbnail(url=member.avatar.url)

    await channel.send(content=member.mention, embed=embed)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    dev_channel_id = int(get_config_val("DEVINETTE_CHANNEL_ID", "0"))
    if CURRENT_DEVINETTE["active"] and message.channel.id == dev_channel_id:
        reponse_user = message.content.strip().lower()
        reponse_attendue = CURRENT_DEVINETTE["reponse"]

        if reponse_attendue and reponse_attendue in reponse_user:
            CURRENT_DEVINETTE["active"] = False
            pts_gagnes = int(get_config_val("DEVINETTE_POINTS", "20"))
            user_id = str(message.author.id)

            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR IGNORE INTO users (user_id, username, points) VALUES (?, ?, 0)",
                (user_id, str(message.author))
            )
            cursor.execute(
                "UPDATE users SET points = points + ? WHERE user_id = ?",
                (pts_gagnes, user_id)
            )
            conn.commit()
            conn.close()

            await message.channel.send(
                f"🎉 Bravo {message.author.mention} ! La réponse était bien **{reponse_attendue}** !\n"
                f"💰 Tu remportes +{pts_gagnes} points !"
            )

    await bot.process_commands(message)

# ==========================================
# 7. COMMANDES SLASH
# ==========================================

# --- ANIMATION TEST ---
@bot.tree.command(name="test_animation", description="Teste l'animation dynamique de message sur Discord")
async def test_animation(interaction: discord.Interaction):
    await interaction.response.defer()

    frames = [
        "⏳ Initialisation du système...\n[▱▱▱▱▱▱▱▱▱▱] 0%",
        "⚙️ Chargement des données...\n[██▱▱▱▱▱▱▱▱] 20%",
        "🤖 Connexion à l'IA Gemini...\n[████▱▱▱▱▱▱] 40%",
        "✨ Mise en place des tickets...\n[██████▱▱▱▱] 60%",
        "🔥 Dernières vérifications...\n[████████▱▱] 80%",
        "🎉 Animation terminée avec succès !\n[██████████] 100%"
    ]

    msg = await interaction.followup.send(frames[0])

    for frame in frames[1:]:
        await asyncio.sleep(1.2)
        await msg.edit(content=frame)


# --- FORCE DEVINETTE ---
@bot.tree.command(name="force_devinette", description="Force l'envoi immédiat d'une devinette pour tester")
async def force_devinette(interaction: discord.Interaction):
    if not check_admin(interaction):
        return await interaction.response.send_message("❌ Admin requis.", ephemeral=True)
    await interaction.response.send_message("⏳ Lancement de la devinette...", ephemeral=True)
    await envoyer_devinette()


# --- PROFIL & DAILY ---
@bot.tree.command(name="profil", description="Affiche tes points et statistiques")
async def profil(interaction: discord.Interaction, membre: discord.Member = None):
    target = membre or interaction.user
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT points, xp, level FROM users WHERE user_id = ?", (str(target.id),))
    row = cursor.fetchone()
    conn.close()

    pts = row[0] if row else 0
    xp = row[1] if row else 0
    lvl = row[2] if row else 1

    embed = discord.Embed(title=f"👤 Profil de {target.display_name}", color=discord.Color.purple())
    embed.add_field(name="💰 Points", value=f"{pts} pts", inline=True)
    embed.add_field(name="⭐ Level", value=f"Level {lvl}", inline=True)
    embed.add_field(name="✨ XP", value=f"{xp} XP", inline=True)
    if target.avatar:
        embed.set_thumbnail(url=target.avatar.url)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="daily", description="Récupère tes points quotidiens gratuits")
async def daily(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    user_id = str(interaction.user.id)
    now = int(time_module.time())
    cooldown = int(get_config_val("DAILY_COOLDOWN", "86400"))
    pts_gagnes = int(get_config_val("DAILY_POINTS", "20"))

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT points, last_daily FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()

    last_daily = row[1] if row else 0
    points_actuels = row[0] if row else 0

    if now - last_daily < cooldown:
        restant = cooldown - (now - last_daily)
        heures = restant // 3600
        minutes = (restant % 3600) // 60
        await interaction.followup.send(
            f"❌ Reviens dans {heures}h et {minutes}m pour tes prochains points.",
            ephemeral=True
        )
        conn.close()
        return

    nouveaux_points = points_actuels + pts_gagnes
    cursor.execute("""
        INSERT INTO users (user_id, username, points, last_daily) VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET points = ?, last_daily = ?
    """, (user_id, str(interaction.user), nouveaux_points, now, nouveaux_points, now))
    conn.commit()
    conn.close()

    await interaction.followup.send(
        f"💵 Crédité ! +{pts_gagnes} points (Total: {nouveaux_points} pts).",
        ephemeral=True
    )


# --- CONFIGURATIONS ADMIN ---
@bot.tree.command(name="setup_ticket", description="Envoie le panneau des tickets")
async def setup_ticket(interaction: discord.Interaction, salon: discord.TextChannel):
    if not check_admin(interaction):
        return await interaction.response.send_message("❌ Admin requis.", ephemeral=True)

    embed = discord.Embed(
        title="🎫 Support / Assistance",
        description="Clique sur le bouton ci-dessous pour ouvrir un ticket.",
        color=discord.Color.blue()
    )
    await salon.send(embed=embed, view=TicketSystemView())
    await interaction.response.send_message(f"✅ Panneau envoyé dans {salon.mention}", ephemeral=True)


@bot.tree.command(name="config_ticket_categorie", description="Définit la catégorie des tickets")
async def config_ticket_category(interaction: discord.Interaction, categorie: discord.CategoryChannel):
    if not check_admin(interaction):
        return await interaction.response.send_message("❌ Admin requis.", ephemeral=True)
    set_config_val("TICKET_CATEGORY_ID", categorie.id)
    await interaction.response.send_message(f"⚙️ Catégorie des tickets : {categorie.name}", ephemeral=True)


@bot.tree.command(name="config_welcome_salon", description="Définit le salon d'accueil")
async def config_welcome_salon(interaction: discord.Interaction, salon: discord.TextChannel):
    if not check_admin(interaction):
        return await interaction.response.send_message("❌ Admin requis.", ephemeral=True)
    set_config_val("WELCOME_CHANNEL_ID", salon.id)
    await interaction.response.send_message(f"⚙️ Salon de bienvenue : {salon.mention}", ephemeral=True)


@bot.tree.command(name="config_devinette_salon", description="Définit le salon des devinettes IA")
async def config_devinette_salon(interaction: discord.Interaction, salon: discord.TextChannel):
    if not check_admin(interaction):
        return await interaction.response.send_message("❌ Admin requis.", ephemeral=True)
    set_config_val("DEVINETTE_CHANNEL_ID", salon.id)
    await interaction.response.send_message(
        f"⚙️ Salon devinettes configuré sur {salon.mention} (Programmé à :00 et :30) !",
        ephemeral=True
    )


@bot.tree.command(name="clear", description="Supprime des messages")
async def clear(interaction: discord.Interaction, nombre: int = 10):
    if not check_admin(interaction):
        return await interaction.response.send_message("❌ Admin requis.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=nombre)
    await interaction.followup.send(f"🗑️ {len(deleted)} message(s) supprimé(s).", ephemeral=True)


# ==========================================
# 8. DÉMARRAGE
# ==========================================
keep_alive()

discord_token = os.environ.get("DISCORD_TOKEN")
if discord_token:
    bot.run(discord_token)
else:
    print("❌ DISCORD_TOKEN manquant dans les variables d'environnement.")
