from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any

import aiohttp
import base58
from solders.keypair import Keypair


@dataclass(frozen=True)
class PacificaCredentials:
    account: str
    agent_public_key: str
    agent_private_key: str


class PacificaApiError(RuntimeError):
    pass


class PacificaClient:
    def __init__(self, rest_base: str) -> None:
        self.rest_base = rest_base.rstrip("/")

    async def create_alo_limit_order(
        self,
        credentials: PacificaCredentials,
        symbol: str,
        side: str,
        price: str,
        size: str,
    ) -> dict[str, Any]:
        data = {
            "symbol": symbol,
            "side": side,
            "price": price,
            "amount": size,
            "reduce_only": False,
            "tif": "ALO",
            "client_order_id": str(uuid.uuid4()),
        }
        return await self._signed_request("POST", "/api/v1/orders/create", "create_order", credentials, data)

    async def open_orders(self, credentials: PacificaCredentials) -> dict[str, Any]:
        return await self._get("/api/v1/orders", {"account": credentials.account})

    async def account_info(self, credentials: PacificaCredentials) -> dict[str, Any]:
        return await self._get("/api/v1/account", {"account": credentials.account})

    async def positions(self, credentials: PacificaCredentials) -> dict[str, Any]:
        return await self._get("/api/v1/positions", {"account": credentials.account})

    async def markets(self) -> dict[str, Any]:
        return await self._get("/api/v1/info", {})

    async def prices(self) -> dict[str, Any]:
        return await self._get("/api/v1/info/prices", {})

    async def price(self, symbol: str) -> dict[str, Any] | None:
        prices = await self.prices()
        for item in prices.get("data", []):
            if item.get("symbol") == symbol:
                return item
        return None

    async def orderbook(self, symbol: str) -> dict[str, Any]:
        return await self._get("/api/v1/book", {"symbol": symbol})

    async def cancel_order(self, credentials: PacificaCredentials, order_id: str) -> dict[str, Any]:
        data = {"order_id": int(order_id)}
        return await self._signed_request("POST", "/api/v1/orders/cancel", "cancel_order", credentials, data)

    async def _signed_request(
        self,
        method: str,
        path: str,
        operation_type: str,
        credentials: PacificaCredentials,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        timestamp = int(time.time() * 1000)
        expiry_window = 5_000
        signature = sign_payload(
            credentials.agent_private_key,
            operation_type,
            timestamp,
            expiry_window,
            data,
        )
        body = {
            "account": credentials.account,
            "agent_wallet": credentials.agent_public_key,
            "signature": signature,
            "timestamp": timestamp,
            "expiry_window": expiry_window,
            **data,
        }

        async with aiohttp.ClientSession() as session:
            async with session.request(
                method,
                f"{self.rest_base}{path}",
                json=body,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as response:
                data = await decode_response(response)

                if response.status >= 400:
                    raise PacificaApiError(f"{response.status}: {data}")

                if isinstance(data, dict):
                    return data
                return {"data": data}

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self.rest_base}{path}",
                params=params,
                headers={"Accept": "*/*"},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as response:
                data = await decode_response(response)
                if response.status >= 400:
                    raise PacificaApiError(f"{response.status}: {data}")
                if isinstance(data, dict):
                    return data
                return {"data": data}


def sign_payload(
    agent_private_key: str,
    operation_type: str,
    timestamp: int,
    expiry_window: int,
    data: dict[str, Any],
) -> str:
    keypair = keypair_from_secret(agent_private_key)
    message_data = {
        "timestamp": timestamp,
        "expiry_window": expiry_window,
        "type": operation_type,
        "data": data,
    }
    message = json.dumps(sort_json_keys(message_data), separators=(",", ":")).encode("utf-8")
    signature = keypair.sign_message(message)
    return base58.b58encode(bytes(signature)).decode("ascii")


def keypair_from_secret(secret: str) -> Keypair:
    cleaned = secret.strip()
    if cleaned.startswith("["):
        values = json.loads(cleaned)
        return Keypair.from_bytes(bytes(values))

    try:
        return Keypair.from_base58_string(cleaned)
    except ValueError:
        raw = base58.b58decode(cleaned)
        return Keypair.from_bytes(raw)


def sort_json_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: sort_json_keys(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [sort_json_keys(item) for item in value]
    return value


async def decode_response(response: aiohttp.ClientResponse) -> Any:
    text = await response.text()
    try:
        return json.loads(text) if text else {}
    except json.JSONDecodeError:
        return {"raw": text}


def amount_from_usd(notional_usd: str, price: str, lot_size: str) -> str:
    notional = Decimal(notional_usd)
    limit_price = Decimal(price)
    lot = Decimal(lot_size)
    raw_amount = notional / limit_price
    lots = (raw_amount / lot).to_integral_value(rounding=ROUND_DOWN)
    amount = lots * lot
    if amount <= 0:
        raise ValueError("USD size is too small for this market's lot size at that price")
    return format(amount.normalize(), "f")
