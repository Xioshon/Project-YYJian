---
name: telegram
description: Handle Telegram messages, stickers, reactions, and reply rendering.
triggers: telegram, sticker, reaction, 表情包, 貼圖
allowed_tools: search_sticker, send_telegram_media, react_to_message
---

Model-chosen stickers should use reply markers, not send_telegram_media. Use reactions only when requested or natural.
