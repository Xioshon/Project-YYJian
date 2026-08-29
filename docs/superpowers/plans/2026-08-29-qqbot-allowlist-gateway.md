# QQ Bot Allowlist Gateway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect the official QQ Bot to YueYue with fail-closed group/C2C allowlists, while retaining but disabling Telegram through configuration.

**Architecture:** A pure `qq_policy.py` owns openid binding and authorization, an async `qq_gateway.py` owns the Tencent SDK transport, and a small public runtime adapter accepts transport-specific chat IDs. Startup flags choose exactly one gateway. Credentials and learned openids stay in ignored local files.

**Tech Stack:** Python 3.11+, pytest, Tencent `qq-botpy`, PowerShell launcher, YueYue Runtime v3.

**Spec:** `docs/superpowers/specs/2026-08-29-qqbot-allowlist-gateway-design.md`

## Global Constraints

- Use the official QQ Bot AppID/AppSecret WebSocket path.
- Text-only QQ support in this release.
- Never call a provider/model before QQ policy returns `model`.
- Unauthorized group members receive exactly `开发中`.
- Unauthorized private senders receive no reply.
- Telegram source, dependency, and CLI remain present; local configuration sets it disabled.
- Never commit or print AppSecret, binding code, or learned openids.
- All production behavior is introduced test-first.

---

### Task 1: Fail-Closed QQ Policy and Binding Store

**Files:**
- Create: `qq_policy.py`
- Create: `tests_v3/test_qq_policy.py`

**Interfaces:**
- Produces: `QQPolicyConfig(group_labels: frozenset[str], owner_label: str, bind_code: str)`
- Produces: `QQDecision(action: Literal["bind", "model", "development", "ignore"], reply: str = "")`
- Produces: `QQBindingStore(path: str | Path)` with atomic `read()` and `write(data)`
- Produces: `QQAccessPolicy(config, store).decide_group(group_openid, member_openid, content)`
- Produces: `QQAccessPolicy(config, store).decide_c2c(user_openid, content)`

- [ ] **Step 1: Write failing policy tests**

```python
from qq_policy import QQAccessPolicy, QQBindingStore, QQPolicyConfig


def policy(tmp_path):
    return QQAccessPolicy(
        QQPolicyConfig(frozenset({"1011454363", "965457932"}), "2493734026", "bind-123"),
        QQBindingStore(tmp_path / "qq.json"),
    )


def test_unbound_group_is_ignored(tmp_path):
    assert policy(tmp_path).decide_group("group-a", "member-a", "你好").action == "ignore"


def test_binding_group_consumes_command_without_model(tmp_path):
    decision = policy(tmp_path).decide_group(
        "group-a", "owner-in-group-a", "绑定 1011454363 bind-123"
    )
    assert decision.action == "bind"
    assert "绑定成功" in decision.reply


def test_non_owner_in_bound_group_gets_development(tmp_path):
    access = policy(tmp_path)
    access.decide_group("group-a", "owner-a", "绑定 1011454363 bind-123")
    decision = access.decide_group("group-a", "someone-else", "@月月 你好")
    assert decision.action == "development"
    assert decision.reply == "开发中"


def test_owner_in_bound_group_reaches_model(tmp_path):
    access = policy(tmp_path)
    access.decide_group("group-a", "owner-a", "绑定 1011454363 bind-123")
    assert access.decide_group("group-a", "owner-a", "你好").action == "model"


def test_only_bound_owner_c2c_reaches_model(tmp_path):
    access = policy(tmp_path)
    assert access.decide_c2c("stranger", "你好").action == "ignore"
    assert access.decide_c2c("owner-c2c", "绑定 2493734026 bind-123").action == "bind"
    assert access.decide_c2c("owner-c2c", "你好").action == "model"
    assert access.decide_c2c("stranger", "你好").action == "ignore"


def test_binding_cannot_overwrite_existing_group_label(tmp_path):
    access = policy(tmp_path)
    assert access.decide_group("group-a", "owner-a", "绑定 1011454363 bind-123").action == "bind"
    assert access.decide_group("group-b", "owner-b", "绑定 1011454363 bind-123").action == "ignore"
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests_v3/test_qq_policy.py -q`

Expected: collection fails with `ModuleNotFoundError: No module named 'qq_policy'`.

- [ ] **Step 3: Implement the minimal policy**

```python
@dataclass(frozen=True)
class QQDecision:
    action: Literal["bind", "model", "development", "ignore"]
    reply: str = ""


class QQAccessPolicy:
    def decide_group(self, group_openid: str, member_openid: str, content: str) -> QQDecision:
        command = self._binding_command(content)
        if command and command.label in self.config.group_labels:
            return self._bind_group(command.label, group_openid, member_openid)
        binding = self._group_binding(group_openid)
        if not binding:
            return QQDecision("ignore")
        if secrets.compare_digest(binding["owner_member_openid"], member_openid):
            return QQDecision("model")
        return QQDecision("development", "开发中")
```

Implement C2C with the same fail-closed order. Persist schema version 1 JSON through a sibling temporary file, `flush`, `fsync`, and `os.replace`.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `python -m pytest tests_v3/test_qq_policy.py -q`

Expected: all policy tests pass.

- [ ] **Step 5: Commit**

```powershell
git add qq_policy.py tests_v3/test_qq_policy.py
git commit -m "feat: add fail-closed QQ allowlist policy"
```

---

### Task 2: Transport-Neutral Runtime Entry

**Files:**
- Modify: `yueyue_v3/runtime.py`
- Modify: `tests_v3/test_runtime_integration.py`

**Interfaces:**
- Produces: `YueYueRuntimeV3.process_text_turn(chat_id, user_input, tool_callback=None, response_policy=None, message_id="") -> str`
- Preserves: `YueYueRuntimeV3.chat(...) -> dict[str, str]`

- [ ] **Step 1: Write a failing chat-ID isolation test**

```python
def test_process_text_turn_preserves_transport_chat_id(runtime_root):
    provider = ScriptedProvider([ProviderResponse("在呢", "", [])])
    runtime = YueYueRuntimeV3(runtime_root, provider)
    reply = runtime.process_text_turn("qq:c2c:owner", "你好", message_id="qq-message-1")
    assert reply == "在呢"
    received = [e for e in runtime.events.event_store.read() if e["kind"] == "turn.received"][-1]
    assert received["payload"]["chat_id"] == "qq:c2c:owner"
```

Update `_process_turn`'s `turn.received` event payload to include `chat_id` so the assertion is observable.

- [ ] **Step 2: Run the test and verify RED**

Run: `python -m pytest tests_v3/test_runtime_integration.py::test_process_text_turn_preserves_transport_chat_id -q`

Expected: `AttributeError` for the missing `process_text_turn` method.

- [ ] **Step 3: Implement the public adapter and delegate `chat()` to it**

```python
def process_text_turn(self, chat_id, user_input, tool_callback=None, response_policy=None, message_id=""):
    primary = extract_primary_message(user_input) or str(user_input or "")
    route = str(getattr(response_policy, "route", "") or "")
    mode = _mode_from_route(route) if route else classify_turn_mode(primary)
    turn = TurnEnvelope(str(chat_id), primary, mode, str(message_id or ""))
    if route.casefold() == "screen_observe":
        with self.events.writer_scope():
            return self._screen_observe_turn(turn, tool_callback)
    return self.process_turn(turn, tool_callback)
```

Keep benign-testing coercion and existing Telegram behavior unchanged.

- [ ] **Step 4: Run focused and existing runtime integration tests**

Run: `python -m pytest tests_v3/test_runtime_integration.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add yueyue_v3/runtime.py tests_v3/test_runtime_integration.py
git commit -m "refactor: expose transport-neutral text turns"
```

---

### Task 3: QQ Gateway Dispatch and Zero-Model Authorization Proof

**Files:**
- Create: `qq_gateway.py`
- Create: `tests_v3/test_qq_gateway.py`
- Modify: `requirements.txt`

**Interfaces:**
- Consumes: `QQAccessPolicy.decide_group/decide_c2c`
- Consumes: `YueYueRuntimeV3.process_text_turn(...)`
- Produces: `QQMessageDispatcher(agent, policy).handle_group(message, send)`
- Produces: `QQMessageDispatcher(agent, policy).handle_c2c(message, send)`
- Produces: `QQGateway(appid, secret, agent, policy).start()`

- [ ] **Step 1: Write failing dispatcher tests with a counting agent**

```python
class CountingAgent:
    def __init__(self):
        self.calls = []

    def process_text_turn(self, chat_id, text, **kwargs):
        self.calls.append((chat_id, text, kwargs))
        return "月月回覆"


def test_group_non_owner_never_calls_model(tmp_path):
    access = bound_group_policy(tmp_path)
    agent = CountingAgent()
    sent = []
    dispatcher = QQMessageDispatcher(agent, access)
    asyncio.run(
        dispatcher.dispatch_group(QQInbound("m1", "group-a", "stranger", "你好"), sent.append)
    )
    assert agent.calls == []
    assert sent == ["开发中"]


def test_group_owner_calls_model_once(tmp_path):
    access = bound_group_policy(tmp_path)
    agent = CountingAgent()
    sent = []
    dispatcher = QQMessageDispatcher(agent, access)
    asyncio.run(
        dispatcher.dispatch_group(QQInbound("m2", "group-a", "owner-a", "你好"), sent.append)
    )
    assert len(agent.calls) == 1
    assert agent.calls[0][0] == "qq:group:group-a"
    assert sent == ["月月回覆"]
```

```python
def test_unauthorized_c2c_is_silent(tmp_path):
    agent = CountingAgent()
    sent = []
    dispatcher = QQMessageDispatcher(agent, policy(tmp_path))
    asyncio.run(dispatcher.dispatch_c2c(QQInbound("m3", "", "stranger", "你好"), sent.append))
    assert agent.calls == []
    assert sent == []


def test_binding_ack_does_not_call_model(tmp_path):
    agent = CountingAgent()
    sent = []
    dispatcher = QQMessageDispatcher(agent, policy(tmp_path))
    asyncio.run(
        dispatcher.dispatch_group(
            QQInbound("m4", "group-a", "owner-a", "绑定 1011454363 bind-123"), sent.append
        )
    )
    assert agent.calls == []
    assert sent and "绑定成功" in sent[0]


def test_qq_reply_strips_unsupported_transport_markers(tmp_path):
    agent = CountingAgent()
    agent.process_text_turn = lambda *args, **kwargs: "收到\n[表情包: cat.webp]\n[系統截圖: shot.png]"
    sent = []
    dispatcher = QQMessageDispatcher(agent, bound_group_policy(tmp_path))
    asyncio.run(dispatcher.dispatch_group(QQInbound("m5", "group-a", "owner-a", "你好"), sent.append))
    assert sent == ["收到"]


def test_model_exception_returns_owner_safe_text(tmp_path):
    agent = CountingAgent()
    agent.process_text_turn = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("secret detail"))
    sent = []
    dispatcher = QQMessageDispatcher(agent, bound_group_policy(tmp_path))
    asyncio.run(dispatcher.dispatch_group(QQInbound("m6", "group-a", "owner-a", "你好"), sent.append))
    assert sent == ["刚刚没接稳，你再发一次。"]
    assert "secret detail" not in sent[0]
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests_v3/test_qq_gateway.py -q`

Expected: collection fails because `qq_gateway` does not exist.

- [ ] **Step 3: Implement the SDK-independent dispatcher**

```python
async def _model_reply(self, chat_id, inbound):
    policy = response_policy_for(classify_interaction(inbound.content))
    return await asyncio.to_thread(
        self.agent.process_text_turn,
        chat_id,
        inbound.content,
        response_policy=policy,
        message_id=inbound.message_id,
    )
```

Only execute `_model_reply` for `decision.action == "model"`. Return immediately for `ignore`; send `decision.reply` for `bind` and `development`.

- [ ] **Step 4: Implement the lazy official SDK adapter**

Create the `botpy.Client` subclass inside `QQGateway.start()` so policy tests and health checks do not require connecting. Use `botpy.Intents(public_messages=True)` and handlers `on_group_at_message_create` / `on_c2c_message_create`. Reply with `post_group_message` / `post_c2c_message`, `msg_type=0`, inbound `msg_id`, and monotonically increasing `msg_seq`.

- [ ] **Step 5: Add and install the pinned dependency**

Add `qq-botpy>=1.2.1,<2` to `requirements.txt` and run:

`python -m pip install "qq-botpy>=1.2.1,<2"`

- [ ] **Step 6: Run gateway tests and compile**

Run: `python -m pytest tests_v3/test_qq_gateway.py -q`

Run: `python -m py_compile qq_policy.py qq_gateway.py yueyue_v3/runtime.py`

Expected: both commands pass.

- [ ] **Step 7: Commit**

```powershell
git add qq_gateway.py tests_v3/test_qq_gateway.py requirements.txt
git commit -m "feat: add official QQ bot gateway"
```

---

### Task 4: Configuration, CLI, and Launcher Selection

**Files:**
- Modify: `.env.example`
- Modify: `main.py`
- Modify: `start_yueyue.ps1`
- Modify: `tests_v3/test_health.py`
- Create: `tests_v3/test_channel_config.py`

**Interfaces:**
- Produces: `channel_enabled(name: str) -> bool`
- Produces: `selected_gateway() -> Literal["qq", "telegram"]` that raises on zero or multiple enabled transports outside check-only mode
- Produces: `main.py --qq`

- [ ] **Step 1: Write failing configuration tests**

```python
def test_qq_enabled_telegram_disabled_selects_qq(monkeypatch):
    monkeypatch.setenv("QQ_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_ENABLED", "false")
    assert main.selected_gateway() == "qq"


def test_two_enabled_gateways_fail_closed(monkeypatch):
    monkeypatch.setenv("QQ_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_ENABLED", "true")
    with pytest.raises(RuntimeError, match="exactly one"):
        main.selected_gateway()
```

Add a source-contract test asserting `--telegram`, `TelegramGateway`, and the Telegram dependency remain present.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests_v3/test_channel_config.py -q`

Expected: failure because `selected_gateway` does not exist.

- [ ] **Step 3: Implement flags, health, and CLI**

Read booleans through `env_value`, add QQ health without exposing the secret, add `--qq`, and instantiate:

```python
policy = QQAccessPolicy.from_env(
    os.path.join(PROJECT_CACHE_DIR, "qq_allowlist_bindings.json")
)
QQGateway(env_value("QQ_APP_ID"), env_value("QQ_APP_SECRET"), build_agent(), policy).start()
```

- [ ] **Step 4: Update the launcher**

Add `qq_policy.py` and `qq_gateway.py` to required/compiled files. Replace the hard-coded Telegram start with a config probe that starts `main.py --qq` when QQ is true and Telegram is false. `-CheckOnly` reports that no gateway was started.

- [ ] **Step 5: Document empty configuration keys**

Append the exact empty/example keys from the design to `.env.example`; never add real values.

- [ ] **Step 6: Run focused config/startup tests**

Run: `python -m pytest tests_v3/test_channel_config.py tests_v3/test_health.py -q`

Run: `powershell -ExecutionPolicy Bypass -File .\start_yueyue.ps1 -CheckOnly`

Expected: all checks pass and QQ credentials are reported only as `ok`/`missing`.

- [ ] **Step 7: Commit**

```powershell
git add .env.example main.py start_yueyue.ps1 tests_v3/test_channel_config.py tests_v3/test_health.py
git commit -m "feat: select QQ gateway by configuration"
```

---

### Task 5: Local Secrets, Binding, Live Smoke, and Documentation

**Files:**
- Modify locally only: `.env` (ignored; never stage)
- Modify: `README.md`
- Modify: `ARCHITECTURE.md`
- Modify: `RUNBOOK.md`

**Interfaces:**
- Consumes all earlier tasks.

- [ ] **Step 1: Generate a one-time local binding code**

Run: `[System.Convert]::ToHexString([System.Security.Cryptography.RandomNumberGenerator]::GetBytes(12)).ToLowerInvariant()`

Do not print the resulting code in assistant messages, logs, commits, or tracked files.

- [ ] **Step 2: Update ignored `.env` through `apply_patch`**

Set `QQ_ENABLED=true`, the supplied AppID/AppSecret, approved labels, owner label, and generated binding code. Set `TELEGRAM_ENABLED=false`. Preserve `TELEGRAM_BOT_TOKEN` and every existing Telegram setting.

- [ ] **Step 3: Update operator documentation**

Document QQ startup, binding commands, openid privacy, allowlist behavior, Telegram enable/disable flags, and the text-only first-release limitation. Do not include the real binding code or credentials.

- [ ] **Step 4: Run the full regression gate**

Run: `powershell -ExecutionPolicy Bypass -File .\start_yueyue.ps1 -CheckOnly -SelfTest`

Expected: pytest, scripts, Ruff, health, system audit, and secret scan pass.

- [ ] **Step 5: Start QQ and bind the existing group**

Run: `powershell -ExecutionPolicy Bypass -File .\start_yueyue.ps1 -Restart`

Ask the owner to mention the bot in the existing approved group with the one-time binding command. Verify the ignored binding store records the group openid and owner member openid without printing either value.

- [ ] **Step 6: Live authorization smoke**

Have the owner mention the bot normally and verify exactly one provider call. Have a non-owner mention it and verify the literal `开发中` reply with no increase in provider-call events. Bind C2C separately and verify unauthorized C2C remains silent.

- [ ] **Step 7: Commit tracked docs and push branch**

```powershell
git add README.md ARCHITECTURE.md RUNBOOK.md
git commit -m "docs: add QQ bot operations"
git push origin codex/qqbot-allowlist-gateway
```

Verify `git status --short` shows no tracked credential file and `.env` remains ignored.
