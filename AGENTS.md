# Agent rules for this repository

- Never run the `gws` CLI (or `python -m bizcard_intake` commands that call it: save, retry-pending, doctor, setup, migrate-names, rollback-names) from an agent/Codex session. Inside the agent sandbox gws cannot read its Keychain key, treats the stored credentials as undecryptable and deletes them, which logs the bot out of Google (happened on 2026-11-03). Ask the user to run such commands in Terminal.app instead.
- Tests must mock `contacts.run_gws`; run them with `python3 -B -m unittest discover -s tests`.
- Keep image processing (rectify, OCR sizing, profile crop) unchanged unless the user asks; it is tuned and the user is happy with it.
- Restarting the bot: `launchctl stop com.bizcard.intake` (launchd relaunches it).
