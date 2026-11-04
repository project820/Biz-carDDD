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
the bot tells the `BIZCARD_ALERT_CHAT_ID` chat within a day, failed saves stay
on hold as cards and are saved automatically after you log in again
(`python -m bizcard_intake retry-pending` forces it).

Keep the encryption key in a file so every caller (launchd, Terminal, agents)
can read it: set `GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND=file` in `.env` and in your
shell profile, and log in with the same variable set. A gws run in keychain mode
that cannot read the key deletes `credentials.enc`.

## Using the bot

1. Send one or more card photos (an album is fine). Every photo becomes its own
   card with a number, a preview image and a draft:
   ```
   [명함 12] 명함 초안
   표시 이름: 정호준 영업팀장 누리네트웍스

   - 이름: 정호준
   - 직함: 영업팀장
   - 직함원문: 영업팀장/컨설턴트 (Sales Executive)
   - 회사: 누리네트웍스
   - 휴대전화: 010-5555-0101
   ...
   ```
   Buttons: 저장 · 직접 수정 · 뒷면 추가 · 인식 재시도 · 취소. For an album the bot
   also sends a summary with "저장 가능한 N건 모두 저장".
2. To fix a card, reply to its draft with `항목: 값` lines (`직함: 과장`,
   `휴대전화: 010-1234-5678`, `팩스: -` to clear). These are applied as typed,
   without the AI, and survive re-recognition and back-side merges. Free text
   ("이메일은 kim@acme.com") still goes through the AI reviser.
3. `목록` lists every card that is not saved yet, with a save-all button.
4. If Google is unreachable or logged out, the card is kept as "저장 보류" and
   saved automatically once Google works again (backoff from 2 minutes up to 6
   hours; checked every minute and at the daily health check). A contact whose
   photo upload failed is saved anyway and the photo is retried.

Cards live in `data/cards/<id>.json` (one file each, atomic writes). Old
`data/sessions` and `data/pending_contacts` files are migrated on startup:
entries already saved are retired, approved-but-failed ones become retry
cards, and drafts nobody approved stay in review and are never saved on their
own.

### Design choices (Korean cards)

- The phone and Messages show `이름 짧은직함 회사`, e.g. `최민준 차장 (주)다솔이엔지`.
  That whole string is Google's *given name*; *family name* stays empty so the
  device's first/last-name order cannot put the company first. The full title
  and company stay in the organization fields.
- No title is invented; a card without one gets a 확인 필요 note.
- `052-555-0126~7` is saved as two numbers without asking.
- Duplicate detection scans all contacts and needs a matching mobile number and
  exact name; a retry reuses the contact id the first attempt created.
- Card images are deleted after a successful save.

### Renaming contacts saved with the old layout

Contacts saved before November 2026 have the company in the family name. Run in
Terminal:

```bash
python3 -m bizcard_intake migrate-names                # preview
python3 -m bizcard_intake migrate-names --apply --limit 1
python3 -m bizcard_intake migrate-names --apply        # everything, backup first
python3 -m bizcard_intake rollback-names data/name-migration/<backup>.json
```

Only the name field changes; phones, company, photo and notes are untouched.

### Messages shows a number instead of the name

Messages decides "unknown sender" when a conversation starts. If the person
texted before the Google card synced to the Mac (Google syncs roughly hourly),
the thread can stay unknown. Quit and reopen Messages; if it persists, make any
small edit to the card so it syncs again, then use "알고 있음으로 표시".

## Parser and tokens

| Backend | How | Per card |
|---|---|---|
| `openai` (default when an API key is stored) | Responses API, `gpt-6-luna`, reasoning `low`, strict JSON schema, `store: false` | billed on the OpenAI API account; token counts are logged as `openai model=... tokens in=... out=...` |
| `codex` | `codex exec` in an empty dir with `--ignore-user-config --ignore-rules`, reasoning `low` | ~5–8k tokens of the ChatGPT plan's Codex allowance |
| `claude` | `claude -p --json-schema --allowedTools Read` | Claude subscription |

The API key is the one stored for the Decisions API (`~/.config/gb-invest/decisions.env`);
the bot reads only the `OPENAI_API_KEY` line and never logs it. If an API call
fails (network, quota, rate limit) the same prompt is retried once with the
codex CLI, then claude. The image sent to the model is a 1024px copy; the 1600px
one is only for Apple Vision OCR. Typed `항목: 값` edits, saving and retries use no
model at all.

Set `BIZCARD_PARSER=openai|codex|claude`, `BIZCARD_OPENAI_MODEL`,
`BIZCARD_REASONING_EFFORT`, `BIZCARD_CODEX_MODEL`, `BIZCARD_CLAUDE_MODEL` in `.env`.

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
python -m bizcard_intake save <card-id>
python -m bizcard_intake retry-pending     # retry saves on hold now
python -m bizcard_intake list              # cards not saved yet
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
