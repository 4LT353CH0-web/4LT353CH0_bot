"""
Hermes — Gateway Discord
Reçoit les messages Discord → même logique que Telegram
Nécessite : pip install discord.py
Nécessite dans .env : DISCORD_BOT_TOKEN=xxx, DISCORD_ALLOWED_CHANNEL_IDS=123,456
"""

import os
import asyncio
from dotenv import load_dotenv

load_dotenv()

DISCORD_BOT_TOKEN       = os.getenv("DISCORD_BOT_TOKEN", "")
DISCORD_ALLOWED_CHANNELS = {
    int(x) for x in os.getenv("DISCORD_ALLOWED_CHANNEL_IDS", "").split(",") if x.strip()
}

def run_discord_gateway(handle_fn):
    """
    Lance le bot Discord. handle_fn(text, user_id, reply_fn) est appelé pour chaque message.
    Appeler depuis un thread séparé si on veut paralléliser avec Telegram.

    Usage dans bot.py :
        import threading, gateway_discord
        t = threading.Thread(target=gateway_discord.run_discord_gateway, args=(my_handler,), daemon=True)
        t.start()
    """
    try:
        import discord
    except ImportError:
        print("⚠️  discord.py non installé — gateway Discord désactivée")
        print("    Installer avec : pip install discord.py")
        return

    if not DISCORD_BOT_TOKEN:
        print("⚠️  DISCORD_BOT_TOKEN absent du .env — gateway Discord désactivée")
        return

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        print(f"🎮 Discord gateway connectée : {client.user}")

    @client.event
    async def on_message(message):
        # Ignorer les messages du bot lui-même
        if message.author == client.user:
            return
        # Filtrer les canaux autorisés
        if DISCORD_ALLOWED_CHANNELS and message.channel.id not in DISCORD_ALLOWED_CHANNELS:
            return

        async def reply_fn(text: str):
            # Découper si > 2000 chars (limite Discord)
            limit = 1990
            for i in range(0, len(text), limit):
                await message.channel.send(text[i:i+limit])

        # Appeler le handler commun
        await handle_fn(message.content, message.author.id, reply_fn)

    client.run(DISCORD_BOT_TOKEN)


# ── Instructions d'activation ─────────────────────────────────────────────────
"""
Pour activer le gateway Discord :

1. Créer un bot sur discord.com/developers/applications
   → Onglet "Bot" → Enable "Message Content Intent"
   → Copier le token

2. Inviter le bot dans ton serveur :
   → OAuth2 → URL Generator → Scopes: bot → Permissions: Send Messages, Read Messages
   → Ouvrir l'URL générée

3. Ajouter dans .env du VPS :
   DISCORD_BOT_TOKEN=ton_token_ici
   DISCORD_ALLOWED_CHANNEL_IDS=id_du_canal

4. Décommenter dans bot.py :
   import threading, gateway_discord
   threading.Thread(target=gateway_discord.run_discord_gateway, args=(discord_handler,), daemon=True).start()

5. Redémarrer hermes
"""
