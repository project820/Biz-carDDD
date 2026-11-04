# Agent rules for this repository

- gws keeps its credentials with the file key backend (`~/.config/gws/.encryption_key`). Every gws call must run with `GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND=file`; it is set in `.env` (the bot) and `~/.zshenv` (shells). Without it gws tries the macOS keychain, cannot decrypt `credentials.enc` and deletes it, logging the bot out of Google (happened on 2026-11-03).
- Re-login when needed: `GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND=file gws auth login --scopes https://www.googleapis.com/auth/contacts`, then finish the Google consent page in the browser with the Google account that should receive the contacts.
- Tests must mock `contacts.run_gws`; run them with `python3 -B -m unittest discover -s tests`.
- Keep image processing (rectify, OCR sizing, profile crop) unchanged unless the user asks; it is tuned and the user is happy with it.
- Restarting the bot: `launchctl stop com.bizcard.intake` (launchd relaunches it).
