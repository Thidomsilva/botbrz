#!/usr/bin/env python3
"""
Registra o webhook do Telegram após o deploy no Vercel.

Uso:
  TELEGRAM_TOKEN=xxx VERCEL_URL=https://seu-projeto.vercel.app python set_webhook.py
"""
import os
import httpx

TOKEN = os.environ["TELEGRAM_TOKEN"]
VERCEL_URL = os.environ["VERCEL_URL"].rstrip("/")
WEBHOOK_URL = f"{VERCEL_URL}/api/telegram/webhook"

resp = httpx.post(
    f"https://api.telegram.org/bot{TOKEN}/setWebhook",
    json={"url": WEBHOOK_URL, "allowed_updates": ["message"]},
)
print(resp.json())
