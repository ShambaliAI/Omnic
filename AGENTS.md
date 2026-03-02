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
