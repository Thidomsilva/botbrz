"""
BRZ Monitor - Backend FastAPI para Vercel
Monitora preço e volume do BRZ e envia alertas no Telegram.

Arquitetura 24/7 serverless:
  - Estado persistido no Upstash Redis (REST API)
  - Checagem acionada por cron externo (cron-job.org) via GET /api/check
  - Sem scheduler interno (incompatível com serverless)
"""

import os
import time
import httpx
from datetime import datetime, timezone
from fastapi import FastAPI, Request

app = FastAPI(title="BRZ Monitor API")

# ── Configurações ─────────────────────────────────────────────────────────────
TELEGRAM_TOKEN              = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID            = os.getenv("TELEGRAM_CHAT_ID", "")
COINGECKO_API_KEY           = os.getenv("COINGECKO_API_KEY", "")
# Vercel KV (Redis integrado ao Vercel — crie em Storage → KV no dashboard)
KV_URL   = os.getenv("KV_REST_API_URL", "")
KV_TOKEN = os.getenv("KV_REST_API_TOKEN", "")

PRICE_CHANGE_THRESHOLD_PCT  = float(os.getenv("PRICE_CHANGE_PCT", "1.5"))
VOLUME_CHANGE_THRESHOLD_PCT = float(os.getenv("VOLUME_CHANGE_PCT", "80"))
PRICE_REPORT_INTERVAL_MIN   = int(os.getenv("PRICE_REPORT_INTERVAL", "5"))

# ── Upstash Redis (REST) ──────────────────────────────────────────────────────
async def redis_get(key: str) -> str | None:
    if not KV_URL:
        return None
    async with httpx.AsyncClient(timeout=5) as c:
        r = await c.get(
            f"{KV_URL}/get/{key}",
            headers={"Authorization": f"Bearer {KV_TOKEN}"},
        )
        return r.json().get("result")

async def redis_set(key: str, value: str) -> None:
    if not KV_URL:
        return
    async with httpx.AsyncClient(timeout=5) as c:
        await c.post(
            f"{KV_URL}/set/{key}",
            headers={"Authorization": f"Bearer {KV_TOKEN}"},
            json=[value],
        )

async def redis_mget(*keys: str) -> list:
    """Busca múltiplas chaves num único round-trip."""
    if not KV_URL:
        return [None] * len(keys)
    async with httpx.AsyncClient(timeout=5) as c:
        r = await c.post(
            f"{KV_URL}/pipeline",
            headers={"Authorization": f"Bearer {KV_TOKEN}"},
            json=[["GET", k] for k in keys],
        )
        return [item.get("result") for item in r.json()]

# ── CoinGecko ────────────────────────────────────────────────────────────────
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
BRZ_ID = "brz"

async def fetch_brz_price() -> dict | None:
    params = {
        "ids": BRZ_ID,
        "vs_currencies": "brl,usd",
        "include_24hr_vol": "true",
        "include_24hr_change": "true",
        "include_last_updated_at": "true",
    }
    headers = {"x-cg-demo-api-key": COINGECKO_API_KEY} if COINGECKO_API_KEY else {}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(COINGECKO_URL, params=params, headers=headers)
            r.raise_for_status()
            data = r.json().get(BRZ_ID, {})
            return {
                "price_brl": data.get("brl"),
                "price_usd": data.get("usd"),
                "volume_usd": data.get("usd_24h_vol"),
                "change_24h": data.get("usd_24h_change"),
                "fetched_at": int(time.time()),
            }
    except Exception as e:
        print(f"[BRZ] Erro ao buscar preço: {e}")
        return None

# ── Telegram ──────────────────────────────────────────────────────────────────
async def send_telegram(message: str, chat_id: str = None) -> None:
    if not TELEGRAM_TOKEN:
        return
    cid = chat_id or TELEGRAM_CHAT_ID
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, json={
                "chat_id": cid,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
    except Exception as e:
        print(f"[Telegram] Erro: {e}")

# ── Mensagens ─────────────────────────────────────────────────────────────────
def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")

def _brz_per_brl(price_brl: float | None) -> float | None:
    if not price_brl:
        return None
    return 1 / price_brl

def fmt_price_report(p: dict, titulo: str = "📊 Atualização de Preço") -> str:
    arrow = "🔺" if p["change_24h"] > 0 else "🔻"
    inv_brl = _brz_per_brl(p.get("price_brl"))
    inv_line = f"🔁 <b>1 BRL:</b>  <code>{inv_brl:.6f} BRZ</code>\n" if inv_brl else ""
    return (
        f"{titulo} — <b>BRZ</b>\n\n"
        f"💵 <b>USD:</b>  <code>${p['price_usd']:.6f}</code>\n"
        f"🇧🇷 <b>BRL:</b>  <code>R${p['price_brl']:.6f}</code>\n"
        f"{inv_line}"
        f"📦 <b>Vol 24h:</b>  <code>${p['volume_usd']:,.0f}</code>\n"
        f"{arrow} <b>Var 24h:</b>  {p['change_24h']:+.2f}%\n\n"
        f"🕐 {_ts()}"
    )

def fmt_price_alert(p: dict, pct_brl: float, pct_usd: float) -> str:
    arrow = "🔺" if pct_usd > 0 else "🔻"
    inv_brl = _brz_per_brl(p.get("price_brl"))
    inv_line = f"🔁 <b>1 BRL:</b>  <code>{inv_brl:.6f} BRZ</code>\n" if inv_brl else ""
    return (
        f"{arrow} <b>ALERTA DE VARIAÇÃO — BRZ</b>\n\n"
        f"💵 <b>USD:</b>  <code>${p['price_usd']:.6f}</code>  ({pct_usd:+.2f}%)\n"
        f"🇧🇷 <b>BRL:</b>  <code>R${p['price_brl']:.6f}</code>  ({pct_brl:+.2f}%)\n"
        f"{inv_line}"
        f"📦 <b>Vol 24h:</b>  <code>${p['volume_usd']:,.0f}</code>\n"
        f"📈 <b>Var 24h:</b>  {p['change_24h']:+.2f}%\n\n"
        f"🕐 {_ts()}"
    )

def fmt_volume_alert(p: dict, pct_vol: float) -> str:
    inv_brl = _brz_per_brl(p.get("price_brl"))
    inv_line = f"🔁 1 BRL:  <code>{inv_brl:.6f} BRZ</code>\n" if inv_brl else ""
    return (
        f"📢 <b>ALERTA DE VOLUME ANORMAL — BRZ</b>\n\n"
        f"📦 <b>Volume atual:</b>  <code>${p['volume_usd']:,.0f}</code>  ({pct_vol:+.1f}%)\n\n"
        f"💵 USD:  <code>${p['price_usd']:.6f}</code>\n"
        f"🇧🇷 BRL:  <code>R${p['price_brl']:.6f}</code>\n\n"
        f"{inv_line}"
        f"🕐 {_ts()}"
    )

# ── Checagem principal ────────────────────────────────────────────────────────
async def check_and_alert() -> dict:
    """Chamada pelo cron externo (GET /api/check) a cada minuto."""

    # Lê todo o estado do Redis num único round-trip
    vals = await redis_mget(
        "bot_enabled", "last_price_usd", "last_price_brl",
        "last_volume_usd", "last_report_ts", "alerts_sent"
    )
    bot_enabled, lp_usd, lp_brl, lv, lr_ts, al_sent = vals

    # Se desativado, não faz nada
    if bot_enabled == "0":
        return {"status": "disabled"}

    current = await fetch_brz_price()
    if not current:
        return {"status": "error", "message": "Falha ao buscar preço"}

    now           = int(time.time())
    last_usd      = float(lp_usd)  if lp_usd  else None
    last_brl      = float(lp_brl)  if lp_brl  else None
    last_vol      = float(lv)      if lv       else None
    last_rpt      = int(lr_ts)     if lr_ts    else 0
    alerts_sent   = int(al_sent)   if al_sent  else 0
    alerts        = []

    # ── Alerta de variação de preço ───────────────────────────────────────────
    if last_usd:
        pct_usd = ((current["price_usd"] - last_usd) / last_usd) * 100
        pct_brl = ((current["price_brl"] - last_brl) / last_brl) * 100
        if abs(pct_usd) >= PRICE_CHANGE_THRESHOLD_PCT:
            await send_telegram(fmt_price_alert(current, pct_brl, pct_usd))
            alerts_sent += 1
            alerts.append({"type": "price_alert", "pct_usd": round(pct_usd, 3)})

    # ── Alerta de volume anormal ──────────────────────────────────────────────
    if last_vol and current["volume_usd"]:
        pct_vol = ((current["volume_usd"] - last_vol) / last_vol) * 100
        if pct_vol >= VOLUME_CHANGE_THRESHOLD_PCT:
            await send_telegram(fmt_volume_alert(current, pct_vol))
            alerts_sent += 1
            alerts.append({"type": "volume_alert", "pct_vol": round(pct_vol, 1)})

    # ── Atualização periódica de preço ────────────────────────────────────────
    elapsed_min = (now - last_rpt) / 60
    if elapsed_min >= PRICE_REPORT_INTERVAL_MIN:
        await send_telegram(fmt_price_report(current))
        await redis_set("last_report_ts", str(now))
        alerts.append({"type": "periodic_report"})

    # Persiste novo estado
    await redis_set("last_price_usd", str(current["price_usd"]))
    await redis_set("last_price_brl", str(current["price_brl"]))
    await redis_set("last_volume_usd", str(current["volume_usd"]))
    await redis_set("last_check", str(now))
    await redis_set("alerts_sent", str(alerts_sent))

    return {"status": "ok", "current": current, "alerts": alerts}

# ── Endpoints ─────────────────────────────────────────────────────────────────
@app.get("/")
async def root():
    return {"service": "BRZ Monitor", "status": "running"}

@app.get("/api/check")
async def api_check():
    """Chamado pelo cron externo (cron-job.org) a cada minuto."""
    return await check_and_alert()

@app.get("/api/status")
async def api_status():
    vals = await redis_mget(
        "bot_enabled", "last_price_usd", "last_price_brl",
        "last_volume_usd", "last_check", "alerts_sent", "last_report_ts"
    )
    keys = ["bot_enabled", "last_price_usd", "last_price_brl",
            "last_volume_usd", "last_check", "alerts_sent", "last_report_ts"]
    return dict(zip(keys, vals))

@app.post("/api/telegram/webhook")
async def telegram_webhook(request: Request):
    """Processa comandos do Telegram."""
    body = await request.json()
    message = body.get("message", {})
    raw_text = message.get("text", "")
    parts = raw_text.strip().lower().split()
    text = parts[0].split("@")[0] if parts else ""
    chat_id  = str(message.get("chat", {}).get("id", ""))

    if not chat_id or not text.startswith("/"):
        return {"ok": True}

    if text in ("/preco", "/status"):
        price = await fetch_brz_price()
        reply = fmt_price_report(price, "📊 Preço Atual") if price else "❌ Não foi possível obter o preço agora."
        await send_telegram(reply, chat_id)

    elif text == "/ativar":
        await redis_set("bot_enabled", "1")
        await redis_set("last_report_ts", "0")   # força envio imediato no próximo cron
        reply = (
            "✅ <b>Monitor ativado!</b>\n\n"
            f"Você receberá:\n"
            f"• Atualização de preço a cada {PRICE_REPORT_INTERVAL_MIN} min\n"
            f"• Alerta quando preço variar ≥ {PRICE_CHANGE_THRESHOLD_PCT}%\n"
            f"• Alerta quando volume subir ≥ {VOLUME_CHANGE_THRESHOLD_PCT}%"
        )
        await send_telegram(reply, chat_id)

    elif text == "/desativar":
        await redis_set("bot_enabled", "0")
        reply = "⏸ <b>Monitor pausado.</b>\n\nEnvie /ativar para reativar."
        await send_telegram(reply, chat_id)

    elif text == "/help":
        enabled = await redis_get("bot_enabled")
        status_emoji = "✅ Ativo" if enabled != "0" else "⏸ Pausado"
        reply = (
            f"🤖 <b>BRZ Monitor — Comandos</b>\n\n"
            f"/ativar — Ativa o monitoramento\n"
            f"/desativar — Pausa o monitoramento\n"
            f"/preco — Preço atual do BRZ\n"
            f"/status — Preço atual do BRZ\n"
            f"/help — Esta mensagem\n\n"
            f"⚙️ <b>Configurações:</b>\n"
            f"• Atualização a cada {PRICE_REPORT_INTERVAL_MIN} min\n"
            f"• Alerta de preço ≥ {PRICE_CHANGE_THRESHOLD_PCT}%\n"
            f"• Alerta de volume ≥ {VOLUME_CHANGE_THRESHOLD_PCT}%\n\n"
            f"📡 Status: {status_emoji}"
        )
        await send_telegram(reply, chat_id)

    return {"ok": True}
