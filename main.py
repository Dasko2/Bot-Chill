import os
import sqlite3
import aiohttp
import asyncio
import re
import random
import colorsys
import time as time_module
from datetime import datetime, timedelta, timezone, time as dt_time

import discord
from discord import app_commands
from discord.ext import commands, tasks
from flask import Flask
from threading import Thread

# ==========================================
# KEEP ALIVE (serveur Flask pour rester en vie)
# ==========================================
app = Flask('')

@app.route('/')
def home():
    return "Bot Combiné (Pronos, XP, Invites, Sondages & Salons Éphémères) en vie !"

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_web_server)
    t.daemon = True
    t.start()

# ==========================================
# BASE DE DONNÉES
# ==========================================
DB_NAME = "bot_data.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # Table utilisateurs
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            username TEXT,
            points INTEGER DEFAULT 0,
            xp INTEGER DEFAULT 0,
            level INTEGER DEFAULT 1,
            invites_count INTEGER DEFAULT 0,
            last_daily INTEGER DEFAULT 0,
            invited_by TEXT DEFAULT NULL
        )
    ''')

    # Table prédictions football
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS predictions (
            prediction_id TEXT PRIMARY KEY,
            user_id TEXT,
            match_id TEXT,
            predicted_home INTEGER,
            predicted_away INTEGER,
            predicted_winner TEXT,
            prediction_type TEXT,
            status TEXT DEFAULT 'PENDING'
        )
    ''')

    # Table configuration
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')

    # Table boutique
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS shop (
            item_id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_name TEXT UNIQUE,
            description TEXT,
            price INTEGER
        )
    ''')

    # Table rôles admin
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS authorized_roles (
            role_id TEXT PRIMARY KEY
        )
    ''')

    # Table matchs football
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS match_data (
            match_id TEXT PRIMARY KEY,
            home_team TEXT,
            away_team TEXT,
            announced_at INTEGER DEFAULT 0,
            message_id TEXT,
            channel_id TEXT,
            status TEXT DEFAULT 'SCHEDULED'
        )
    ''')

    # Table matchs rugby
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS rugby_match_data (
            match_id TEXT PRIMARY KEY,
            home_team TEXT,
            away_team TEXT,
            announced_at INTEGER DEFAULT 0,
            message_id TEXT,
            channel_id TEXT,
            status TEXT DEFAULT 'SCHEDULED'
        )
    ''')

    # Table prédictions rugby
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS rugby_predictions (
            prediction_id TEXT PRIMARY KEY,
            user_id TEXT,
            match_id TEXT,
            predicted_home INTEGER,
            predicted_away INTEGER,
            predicted_winner TEXT,
            prediction_type TEXT,
            status TEXT DEFAULT 'PENDING'
        )
    ''')

    # Tables salons éphémères
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ephemeral_channels (
            channel_id TEXT PRIMARY KEY,
            creator_id TEXT,
            control_message_id TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ephemeral_blacklist (
            channel_id TEXT,
            user_id TEXT,
            PRIMARY KEY (channel_id, user_id)
        )
    ''')

    # Table sondages
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS polls (
            poll_id TEXT PRIMARY KEY,
            title TEXT,
            poll_type TEXT,
            choices TEXT,
            expires_at TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS poll_responses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            poll_id TEXT,
            user_id TEXT,
            response TEXT
        )
    ''')

    # Table salons médias
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS media_channels (
            channel_id TEXT PRIMARY KEY
        )
    ''')

    # Tables warns
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS warns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            moderator_id TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    ''')

    # Valeurs par défaut de configuration
    defaults = {
        'POINTS_EXACT': '100',
        'POINTS_CONSOLATION': '50',
        'DAILY_POINTS': '20',
        'DAILY_COOLDOWN': '86400',
        'LOG_CHANNEL_ID': '0',
        'SHOP_CHANNEL_ID': '0',
        'WELCOME_CHANNEL_ID': '0',
        'TICKET_CATEGORY_ID': '0',
        'EPHEMERAL_GENERATOR_ID': '0',
        'EPHEMERAL_REQUIRED_ROLE_ID': '0',
        'LEVEL_CHANNEL_ID': '0',
        'MOD_LOG_CHANNEL_ID': '0',
        'SONDAGE_CHANNEL_ID': '0',
        'VIP_ROLE_ID': '0',
        'VIP_GRAD_ROLE_IDS': '',
        'MSG_WELCOME_TITLE': '👋 Bienvenue {member} !',
        'MSG_WELCOME_DESC': 'Bienvenue sur {guild} !\nProfite bien de ton séjour parmi nous.',
        'MSG_TICKET_PANEL_TITLE': '🎫 SUPPORT TECHNIQUE',
        'MSG_TICKET_PANEL_DESC': 'Besoin d\'aide ? Clique sur le bouton ci-dessous pour ouvrir un ticket.',
        'MSG_TICKET_WELCOME_TITLE': '🎫 Ticket d\'assistance de {member}',
        'MSG_TICKET_WELCOME_DESC': 'Bonjour {member_mention},\nLe staff va s\'occuper de toi sous peu !',
        'MSG_DAILY_SUCCESS': '💵 Crédité ! +{points_gagnes} points (Total: {total_points} pts).',
        'MSG_DAILY_COOLDOWN': '❌ Reviens dans {heures}h et {minutes}m.',
        'MSG_PRONO_TOO_LATE': '❌ Trop tard ! Le match a déjà commencé.',
        'MSG_PRONO_BAD_INPUT': '❌ Tu dois entrer des nombres entiers valides !',
        'MSG_PRONO_SIMPLE_SUCCESS': '✅ Pronostic simple enregistré : {choix} !',
        'MSG_PRONO_EXACT_SUCCESS': '✅ Pronostic score exact enregistré : {home} - {away} !',
        'MSG_SHOP_INSUFFICIENT': '❌ Solde insuffisant ! (Il te manque {manque} pts)',
        'MSG_SHOP_SUCCESS': '🎉 Félicitations ! Achat de {item} effectué avec succès !',
        'MSG_POINTS_BALANCE': '👤 Tu as actuellement {points} points.',
    }
    for k, v in defaults.items():
        cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", (k, v))

    conn.commit()
    conn.close()

# ==========================================
# CONFIGURATION DU BOT
# ==========================================
class CombinedBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        init_db()
        keep_alive()
        self.add_view(TicketSystemView())
        self.add_view(CloseTicketButton())
        self.add_view(EphemeralControlView())

bot = CombinedBot()

def get_config_val(key, default=""):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM config WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else default

def set_config_val(key, value):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()

def check_admin_privilege(interaction: discord.Interaction) -> bool:
    if interaction.user.id == interaction.guild.owner_id:
        return True
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT role_id FROM authorized_roles")
    authorized_role_ids = [int(row[0]) for row in cursor.fetchall()]
    conn.close()
    user_role_ids = [role.id for role in interaction.user.roles]
    return any(r_id in authorized_role_ids for r_id in user_role_ids)

# ==========================================
# SYSTÈME DE TICKETS
# ==========================================
class CloseTicketButton(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔒 Fermer le ticket", style=discord.ButtonStyle.red, custom_id="close_ticket_btn")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await interaction.followup.send("⚙️ Fermeture du ticket en cours...")
        await asyncio.sleep(2)
        try:
            await interaction.channel.delete()
        except Exception as e:
            await interaction.followup.send(f"❌ Erreur lors de la fermeture : {e}")

class TicketReasonModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Création de ton Ticket")
        self.reason = discord.ui.TextInput(
            label="Raison de l'ouverture",
            style=discord.TextStyle.paragraph,
            placeholder="Explique-nous comment nous pouvons t'aider...",
            required=True,
            min_length=5
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        user = interaction.user

        existing = discord.utils.get(guild.text_channels, name=f"ticket-{user.name.lower()}")
        if existing:
            await interaction.followup.send(f"❌ Tu as déjà un ticket ouvert : {existing.mention}", ephemeral=True)
            return

        category_id = int(get_config_val("TICKET_CATEGORY_ID", "0"))
        category = guild.get_channel(category_id) if category_id != 0 else None

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            user: discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True),
        }

        try:
            channel = await guild.create_text_channel(name=f"ticket-{user.name}", category=category, overwrites=overwrites)
        except Exception as e:
            await interaction.followup.send(f"❌ Erreur lors de la création : {e}", ephemeral=True)
            return

        title = get_config_val("MSG_TICKET_WELCOME_TITLE", "🎫 Ticket de {member}").replace("{member}", user.name)
        desc = get_config_val("MSG_TICKET_WELCOME_DESC", "Bonjour {member_mention},\nLe staff arrive !").replace("{member_mention}", user.mention)

        embed = discord.Embed(title=title, description=desc, color=discord.Color.green())
        embed.add_field(name="Raison :", value=self.reason.value, inline=False)
        await channel.send(content=user.mention, embed=embed, view=CloseTicketButton())
        await interaction.followup.send(f"✅ Ton ticket a été créé : {channel.mention}", ephemeral=True)

class TicketSystemView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✉️ Ouvrir un Ticket", style=discord.ButtonStyle.secondary, custom_id="open_ticket_system_btn")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TicketReasonModal())

# ==========================================
# SALONS ÉPHÉMÈRES (GÉNÉRATEUR VOCAL)
# ==========================================
class EphemeralControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✏️ Renommer", style=discord.ButtonStyle.primary, custom_id="eph_rename")
    async def eph_rename(self, interaction: discord.Interaction, button: discord.ui.Button):
        class RenameModal(discord.ui.Modal, title="Renommer le salon"):
            new_name = discord.ui.TextInput(label="Nouveau nom", placeholder="Ex: Salon de Gamer")
            async def on_submit(self, inter: discord.Interaction):
                await inter.channel.edit(name=self.new_name.value)
                await inter.response.send_message("✅ Salon renommé !", ephemeral=True)
        await interaction.response.send_modal(RenameModal())

    @discord.ui.button(label="👥 Limite de places", style=discord.ButtonStyle.secondary, custom_id="eph_limit")
    async def eph_limit(self, interaction: discord.Interaction, button: discord.ui.Button):
        class LimitModal(discord.ui.Modal, title="Limite de membres"):
            limit = discord.ui.TextInput(label="Nombre maximum (0 = illimité)", placeholder="Ex: 5")
            async def on_submit(self, inter: discord.Interaction):
                try:
                    val = int(self.limit.value)
                    await inter.channel.edit(user_limit=val)
                    await inter.response.send_message(f"✅ Limite fixée à {val} membres !", ephemeral=True)
                except ValueError:
                    await inter.response.send_message("❌ Entrez un nombre valide !", ephemeral=True)
        await interaction.response.send_modal(LimitModal())

    @discord.ui.button(label="🔒 Verrouiller / Déverrouiller", style=discord.ButtonStyle.danger, custom_id="eph_lock")
    async def eph_lock(self, interaction: discord.Interaction, button: discord.ui.Button):
        overwrite = interaction.channel.overwrites_for(interaction.guild.default_role)
        if overwrite.connect is False:
            overwrite.connect = None
            msg = "🔓 Salon déverrouillé !"
        else:
            overwrite.connect = False
            msg = "🔒 Salon verrouillé !"
        await interaction.channel.set_permissions(interaction.guild.default_role, overwrite=overwrite)
        await interaction.response.send_message(msg, ephemeral=True)

@bot.event
async def on_voice_state_update(member, before, after):
    gen_id = int(get_config_val("EPHEMERAL_GENERATOR_ID", "0"))
    if gen_id != 0 and after.channel and after.channel.id == gen_id:
        req_role_id = int(get_config_val("EPHEMERAL_REQUIRED_ROLE_ID", "0"))
        if req_role_id != 0:
            role = member.guild.get_role(req_role_id)
            if role and role not in member.roles:
                await member.move_to(None)
                try:
                    await member.send("❌ Tu n'as pas le rôle requis pour créer un salon éphémère.")
                except Exception:
                    pass
                return

        guild = member.guild
        category = after.channel.category
        new_channel = await guild.create_voice_channel(name=f"🔊 Salon de {member.display_name}", category=category)
        await member.move_to(new_channel)
        
        embed = discord.Embed(title="⚙️ Panneau de contrôle du salon", description="Gère ton salon éphémère avec les boutons ci-dessous :", color=0x3498db)
        control_msg = await new_channel.send(embed=embed, view=EphemeralControlView())

        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO ephemeral_channels (channel_id, creator_id, control_message_id) VALUES (?, ?, ?)",
                       (str(new_channel.id), str(member.id), str(control_msg.id)))
        conn.commit()
        conn.close()

    if before.channel and before.channel.id != gen_id:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT channel_id FROM ephemeral_channels WHERE channel_id = ?", (str(before.channel.id),))
        res = cursor.fetchone()
        if res:
            if len(before.channel.members) == 0:
                await before.channel.delete()
                cursor.execute("DELETE FROM ephemeral_channels WHERE channel_id = ?", (str(before.channel.id),))
                conn.commit()
        conn.close()

# ==========================================
# ÉVÉNEMENT : READY & BIENVENUE
# ==========================================
@bot.event
async def on_ready():
    print(f"✅ Bot connecté sous le nom : {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"🌐 {len(synced)} commandes slash synchronisées avec succès.")
    except Exception as e:
        print(f"❌ Erreur lors de la synchronisation des commandes slash : {e}")

@bot.event
async def on_member_join(member):
    welcome_id = int(get_config_val("WELCOME_CHANNEL_ID", "0"))
    if welcome_id != 0:
        channel = member.guild.get_channel(welcome_id)
        if channel:
            title = get_config_val("MSG_WELCOME_TITLE", "👋 Bienvenue {member} !").replace("{member}", member.name).replace("{guild}", member.guild.name)
            desc = get_config_val("MSG_WELCOME_DESC", "Bienvenue parmi nous !").replace("{member}", member.name).replace("{member_mention}", member.mention).replace("{guild}", member.guild.name)
            embed = discord.Embed(title=title, description=desc, color=0x2ecc71)
            embed.set_thumbnail(url=member.display_avatar.url)
            await channel.send(embed=embed)

# ==========================================
# COMMANDES SLASH PRINCIPALES
# ==========================================

# 🧪 COMMANDE DE TEST D'ANIMATION
@bot.tree.command(name="test_animation", description="Teste l'animation dynamique de message sur Discord")
async def test_animation(interaction: discord.Interaction):
    await interaction.response.defer()
    
    frames = [
        "⏳ **Initialisation...**\n[▱▱▱▱▱▱▱▱▱▱] 0%",
        "⚙️ **Chargement des modules...**\n[██▱▱▱▱▱▱▱▱] 20%",
        "🤖 **Connexion aux services...**\n[████▱▱▱▱▱▱] 40%",
        "✨ **Application des effets...**\n[██████▱▱▱▱] 60%",
        "🔥 **Finalisation...**\n[████████▱▱] 80%",
        "🎉 **Animation réussie avec succès !**\n[██████████] 100%"
    ]

    msg = await interaction.followup.send(frames[0])
    
    for frame in frames[1:]:
        await asyncio.sleep(1.2)
        await msg.edit(content=frame)

@bot.tree.command(name="profil", description="Affiche tes statistiques et ton profil sur le serveur")
async def slash_profil(interaction: discord.Interaction, membre: discord.Member = None):
    target = membre or interaction.user
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT points, xp, level, invites_count FROM users WHERE user_id = ?", (str(target.id),))
    row = cursor.fetchone()
    conn.close()

    pts = row[0] if row else 0
    xp = row[1] if row else 0
    lvl = row[2] if row else 1
    invites = row[3] if row else 0

    embed = discord.Embed(title=f"👤 Profil de {target.display_name}", color=0x9b59b6)
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="💰 Points", value=f"`{pts} pts`", inline=True)
    embed.add_field(name="⭐ Niveau", value=f"`Niveau {lvl}`", inline=True)
    embed.add_field(name="✨ Expérience", value=f"`{xp} XP`", inline=True)
    embed.add_field(name="📩 Invitations", value=f"`{invites} membre(s)`", inline=True)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="daily", description="Récupère tes points quotidiens gratuits")
async def slash_daily(interaction: discord.Interaction):
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
        tpl = get_config_val("MSG_DAILY_COOLDOWN", "❌ Reviens dans {heures}h et {minutes}m.")
        await interaction.followup.send(tpl.replace("{heures}", str(heures)).replace("{minutes}", str(minutes)), ephemeral=True)
        conn.close()
        return

    nouveaux_points = points_actuels + pts_gagnes
    cursor.execute("""
        INSERT INTO users (user_id, username, points, last_daily) VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET points = ?, last_daily = ?
    """, (user_id, str(interaction.user), nouveaux_points, now, nouveaux_points, now))
    conn.commit()
    conn.close()

    tpl = get_config_val("MSG_DAILY_SUCCESS", "💵 Crédité ! +{points_gagnes} points (Total: {total_points} pts).")
    await interaction.followup.send(tpl.replace("{points_gagnes}", str(pts_gagnes)).replace("{total_points}", str(nouveaux_points)), ephemeral=True)

@bot.tree.command(name="setup_ticket", description="Envoie le panneau des tickets dans un salon")
async def slash_setup_ticket(interaction: discord.Interaction, salon: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)
    if not check_admin_privilege(interaction):
        await interaction.followup.send("❌ Permissions insuffisantes.", ephemeral=True)
        return
    p_title = get_config_val("MSG_TICKET_PANEL_TITLE", "🎫 SUPPORT TECHNIQUE")
    p_desc = get_config_val("MSG_TICKET_PANEL_DESC", "Clique ci-dessous pour ouvrir un ticket.")
    embed = discord.Embed(title=p_title, description=p_desc, color=discord.Color.blurple())
    await salon.send(embed=embed, view=TicketSystemView())
    await interaction.followup.send(f"✅ Panneau envoyé dans {salon.mention} !", ephemeral=True)

@bot.tree.command(name="clear", description="Supprime tous les messages du salon")
async def slash_clear(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not check_admin_privilege(interaction):
        await interaction.followup.send("❌ Permissions insuffisantes.", ephemeral=True)
        return
    deleted = await interaction.channel.purge(limit=100)
    await interaction.followup.send(f"🗑️ {len(deleted)} message(s) supprimé(s).", ephemeral=True)

@bot.tree.command(name="help", description="Affiche le menu d'aide du bot")
async def slash_help(interaction: discord.Interaction):
    embed = discord.Embed(title="📚 Commandes disponibles", color=0x3498db)
    embed.add_field(name="👤 Utilisateur", value="/profil, /daily, /test_animation", inline=False)
    embed.add_field(name="🛠️ Admin", value="/setup_ticket, /clear", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ==========================================
# LANCEMENT
# ==========================================
token = os.environ.get("DISCORD_TOKEN")
if token:
    bot.run(token)
else:
    print("❌ DISCORD_TOKEN manquant dans les variables d'environnement.")
