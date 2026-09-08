import os
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)


TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Tiago Crypto AI 1.0\n\n"
        "Sistema online.\n\n"
        "Comandos disponíveis:\n"
        "/price - preço de uma moeda\n"
        "/analyze - analisar uma moeda\n"
        "/scan - procurar oportunidades\n"
        "/help - ajuda"
    )


async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💰 Módulo de preços ainda não ligado.\n"
        "Vamos ligá-lo aos dados de mercado no próximo passo."
    )


async def analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🧠 Módulo de análise iniciado.\n"
        "Em breve vou analisar preço, volume, liquidez, "
        "holders e risco."
    )


async def scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔎 Scanner iniciado.\n"
        "Ainda não existem fontes de mercado ligadas."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 Comandos:\n\n"
        "/start - iniciar o sistema\n"
        "/price - preços\n"
        "/analyze - análise\n"
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
