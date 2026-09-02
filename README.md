# Bizcard Intake Bot

Telegram bot that turns a photo of a business card into a Google Contacts entry.
Runs on a Mac, costs nothing beyond subscriptions you already have: parsing
goes through the **Codex CLI (ChatGPT Plus or higher)** or the **Claude Code CLI
(Claude subscription)**, and saving goes through the free `gws` CLI.

```
photo ─▶ Apple Vision rectify + OCR ─▶ codex / claude (JSON) ─▶ draft in Telegram
                                                                    │ reply to fix, 승인 to save
                                                                    ▼
                                                        Google Contacts (with card photo)
```

## Prerequisites

| Requirement | Why | Check |
|---|---|---|
| macOS with Xcode command line tools | Apple Vision does the card rectification and OCR | `xcode-select --install` |
| Python 3.10+ with `numpy`, `Pillow` (`opencv-python` optional) | image preparation | `pip install -r requirements.txt` |
| [`gws`](https://github.com/googleworkspace/cli) logged in with the contacts scope | writes to Google Contacts | `brew install googleworkspace-cli` |
| **One of**: [`codex`](https://github.com/openai/codex) (ChatGPT Plus/Pro/Business) or [`claude`](https://docs.anthropic.com/en/docs/claude-code) (Claude subscription) | reads the card | `codex login` / `claude login` |
| Telegram bot token from @BotFather | the chat channel | |

`python -m bizcard_intake doctor` checks all of the above and prints the fix for
anything missing. `python -m bizcard_intake setup` walks you through the
missing pieces.

## Quick start

```bash
git clone https://github.com/project820/Biz-carDDD.git && cd Biz-carDDD
cp .env.example .env               # TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_CHAT_IDS
python3 -m bizcard_intake setup    # checks gws / codex or claude / telegram, guides Google login
./scripts/start_bot.sh
```

Send the bot any message to learn your chat id, put it in
`TELEGRAM_ALLOWED_CHAT_IDS`, restart. Only listed chats can use the bot; everyone
else gets a one-line refusal.

### Google login, once

```bash
gws auth login --scopes https://www.googleapis.com/auth/contacts
```

`gws` needs an OAuth client in **your own** Google Cloud project. With `gcloud`
installed, `gws auth setup --login` creates the project and client for you;
without it, `setup` prints the manual console steps. Two things keep the login
from expiring:

- Request only the contacts scope (command above). The default `cloud-platform`
  scope puts the token under Workspace reauth policies.
- Personal Gmail accounts can only create *External* apps: publish the app to
  **In production** in the console. In *Testing* status Google kills the refresh
  token every 7 days. Workspace accounts should pick *Internal*, which never
  expires.

If the token still dies (password change, admin revocation, 6 months unused),
the bot tells the `BIZCARD_ALERT_CHAT_ID` chat within a day, failed saves wait in
`data/pending_contacts/`, and `python -m bizcard_intake retry-pending` replays
them after you log in again.

## Using the bot

1. Send a card photo. The bot rectifies it, runs OCR and the parser, and replies
   with a preview image plus a draft:
   ```
   명함 입력 초안
   - 이름 필드: 홍길동 이사
   - 성씨 필드: ACME
   - 휴대전화: 010-1234-5678
   ...
   - 검증 경고: 회사전화 끝자리 확인 필요
   ```
2. Reply with plain text to correct anything ("두 회선 맞아", "이메일은 kim@acme.com").
   The draft is revised and re-sent.
3. 승인 saves the contact and deletes the card images. 뒷면스캔 merges the back side first.

### Design choices (intentional, Korean cards)

- Google *given name* = full name + title (`홍길동 이사`), *family name* = company.
  With thousands of contacts, name + company on the caller screen is what makes
  a person recognisable, and the card photo is the second check.
- A missing title becomes `대표`.
- `052-297-8976~7` is saved as two numbers without asking.
- Duplicate detection scans all contacts (mobile + name) instead of
  `searchContacts`, whose index lags minutes behind `createContact`.
- Card images are deleted after a successful save.

## Parser and tokens

| Backend | Command | Per card (measured) |
|---|---|---|
| `codex` (default when installed) | `codex exec` in an empty dir with `--ignore-user-config --ignore-rules`, reasoning `low`, JSON asked in the prompt | ~5–6k tokens, ~5 s |
| `claude` | `claude -p --json-schema --allowedTools Read` | ~15 s; Claude Code's own ~47k system prompt is cached but still counts toward the plan |

Why the flags matter: a bare `codex exec` loads `~/.codex/AGENTS.md`, project
rules and plugin instructions on every call (about 12k tokens per card). The
empty working directory and ignore flags cut that in half. `codex --output-schema`
is deliberately not used because it triples the token count. The image sent to
the model is a 1024px copy; the 1600px one is only for Apple Vision OCR.

Set `BIZCARD_PARSER=codex|claude`, `BIZCARD_CODEX_MODEL`, `BIZCARD_CLAUDE_MODEL`
in `.env`.

## Run as a launchd service (auto-start, auto-restart)

```bash
sed -e "s|__PROJECT_ROOT__|$PWD|g" -e "s|__HOME__|$HOME|g" scripts/com.bizcard.intake.plist \
  > ~/Library/LaunchAgents/com.bizcard.intake.plist
launchctl load -w ~/Library/LaunchAgents/com.bizcard.intake.plist

launchctl stop com.bizcard.intake        # restart = stop; launchd relaunches it
tail -f data/logs/bot.log
```

`KeepAlive` is unconditional: the bot comes back after crashes, reboots and
manual stops. At startup and every 24 hours it re-checks codex/claude and the
Google token and alerts `BIZCARD_ALERT_CHAT_ID` on failure.

## CLI

```bash
python -m bizcard_intake doctor              # environment report
python -m bizcard_intake setup               # guided fixes
python -m bizcard_intake scan card.jpg       # parse one image locally, print the draft
python -m bizcard_intake save data/sessions/cli-card.json
python -m bizcard_intake retry-pending
python -m bizcard_intake bot                 # what start_bot.sh runs
```

## Environment variables

```bash
TELEGRAM_BOT_TOKEN=              # required
TELEGRAM_ALLOWED_CHAT_IDS=       # required, comma-separated
BIZCARD_ALERT_CHAT_ID=           # health alerts
BIZCARD_PARSER=auto              # auto | codex | claude
BIZCARD_CODEX_MODEL=gpt-5.6-luna
BIZCARD_CLAUDE_MODEL=haiku
BIZCARD_PARSER_TIMEOUT=90
BIZCARD_PYTHON=                  # interpreter with numpy/Pillow (default: python3 on PATH)
```

## Privacy

Card images and OCR text are sent to the model provider you chose (OpenAI via
Codex, or Anthropic via Claude Code) and the parsed fields to Google Contacts.
Nothing else leaves the machine. Images are deleted after a successful save.

## Tests

```bash
python3 -m unittest discover -s tests
```

## Roadmap

- Ship a Google-verified shared OAuth client so users do not need their own
  Cloud project (rclone-style).
- Linux support: Tesseract OCR path and a systemd unit.
