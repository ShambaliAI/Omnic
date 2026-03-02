# Project Constraints

## SQLite Lifecycle Rules

- The bot will run in group chats and be used by many group members, so SQLite must be used for persistent storage.
- During application startup, the system must check whether the SQLite file exists.
- If the SQLite file does not exist, it must be initialized automatically.
- For every feature that needs SQLite persistence, the feature setup stage must:
  - check whether the required table exists;
  - create the table if it does not exist;
  - validate whether the existing table schema matches the expected schema;
  - apply schema migration when the table structure is incompatible.

## Implementation Guideline

- Keep schema management in feature-level setup functions so each feature owns its required tables.
- Avoid deferred runtime failures: schema checks and migrations should run before handlers start serving traffic.

## Command Menu Sync Rule

- Whenever bot commands are added, removed, or updated, `_setup_command_menu` must be updated in the same change.
- Keep command handlers and Telegram shortcut menu entries consistent to avoid missing `/` menu actions.
- Command updates must cover the relevant Telegram scopes (at least `default`, `all_private_chats`, `all_group_chats`, and `all_chat_administrators` when group usage matters), so group chat shortcut menus can refresh correctly.
