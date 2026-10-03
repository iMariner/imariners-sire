# SIRE Telegram bot: one-time setup (about 15 minutes, from a phone or laptop)

The bot runs in GitHub Actions on this repo every 5 minutes. Nothing runs on a laptop.
Never paste a token into any chat. Tokens go only into GitHub secrets or Hermes settings.

## 1. Telegram bot
1. In Telegram open @BotFather, send /newbot, name it (for example "iMariners SIRE"), username ending in "bot".
2. Copy the token BotFather gives you.
3. Open your new bot and press Start.
4. Message @userinfobot to get your numeric Telegram id.

## 2. DeepSeek key
platform.deepseek.com > API keys > Create. Copy it.

## 3. GitHub token for the private repo
github.com (signed in as iMariner) > Settings > Developer settings > Fine-grained tokens > Generate new token.
- Repository access: Only select repositories: imariners-sire-raw
- Permissions: Contents: Read and write
- Copy the token.

## 4. Put them into this repo
github.com/iMariner/imariners-sire > Settings > Secrets and variables > Actions.

Secrets (New repository secret):
| Name | Value |
|---|---|
| TELEGRAM_BOT_TOKEN | the BotFather token |
| TELEGRAM_OWNER_ID | your numeric Telegram id |
| DEEPSEEK_API_KEY | the DeepSeek key |
| PRIVATE_REPO_TOKEN | the token from step 3 |

Variables tab (New repository variable):
| Name | Value |
|---|---|
| SIRE_BOT_ENABLED | true |
| DEEPSEEK_MODEL | (optional) leave unset to use deepseek-chat |

Workflow permissions are already set to read and write (done on 2026-10-03).

## 5. Test
Actions tab > "SIRE Telegram bot" > Run workflow. Then send /help to your bot; within 5 minutes it answers.
Paste a report; within 5 to 10 minutes you get the summary with Approve / Reject.

## 6. Hermes daily card job
See field/HERMES-DAILY.md: one token and one cron job on hermes7.
