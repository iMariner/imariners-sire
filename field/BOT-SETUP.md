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

## Reports from other seafarers (inbox, added 2026-10-23)

Anyone can open the bot, tap Start and paste, forward or send a PDF. Photos and other files get a polite "text or PDF only" reply. They get one thank-you, and the
message goes into `inbox` in the private state file. **No DeepSeek call happens when a message arrives.**

Once a day at `SIRE_DIGEST_HOUR` UTC (default 07) the bot runs the inbox, or straight away when the owner sends `/inbox`:
1. Messages from one person within 3 minutes are joined into one report (Telegram splits long pastes).
2. Free filters: under 200 characters, or fewer than 2 SIRE words, is dropped.
3. Free duplicate check: an exact fingerprint plus a MinHash of 5-word phrases (0.7 or more means the same report).
   Case, spacing, links and "Forwarded from" lines are ignored. It compares against the day's batch and against
   `state/seen.json` (every report read before). Photos and files are compared by Telegram's `file_unique_id`.
4. DeepSeek reads only what is left, at most `SIRE_DAILY_READS` (default 15) a day; the rest waits a day.
   An inspection with the same date, port and inspector as a published or waiting one gets no summary.
5. The owner gets the usual Approve/Reject summaries ("sent by N people"), one digest with the counts, and copies
   of PDFs that could not be read even with OCR (at most 20 a day; paste the text back to have one read).

**Who gets told (2026-10-25):** everyone who sent a report (text or PDF) gets "Thank you. The SIRE report you sent is now on <page>"
when the owner presses Approve. Someone who sends a report that is already waiting for approval is added to that card, so they are
thanked on approval too. Someone who sends a report that was read before and is no longer waiting gets "I already have that report".
Someone whose inspection (same date, port, inspector) is already on the page is told so. Reject sends nothing to senders.

**PDFs (added 2026-10-24):** the bot reads the text of a PDF (up to 60 pages, 15 MB) with the `pypdf` library, free, and treats
it exactly like a pasted report (same filters, duplicate check and DeepSeek summary). Owner PDFs are read straight away.
The official "SIRE 2.0 EDITOR - Observation Declaration" form has its own rules in `field/extract_prompt.txt`: every
"Not as expected" / "deficient" / "Photo not representative" entry is one observation, "Exceeded normal expectation" entries are
positives (shown on the card under Positives), the count in the card header always equals the list. After the AI answers, the
bot counts the form's own positive and photo labels in the text; if the AI listed fewer it is asked once more, and if it is
still short the card says "Check this one against the form". Long cards are shortened step by step so they fit Telegram.

Scanned PDFs (a photo of each page, no text) are read by OCR: `pdftoppm` + `tesseract` (apt packages `tesseract-ocr`,
`tesseract-ocr-eng`, `poppler-utils`, installed 2026-10-24), first 20 pages, about 13 s for 5 pages, nothing leaves the VPS.
A PDF that still gives no text is passed to the owner as a file. `pypdf` is installed for `claudeops` in
`~/.local/lib/python3.12/site-packages` (the VPS has no pip: unzip the wheel there). Without it PDFs are treated as unreadable.
6. On Approve, everyone who sent that report gets a thank-you with the page link.

Limits per person per day: 30 messages, 80,000 characters. Group chats and other bots are ignored.
