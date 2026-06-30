# Kimchi Reader Deck Sync

An Anki add-on that syncs selected Anki decks to Kimchi Reader by marking matched words as `Seen` or `Known`.

The add-on adds a new menu action:

- `Tools -> Sync Deck with Kimchi Reader`

## What It Does

- Shows a checkbox list of decks from the current Anki collection.
- Lets the user choose exactly which decks to sync each time.
- Logs into Kimchi Reader directly with its email OTP flow.
- Reads the active Anki collection directly from disk.
- Extracts Korean headwords from supported note fields.
- Skips sentence-like entries and unsupported note shapes instead of guessing.
- Uses Kimchi Reader's lemmatizer before submitting any word status changes.
- Filters out words that are already marked in Kimchi Reader.
- Maintains a local SQLite sync cache of expressions that were already confirmed as `Seen` or `Known`, so repeat syncs can skip a lot of lemmatization work.
- Shows a preview of pending changes before any write happens.
- Shows a styled results dialog with the words that actually changed.

## Intended Anki Version

Built against:

- Anki `25.02.5`
- Python `3.9.18`
- Qt `6.6.2`
- PyQt `6.6.1`

It should also work on nearby Anki `25.02.x` builds.

## How Statuses Are Derived

- `Known`: at least one reviewed card for that expression has an interval of `21` days or more.
- `Seen`: at least one reviewed card exists, but none are mature enough to count as `Known`.
- Unreviewed-only expressions are skipped.

If the same expression appears multiple times, `Known` overrides `Seen`.

## Supported Extraction Rules

The add-on prefers these field names:

- `Korean`
- `Vocab`
- `Disambig-Korean`
- `Front`
- `FrontText`

It also includes a few note-type-specific overrides in the code for common Korean note models.

Safety defaults:

- only Korean text is extracted
- entries with more than `2` Korean tokens are skipped as sentence-like
- empty, unsupported, or non-Korean fields are skipped

## Kimchi Reader Login

The add-on uses Kimchi Reader's direct OTP login flow:

1. Enter your Kimchi Reader email.
2. Click `Send Code`.
3. Enter the emailed one-time code.
4. Confirm the previewed changes.

The add-on stores local auth state in `user_files/state.json` so you usually do not need to log in every run.

## Install

Copy the numeric add-on folder into Anki's `addons21` directory.

Example folder layout:

```text
addons21/
  9462155301/
    __init__.py
    backend.py
    config.json
    manifest.json
    meta.json
    README.md
    user_files/
```

Then restart Anki.

## Sync Flow

1. Open Anki.
2. Choose `Tools -> Sync Deck with Kimchi Reader`.
3. Select one or more decks.
4. Log into Kimchi Reader if needed.
5. Review the pending `Seen` and `Known` changes.
6. Click `Apply Changes` to send them.
7. Review the final results dialog.

## Local Files

The add-on writes runtime state under `user_files/`:

- `state.json`
  - saved Kimchi auth tokens and login state
- `kimchi_invalid_words.json`
  - words Kimchi rejected previously
- `kimchi_sync_cache.sqlite3`
  - local expression-status cache used to speed up repeat syncs
- `runs/<timestamp>/`
  - audit artifacts for each sync run

Each run directory may contain:

- `seen.txt`
- `known.txt`
- `submitted_seen.txt`
- `submitted_known.txt`
- `report.json`

## Sharing Safely

Do not share runtime state from `user_files/`.

Before distributing the add-on, remove:

- `__pycache__/`
- `*.pyc`
- `user_files/state.json`
- `user_files/kimchi_invalid_words.json`
- `user_files/kimchi_sync_cache.sqlite3`
- `user_files/runs/`

## Notes

- The add-on talks directly to Kimchi Reader's API.
- It does not require Playwright.
- It does not require AnkiConnect.
- It reads the current collection by creating a temporary SQLite snapshot first, which avoids locking the live Anki database.
