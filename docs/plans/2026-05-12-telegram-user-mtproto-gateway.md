# Telegram User MTProto Gateway Plan

> Status: implemented  
> Owner: Codex  
> Target: bundled platform plugin `telegram_user`

## Goal

Add an opt-in Telegram transport that lets Hermes operate through a regular Telegram user account via Telethon / MTProto, separate from the existing Bot API `telegram` adapter.

This is intended for a dedicated Hermes-owned Telegram account, not a personal primary account.

## Non-Goals

- Do not replace or modify the existing Bot API `telegram` platform.
- Do not auto-reply to every inbox by default.
- Do not use a personal account session without explicit operator configuration.
- Do not implement full parity with the bot adapter's Telegram DM topics, model picker buttons, reactions, or inbound media pipeline in the first PR.

## Safety Model

- Default-deny inbound processing unless a chat/user allowlist or explicit allow-all flag is configured.
- Store Telethon session files under `~/.hermes/secrets/` by default.
- Treat Telethon session strings/files as full account credentials.
- Ignore outgoing/self messages by default.
- Require mention triggers in group/channel contexts by default.
- Rate-limit outbound sends per adapter instance.
- Keep platform namespace separate: `telegram_user`, with separate env vars and sessions.

## Configuration

Required for runtime:

- `TELEGRAM_USER_API_ID`
- `TELEGRAM_USER_API_HASH`
- `TELEGRAM_USER_SESSION_PATH` or `TELEGRAM_USER_SESSION_STRING`

Recommended:

- `TELEGRAM_USER_ALLOWED_CHATS`
- `TELEGRAM_USER_HOME_CHANNEL`

Optional:

- `TELEGRAM_USER_ALLOW_ALL_CHATS`
- `TELEGRAM_USER_REQUIRE_MENTION`
- `TELEGRAM_USER_SEND_INTERVAL_SECONDS`
- `TELEGRAM_USER_SESSION_NAME`

## Implementation Checklist

- [x] Inspect Hermes gateway plugin interface and current Telegram adapter boundaries.
- [x] Write this implementation plan.
- [x] Add bundled plugin files under `plugins/platforms/telegram_user/`.
- [x] Implement Telethon dependency detection and lazy install hook.
- [x] Implement config/env resolution and secure default session path.
- [x] Implement interactive setup/login flow.
- [x] Implement `TelegramUserAdapter.connect()` / `disconnect()`.
- [x] Implement inbound `events.NewMessage` handling with allowlist and mention gates.
- [x] Implement text send, typing, chat info, and outbound file helpers.
- [x] Implement `env_enablement_fn`, `validate_config`, `is_connected`, and `standalone_sender_fn`.
- [x] Register platform metadata, allowlist envs, home-channel delivery, and platform prompt hint.
- [x] Add focused unit tests for config, gating, event conversion, sending, and registration.
- [x] Run targeted gateway/plugin tests.

## Future Work

- Inbound photo/document/video caching into Hermes media caches.
- Forum topic/thread mapping for MTProto groups.
- Rich Markdown/HTML conversion parity with the Bot API adapter.
- Optional read receipts and reactions.
- Dedicated CLI command for login/status/logout outside `hermes gateway setup`.
