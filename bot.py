import os
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)


# ============================================================
# CONFIGURAÇÃO
# ============================================================

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TOKEN")
PORT = int(os.getenv("PORT", "10000"))

DEX_API = "https://api.dexscreener.com"

CACHE_SECONDS = 30

# Simulação
STARTING_BALANCE = 100.0
TRAILING_STOP_PERCENT = 0.20
MAX_POSITION_PERCENT = 0.20

# Estado da simulação
cash = STARTING_BALANCE
positions = {}
trade_history = []

bot_running = False
highest_portfolio_value = STARTING_BALANCE
trailing_stop_value = STARTING_BALANCE * (
    1 - TRAILING_STOP_PERCENT
)

price_cache = {}


# ============================================================
# TOKENS
# ============================================================

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


# ============================================================
# SERVIDOR WEB PARA O RENDER
# ============================================================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(
            b"Tiago Crypto AI is running."
        )

    def log_message(self, format, *args):
        return


def start_web_server():
    server = HTTPServer(
        ("0.0.0.0", PORT),
        HealthHandler,
    )

    print(
        f"Web server running on port {PORT}"
    )

    server.serve_forever()


# ============================================================
# DEX SCREENER
# ============================================================

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
            timeout=15,
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
            reverse=True,
        )

        result = pairs[0]

        price_cache[address] = (
            now,
            result,
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


# ============================================================
# PREÇO DO TOKEN
# ============================================================

def get_price(symbol):

    symbol = symbol.upper()

    if symbol in KNOWN_TOKENS:

        token = KNOWN_TOKENS[symbol]

        data = get_token_data(
            token["address"],
            token["chain"],
        )

    else:

        data = get_token_data(symbol)

    if data.get("error"):
        return None, data.get("message")

    try:

        price = float(
            data.get("priceUsd")
        )

        return price, None

    except Exception:

        return None, "Preço inválido."


# ============================================================
# VALOR DA CARTEIRA
# ============================================================

def get_portfolio_value():

    global cash

    total = cash

    for symbol, position in positions.items():

        price, error = get_price(symbol)

        if price is not None:

            total += (
                position["amount"] * price
            )

    ret
