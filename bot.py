# =====================================================================
# bot.py — Bot de Telegram para el marketplace de camisetas
# =====================================================================
#
import json
import logging
import asyncio
import os
from datetime import datetime

import requests
from telegram import Update, WebAppInfo, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

CONFIG = {
    'BOT_TOKEN': os.environ.get('BOT_TOKEN', ''),
    'ADMIN_CHAT_ID': os.environ.get('ADMIN_ID', ''),
    'MINI_APP_URL': 'https://tienda-luli.vercel.app/index.html',
    'RECEIVER_WALLET': 'UQABcTNu6gmWe6j9V7Nn-vswy8UB7z2eUlsXQhOTns5xb6Vl',
    'CHECK_INTERVAL_SECONDS': 60,
}

pending_orders = {}
confirmed_tx_hashes = set()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[
        InlineKeyboardButton(
            text='🛒 Abrir tienda',
            web_app=WebAppInfo(url=CONFIG['MINI_APP_URL'])
        )
    ]]
    await update.message.reply_text(
        '¡Bienvenido! Toca el botón para ver el catálogo.',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def on_web_app_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        raw = json.loads(update.message.web_app_data.data)
    except (json.JSONDecodeError, AttributeError):
        log.error('No se pudo leer el web_app_data recibido')
        return

    order_id = raw.get('orderId')
    customer_user = update.effective_user

    pending_orders[order_id] = {
        **raw,
        'telegram_user_id': customer_user.id,
        'telegram_username': customer_user.username or 'sin username',
        'created_at': datetime.utcnow().isoformat(),
        'confirmed': False,
    }

    msg = (
        f"🟡 *Nuevo pedido recibido*\n\n"
        f"*ID pedido:* `{order_id}`\n"
        f"*Producto:* {raw.get('productName')}\n"
        f"*Cliente:* {raw.get('name')}\n"
        f"*Telegram:* @{customer_user.username or 'sin username'} (id {customer_user.id})\n"
        f"*Teléfono:* {raw.get('phone') or '—'}\n"
        f"*Dirección:*\n{raw.get('address')}\n\n"
        f"⏳ Esperando confirmación en blockchain..."
    )
    await context.bot.send_message(chat_id=CONFIG['ADMIN_CHAT_ID'], text=msg, parse_mode='Markdown')
    await update.message.reply_text(
        f"✅ Pedido `{order_id}` recibido. Te contactaremos pronto.",
        parse_mode='Markdown'
    )

def get_incoming_transactions(wallet_address: str, limit: int = 20):
    url = f"https://tonapi.io/v2/blockchain/accounts/{wallet_address}/transactions"
    params = {'limit': limit}
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json().get('transactions', [])
    except requests.RequestException as e:
        log.error(f'Error consultando TonAPI: {e}')
        return []

def extract_comment_and_amount(tx: dict):
    in_msg = tx.get('in_msg', {})
    if not in_msg:
        return None, None
    amount = int(in_msg.get('value', 0))
    comment = None
    decoded = in_msg.get('decoded_body', {})
    if decoded:
        comment = decoded.get('text')
    if not comment:
        comment = in_msg.get('msg_data', {}).get('text')
    return comment, amount

async def check_blockchain_for_payments(context: ContextTypes.DEFAULT_TYPE):
    if not pending_orders:
        return
    transactions = get_incoming_transactions(CONFIG['RECEIVER_WALLET'])
    for tx in transactions:
        tx_hash = tx.get('hash')
        if not tx_hash or tx_hash in confirmed_tx_hashes:
            continue
        comment, amount_nano = extract_comment_and_amount(tx)
        if not comment or comment not in pending_orders:
            continue
        order = pending_orders[comment]
        if order['confirmed']:
            continue
        expected_nano = round(order['tonAmount'] * 1e9)
        if amount_nano < expected_nano * 0.98:
            log.warning(f'Pago insuficiente para pedido {comment}')
            continue
        order['confirmed'] = True
        confirmed_tx_hashes.add(tx_hash)
        explorer_link = f"https://tonviewer.com/transaction/{tx_hash}"
        msg = (
            f"✅ *Pago confirmado en blockchain*\n\n"
            f"*ID pedido:* `{comment}`\n"
            f"*Producto:* {order.get('productName')}\n"
            f"*Cliente:* {order.get('name')} (@{order.get('telegram_username')})\n"
            f"*Dirección:*\n{order.get('address')}\n\n"
            f"*Comprobante:* {explorer_link}\n\n"
            f"👉 Ya puedes proceder con el envío manual."
        )
        await context.bot.send_message(chat_id=CONFIG['ADMIN_CHAT_ID'], text=msg, parse_mode='Markdown')
        try:
            await context.bot.send_message(
                chat_id=order['telegram_user_id'],
                text=f"✅ Tu pago para el pedido `{comment}` fue confirmado. Procesaremos tu envío pronto.",
                parse_mode='Markdown'
            )
        except Exception as e:
            log.warning(f"No se pudo notificar al cliente {order['telegram_user_id']}: {e}")

def main():
    app = Application.builder().token(CONFIG['BOT_TOKEN']).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, on_web_app_data))
    app.job_queue.run_repeating(
        check_blockchain_for_payments,
        interval=CONFIG['CHECK_INTERVAL_SECONDS'],
        first=10
    )
    log.info('Bot iniciado, escuchando...')
    app.run_polling()

if __name__ == '__main__':
    main()
