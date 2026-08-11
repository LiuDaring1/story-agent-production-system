# Security and repository privacy

This repository is intended to remain private unless a separate public-release review is completed.

## Never commit

- API keys, passwords, cookies, session state, CAPTCHA material or account screenshots.
- `.env*`, `secrets.*`, `pipeline_config.local.json` or Keychain exports.
- Raw user media, generated media, customer packages or `auto-project/` runs.
- Raw conversation exports or unredacted logs.
- Personal documents and third-party private information.

## Secret sources

Runtime credentials must come from environment variables or the operating system secure store. The repository stores only environment variable names.

## If a secret appears in chat or Git

Rotate it first. Removing it from the latest file is insufficient if it entered Git history; rewrite history and invalidate the old credential.

## Before every external publish

1. Review `git status` and staged files explicitly.
2. Scan the current tree and history for token patterns and private keys.
3. Confirm the remote repository is private.
4. Verify ignored media and runtime directories are not staged.
5. Inspect the remote file list after pushing.
