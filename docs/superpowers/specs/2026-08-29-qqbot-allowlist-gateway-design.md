# QQ Bot Allowlist Gateway Design

## Goal

Add the official QQ Bot as a text-only YueYue transport while keeping the existing Telegram transport installed but disabled by configuration. QQ messages must pass a strict local allowlist before any model or agent runtime call.

## Scope

The first QQ release supports:

- Official QQ Bot WebSocket authentication with AppID and AppSecret.
- `GROUP_AT_MESSAGE_CREATE` group mentions.
- `C2C_MESSAGE_CREATE` private messages.
- Plain-text replies.
- Two approved QQ groups, identified to the owner by the labels `1011454363` and `965457932`.
- One approved owner account, identified by the label `2493734026`.
- One-time, secret-backed binding from those human-readable labels to QQ platform openids.
- Telegram retained in source and dependencies, but disabled by default through `TELEGRAM_ENABLED=false`.

This release does not add QQ media, proactive QQ presence messages, QQ stickers, reactions, or channel/guild messages.

## Platform Constraint

The official QQ Bot API sends `group_openid`, `member_openid`, and `user_openid`; it does not expose raw QQ group numbers or raw QQ numbers in message events. The numeric IDs supplied by the owner are therefore configuration labels. The runtime allowlist uses locally persisted bindings from those labels to the openids observed in authenticated events.

## Components

### `qq_policy.py`

Owns all QQ authorization and binding decisions. It is independent of the QQ SDK and the model runtime so it can be tested with plain Python values.

It provides:

- Configuration parsing for approved group labels, owner label, and one-time binding code.
- An atomic local binding store under `workspace/project_cache/qq_allowlist_bindings.json`.
- A pure decision interface returning one of: `bind`, `model`, `development`, `ignore`.
- Binding command parsing that requires the exact local binding code and an approved numeric label.

Authorization rules:

1. A group openid not bound to either approved group label never reaches the model.
2. A bound group plus the bound owner member openid reaches the model.
3. A bound group plus any other member receives the literal reply `开发中`; the model is not called.
4. A bound owner C2C openid reaches the model.
5. Any other private sender is ignored and the model is not called.
6. Binding commands are consumed by the policy and never sent to the model.

Group member openids and C2C user openids are stored separately because QQ may use different identifier namespaces for the same human account.

### `qq_gateway.py`

Owns the official `qq-botpy` client and transport rendering.

- Subscribes only to public group/C2C message intents needed for this release.
- Converts QQ SDK message objects into a small transport-neutral input record.
- Calls `qq_policy.py` before creating or submitting any YueYue turn.
- Runs the blocking YueYue runtime in a worker thread so the QQ WebSocket event loop stays responsive.
- Uses a transport-prefixed chat key such as `qq:group:<group_openid>` or `qq:c2c:<user_openid>` to keep QQ state separate from Telegram state.
- Replies through the QQ message-reference APIs with the inbound message id.
- Removes unsupported Telegram sticker/screenshot markers from QQ text replies instead of attempting Telegram media sends.
- Logs openids only to ignored local runtime files; secrets are never logged.

### Startup integration

`main.py` gains a `--qq` mode and health fields for QQ configuration. The existing `--telegram` mode and `TelegramGateway` remain intact.

`start_yueyue.ps1` selects the enabled transport from configuration:

- `QQ_ENABLED=true` starts `main.py --qq`.
- `TELEGRAM_ENABLED=false` prevents the launcher from starting Telegram.
- A contradictory configuration that enables both transports fails closed in this first release rather than running two gateways against one process without a lifecycle design.

The interactive menu exposes QQ as a separate option without removing Telegram.

## Configuration and Secrets

Tracked `.env.example` documents empty values only:

```env
QQ_ENABLED=false
QQ_APP_ID=
QQ_APP_SECRET=
QQ_ALLOWED_GROUP_LABELS=1011454363,965457932
QQ_OWNER_LABEL=2493734026
QQ_BIND_CODE=
TELEGRAM_ENABLED=true
```

The real AppID, AppSecret, binding code, and enabled flags live only in the ignored local `.env`. No credential is written to source, tests, docs, logs, commits, or command output.

Bindings live under ignored `workspace/project_cache/`. Until all required openids are bound, the QQ gateway remains fail-closed.

## Binding Flow

1. Start the QQ gateway with a locally generated one-time `QQ_BIND_CODE`.
2. In each approved group, the owner mentions the bot with `绑定 <group-label> <bind-code>`.
3. The policy binds that event's `group_openid` to the approved group label and its `member_openid` to the owner group identity.
4. In private chat, the owner sends `绑定 <owner-label> <bind-code>` to bind the C2C `user_openid`.
5. The bot acknowledges a successful binding without calling the model.
6. After both groups and C2C are bound, remove or rotate `QQ_BIND_CODE`; existing bindings continue to work.

A binding label can only be assigned once unless the local binding file is deliberately edited or removed by the operator. This prevents a later message from silently taking over an existing approved label.

## Failure Handling

- Missing SDK or credentials: startup exits with a clear local error before connecting.
- QQ authentication failure: no fallback transport is started automatically.
- Unknown/unbound group: ignore except for a valid binding command.
- Unauthorized group member: reply exactly `开发中` without model work.
- Unauthorized C2C sender: ignore.
- Model failure for the authorized owner: send a short owner-safe failure message with no raw exception.
- QQ send failure: log a redacted local diagnostic and do not claim delivery success.

## Testing

Tests are written before implementation and prove:

- Unbound groups cannot call the model.
- Unauthorized members in an approved group produce `开发中` and zero model calls.
- Unauthorized private senders produce no reply and zero model calls.
- Authorized owner messages in bound groups and C2C produce exactly one model call.
- Binding requires an approved label and exact binding code.
- Binding cannot overwrite an existing label.
- Group-member and C2C owner openids remain separate.
- Telegram remains importable and its CLI entry remains present while disabled in local configuration.
- Secrets never appear in tracked files.

After focused tests pass, run the full project regression gate and a live QQ smoke test in the already configured group.

## Live Acceptance

The release is accepted when:

1. The QQ bot authenticates and becomes ready.
2. The owner can bind the existing group without a model call.
3. The owner mentions the bot and receives a YueYue model reply.
4. A non-owner mentions the bot and receives only `开发中`, with trace evidence showing no provider call.
5. The owner's private message reaches YueYue after C2C binding.
6. An unauthorized private sender receives no model reply.
7. Telegram does not start, but `main.py --telegram` and `TelegramGateway` remain in the repository.

