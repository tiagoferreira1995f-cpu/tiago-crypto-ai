import os
import requests

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)
KNOWN_TOKENS = {
    "SOL": {
        "chain": "solana",
        "address": "So11111111111111111111111111111111111111112"
    },
    "BONK": {
        "chain": "solana",
        "address": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
    }
}

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Tiago Crypto AI 1.0\n\n"
        "Sistema online.\n\n"
        "Comandos:\n"
        "/price SOL - preço atual\n"
        "/analyze SOL - análise\n"
        "/scan - procurar oportunidades\n"
        "/help - ajuda"
    )


async async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usa assim:\n/price SOL\n/price BONK"
        )
        return

    symbol = context.args[0].upper()

    try:
        # Se for um token conhecido, usar o endereço oficial
        if symbol in KNOWN_TOKENS:
            token_info = KNOWN_TOKENS[symbol]
            address = token_info["address"]
            expected_chain = token_info["chain"]

            response = requests.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{address}",
                timeout=10
            )

            response.raise_for_status()
            data = response.json()

            pairs = [
                pair for pair in data.get("pairs", [])
                if pair.get("chainId") == expected_chain
            ]

        else:
            # Para tokens desconhecidos, procurar pelo símbolo
            response = requests.get(
                "https://api.dexscreener.com/latest/dex/search",
                params={"q": symbol},
                timeout=10
            )

            response.raise_for_status()
            data = response.json()

            pairs = [
                pair for pair in data.get("pairs", [])
                if pair.get("baseToken", {}).get("symbol", "").upper() == symbol
            ]

        if not pairs:
            await update.message.reply_text(
                f"❌ Não encontrei dados confiáveis para {symbol}."
            )
            return

        # Escolher o par com maior liquidez
        pairs.sort(
            key=lambda pair: float(
                pair.get("liquidity", {}).get("usd") or 0
            ),
            reverse=True
        )

        pair = pairs[0]

        base_token = pair.get("baseToken", {})

        name = base_token.get("name", symbol)
        address = base_token.get("address", "N/A")

        price_usd = pair.get("priceUsd", "N/A")
        volume = pair.get("volume", {}).get("h24", "N/A")
        liquidity = pair.get("liquidity", {}).get("usd", "N/A")
        change = pair.get("priceChange", {}).get("h24", "N/A")
        chain = pair.get("chainId", "N/A")
        dex = pair.get("dexId", "N/A")

        message = (
            f"💰 {name} ({symbol})\n\n"
            f"💵 Preço: ${price_usd}\n"
            f"📈 Variação 24h: {change}%\n"
            f"📊 Volume 24h: ${volume}\n"
            f"💧 Liquidez: ${liquidity}\n"
            f"⛓️ Chain: {chain}\n"
            f"🏦 DEX: {dex}\n\n"
            f"🔑 Contract:\n{address}"
        )

        await update.message.reply_text(message)

    except Exception as e:
        print(f"Erro: {e}")
        await update.message.reply_text(
            "⚠️ Não consegui obter os dados neste momento."
        )


async def analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🧠 O módulo de análise será ligado ao scanner de mercado."
    )


async def scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔎 O scanner ainda está a ser configurado."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 Comandos:\n\n"
        "/start - iniciar\n"
        "/price SOL - preço\n"
        "/analyze SOL - análise\n"
        "/scan - scanner\n"
        "/help - ajuda"
    )


app = Application.builder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("price", price))
app.add_handler(CommandHandler("analyze", analyze))
app.add_handler(CommandHandler("scan", scan))
app.add_handler(CommandHandler("help", help_command))

app.run_polling()
