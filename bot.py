# =====================================================================
# bot.py — Bot de Telegram para el marketplace de camisetas
# =====================================================================
#
# Qué hace este bot:
# 1. Responde /start con el botón para abrir la Mini App (catálogo)
# 2. Cuando el cliente completa el pago en la Mini App, recibe los
#    datos del pedido (web_app_data) y te los reenvía a TI inmediatamente
# 3. Cada minuto, revisa tu wallet en la blockchain TON para confirmar
#    que el pago realmente llegó (no confía solo en lo que dice la app)
# 4. Cuando confirma un pago en blockchain, te manda un mensaje final
#    con el comprobante on-chain
#
# Requisitos antes de correrlo:
#   pip install python-telegram-bot==21.* requests
#
# Variables que tienes que completar abajo en CONFIG.
# =====================================================================

import json
import logging
import asyncio
import os
from datetime import datetime

import requests
from telegram import Update, WebAppInfo, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger(__name__)

# =====================================================================
# CONFIGURACIÓN — valores leídos desde variables de entorno
# =====================================================================
CONFIG = {
        # El token que te dio BotFather al crear el bot
    "BOT_TOKEN": os.environ.get("BOT_TOKEN", ""),

        # Tu ID de Telegram personal (el admin que recibe los avisos de pedidos)
        "ADMIN_CHAT_ID": os.environ.get("ADMIN_ID", ""),

        # URL pública donde alojaste index.html (la Mini App)
        "MINI_APP_URL": "https://tienda-luli.vercel.app/index.html",

        # Tu wallet de Tonkeeper donde recibes los pagos
        "RECEIVER_WALLET": "UQABcTNu6gmWe6j9V7Nn-vswy8UB7z2eUlsXQhOTns5xb6Vl",

        # Cada cuántos segundos revisar la blockchain por pagos nuevos
        "CHECK_INTERVAL_SECONDS": 60,
}

# Pedidos pendientes de confirmación on-chain.
pending_orders = {}

# Pedidos ya confirmados, para no avisar dos veces por la misma transacción
confirmed_tx_hashes = set()

# =====================================================================
# /start — muestra el botón para abrir la tienda
# =====================================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        keyboard = [[
                    InlineKeyboardButton(
                                    text="🛒 Abrir tienda",
                                    web_app=WebAppInfo(url=CONFIG["MINI_APP_URL"])
                    )
        ]]
        await update.message.reply_text(
            "¡Bienvenido! Toca el botón para ver el catálogo.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

# =====================================================================
# Recibe los datos cuando el cliente termina el checkout en la Mini App
# =====================================================================
async def on_web_app_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
                    data = json.loads(update.message.web_app_data.data)
except (json.JSONDecodeError, AttributeError):
        log.error("No se pudo leer el web_app_data recibido")
        return

    order_id = data.get("orderId")
    customer_user = update.effective_user

    pending_orders[order_id] = {
                **data,
                "telegram_user_id": customer_user.id,
                "telegram_username": customer_user.username or "sin username",
                "created_at": datetime.utcnow().isoformat(),
                "confirmed": False,
    }

    msg = (
                f"🟡 *Nuevo pedido recibido* (pendiente de confirmar en blockchain)\n\n"
                f"*ID pedido:* `{order_id}`\n"
                f"*Producto:* {data.get('productName')}\n"
                f"*Precio:* ${data.get('priceUSD')} USD (~{data.get('tonAmount'):.4f} TON)\n\n"
                f"*Cliente:* {data.get('name')}\n"
                f"*Telegram:* @{customer_user.username or 'sin username'} (id {customer_user.id})\n"
                f"*Teléfono:* {data.get('phone') or '—'}\n"
                f"*Dirección de envío:*\n{data.get('address')}\n\n"
                f"⏳ Esperando confirmación en blockchain (puede tardar 1-2 min)..."
    )
    await context.bot.send_message(chat_id=CONFIG["ADMIN_CHAT_ID"], text=msg, parse_mode="Markdown")

    await update.message.reply_text(
                f"✅ Pedido `{order_id}` recibido. Estamos confirmando tu pago, "
                f"te contactaremos por aquí en breve.",
                parse_mode="Markdown"
    )

# =====================================================================
# Verificación en blockchain
# =====================================================================
def get_incoming_transactions(wallet_address: str, limit: int = 20):
        url = f"https://tonapi.io/v2/blockchain/accounts/{wallet_address}/transactions"
        params = {"limit": limit}
        try:
                    resp = requests.get(url, params=params, timeout=10)
                    resp.raise_for_status()
                    return resp.json().get("transactions", [])
except requests.RequestException as e:
        log.error(f"Error consultando TonAPI: {e}")
        return []

def extract_comment_and_amount(tx: dict):
        in_msg = tx.get("in_msg", {})
        if not in_msg:
                    return None, None

        amount = int(in_msg.get("value", 0))

    comment = None
    decoded = in_msg.get("decoded_body", {})
    if decoded:
                comment = decoded.get("text")
            if not comment:
                        msg_data = in_msg.get("msg_data", {})
                        comment = msg_data.get("text")

    return comment, amount

async def check_blockchain_for_payments(context: ContextTypes.DEFAULT_TYPE):
        if not pending_orders:
                    return

        transactions = get_incoming_transactions(CONFIG["RECEIVER_WALLET"])

    for tx in transactions:
                tx_hash = tx.get("hash")
                if not tx_hash or tx_hash in confirmed_tx_hashes:
                                continue

                comment, amount_nano = extract_comment_and_amount(tx)
                if not comment or comment not in pending_orders:
                                continue

                order = pending_orders[comment]
                if order["confirmed"]:
                                continue

                expected_nano = round(order["tonAmount"] * 1e9)
                if amount_nano < expected_nano * 0.98:
                                log.warning(
                                                    f"Pedido {comment}: monto recibido ({amount_nano}) es menor "
                                                    f"al esperado ({expected_nano}). Revisar manualmente."
                                )
                                continue

                order["confirmed"] = True
                confirmed_tx_hashes.add(tx_hash)

        explorer_link = f"https://tonviewer.com/transaction/{tx_hash}"
        msg = (
                        f"✅ *Pago confirmado en blockchain*\n\n"
                        f"*ID pedido:* `{comment}`\n"
                        f"*Producto:* {order.get('productName')}\n"
                        f"*Cliente:* {order.get('name')} (@{order.get('telegram_username')})\n"
                        f"*Dirección de envío:*\n{order.get('address')}\n\n"
                        f"*Comprobante:* {explorer_link}\n\n"
                        f"👉 Ya puedes proceder con el envío manual."
        )
        await context.bot.send_message(chat_id=CONFIG["ADMIN_CHAT_ID"], text=msg, parse_mode="Markdown")

        try:
                        await context.bot.send_message(
                                            chat_id=order["telegram_user_id"],
                                            text=f"✅ Tu pago para el pedido `{comment}` fue confirmado. "
                                                 f"Procesaremos tu envío pronto.",
                                            parse_mode="Markdown"
                        )
except Exception as e:
                log.warning(f"No se pudo notificar al cliente {order['telegram_user_id']}: {e}")

# =====================================================================
# MAIN
# =====================================================================
def main():
        app = Application.builder().token(CONFIG["BOT_TOKEN"]).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, on_web_app_data))

    app.job_queue.run_repeating(
                check_blockchain_for_payments,
                interval=CONFIG["CHECK_INTERVAL_SECONDS"],
                first=10
    )

    log.info("Bot iniciado, escuchando...")
    app.run_polling()

if __name__ == "__main__":
        main()
