#!/bin/bash
# Bootstrap VPS frais pour Projet Hermes
# Usage : coller dans la console IONOS en root
# Tout le code vient de GitHub — pas besoin de retaper

set -e

echo "=== Bootstrap Hermes VPS ==="

# 1. Dépendances système
apt update && apt install -y python3-pip git

# 2. Clé SSH pour GitHub (si absente)
if [ ! -f ~/.ssh/id_ed25519 ]; then
    ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N ""
    echo ""
    echo "⚠️  AJOUTE CETTE CLÉ SSH SUR github.com/settings/ssh AVANT DE CONTINUER :"
    echo ""
    cat ~/.ssh/id_ed25519.pub
    echo ""
    read -p "Appuie sur Entrée une fois la clé ajoutée..."
fi

# Accepter github.com dans known_hosts
ssh-keyscan github.com >> ~/.ssh/known_hosts 2>/dev/null

# 3. Cloner le bot
if [ ! -d ~/4LT353CH0_bot ]; then
    git clone git@github.com:4LT353CH0-web/4LT353CH0_bot.git ~/4LT353CH0_bot
else
    echo "Bot déjà cloné — git pull"
    cd ~/4LT353CH0_bot && git pull
fi

# 4. Cloner les vaults
for repo in claude-connaissance claude-musique claude-creative-coding claude-opendesign; do
    if [ ! -d ~/$repo ]; then
        git clone git@github.com:4LT353CH0-web/$repo.git ~/$repo || echo "⚠ $repo non cloné (repo privé ou inexistant)"
    else
        echo "$repo déjà présent"
    fi
done

# 5. Installer les dépendances Python
python3 -m pip install --break-system-packages \
    python-telegram-bot google-genai python-dotenv

# 6. Créer le .env si absent
if [ ! -f ~/4LT353CH0_bot/.env ]; then
    echo ""
    echo "⚠️  Crée le fichier .env manuellement :"
    echo ""
    cat <<'EOF'
cat > ~/4LT353CH0_bot/.env <<ENVEOF
TELEGRAM_TOKEN=COLLER_ICI
GEMINI_API_KEY=COLLER_ICI
VAULT_PATH=/root/claude-connaissance
TELEGRAM_CHAT_ID=8857974401
DISCORD_WEBHOOK=https://discord.com/api/webhooks/1497502798986870825/OJFKCTkHD8-JGuaPrA7dDaeJVWAAF_7LikenyB3qL1HtU1MQlDdByTpKXs9yTxvGBUPG
ENVEOF
EOF
    read -p "Appuie sur Entrée une fois le .env créé..."
else
    echo ".env déjà présent"
fi

# 7. Systemd user service
mkdir -p ~/.config/systemd/user
cp ~/4LT353CH0_bot/systemd/hermes.service ~/.config/systemd/user/ 2>/dev/null || echo "hermes.service manquant dans systemd/"
cp ~/4LT353CH0_bot/systemd/hermes-nightly.service ~/.config/systemd/user/ 2>/dev/null || true
cp ~/4LT353CH0_bot/systemd/hermes-nightly.timer   ~/.config/systemd/user/ 2>/dev/null || true

mkdir -p ~/4LT353CH0_bot/logs

export XDG_RUNTIME_DIR=/run/user/$(id -u)
systemctl --user daemon-reload
systemctl --user enable --now hermes 2>/dev/null || echo "Lancement hermes..."
systemctl --user enable --now hermes-nightly.timer 2>/dev/null || true

echo ""
echo "=== ✓ Bootstrap terminé ==="
echo "Bot    : systemctl --user status hermes"
echo "Timer  : systemctl --user list-timers | grep hermes"
echo "Logs   : journalctl --user -u hermes -f"
