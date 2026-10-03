#!/bin/bash
# Asks for the bot keys without showing them, and saves them to ~/.sire.env (readable only by this user).
set -e
umask 077
ask() { local v; read -r -s -p "$1: " v; echo; printf '%s' "$v"; }
echo "Paste each value and press Enter. Nothing shows while you paste."
T=$(ask "1/4 Telegram bot token (from BotFather)")
O=$(ask "2/4 Your numeric Telegram id")
D=$(ask "3/4 DeepSeek API key")
G=$(ask "4/4 GitHub token sire-automation")
cat > ~/.sire.env <<ENV
TELEGRAM_BOT_TOKEN=$T
TELEGRAM_OWNER_ID=$O
DEEPSEEK_API_KEY=$D
GITHUB_TOKEN=$G
PRIVATE_REPO_TOKEN=$G
REPO=iMariner/imariners-sire
RAW_REPO=iMariner/imariners-sire-raw
ENV
chmod 600 ~/.sire.env
echo "SAVED: 4 keys stored in ~/.sire.env. Tell Claude 'keys saved'."
