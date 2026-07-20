import os
import sqlite3
import asyncio
import random
from threading import Thread
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands, tasks
from flask import Flask
from google import genai

# ==========================================
# 1. SERVEUR FLASK (Hébergement Gratuit 24/7)
# ==========================================
app = Flask('')

@app.route('/')
def home():
    return "Bot FlaviBot (Devinettes IA, Tickets, Bienvenue) est en ligne !"

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

    # Table des utilisateurs (points devinettes, XP)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            username TEXT,
            points INTEGER DEFAULT 0,
            xp INTEGER DEFAULT 0,
            level INTEGER DEFAULT 1
        )
    ''')

    # Table des tickets
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

    # Table de configuration du serveur
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')

    # Configurations par défaut
    defaults = {
        'WELCOME_CHANNEL_ID': '0',
        'MSG_WELCOME_TITLE': '👋 Bienvenue {member} !',
        'MSG_WELCOME_DESC': 'Ravi de te voir parmi nous sur {guild} !',
        'TICKET_CATEGORY_ID': '0',
        'DEVINETTE_CHANNEL_ID': '0',
        'DEVINETTE_POINTS': '20'
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

# ==========================================
# 3. INITIALISATION DU BOT
# ==========================================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Variable globale pour la devinette courante
CURRENT_DEVINETTE = {
    "reponse": None,
    "active": False
}

# Check de permission administrateur
def check_admin(interaction: discord.Interaction) -> bool:
    return interaction.user.guild_permissions.administrator

# ==========================================
# 4. SYSTÈME DE TICKETS (Views & Modals)
# ==========================================
class TicketReasonModal(discord.ui.Modal, title="Ouvrir un Ticket"):
    reason = discord.ui.TextInput(
        label="Raison du ticket",
        style=discord.TextStyle.paragraph,
        placeholder="Décris le problème ou ta demande...",
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

        channel_name = f"ticket-{interaction.user.name}"
        ticket_channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            reason=f"Ticket ouvert par {interaction.user}"
        )

        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO tickets (channel_id, user_id, reason) VALUES (?, ?, ?)",
                       (str(ticket_channel.id), str(interaction.user.id), self.reason.value))
        conn.commit()
        conn.close()

        embed = discord.Embed(
            title=f"🎫 Ticket de {interaction.user.display_name}",
            description=f"**Raison :** {self.reason.value}\n\nUn membre du staff va prendre en charge ta demande.",
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
# 5. SYSTÈME DE DEVINETTES IA (GEMINI)
# ==========================================
def generer_devinette_gemini():
    """Appelle Gemini Flash pour créer une devinette."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("⚠️ Clef GEMINI_API_KEY manquante.")
        return None, None

    client = genai.Client(api_key=api_key)
    prompt = (
        "Génère une devinette amusante et courte en français avec sa réponse.\n"
        "Format de réponse OBLIGATOIRE sur exactement 2 lignes :\n"
        "Devinette: [Question de la devinette]\n"
        "Réponse: [Un seul mot précis pour la réponse]"
    )

    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
        )
        lines = response.text.strip().split('\n')
        question, reponse = "", ""
        for line in lines:
            if line.startswith("Devinette:"):
                question = line.replace("Devinette:", "").strip()
            elif line.startswith("Réponse:"):
                reponse = line.replace("Réponse:", "").strip().lower()
        return question, reponse
    except Exception as e:
        print(f"❌ Erreur lors de la génération Gemini: {e}")
        return None, None

@tasks.loop(minutes=30)
async def devinette_loop():
    dev_channel_id = int(get_config_val("DEVINETTE_CHANNEL_ID", "0"))
    if dev_channel_id == 0:
        return

    channel = bot.get_channel(dev_channel_id)
    if not channel:
        return

    question, reponse = await asyncio.to_thread(generer_devinette_gemini)
    if not question or not reponse:
        return

    CURRENT_DEVINETTE["reponse"] = reponse
    CURRENT_DEVINETTE["active"] = True

    pts = get_config_val("DEVINETTE_POINTS", "20")
    embed = discord.Embed(
        title="🧩 Nouvelle Devinette !",
        description=f"**{question}**\n\n*Tape la réponse directement dans ce salon. Le premier qui trouve gagne **{pts} points** !*",
        color=discord.Color.gold()
    )
    await channel.send(embed=embed)

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
        
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synchronisé {len(synced)} commandes slash.")
    except Exception as e:
        print(f"❌ Erreur de synchro: {e}")

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
    desc = desc.replace("{member}", member.display_name).replace("{member_mention}", member.mention).replace("{guild}", member.guild.name)

    embed = discord.Embed(title=title, description=desc, color=discord.Color.blue())
    if member.avatar:
        embed.set_thumbnail(url=member.avatar.url)

    await channel.send(content=member.mention, embed=embed)

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Vérification des réponses aux devinettes
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
            cursor.execute("INSERT OR IGNORE INTO users (user_id, username, points) VALUES (?, ?, 0)", (user_id, str(message.author)))
            cursor.execute("UPDATE users SET points = points + ? WHERE user_id = ?", (pts_gagnes, user_id))
            conn.commit()
            conn.close()

            await message.channel.send(
                f"🎉 Bravo {message.author.mention} ! La réponse était bien **{reponse_attendue}** !\n"
                f"💰 Tu remportes **+{pts_gagnes} points** !"
            )

    await bot.process_commands(message)

# ==========================================
# 7. COMMANDES SLASH
# ==========================================

# --- CONFIGURATION TICKETS ---
@bot.tree.command(name="setup_ticket", description="Envoie le panneau de création des tickets")
async def setup_ticket(interaction: discord.Interaction, salon: discord.TextChannel):
    if not check_admin(interaction):
        return await interaction.response.send_message("❌ Admin requis.", ephemeral=True)
    
    embed = discord.Embed(
        title="🎫 Support / Assistance",
        description="Clique sur le bouton ci-dessous pour ouvrir un ticket et contacter l'équipe d'administration.",
        color=discord.Color.blue()
    )
    await salon.send(embed=embed, view=TicketSystemView())
    await interaction.response.send_message(f"✅ Panneau de ticket envoyé dans {salon.mention}", ephemeral=True)

@bot.tree.command(name="config_ticket_categorie", description="Définit la catégorie de création des tickets")
async def config_ticket_category(interaction: discord.Interaction, categorie: discord.CategoryChannel):
    if not check_admin(interaction):
        return await interaction.response.send_message("❌ Admin requis.", ephemeral=True)
    set_config_val("TICKET_CATEGORY_ID", categorie.id)
    await interaction.response.send_message(f"⚙️ Catégorie des tickets : {categorie.name}", ephemeral=True)

# --- CONFIGURATION BIENVENUE ---
@bot.tree.command(name="config_welcome_salon", description="Définit le salon d'accueil")
async def config_welcome_salon(interaction: discord.Interaction, salon: discord.TextChannel):
    if not check_admin(interaction):
        return await interaction.response.send_message("❌ Admin requis.", ephemeral=True)
    set_config_val("WELCOME_CHANNEL_ID", salon.id)
    await interaction.response.send_message(f"⚙️ Salon de bienvenue configuré : {salon.mention}", ephemeral=True)

# --- CONFIGURATION DEVINETTES ---
@bot.tree.command(name="config_devinette_salon", description="Définit le salon des devinettes IA")
async def config_devinette_salon(interaction: discord.Interaction, salon: discord.TextChannel):
    if not check_admin(interaction):
        return await interaction.response.send_message("❌ Admin requis.", ephemeral=True)
    set_config_val("DEVINETTE_CHANNEL_ID", salon.id)
    await interaction.response.send_message(f"⚙️ Salon des devinettes configuré sur {salon.mention} (Fréquence : 30 min)", ephemeral=True)

# --- PROFIL / CLASSEMENT ---
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
    embed.add_field(name="💰 Points", value=str(pts), inline=True)
    embed.add_field(name="⭐ Level", value=str(lvl), inline=True)
    embed.add_field(name="✨ XP", value=str(xp), inline=True)
    if target.avatar:
        embed.set_thumbnail(url=target.avatar.url)

    await interaction.response.send_message(embed=embed)

# ==========================================
# 8. DÉMARRAGE DU BOT
# ==========================================
keep_alive()  # Démarre le serveur Web Flask en arrière-plan

discord_token = os.environ.get("DISCORD_TOKEN")
if discord_token:
    bot.run(discord_token)
else:
    print("❌ Token DISCORD_TOKEN introuvable dans les variables d'environnement.")
