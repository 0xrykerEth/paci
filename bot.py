from __future__ import annotations

import json
import logging
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from pacifica_client import PacificaApiError, PacificaClient, amount_from_usd
from storage import NotificationStore, UserProfile, UserStore
from trade_monitor import TradeMonitor

SOLANA_KEY, AGENT_PUBLIC_KEY, AGENT_PRIVATE_KEY = range(3)
TRADE_SIDE, TRADE_SYMBOL, TRADE_PRICE, TRADE_USD, TRADE_CONFIRM = range(3, 8)


def load_config() -> tuple[str, str, str, str]:
    load_dotenv(dotenv_path=Path(__file__).with_name(".env"), override=True)
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN in .env")
    rest_base = os.getenv("PACIFICA_REST_BASE", "https://api.pacifica.fi").strip()
    ws_url = os.getenv("PACIFICA_WS_URL", "wss://ws.pacifica.fi/ws").strip()
    if ws_url == "wss://api.pacifica.fi/ws":
        ws_url = "wss://ws.pacifica.fi/ws"
    users_file = os.getenv("PACIFICA_USERS_FILE", "data/users.json").strip()
    return token, rest_base, ws_url, users_file


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Send your Solana parent public key.")
    return SOLANA_KEY


async def receive_solana_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["solana_public_key"] = update.message.text.strip()
    await update.message.reply_text("Send your Pacifica agent public key.")
    return AGENT_PUBLIC_KEY


async def receive_agent_public_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["agent_public_key"] = update.message.text.strip()
    await update.message.reply_text("Send your Pacifica agent private key.")
    return AGENT_PRIVATE_KEY


async def receive_agent_private_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    store: UserStore = context.application.bot_data["store"]
    monitor: TradeMonitor = context.application.bot_data["monitor"]
    user_id = update.effective_user.id
    profile = UserProfile(
        telegram_user_id=user_id,
        solana_public_key=context.user_data["solana_public_key"],
        agent_public_key=context.user_data["agent_public_key"],
        agent_private_key=update.message.text.strip(),
    )
    store.save(profile)
    monitor.ensure_running(user_id, profile)
    context.user_data.clear()
    await update.message.reply_text(
        "Saved. Use /trade for button-driven ALO orders, or /buy SYMBOL PRICE USD and /sell SYMBOL PRICE USD."
    )
    return ConversationHandler.END


async def cancel_setup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Setup canceled.")
    return ConversationHandler.END


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    saved = await require_profile(update, context)
    if saved is None:
        return
    await update.message.reply_text(
        "\n".join(
            [
                f"Solana parent: {saved.solana_public_key}",
                f"Agent public: {saved.agent_public_key}",
                "Agent private: saved",
            ]
        )
    )


async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await place_order(update, context, "BUY")


async def sell(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await place_order(update, context, "SELL")


async def place_order(update: Update, context: ContextTypes.DEFAULT_TYPE, side: str) -> None:
    saved = await require_profile(update, context)
    if saved is None:
        return

    client: PacificaClient = context.application.bot_data["pacifica"]
    parsed = await parse_order_args(context.args, side, client)
    if parsed is None:
        await update.message.reply_text(
            f"Usage: /{side.lower()} BTC 100  or  /{side.lower()} BTC 65000 100\n"
            "Two-argument mode uses best bid for buys and best ask for sells."
        )
        return

    symbol, pacifica_side, price, notional_usd = parsed
    try:
        market = await get_market(client, symbol)
        if market is None:
            await update.message.reply_text(f"Unknown market: {symbol}. Use /markets to see tradable symbols.")
            return
        size = amount_from_usd(notional_usd, price, market["lot_size"])
        result = await client.create_alo_limit_order(saved.credentials, symbol, pacifica_side, price, size)
    except PacificaApiError as exc:
        await update.message.reply_text(f"Order rejected: {exc}")
        return
    except Exception as exc:
        logging.exception("Order failed")
        await update.message.reply_text(f"Order failed: {exc}")
        return

    await update.message.reply_text(
        f"ALO {side.lower()} order sent\n"
        f"Symbol: {symbol}\n"
        f"Price: {price}\n"
        f"USD: {notional_usd}\n"
        f"Amount: {size}\n\n"
        f"{format_response(result)}"
    )


async def trade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    saved = await require_profile(update, context)
    if saved is None:
        return ConversationHandler.END
    context.user_data["trade"] = {}
    keyboard = [[InlineKeyboardButton("Buy", callback_data="trade_side:BUY")], [InlineKeyboardButton("Sell", callback_data="trade_side:SELL")]]
    await update.message.reply_text("Choose side.", reply_markup=InlineKeyboardMarkup(keyboard))
    return TRADE_SIDE


async def trade_side(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    side = query.data.split(":", 1)[1]
    context.user_data["trade"]["side"] = side
    client: PacificaClient = context.application.bot_data["pacifica"]
    try:
        markets = (await client.markets()).get("data", [])
    except Exception as exc:
        await query.edit_message_text(f"Could not fetch markets: {exc}")
        return ConversationHandler.END

    symbols = [item["symbol"] for item in markets[:48]]
    context.user_data["markets"] = {item["symbol"]: item for item in markets}
    buttons = []
    for index in range(0, min(len(symbols), 24), 4):
        buttons.append([InlineKeyboardButton(symbol, callback_data=f"trade_symbol:{symbol}") for symbol in symbols[index:index + 4]])
    await query.edit_message_text(
        "Choose market, or type any Pacifica symbol.",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return TRADE_SYMBOL


async def trade_symbol_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    symbol = query.data.split(":", 1)[1]
    return await set_trade_symbol(update, context, symbol, edit=True)


async def trade_symbol_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await set_trade_symbol(update, context, update.message.text.strip(), edit=False)


async def set_trade_symbol(update: Update, context: ContextTypes.DEFAULT_TYPE, symbol: str, edit: bool) -> int:
    client: PacificaClient = context.application.bot_data["pacifica"]
    market = await get_market(client, symbol)
    if market is None:
        text = f"Unknown market: {symbol}. Send a symbol exactly as Pacifica lists it, like BTC, ETH, SOL, kPEPE."
        if edit:
            await update.callback_query.edit_message_text(text)
        else:
            await update.message.reply_text(text)
        return TRADE_SYMBOL

    symbol = market["symbol"]
    context.user_data["trade"]["symbol"] = symbol
    context.user_data["trade"]["market"] = market
    price_info = await client.price(symbol)
    mid = price_info.get("mid") if price_info else None
    mark = price_info.get("mark") if price_info else None
    text = f"{symbol} selected.\nMid: {mid or 'n/a'}\nMark: {mark or 'n/a'}\nSend your ALO limit price."
    if edit:
        await update.callback_query.edit_message_text(text)
    else:
        await update.message.reply_text(text)
    return TRADE_PRICE


async def trade_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    price = update.message.text.strip()
    if not positive_decimal(price):
        await update.message.reply_text("Send a positive limit price.")
        return TRADE_PRICE
    context.user_data["trade"]["price"] = price
    await update.message.reply_text("Send order size in USD, for example 50 or 250.75.")
    return TRADE_USD


async def trade_usd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    notional_usd = update.message.text.strip()
    if not positive_decimal(notional_usd):
        await update.message.reply_text("Send a positive USD notional.")
        return TRADE_USD

    trade_data = context.user_data["trade"]
    market = trade_data["market"]
    try:
        amount = amount_from_usd(notional_usd, trade_data["price"], market["lot_size"])
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return TRADE_USD

    notional = Decimal(notional_usd)
    min_order = Decimal(market["min_order_size"])
    max_order = Decimal(market["max_order_size"])
    if notional < min_order or notional > max_order:
        await update.message.reply_text(
            f"USD size must be between {market['min_order_size']} and {market['max_order_size']} for {market['symbol']}."
        )
        return TRADE_USD

    trade_data["notional_usd"] = notional_usd
    trade_data["amount"] = amount
    side = trade_data["side"]
    symbol = trade_data["symbol"]
    price = trade_data["price"]
    text = (
        f"Confirm ALO {side.lower()}\n"
        f"Symbol: {symbol}\n"
        f"Price: {price}\n"
        f"USD: {notional_usd}\n"
        f"Amount: {amount}"
    )
    keyboard = [[InlineKeyboardButton("Place order", callback_data="trade_confirm:yes")], [InlineKeyboardButton("Cancel", callback_data="trade_confirm:no")]]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return TRADE_CONFIRM


async def trade_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data.endswith(":no"):
        context.user_data.pop("trade", None)
        await query.edit_message_text("Order canceled.")
        return ConversationHandler.END

    saved = await require_profile(update, context)
    if saved is None:
        return ConversationHandler.END

    trade_data = context.user_data["trade"]
    pacifica_side = "bid" if trade_data["side"] == "BUY" else "ask"
    client: PacificaClient = context.application.bot_data["pacifica"]
    try:
        result = await client.create_alo_limit_order(
            saved.credentials,
            trade_data["symbol"],
            pacifica_side,
            trade_data["price"],
            trade_data["amount"],
        )
    except Exception as exc:
        await query.edit_message_text(f"Order failed: {exc}")
        return ConversationHandler.END

    await query.edit_message_text(f"ALO order sent.\n{format_response(result)}")
    context.user_data.pop("trade", None)
    return ConversationHandler.END


async def orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    saved = await require_profile(update, context)
    if saved is None:
        return

    client: PacificaClient = context.application.bot_data["pacifica"]
    try:
        result = await client.open_orders(saved.credentials)
    except Exception as exc:
        await update.message.reply_text(f"Could not fetch open orders: {exc}")
        return
    await update.message.reply_text(format_response(result))


async def account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    saved = await require_profile(update, context)
    if saved is None:
        return
    client: PacificaClient = context.application.bot_data["pacifica"]
    try:
        result = await client.account_info(saved.credentials)
    except Exception as exc:
        await update.message.reply_text(f"Could not fetch account info: {exc}")
        return
    await update.message.reply_text(format_account(result))


async def positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    saved = await require_profile(update, context)
    if saved is None:
        return
    client: PacificaClient = context.application.bot_data["pacifica"]
    try:
        result = await client.positions(saved.credentials)
    except Exception as exc:
        await update.message.reply_text(f"Could not fetch positions: {exc}")
        return
    await update.message.reply_text(format_positions(result))


async def markets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    client: PacificaClient = context.application.bot_data["pacifica"]
    try:
        result = await client.markets()
    except Exception as exc:
        await update.message.reply_text(f"Could not fetch markets: {exc}")
        return
    data = result.get("data", [])
    symbols = [item.get("symbol", "") for item in data if item.get("symbol")]
    await update.message.reply_text("Markets:\n" + chunked_symbols(symbols))


async def price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /price BTC")
        return
    symbol = context.args[0].strip()
    client: PacificaClient = context.application.bot_data["pacifica"]
    item = await client.price(symbol)
    if item is None:
        await update.message.reply_text(f"Unknown market: {symbol}")
        return
    await update.message.reply_text(format_price(item))


async def cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    saved = await require_profile(update, context)
    if saved is None:
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /cancel <order_id>")
        return

    client: PacificaClient = context.application.bot_data["pacifica"]
    try:
        result = await client.cancel_order(saved.credentials, context.args[0])
    except Exception as exc:
        await update.message.reply_text(f"Cancel failed: {exc}")
        return
    await update.message.reply_text(format_response(result))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "\n".join(
            [
                "Setup",
                "/start - save or update keys",
                "/profile - show saved public keys",
                "",
                "Trading",
                "/trade - interactive ALO order",
                "/buy SYMBOL USD - ALO buy at best bid",
                "/sell SYMBOL USD - ALO sell at best ask",
                "/buy SYMBOL PRICE USD - ALO buy at custom limit",
                "/sell SYMBOL PRICE USD - ALO sell at custom limit",
                "/orders - open orders",
                "/cancel ORDER_ID - cancel an order",
                "",
                "Account",
                "/account - balance, equity, margin info",
                "/positions - current positions",
                "",
                "Market data",
                "/markets - list all Pacifica markets",
                "/price SYMBOL - mark/mid/oracle price",
                "",
                "Help",
                "/commands - show this list",
                "/help - show this list",
            ]
        )
    )


async def require_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> UserProfile | None:
    store: UserStore = context.application.bot_data["store"]
    saved = store.get(update.effective_user.id)
    if saved is None:
        text = "Run /start first to save your Solana parent wallet and agent keys."
        if update.message is not None:
            await update.message.reply_text(text)
        elif update.callback_query is not None:
            await update.callback_query.edit_message_text(text)
        return None
    return saved


async def parse_order_args(args: list[str], side: str, client: PacificaClient) -> tuple[str, str, str, str] | None:
    if len(args) not in {2, 3}:
        return None
    symbol = args[0]
    if len(args) == 2:
        size = args[1]
        market = await get_market(client, symbol)
        if market is None:
            return None
        symbol = market["symbol"]
        price = await default_alo_price(client, symbol, side)
        if price is None:
            return None
    else:
        price = args[1]
        size = args[2]
    try:
        if Decimal(price) <= 0 or Decimal(size) <= 0:
            return None
    except InvalidOperation:
        return None
    pacifica_side = "bid" if side == "BUY" else "ask"
    return symbol, pacifica_side, price, size


async def default_alo_price(client: PacificaClient, symbol: str, side: str) -> str | None:
    book = await client.orderbook(symbol)
    levels = book.get("data", {}).get("l", [])
    if len(levels) < 2:
        return None
    bid_levels = levels[0]
    ask_levels = levels[1]
    if side == "BUY" and bid_levels:
        return bid_levels[0]["p"]
    if side == "SELL" and ask_levels:
        return ask_levels[0]["p"]
    return None


def positive_decimal(value: str) -> bool:
    try:
        return Decimal(value) > 0
    except InvalidOperation:
        return False


async def get_market(client: PacificaClient, symbol: str) -> dict[str, Any] | None:
    normalized = symbol.strip()
    if normalized.upper().endswith("-PERP"):
        normalized = normalized[:-5]
    markets = await client.markets()
    for item in markets.get("data", []):
        if item.get("symbol") == normalized:
            return item
    upper = normalized.upper()
    for item in markets.get("data", []):
        if item.get("symbol") == upper:
            return item
    lower = normalized.lower()
    for item in markets.get("data", []):
        if str(item.get("symbol", "")).lower() == lower:
            return item
    return None


def format_response(payload: Any) -> str:
    text = json.dumps(payload, indent=2, sort_keys=True)
    return text[:3900]


def format_account(payload: dict[str, Any]) -> str:
    data = payload.get("data", payload)
    keys = [
        ("balance", "Balance"),
        ("account_equity", "Equity"),
        ("available_to_spend", "Available to spend"),
        ("available_to_withdraw", "Available to withdraw"),
        ("total_margin_used", "Margin used"),
        ("cross_mmr", "Cross MMR"),
        ("positions_count", "Positions"),
        ("orders_count", "Open orders"),
        ("fee_level", "Fee level"),
        ("maker_fee", "Maker fee"),
        ("taker_fee", "Taker fee"),
    ]
    return "\n".join(f"{label}: {data.get(key, 'n/a')}" for key, label in keys)


def format_positions(payload: dict[str, Any]) -> str:
    positions_data = payload.get("data", [])
    if not positions_data:
        return "No open positions."
    lines = ["Positions:"]
    for item in positions_data[:30]:
        side = "long" if item.get("side") == "bid" else "short" if item.get("side") == "ask" else item.get("side", "n/a")
        lines.append(
            f"{item.get('symbol')}: {side} {item.get('amount')} @ {item.get('entry_price')} | funding {item.get('funding', 'n/a')}"
        )
    return "\n".join(lines)[:3900]


def format_price(item: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"{item.get('symbol')}",
            f"Mark: {item.get('mark', 'n/a')}",
            f"Mid: {item.get('mid', 'n/a')}",
            f"Oracle: {item.get('oracle', 'n/a')}",
            f"Funding: {item.get('funding', 'n/a')}",
            f"Next funding: {item.get('next_funding', 'n/a')}",
            f"24h volume: {item.get('volume_24h', 'n/a')}",
        ]
    )


def chunked_symbols(symbols: list[str]) -> str:
    lines = []
    for index in range(0, len(symbols), 8):
        lines.append(", ".join(symbols[index:index + 8]))
    return "\n".join(lines)[:3900]


async def post_init(application: Application) -> None:
    monitor: TradeMonitor = application.bot_data["monitor"]
    await monitor.start_all()


async def post_shutdown(application: Application) -> None:
    monitor: TradeMonitor = application.bot_data["monitor"]
    await monitor.stop()


def main() -> None:
    global app
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token, rest_base, ws_url, users_file = load_config()
    logging.info("Using Pacifica REST base: %s", rest_base)
    logging.info("Using Pacifica websocket URL: %s", ws_url)

    store = UserStore(users_file)
    notifications = NotificationStore(str(Path(users_file).with_name("notifications.json")))
    pacifica = PacificaClient(rest_base)

    async def notify(user_id: int, message: str) -> None:
        await app.bot.send_message(chat_id=user_id, text=message[:3900])

    monitor = TradeMonitor(ws_url, store, notifications, notify)
    app = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.bot_data["store"] = store
    app.bot_data["pacifica"] = pacifica
    app.bot_data["monitor"] = monitor

    setup_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            SOLANA_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_solana_key)],
            AGENT_PUBLIC_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_agent_public_key)],
            AGENT_PRIVATE_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_agent_private_key)],
        },
        fallbacks=[CommandHandler("cancel", cancel_setup)],
    )

    trade_handler = ConversationHandler(
        entry_points=[CommandHandler("trade", trade)],
        states={
            TRADE_SIDE: [CallbackQueryHandler(trade_side, pattern=r"^trade_side:")],
            TRADE_SYMBOL: [
                CallbackQueryHandler(trade_symbol_button, pattern=r"^trade_symbol:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, trade_symbol_text),
            ],
            TRADE_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, trade_price)],
            TRADE_USD: [MessageHandler(filters.TEXT & ~filters.COMMAND, trade_usd)],
            TRADE_CONFIRM: [CallbackQueryHandler(trade_confirm, pattern=r"^trade_confirm:")],
        },
        fallbacks=[CommandHandler("cancel", cancel_setup)],
    )

    app.add_handler(setup_handler)
    app.add_handler(trade_handler)
    app.add_handler(CommandHandler("profile", profile))
    app.add_handler(CommandHandler("buy", buy))
    app.add_handler(CommandHandler("sell", sell))
    app.add_handler(CommandHandler("account", account))
    app.add_handler(CommandHandler("positions", positions))
    app.add_handler(CommandHandler("markets", markets))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("orders", orders))
    app.add_handler(CommandHandler("cancel", cancel_order))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("commands", help_command))
    app.run_polling()


if __name__ == "__main__":
    main()
