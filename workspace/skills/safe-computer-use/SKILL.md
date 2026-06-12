---
name: safe-computer-use
description: Operate the local UI conservatively with explicit checkpoints.
triggers: click, screen, computer, 電腦, 點擊, 螢幕
allowed_tools: get_screen_ui, click_ui_element, type_keyboard, press_hotkey
---

Inspect UI before clicking. Prefer hotkeys for deterministic actions. Stop on unexpected state.
