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

COMMANDS = [
  {"command": "ativar", "description": "Ativa o monitoramento"},
  {"command": "desativar", "description": "Pausa o monitoramento"},
  {"command": "preco", "description": "Mostra o preco atual do BRZ"},
  {"command": "status", "description": "Mostra o status atual"},
  {"command": "help", "description": "Lista os comandos"},
]

with httpx.Client(timeout=20) as client:
  webhook_resp = client.post(
    f"https://api.telegram.org/bot{TOKEN}/setWebhook",
    json={"url": WEBHOOK_URL, "allowed_updates": ["message"]},
  )
  commands_resp = client.post(
    f"https://api.telegram.org/bot{TOKEN}/setMyCommands",
    json={"commands": COMMANDS},
  )
  info_resp = client.get(f"https://api.telegram.org/bot{TOKEN}/getWebhookInfo")

print("setWebhook:", webhook_resp.json())
print("setMyCommands:", commands_resp.json())
print("getWebhookInfo:", info_resp.json())
