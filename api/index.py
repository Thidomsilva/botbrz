"""
BRZ Monitor - Backend FastAPI para Vercel
Monitora preço e volume do BRZ e envia alertas no Telegram.
"""

import os
import json
import time
import httpx
from datetime import datetime, timezone
from fastapi import FastAPI, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from apscheduler.schedulers.background import BackgroundScheduler

app = FastAPI(title="BRZ Monitor API")

# ── Configurações via variáveis de ambiente ──────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY", "")   # opcional - plano gratuito funciona

# Limites configuráveis (podem virar env vars futuramente)
PRICE_CHANGE_THRESHOLD_PCT = float(os.getenv("PRICE_CHANGE_PCT", "1.5"))   # % de variação para alertar
VOLUME_CHANGE_THRESHOLD_PCT = float(os.getenv("VOLUME_CHANGE_PCT", "80"))  # % de aumento de volume
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL", "60"))            # frequência de checagem

# ── Estado em memória (substituir por Redis/DB em produção) ──────────────────
state = {
    "last_price_brl": None,
    "last_price_usd": None,
    "last_volume_usd": None,
    "last_check": None,
    "alerts_sent": 0,
    "history": [],          # últimas 100 leituras
}

# ── CoinGecko ────────────────────────────────────────────────────────────────
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
BRZ_ID = "brz"

async def fetch_brz_price() -> dict | None:
    """Busca preço e volume do BRZ na CoinGecko."""
    params = {
        "ids": BRZ_ID,
        "vs_currencies": "brl,usd",
        "include_24hr_vol": "true",
        "include_24hr_change": "true",
        "include_last_updated_at": "true",
    }
    headers = {}
    if COINGECKO_API_KEY:
        headers["x-cg-demo-api-key"] = COINGECKO_API_KEY

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
                "updated_at": data.get("last_updated_at"),
                "fetched_at": int(time.time()),
            }
    except Exception as e:
        print(f"[BRZ] Erro ao buscar preço: {e}")
        return None

# ── Telegram ─────────────────────────────────────────────────────────────────
async def send_telegram(message: str):
    """Envia mensagem para o chat configurado."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Telegram] Token ou chat_id não configurado.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, json=payload)
    except Exception as e:
        print(f"[Telegram] Erro ao enviar mensagem: {e}")

def format_price_alert(current: dict, pct_brl: float, pct_usd: float) -> str:
    arrow = "🔺" if pct_usd > 0 else "🔻"
    ts = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    return (
        f"{arrow} <b>ALERTA DE VARIAÇÃO — BRZ</b>\n\n"
        f"💵 <b>USD:</b> <code>${current['price_usd']:.6f}</code>  ({pct_usd:+.2f}%)\n"
        f"🇧🇷 <b>BRL:</b> <code>R${current['price_brl']:.6f}</code>  ({pct_brl:+.2f}%)\n"
        f"📊 <b>Vol 24h:</b> <code>${current['volume_usd']:,.0f}</code>\n"
        f"📈 <b>Var 24h:</b> {current['change_24h']:+.2f}%\n\n"
        f"🕐 {ts}"
    )

def format_volume_alert(current: dict, pct_vol: float) -> str:
    ts = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    return (
        f"📢 <b>ALERTA DE VOLUME ANORMAL — BRZ</b>\n\n"
        f"📦 <b>Volume atual:</b> <code>${current['volume_usd']:,.0f}</code>\n"
        f"📈 <b>Variação de volume:</b> {pct_vol:+.1f}%\n\n"
        f"💵 USD: <code>${current['price_usd']:.6f}</code>\n"
        f"🇧🇷 BRL: <code>R${current['price_brl']:.6f}</code>\n\n"
        f"🕐 {ts}"
    )

# ── Lógica de monitoramento ───────────────────────────────────────────────────
async def check_and_alert():
    """Função principal de checagem — chamada pelo scheduler e pelo endpoint /check."""
    current = await fetch_brz_price()
    if not current:
        return {"status": "error", "message": "Falha ao buscar preço"}

    alerts = []

    # Salva no histórico (máx 100 entradas)
    state["history"].append(current)
    if len(state["history"]) > 100:
        state["history"].pop(0)

    state["last_check"] = current["fetched_at"]

    # ── Alerta de variação de preço ─────────────────────────────────────────
    if state["last_price_usd"] is not None:
        pct_usd = ((current["price_usd"] - state["last_price_usd"]) / state["last_price_usd"]) * 100
        pct_brl = ((current["price_brl"] - state["last_price_brl"]) / state["last_price_brl"]) * 100

        if abs(pct_usd) >= PRICE_CHANGE_THRESHOLD_PCT:
            msg = format_price_alert(current, pct_brl, pct_usd)
            await send_telegram(msg)
            state["alerts_sent"] += 1
            alerts.append({"type": "price", "pct_usd": pct_usd, "pct_brl": pct_brl})

    # ── Alerta de volume anormal ────────────────────────────────────────────
    if state["last_volume_usd"] and current["volume_usd"]:
        pct_vol = ((current["volume_usd"] - state["last_volume_usd"]) / state["last_volume_usd"]) * 100
        if pct_vol >= VOLUME_CHANGE_THRESHOLD_PCT:
            msg = format_volume_alert(current, pct_vol)
            await send_telegram(msg)
            state["alerts_sent"] += 1
            alerts.append({"type": "volume", "pct_vol": pct_vol})

    # Atualiza estado
    state["last_price_brl"] = current["price_brl"]
    state["last_price_usd"] = current["price_usd"]
    state["last_volume_usd"] = current["volume_usd"]

    return {"status": "ok", "current": current, "alerts": alerts}

# ── Scheduler (cron interno) ──────────────────────────────────────────────────
scheduler = BackgroundScheduler()

@app.on_event("startup")
def start_scheduler():
    import asyncio

    def run_check():
        asyncio.run(check_and_alert())

    scheduler.add_job(run_check, "interval", seconds=CHECK_INTERVAL_SECONDS, id="brz_check")
    scheduler.start()
    print(f"[BRZ] Scheduler iniciado — intervalo: {CHECK_INTERVAL_SECONDS}s")

@app.on_event("shutdown")
def stop_scheduler():
    scheduler.shutdown()

# ── Endpoints ─────────────────────────────────────────────────────────────────
@app.get("/")
async def root():
    return {"service": "BRZ Monitor", "status": "running"}

@app.get("/api/status")
async def status():
    """Retorna estado atual do monitor."""
    return {
        "last_price_usd": state["last_price_usd"],
        "last_price_brl": state["last_price_brl"],
        "last_volume_usd": state["last_volume_usd"],
        "last_check": state["last_check"],
        "alerts_sent": state["alerts_sent"],
        "config": {
            "price_threshold_pct": PRICE_CHANGE_THRESHOLD_PCT,
            "volume_threshold_pct": VOLUME_CHANGE_THRESHOLD_PCT,
            "check_interval_seconds": CHECK_INTERVAL_SECONDS,
        },
    }

@app.get("/api/check")
async def manual_check(background_tasks: BackgroundTasks):
    """Dispara checagem manual e retorna resultado."""
    result = await check_and_alert()
    return result

@app.get("/api/history")
async def history(limit: int = 20):
    """Retorna histórico de leituras."""
    return {"history": state["history"][-limit:], "total": len(state["history"])}

@app.post("/api/telegram/webhook")
async def telegram_webhook(request: Request):
    """Webhook do Telegram para processar comandos do bot."""
    body = await request.json()
    message = body.get("message", {})
    text = message.get("text", "").strip()
    chat_id = message.get("chat", {}).get("id")

    if not chat_id:
        return {"ok": True}

    if text == "/status" or text.startswith("/status"):
        price = await fetch_brz_price()
        if price:
            ts = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
            reply = (
                f"📊 <b>BRZ — Status Atual</b>\n\n"
                f"💵 <b>USD:</b> <code>${price['price_usd']:.6f}</code>\n"
                f"🇧🇷 <b>BRL:</b> <code>R${price['price_brl']:.6f}</code>\n"
                f"📦 <b>Vol 24h:</b> <code>${price['volume_usd']:,.0f}</code>\n"
                f"📈 <b>Var 24h:</b> {price['change_24h']:+.2f}%\n\n"
                f"🤖 Alertas enviados: {state['alerts_sent']}\n"
                f"🕐 {ts}"
            )
        else:
            reply = "❌ Não foi possível obter o preço agora. Tente novamente."

        await send_telegram(reply)

    elif text == "/help":
        reply = (
            "🤖 <b>BRZ Monitor — Comandos</b>\n\n"
            "/status — Preço e volume atuais do BRZ\n"
            "/help — Esta mensagem\n\n"
            f"⚙️ Alertas ativos:\n"
            f"• Variação de preço ≥ {PRICE_CHANGE_THRESHOLD_PCT}%\n"
            f"• Variação de volume ≥ {VOLUME_CHANGE_THRESHOLD_PCT}%\n"
            f"• Checagem a cada {CHECK_INTERVAL_SECONDS}s"
        )
        await send_telegram(reply)

    return {"ok": True}
