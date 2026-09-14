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

price_cache = {}


# ============================================================
# TOKENS CONHECIDOS
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
# SERVIDOR HTTP PARA O RENDER
# ============================================================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Tiago Crypto AI is running.")

    def log_message(self, format, *args):
        return


def start_web_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    print(f"Web server running on port {PORT}")
    server.serve_forever()


# ============================================================
# DEX SCREENER
# ============================================================

def get_token_data(address, expected_chain=None):

    now = time.time()

    # Usar cache para evitar pedidos repetidos
    if address in price_cache:

        cached_time, cached_data = price_cache[address]

        if now - cached_time < CACHE_SECONDS:
            print(f"Using cached data for {address}")
            return cached_data

    url = f"{DEX_API}/latest/dex/tokens/{address}"

    headers = {
        "User-Agent": "Tiago-Crypto-AI/1.0"
    }

    try:

        response = requests.get(
            url,
            headers=headers,
            timeout=15,
        )

        # Limite da API
        if response.status_code == 429:

            print("DEX Screener returned HTTP 429")

            return {
                "error": "429",
                "message": (
                    "A DEX Screener está a limitar temporariamente "
                    "os pedidos. Tenta novamente dentro de alguns segundos."
                ),
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
                "message": "Não encontrei pares para este token.",
            }

        # Escolher o par com maior liquidez
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
            "message": (
                "Não consegui contactar a DEX Screener neste momento."
            ),
        }

    except Exception as error:

        print(f"Unexpected DEX error: {error}")

        return {
            "error": "unknown",
            "message": (
                "Ocorreu um erro ao obter os dados do token."
            ),
        }


# ============================================================
# FORMATAR NÚMEROS
# ============================================================

def format_number(value):

    if value is None:
        return "N/A"

    try:

        number = float(value)

        if number >= 1_000_000_000:
            return f"{number / 1_000_000_000:.2f}B"

        if number >= 1_000_000:
            return f"{number / 1_000_000:.2f}M"

        if number >= 1_000:
            return f"{number / 1_000:.2f}K"

        return f"{number:.2f}"

    except Exception:
        return str(value)


# ============================================================
# /START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    message = (
        "🤖 Tiago Crypto AI\n\n"
        "Bot de análise de mercado crypto.\n\n"
        "Comandos disponíveis:\n"
        "/price SOL\n"
        "/price BONK\n"
        "/price <contract>\n"
        "/analyze SOL\n"
 
