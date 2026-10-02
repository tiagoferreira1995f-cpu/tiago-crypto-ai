import os
import time
import threading
import logging
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes


# This bot is deliberately a paper-trading bot.  It has no exchange API keys,
# no wallet integration and no code capable of sending a real order.
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TOKEN")
PORT = int(os.getenv("PORT", "10000"))

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DEX_API = "https://api.dexscreener.com"
CACHE_SECONDS = 90
RATE_LIMIT_BACKOFF_SECONDS = 300
MONITOR_SECONDS = 120

STARTING_BALANCE = 100.0
TRAILING_STOP_PERCENT = 0.20
MAX_POSITION_PERCENT = 0.20
MAX_TOTAL_EXPOSURE_PERCENT = 0.40
AUTO_POSITION_EUR = 20.0
MIN_CASH_RESERVE = 20.0
COOLDOWN_SECONDS = 15 * 60
HARD_STOP_PERCENT = 0.08
TAKE_PROFIT_PERCENT = 0.15
MAX_HOLD_SECONDS = 6 * 60 * 60


cash = STARTING_BALANCE
positions = {}
trade_history = []
bot_running = False

highest_portfolio_value = STARTING_BALANCE
trailing_stop_value = STARTING_BALANCE * (1 - TRAILING_STOP_PERCENT)

# address -> (saved_at, DEX pair data).  Cached data is also kept after it
# becomes stale so a temporary DEX error never makes an open paper position
# disappear from the portfolio.
price_cache = {}
rate_limited_until = 0.0
price_samples = {symbol: deque(maxlen=12) for symbol in ("SOL", "BONK")}
last_trade_at = {symbol: 0.0 for symbol in ("SOL", "BONK")}
state_lock = threading.RLock()


KNOWN_TOKENS = {
    "SOL": {
        "chain": "solana",
        "address": "So11111111111111111111111111111111111111112",
        "min_liquidity": 100_000.0,
        "min_volume_h1": 10_000.0,
    },
    "BONK": {
        "chain": "solana",
        "address": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
        "min_liquidity": 100_000.0,
        "min_volume_h1": 5_000.0,
    },
}


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Tiago Crypto AI paper trader is running.")

    def log_message(self, format, *args):
        return


def start_web_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    print(f"Web server running on port {PORT}")
    server.serve_forever()


def cached_data(address):
    entry = price_cache.get(address)
    return entry[1] if entry else None


def get_token_data(address, expected_chain=None, force_refresh=False):
    """Return the most liquid pair, falling back to the last valid cache."""
    global rate_limited_until

    now = time.time()
    entry = price_cache.get(address)
    if entry and not force_refresh and now - entry[0] < CACHE_SECONDS:
        return entry[1]

    if now < rate_limited_until:
        cached = cached_data(address)
        if cached:
            return cached
        return {
            "error": "429",
            "message": "DEX Screener está temporariamente em pausa por limite de pedidos.",
        }

    try:
        response = requests.get(
            f"{DEX_API}/latest/dex/tokens/{address}",
            headers={"User-Agent": "Tiago-Crypto-AI-Paper/3.0"},
            timeout=15,
        )
        if response.status_code == 429:
            rate_limited_until = now + RATE_LIMIT_BACKOFF_SECONDS
            print("DEX Screener HTTP 429; using cache during backoff.")
            cached = cached_data(address)
            if cached:
                return cached
            return {
                "error": "429",
                "message": "DEX Screener está a limitar os pedidos e não existe preço em cache.",
            }

        response.raise_for_status()
        pairs = response.json().get("pairs", [])
        if expected_chain:
            pairs = [pair for pair in pairs if pair.get("chainId") == expected_chain]
        if not pairs:
            return {"error": "not_found", "message": "Não encontrei pares para este token."}

        pairs.sort(
            key=lambda pair: float(pair.get("liquidity", {}).get("usd") or 0),
            reverse=True,
        )
        result = pairs[0]
        price_cache[address] = (now, result)
        return result

    except requests.exceptions.RequestException as error:
        print(f"DEX API error: {error}")
        cached = cached_data(address)
        if cached:
            return cached
        return {"error": "request", "message": "Erro ao contactar a DEX Screener."}
    except (TypeError, ValueError, KeyError) as error:
        print(f"Unexpected DEX response: {error}")
        cached = cached_data(address)
        if cached:
            return cached
        return {"error": "unknown", "message": "Resposta inválida da DEX Screener."}


def get_market_snapshot(symbol, force_refresh=False):
    symbol = symbol.upper()
    token = KNOWN_TOKENS.get(symbol)
    if not token:
        return None, f"Token não suportado: {symbol}. Usa SOL ou BONK."

    data = get_token_data(token["address"], token["chain"], force_refresh)
    if data.get("error"):
        return None, data["message"]
    try:
        return {
            "price": float(data["priceUsd"]),
            "liquidity": float(data.get("liquidity", {}).get("usd") or 0),
            "volume_h1": float(data.get("volume", {}).get("h1") or 0),
            "change_m5": float(data.get("priceChange", {}).get("m5") or 0),
            "change_h1": float(data.get("priceChange", {}).get("h1") or 0),
        }, None
    except (TypeError, ValueError, KeyError):
        return None, "Preço inválido recebido da DEX Screener."


def get_price(symbol, force_refresh=False):
    snapshot, error = get_market_snapshot(symbol, force_refresh)
    return (snapshot["price"], None) if snapshot else (None, error)


def get_portfolio_value():
    with state_lock:
        total = cash
        open_positions = list(positions.items())
    for symbol, position in open_positions:
        price, _ = get_price(symbol)
        if price is not None:
            total += position["amount"] * price
    return total


def get_total_exposure():
    with state_lock:
        open_positions = list(positions.items())
    exposure = 0.0
    for symbol, position in open_positions:
        price, _ = get_price(symbol)
        if price is not None:
            exposure += position["amount"] * price
    return exposure


def update_trailing_stop():
    global highest_portfolio_value, trailing_stop_value
    portfolio_value = get_portfolio_value()
    with state_lock:
        if portfolio_value > highest_portfolio_value:
            highest_portfolio_value = portfolio_value
            trailing_stop_value = highest_portfolio_value * (1 - TRAILING_STOP_PERCENT)
            print(f"New portfolio high: EUR {highest_portfolio_value:.2f}")
            print(f"New trailing stop: EUR {trailing_stop_value:.2f}")
    return portfolio_value


def record_trade(trade_type, symbol, value, price, reason, profit=None):
    trade = {
        "type": trade_type,
        "symbol": symbol,
        "value": value,
        "price": price,
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "reason": reason,
    }
    if profit is not None:
        trade["profit"] = profit
    trade_history.append(trade)


def close_position(symbol, reason):
    """Close a paper position only; this function never sends a real order."""
    global cash
    symbol = symbol.upper()
    with state_lock:
        position = positions.get(symbol)
    if not position:
        return f"Não tens uma posição em {symbol}."

    price, error = get_price(symbol, force_refresh=True)
    if price is None:
        return error

    value = position["amount"] * price
    profit = value - position["entry_value"]
    with state_lock:
        # The position may have been manually sold while a monitor request was pending.
        if symbol not in positions:
            return f"Não tens uma posição em {symbol}."
        cash += value
        del positions[symbol]
        last_trade_at[symbol] = time.time()
        record_trade("SELL", symbol, value, price, reason, profit)

    emoji = "🟢" if profit >= 0 else "🔴"
    return (
        f"🔴 VENDA SIMULADA\n\nToken: {symbol}\nValor: €{value:.2f}\n"
        f"{emoji} P/L: €{profit:.2f}\nMotivo: {reason}"
    )


def close_all_positions(reason="TRAILING STOP"):
    with state_lock:
        symbols = list(positions.keys())
    for symbol in symbols:
        result = close_position(symbol, reason)
        print(result)


def check_trailing_stop():
    global bot_running
    portfolio_value = update_trailing_stop()
    with state_lock:
        should_stop = portfolio_value <= trailing_stop_value
    if should_stop:
        print("TRAILING STOP ACTIVATED")
        close_all_positions("TRAILING STOP 20%")
        with state_lock:
            bot_running = False
        return True
    return False


def simulated_buy(symbol, amount_eur, reason="MANUAL"):
    """Open or add to a paper position.  No real-money order is possible."""
    global cash
    symbol = symbol.upper()
    if symbol not in KNOWN_TOKENS:
        return f"Token não suportado: {symbol}. Usa SOL ou BONK."
    if amount_eur <= 0:
        return "Valor de compra inválido."

    max_position = STARTING_BALANCE * MAX_POSITION_PERCENT
    with state_lock:
        if amount_eur > cash:
            return f"Saldo insuficiente. Saldo: €{cash:.2f}"
        current_entry_value = positions.get(symbol, {}).get("entry_value", 0.0)
        if current_entry_value + amount_eur > max_position:
            return f"Máximo por posição: €{max_position:.2f}"
    if get_total_exposure() + amount_eur > STARTING_BALANCE * MAX_TOTAL_EXPOSURE_PERCENT:
        return f"Exposição máxima total: €{STARTING_BALANCE * MAX_TOTAL_EXPOSURE_PERCENT:.2f}"

    price, error = get_price(symbol, force_refresh=True)
    if price is None:
        return error

    tokens = amount_eur / price
    with state_lock:
        if amount_eur > cash:
            return f"Saldo insuficiente. Saldo: €{cash:.2f}"
        cash -= amount_eur
        if symbol in positions:
            positions[symbol]["amount"] += tokens
            positions[symbol]["entry_value"] += amount_eur
        else:
            positions[symbol] = {
                "amount": tokens,
                "entry_price": price,
                "entry_value": amount_eur,
                "opened_at": time.time(),
            }
        last_trade_at[symbol] = time.time()
        record_trade("BUY", symbol, amount_eur, price, reason)

    return (
        f"🟢 COMPRA SIMULADA\n\nToken: {symbol}\nValor: €{amount_eur:.2f}\n"
        f"Preço: ${price:.8f}\nTokens: {tokens:.6f}\nMotivo: {reason}"
    )


def simulated_sell(symbol):
    return close_position(symbol, "MANUAL")


def update_price_samples(symbol, price):
    samples = price_samples[symbol]
    samples.append((time.time(), price))
    if len(samples) < 2 or samples[0][1] <= 0:
        return None
    return ((price / samples[0][1]) - 1) * 100


def strategy_entry_reason(symbol, snapshot, sample_change):
    """Objective entry rule: liquid market, active volume and aligned momentum."""
    token = KNOWN_TOKENS[symbol]
    if snapshot["liquidity"] < token["min_liquidity"]:
        return None
    if snapshot["volume_h1"] < token["min_volume_h1"]:
        return None
    if snapshot["change_m5"] < 0.50 or snapshot["change_h1"] < 0.0:
        return None
    if sample_change is None or sample_change < 0.20:
        return None
    return "AUTO ENTRY: tendência + momentum + liquidez"


def strategy_exit_reason(symbol, snapshot):
    with state_lock:
        position = positions.get(symbol)
    if not position:
        return None
    change_from_entry = (snapshot["price"] / position["entry_price"] - 1) * 100
    held_seconds = time.time() - position["opened_at"]
    if change_from_entry <= -(HARD_STOP_PERCENT * 100):
        return "AUTO HARD STOP 8%"
    if change_from_entry >= TAKE_PROFIT_PERCENT * 100 and snapshot["change_m5"] <= -0.50:
        return "AUTO TAKE PROFIT + reversão"
    if change_from_entry > 0 and snapshot["change_m5"] <= -1.00 and snapshot["change_h1"] < 0:
        return "AUTO saída por reversão de momentum"
    if held_seconds >= MAX_HOLD_SECONDS:
        return "AUTO tempo máximo de posição"
    return None


def run_auto_strategy():
    """One paper-trading cycle.  It cannot trade tokens outside SOL/BONK."""
    for symbol in KNOWN_TOKENS:
        snapshot, error = get_market_snapshot(symbol, force_refresh=True)
        if snapshot is None:
            print(f"Auto strategy {symbol}: {error}")
            continue
        sample_change = update_price_samples(symbol, snapshot["price"])

        exit_reason = strategy_exit_reason(symbol, snapshot)
        if exit_reason:
            print(close_position(symbol, exit_reason))
            continue

        with state_lock:
            has_position = symbol in positions
            enough_cash = cash - AUTO_POSITION_EUR >= MIN_CASH_RESERVE
            cooldown_active = time.time() - last_trade_at[symbol] < COOLDOWN_SECONDS
        if has_position or cooldown_active or not enough_cash:
            continue

        entry_reason = strategy_entry_reason(symbol, snapshot, sample_change)
        if entry_reason:
            print(simulated_buy(symbol, AUTO_POSITION_EUR, entry_reason))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Tiago Crypto AI AUTO TRADER v1\n\n"
        "Modo: SIMULAÇÃO EXCLUSIVA\nCapital inicial: €100\n\n"
        "Comandos:\n/price SOL\n/price BONK\n/buy SOL 20\n/sell SOL\n"
        "/portfolio\n/status\n/startbot\n/stopbot\n/history\n/help\n\n"
        "⚠️ Não usa exchange, carteira, levantamentos nem dinheiro real."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📚 COMANDOS\n\n/price SOL — consultar preço\n/buy SOL 20 — compra virtual\n"
        "/sell SOL — venda virtual\n/portfolio — carteira\n/status — estado e estratégia\n"
        "/startbot — ativar AUTO TRADER\n/stopbot — parar AUTO TRADER\n/history — operações\n\n"
        "AUTO TRADER: SOL/BONK, máximo €20 por posição, máximo €40 expostos, "
        "cooldown de 15 min, hard stop 8% e trailing stop global 20%."
    )


async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Exemplo: /price SOL")
        return
    symbol = context.args[0].upper()
    price, error = get_price(symbol)
    await update.message.reply_text(f"💰 {symbol}\n\nPreço: ${price:.8f}" if price else f"❌ {error}")


async def buy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Exemplo: /buy SOL 20")
        return
    try:
        amount = float(context.args[1])
    except ValueError:
        await update.message.reply_text("Valor inválido.")
        return
    await update.message.reply_text(simulated_buy(context.args[0], amount))


async def sell_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Exemplo: /sell SOL")
        return
    await update.message.reply_text(simulated_sell(context.args[0]))


async def portfolio_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    portfolio_value = get_portfolio_value()
    with state_lock:
        open_positions = list(positions.items())
        current_cash = cash
    message = f"💼 PORTFÓLIO\n\nCash: €{current_cash:.2f}\nValor total: €{portfolio_value:.2f}\nP/L: €{portfolio_value - STARTING_BALANCE:.2f}\n\n"
    if not open_positions:
        await update.message.reply_text(message + "Sem posições abertas.")
        return
    message += "📊 POSIÇÕES\n\n"
    for symbol, position in open_positions:
        price, _ = get_price(symbol)
        if price is not None:
            value = position["amount"] * price
            message += f"{symbol}\nValor: €{value:.2f}\nP/L: €{value - position['entry_value']:.2f}\n\n"
    await update.message.reply_text(message)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    portfolio_value = update_trailing_stop()
    with state_lock:
        state = "🟢 ATIVO" if bot_running else "🔴 PARADO"
        high = highest_portfolio_value
        stop = trailing_stop_value
    await update.message.reply_text(
        "🤖 TIAGO CRYPTO AI AUTO TRADER v1\n\n"
        f"Estado: {state}\nCarteira: €{portfolio_value:.2f}\nP/L: €{portfolio_value - STARTING_BALANCE:.2f}\n\n"
        f"Máximo histórico: €{high:.2f}\nTrailing stop: €{stop:.2f}\n"
        "Estratégia: SOL/BONK, momentum + liquidez + volume\n"
        "Risco: €20/posição, €40 total, cooldown 15 min, hard stop 8%.\n"
        "⚠️ Apenas simulação."
    )


def signal_report(symbol):
    snapshot, error = get_market_snapshot(symbol)
    if snapshot is None:
        return f"{symbol}: ❌ {error}"

    samples = price_samples[symbol]
    sample_change = None
    if len(samples) >= 2 and samples[0][1] > 0:
        sample_change = ((samples[-1][1] / samples[0][1]) - 1) * 100

    token = KNOWN_TOKENS[symbol]
    with state_lock:
        has_position = symbol in positions
        cash_available = cash - AUTO_POSITION_EUR >= MIN_CASH_RESERVE
        cooldown_remaining = max(
            0,
            int(COOLDOWN_SECONDS - (time.time() - last_trade_at[symbol])),
        )

    checks = [
        ("Liquidez", snapshot["liquidity"] >= token["min_liquidity"],
         f"${snapshot['liquidity']:,.0f} / mínimo ${token['min_liquidity']:,.0f}"),
        ("Volume 1h", snapshot["volume_h1"] >= token["min_volume_h1"],
         f"${snapshot['volume_h1']:,.0f} / mínimo ${token['min_volume_h1']:,.0f}"),
        ("Momentum 5m", snapshot["change_m5"] >= 0.50,
         f"{snapshot['change_m5']:+.2f}% / mínimo +0.50%"),
        ("Tendência 1h", snapshot["change_h1"] >= 0.0,
         f"{snapshot['change_h1']:+.2f}% / mínimo +0.00%"),
        ("Duas leituras", sample_change is not None and sample_change >= 0.20,
         "ainda sem duas leituras" if sample_change is None
         else f"{sample_change:+.2f}% / mínimo +0.20%"),
    ]

    lines = [f"📡 SINAL {symbol}", f"Preço: ${snapshot['price']:.8f}", ""]
    for label, passed, detail in checks:
        lines.append(f"{'✅' if passed else '❌'} {label}: {detail}")
    lines.append(f"{'❌' if has_position else '✅'} Posição: {'já aberta' if has_position else 'sem posição'}")
    lines.append(f"{'✅' if cash_available else '❌'} Reserva de caixa: {'suficiente' if cash_available else 'insuficiente'}")
    lines.append(
        f"{'❌' if cooldown_remaining else '✅'} Cooldown: "
        f"{'restam ' + str(cooldown_remaining // 60 + 1) + ' min' if cooldown_remaining else 'livre'}"
    )
    return "\n".join(lines)


async def signals_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reports = [signal_report(symbol) for symbol in KNOWN_TOKENS]
    await update.message.reply_text("\n\n".join(reports))

async def startbot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_running
    with state_lock:
        bot_running = True
    await update.message.reply_text(
        "🟢 AUTO TRADER ativado em SIMULAÇÃO.\n\n"
        "A cada 2 minutos analisa SOL/BONK e só abre posições se as regras "
        "objetivas de liquidez, volume e momentum forem cumpridas."
    )


async def stopbot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_running
    with state_lock:
        bot_running = False
    await update.message.reply_text("🔴 AUTO TRADER parado. As posições simuladas existentes mantêm-se abertas.")


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with state_lock:
        recent = trade_history[-10:]
    if not recent:
        await update.message.reply_text("Ainda não existem operações.")
        return
    message = "📜 ÚLTIMAS OPERAÇÕES\n\n"
    for trade in recent:
        message += f"{trade['type']} {trade['symbol']}\nValor: €{trade['value']:.2f}\n{trade['time']}\nMotivo: {trade['reason']}\n"
        if "profit" in trade:
            message += f"P/L: €{trade['profit']:.2f}\n"
        message += "\n"
    await update.message.reply_text(message)


async def telegram_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Record Telegram failures in Render logs without stopping the bot."""
    logger.error(
        "Telegram update failed (update=%s)",
        update,
        exc_info=context.error,
    )


def trading_monitor():
    while True:
        try:
            with state_lock:
                running = bot_running
            if running:
                if not check_trailing_stop():
                    run_auto_strategy()
        except Exception as error:
            # A market-data failure must never terminate Telegram polling.
            print(f"Trading monitor error: {error}")
        time.sleep(MONITOR_SECONDS)


def main():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN não está configurado.")

    threading.Thread(target=start_web_server, daemon=True).start()
    threading.Thread(target=trading_monitor, daemon=True).start()

    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("price", price_command))
    application.add_handler(CommandHandler("buy", buy_command))
    application.add_handler(CommandHandler("sell", sell_command))
    application.add_handler(CommandHandler("portfolio", portfolio_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("signals", signals_command))
    application.add_handler(CommandHandler("startbot", startbot_command))
    application.add_handler(CommandHandler("stopbot", stopbot_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_error_handler(telegram_error_handler)

    print("Tiago Crypto AI AUTO TRADER v1 started in paper-trading mode.")
    application.run_polling()


if __name__ == "__main__":
    main()
