from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

import websockets

from storage import NotificationStore, UserProfile, UserStore

NotifyFn = Callable[[int, str], Awaitable[None]]


class TradeMonitor:
    def __init__(self, ws_url: str, store: UserStore, notifications: NotificationStore, notify: NotifyFn) -> None:
        self.ws_url = ws_url
        self.store = store
        self.notifications = notifications
        self.notify = notify
        self._tasks: dict[int, asyncio.Task[None]] = {}

    async def start_all(self) -> None:
        for user_id, profile in self.store.all().items():
            self.ensure_running(user_id, profile)

    def ensure_running(self, user_id: int, profile: UserProfile) -> None:
        task = self._tasks.get(user_id)
        if task is not None and not task.done():
            return
        self._tasks[user_id] = asyncio.create_task(self._run_user(profile))

    async def stop(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        for task in self._tasks.values():
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run_user(self, profile: UserProfile) -> None:
        while True:
            try:
                async with websockets.connect(self.ws_url, ping_interval=20, ping_timeout=20) as websocket:
                    await self._subscribe(websocket, profile.solana_public_key)
                    async for raw_message in websocket:
                        await self._handle_message(profile.telegram_user_id, raw_message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.notify(
                    profile.telegram_user_id,
                    f"Pacifica trade monitor reconnecting via {self.ws_url}: {exc}",
                )
                await asyncio.sleep(5)

    async def _subscribe(self, websocket: Any, account: str) -> None:
        subscriptions = [
            {"method": "subscribe", "params": {"source": "account_trades", "account": account}},
            {"method": "subscribe", "params": {"source": "account_order_updates", "account": account}},
        ]
        for message in subscriptions:
            await websocket.send(json.dumps(message))

    async def _handle_message(self, telegram_user_id: int, raw_message: str | bytes) -> None:
        text = raw_message.decode("utf-8") if isinstance(raw_message, bytes) else raw_message
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            return

        channel = str(message.get("channel") or message.get("type") or "")
        payload = message.get("data", message)

        if "trade" in channel:
            await self._notify_once_if_twap(telegram_user_id, payload, f"Trade filled:\n{format_payload(payload)}")
            return

        status = str(_deep_get(payload, "os") or _deep_get(payload, "status") or "").lower()
        filled_size = _deep_get(payload, "f") or _deep_get(payload, "filled_amount") or _deep_get(payload, "filledSize")
        if status in {"filled", "partially_filled", "partial_fill"} or _positive_decimal(filled_size):
            await self._notify_once_if_twap(telegram_user_id, payload, f"Order update:\n{format_payload(payload)}")

    async def _notify_once_if_twap(self, telegram_user_id: int, payload: Any, message: str) -> None:
        key = twap_notification_key(payload)
        if key is None:
            await self.notify(telegram_user_id, message)
            return
        if self.notifications.seen(telegram_user_id, key):
            return
        self.notifications.mark_seen(telegram_user_id, key)
        await self.notify(telegram_user_id, message)


def format_payload(payload: Any) -> str:
    if isinstance(payload, list):
        return "\n\n".join(format_payload(item) for item in payload)[:3500]
    if isinstance(payload, dict):
        preferred_keys = [
            "s",
            "d",
            "p",
            "a",
            "f",
            "oe",
            "os",
            "i",
            "I",
            "symbol",
            "side",
            "price",
            "amount",
            "size",
            "filled_amount",
            "filledSize",
            "status",
            "order_id",
            "orderId",
            "trade_id",
            "tradeId",
        ]
        lines = [f"{key}: {payload[key]}" for key in preferred_keys if key in payload]
        if lines:
            return "\n".join(lines)
    return json.dumps(payload, indent=2, sort_keys=True)[:3500]


def _positive_decimal(value: Any) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def twap_notification_key(payload: Any) -> str | None:
    candidates = [
        "twap_id",
        "twapId",
        "parent_order_id",
        "parentOrderId",
        "strategy_id",
        "strategyId",
        "algo_order_id",
        "algoOrderId",
        "client_order_id",
        "clientOrderId",
    ]
    for key in candidates:
        value = _deep_get(payload, key)
        if value is None:
            continue
        text = str(value)
        if key.lower().startswith("client") and "twap" not in text.lower():
            continue
        return f"twap:{key}:{text}"

    kind = str(_deep_get(payload, "type") or _deep_get(payload, "order_type") or _deep_get(payload, "orderType") or "")
    if "twap" not in kind.lower():
        return None

    fallback = _deep_get(payload, "i") or _deep_get(payload, "I") or _deep_get(payload, "order_id") or _deep_get(payload, "orderId")
    if fallback is None:
        fallback = json.dumps(payload, sort_keys=True)[:500]
    return f"twap:{kind}:{fallback}"


def _deep_get(payload: Any, key: str) -> Any:
    if isinstance(payload, dict):
        if key in payload:
            return payload[key]
        for value in payload.values():
            found = _deep_get(value, key)
            if found is not None:
                return found
    if isinstance(payload, list):
        for value in payload:
            found = _deep_get(value, key)
            if found is not None:
                return found
    return None
