# Pacifica Telegram ALO Bot

Multi-user Telegram bot for placing Pacifica buy/sell limit orders as ALO
(add-liquidity-only) orders using Pacifica agent keys.

## Setup

1. Create a Telegram bot with BotFather and copy the token.
2. Install dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

3. Copy `.env.example` to `.env` and set `TELEGRAM_BOT_TOKEN`.
   Mainnet websocket notifications use `wss://ws.pacifica.fi/ws`.
4. Run:

```powershell
python bot.py
```

User credentials are stored in `data/users.json`, keyed by Telegram user id.
Agent keys cannot withdraw funds on Pacifica, but this file still allows trading
as the configured account. Keep it private and backed up.
TWAP notification dedupe state is stored in `data/notifications.json`.

## Commands

- `/start` - register or update your Solana parent wallet and Pacifica agent keys.
- `/profile` - show the currently saved public keys.
- `/trade` - interactive ALO order flow with buttons and USD sizing.
- `/buy BTC 100` - place an ALO buy using $100 notional at current best bid.
- `/sell ETH 50` - place an ALO sell using $50 notional at current best ask.
- `/buy BTC 65000 100` - place an ALO buy using a custom limit price and $100 notional.
- `/account` - show balance, equity, available margin, fees, and counts.
- `/positions` - show current open positions.
- `/markets` - list all Pacifica markets.
- `/price BTC` - show mark, mid, oracle, funding, and volume.
- `/orders` - list open orders for the saved account.
- `/cancel <order_id>` - cancel an order.
- `/commands` - show every available command.
- `/help` - show every available command.

All order commands use the saved parent Solana public key as `account` and the
saved Pacifica agent private key for request signing.
