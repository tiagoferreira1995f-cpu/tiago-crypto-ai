import os
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes


TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TOKEN")
PORT = int(os.getenv("PORT", "10000"))

DEX_API = "https://api.dexscreener.com"
CACHE_SECONDS = 30

STARTING_BALANCE = 100.0
TRAILING_STOP_PERCENT = 0.20
MAX_POSITION_PERCENT = 0.20


cash = STARTING_BALANCE
positions = {}
trade_history = []

bot_running = False

highest_portfolio_value = STARTING_BALANCE
trailing_stop_value = STARTING_BALANCE * (1 - TRAILING_STOP_PERCENT)

price_cache = {}


KNOWN_TOKENS = {
    "SOL": {
        "chain": "solana",
        "address": "So11111111111111111111111111111111111111112",
    },
    "BONK": {
        "chain": "solana",
        "address": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
    },
}


class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Tiago Crypto AI is running.")

    def log_message(self, format, *args):
        return


def start_web_server():

    server = HTTPServer(
        ("0.0.0.0", PORT),
        HealthHandler
    )

    print(f"Web server running on port {PORT}")

    server.serve_forever()


def get_token_data(address, expected_chain=None):

    now = time.time()

    if address in price_cache:

        cached_time, cached_data = price_cache[address]

        if now - cached_time < CACHE_SECONDS:
            return cached_data

    url = f"{DEX_API}/latest/dex/tokens/{address}"

    headers = {
        "User-Agent": "Tiago-Crypto-AI/2.0"
    }

    try:

        response = requests.get(
            url,
            headers=headers,
            timeout=15
        )

        if response.status_code == 429:

            print("DEX Screener HTTP 429")

            return {
                "error": "429",
                "message": "DEX Screener está a limitar os pedidos."
            }

        response.raise_for_status()

        data = response.json()

        pairs = data.get("pairs", [])

        if expected_chain:

            pairs = [
                pair
                for pair in pairs
                if pair.get("chainId") == expected_chain
            ]

        if not pairs:

            return {
                "error": "not_found",
                "message": "Não encontrei pares para este token."
            }

        pairs.sort(
            key=lambda pair: float(
                pair.get("liquidity", {}).get("usd") or 0
            ),
            reverse=True
        )

        result = pairs[0]

        price_cache[address] = (
            now,
            result
        )

        return result

    except requests.exceptions.RequestException as error:

        print(f"DEX API error: {error}")

        return {
            "error": "request",
            "message": "Erro ao contactar a DEX Screener."
        }

    except Exception as error:

        print(f"Unexpected DEX error: {error}")

        return {
            "error": "unknown",
            "message": "Erro desconhecido."
        }


def get_price(symbol):

    symbol = symbol.upper()

    if symbol in KNOWN_TOKENS:

        token = KNOWN_TOKENS[symbol]

        data = get_token_data(
            token["address"],
            token["chain"]
        )

    else:

        data = get_token_data(symbol)

    if data.get("error"):

        return None, data.get("message")

    try:

        price = float(data.get("priceUsd"))

        return price, None

    except Exception:

        return None, "Preço inválido."


def get_portfolio_value():

    global cash

    total = cash

    for symbol, position in positions.items():

        price, error = get_price(symbol)

        if price is not None:

            total += (
                position["amount"] * price
            )

    return total


def update_trailing_stop():

    global highest_portfolio_value
    global trailing_stop_value

    portfolio_value = get_portfolio_value()

    if portfolio_value > highest_portfolio_value:

        highest_portfolio_value = portfolio_value

        trailing_stop_value = (
            highest_portfolio_value
            * (1 - TRAILING_STOP_PERCENT)
        )

        print(
            f"New portfolio high: "
            f"€{highest_portfolio_value:.2f}"
        )

        print(
            f"New trailing stop: "
            f"€{trailing_stop_value:.2f}"
        )

    return portfolio_value


def close_all_positions():

    global cash

    for symbol in list(positions.keys()):

        position = positions[symbol]

        price, error = get_price(symbol)

        if price is None:
            continue

        value = position["amount"] * price

        entry_value = position["entry_value"]

        profit = value - entry_value

        cash += value

        trade_history.append(
            {
                "type": "SELL",
                "symbol": symbol,
                "value": value,
                "profit": profit,
                "time": time.strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "reason": "TRAILING STOP",
            }
        )

        del positions[symbol]

    return cash


def check_trailing_stop():

    global bot_running

    portfolio_value = update_trailing_stop()

    if portfolio_value <= trailing_stop_value:

        print("TRAILING STOP ACTIVATED")

        close_all_positions()

        bot_running = False

        return True

    return False


def simulated_buy(symbol, amount_eur):

    global cash

    symbol = symbol.upper()

    if amount_eur <= 0:

        return "Valor de compra inválido."

    if amount_eur > cash:

        return (
            f"Saldo insuficiente. "
            f"Saldo: €{cash:.2f}"
        )

    max_position = (
        STARTING_BALANCE
        * MAX_POSITION_PERCENT
    )

    if amount_eur > max_position:

        return (
            f"Máximo por posição: "
            f"€{max_position:.2f}"
        )

    price, error = get_price(symbol)

    if price is None:
        return error

    tokens = amount_eur / price

    cash -= amount_eur

    if symbol in positions:

        positions[symbol]["amount"] += tokens

        positions[symbol]["entry_value"] += amount_eur

    else:

        positions[symbol] = {
            "amount": tokens,
            "entry_price": price,
            "entry_value": amount_eur,
        }

    trade_history.append(
        {
            "type": "BUY",
            "symbol": symbol,
            "value": amount_eur,
            "price": price,
            "time": time.strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
        }
    )

    return (
        f"🟢 COMPRA SIMULADA\n\n"
        f"Token: {symbol}\n"
        f"Valor: €{amount_eur:.2f}\n"
        f"Preço: ${price:.8f}\n"
        f"Tokens: {tokens:.6f}"
    )


def simulated_sell(symbol):

    global cash

    symbol = symbol.upper()

    if symbol not in positions:

        return (
            f"Não tens uma posição "
            f"em {symbol}."
        )

    position = positions[symbol]

    price, error = get_price(symbol)

    if price is None:
        return error

    value = position["amount"] * price

    profit = (
        value
        - position["entry_value"]
    )

    cash += value

    trade_history.append(
        {
            "type": "SELL",
            "symbol": symbol,
            "value": value,
            "profit": profit,
            "time": time.strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "reason": "MANUAL",
        }
    )

    del positions[symbol]

    emoji = "🟢" if profit >= 0 else "🔴"

    return (
        f"🔴 VENDA SIMULADA\n\n"
        f"Token: {symbol}\n"
        f"Valor: €{value:.2f}\n"
        f"{emoji} P/L: €{profit:.2f}"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    message = (
        "🤖 Tiago Crypto AI 2.0\n\n"
        "Modo: SIMULAÇÃO\n"
        "Capital inicial: €100\n\n"
        "Comandos:\n"
        "/price SOL\n"
        "/price BONK\n"
        "/buy SOL 20\n"
        "/sell SOL\n"
        "/portfolio\n"
        "/status\n"
        "/startbot\n"
        "/stopbot\n"
        "/history\n"
        "/help\n\n"
        "⚠️ Nenhum dinheiro real é utilizado."
    )

    await update.message.reply_text(message)


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    message = (
        "📚 COMANDOS\n\n"
        "/price SOL\n"
        "Consultar preço.\n\n"
        "/buy SOL 20\n"
        "Comprar €20 virtualmente.\n\n"
        "/sell SOL\n"
        "Vender posição virtual.\n\n"
        "/portfolio\n"
        "Ver carteira.\n\n"
        "/status\n"
        "Ver estado do bot e trailing stop.\n\n"
        "/startbot\n"
        "Ativar monitorização.\n\n"
        "/stopbot\n"
        "Parar monitorização.\n\n"
        "/history\n"
        "Ver operações.\n\n"
        "⚠️ Tudo é simulado."
    )

    await update.message.reply_text(message)


async def price_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not context.args:

        await update.message.reply_text(
            "Exemplo: /price SOL"
        )

        return

    symbol = context.args[0].upper()

    price, error = get_price(symbol)

    if price is None:

        await update.message.reply_text(
            f"❌ {error}"
        )

        return

    await update.message.reply_text(
        f"💰 {symbol}\n\n"
        f"Preço: ${price:.8f}"
    )


async def buy_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if len(context.args) < 2:

        await update.message.reply_text(
            "Exemplo: /buy SOL 20"
        )

        return

    symbol = context.args[0].upper()

    try:

        amount = float(
            context.args[1]
        )

    except ValueError:

        await update.message.reply_text(
            "Valor inválido."
        )

        return

    result = simulated_buy(
        symbol,
        amount
    )

    await update.message.reply_text(
        result
    )


async def sell_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not context.args:

        await update.message.reply_text(
            "Exemplo: /sell SOL"
        )

        return

    symbol = context.args[0].upper()

    result = simulated_sell(
        symbol
    )

    await update.message.reply_text(
        result
    )


async def portfolio_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    portfolio_value = get_portfolio_value()

    profit = (
        portfolio_value
        - STARTING_BALANCE
    )

    message = (
        f"💼 PORTFÓLIO\n\n"
        f"Cash: €{cash:.2f}\n"
        f"Valor total: €{portfolio_value:.2f}\n"
        f"P/L: €{profit:.2f}\n\n"
    )

    if positions:

        message += "📊 POSIÇÕES\n\n"

        for symbol, position in positions.items():

            price, error = get_price(symbol)

            if price is None:
                continue

            current_value = (
                position["amount"]
                * price
            )

            position_profit = (
                current_value
                - position["entry_value"]
            )

            message += (
                f"{symbol}\n"
                f"Valor: €{current_value:.2f}\n"
                f"P/L: €{position_profit:.2f}\n\n"
            )

    else:

        message += "Sem posições abertas."

    await update.message.reply_text(
        message
    )


async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    portfolio_value = update_trailing_stop()

    profit = (
        portfolio_value
        - STARTING_BALANCE
    )

    state = (
        "🟢 ATIVO"
        if bot_running
        else "🔴 PARADO"
    )

    message = (
        "🤖 TIAGO CRYPTO AI\n\n"
        f"Estado: {state}\n"
        f"Carteira: €{portfolio_value:.2f}\n"
        f"P/L: €{profit:.2f}\n\n"
        f"Máximo histórico: "
        f"€{highest_portfolio_value:.2f}\n"
        f"Trailing stop: "
        f"€{trailing_stop_value:.2f}\n"
        f"Proteção: "
        f"{TRAILING_STOP_PERCENT * 100:.0f}%\n"
    )

    await update.message.reply_text(
        message
    )


async def startbot_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    global bot_running

    bot_running = True

    await update.message.reply_text(
        "🟢 Tiago Crypto AI ativado.\n\n"
        "Monitorização em modo simulação."
    )


async def stopbot_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    global bot_running

    bot_running = False

    await update.message.reply_text(
        "🔴 Tiago Crypto AI parado."
    )


async def history_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not trade_history:

        await update.message.reply_text(
            "Ainda não existem operações."
        )

        return

    recent = trade_history[-10:]

    message = (
        "📜 ÚLTIMAS OPERAÇÕES\n\n"
    )

    for trade in recent:

        message += (
            f"{trade['type']} "
            f"{trade['symbol']}\n"
            f"Valor: "
            f"€{trade['value']:.2f}\n"
            f"{trade['time']}\n"
        )

        if "profit" in trade:

            message += (
                f"P/L: "
                f"€{trade['profit']:.2f}\n"
            )

        message += "\n"

    await update.message.reply_text(
        message
    )


def trading_monitor():

    global bot_running

    while True:

        try:

            if bot_running:

                stopped = check_trailing_stop()

                if stopped:

                    print(
                        "Trading stopped "
                        "by trailing stop."
                    )

            time.sleep(30)

        except Exception as error:

            print(
                f"Monitor error: {error}"
            )

            time.sleep(30)


def main():

    if not TOKEN:

        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN "
            "não está configurado."
        )

    web_thread = threading.Thread(
        target=start_web_server,
        daemon=True
    )

    web_thread.start()

    monitor_thread = threading.Thread(
        target=trading_monitor,
        daemon=True
    )

    monitor_thread.start()

    application = (
        Application.builder()
        .token(TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    application.add_handler(
        CommandHandler(
            "help",
            help_command
        )
    )

    application.add_handler(
        CommandHandler(
            "price",
            price_command
        )
    )

    application.add_handler(
        CommandHandler(
            "buy",
            buy_command
        )
    )

    application.add_handler(
        CommandHandler(
            "sell",
            sell_command
        )
    )

    application.add_handler(
        CommandHandler(
            "portfolio",
            portfolio_command
        )
    )

    application.add_handler(
        CommandHandler(
            "status",
            status_command
        )
    )

    application.add_handler(
        CommandHandler(
            "startbot",
            startbot_command
        )
    )

    application.add_handler(
        CommandHandler(
            "stopbot",
            stopbot_command
        )
    )

    application.add_handler(
        CommandHandler(
            "history",
            history_command
        )
    )

    print(
        "Tiago Crypto AI started."
    )

    application.run_polling()


if __name__ == "__main__":
    main()
