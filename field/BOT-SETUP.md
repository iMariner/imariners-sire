# SIRE Telegram bot: how it runs

**Live setup (since 2026-10-03):** always-on service on the iMariners Webyne VPS (103.109.180.212).
- systemd unit `sire-bot` runs `python3 scripts/sire_bot.py --loop` as user `claudeops`, from `/home/claudeops/imariners-sire`.
- Keys: `/home/claudeops/.sire.env` (mode 600): TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_ID, DEEPSEEK_API_KEY, GITHUB_TOKEN (= PRIVATE_REPO_TOKEN, the fine-grained token `sire-automation`: Contents + Pull requests read/write on imariners-sire and imariners-sire-raw).
- Replies in seconds (Telegram long polling). Restarts on crash and reboot. Every 15 minutes it pulls this repo; if `scripts/sire_bot.py` changed, it restarts itself with the new code.
- Logs: `journalctl -u sire-bot -f` (as root). Restart: `systemctl restart sire-bot`.

**Changing a key:** ask Claude to "update the bot keys". It opens a hidden prompt on the Mac that writes `~/.sire.env` on the VPS over SSH, then restarts the service.
Where to get keys again: Telegram token: @BotFather > /mybots > bot > API Token. Telegram id: @userinfobot.
DeepSeek: create a new key. GitHub: github.com/settings/personal-access-tokens > sire-automation > Regenerate.

**Backup runner:** `.github/workflows/sire-bot.yml` can run the same bot in GitHub Actions (one pass per run).
It is OFF (repo variable `SIRE_BOT_ENABLED=false`). Never run both at once: Telegram allows only one reader.
To use it: set the four repo secrets, set `SIRE_BOT_ENABLED=true`, and stop the VPS service.

**Hermes daily card job:** see field/HERMES-DAILY.md.
