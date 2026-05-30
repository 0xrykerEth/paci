from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import RLock

from pacifica_client import PacificaCredentials


@dataclass
class UserProfile:
    telegram_user_id: int
    solana_public_key: str
    agent_public_key: str
    agent_private_key: str

    @property
    def credentials(self) -> PacificaCredentials:
        return PacificaCredentials(
            account=self.solana_public_key,
            agent_public_key=self.agent_public_key,
            agent_private_key=self.agent_private_key,
        )


class UserStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._lock = RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("{}", encoding="utf-8")

    def all(self) -> dict[int, UserProfile]:
        with self._lock:
            data = self._read()
            return {int(user_id): self._profile_from_dict(user_id, profile) for user_id, profile in data.items()}

    def get(self, telegram_user_id: int) -> UserProfile | None:
        with self._lock:
            data = self._read()
            profile = data.get(str(telegram_user_id))
            if profile is None:
                return None
            return self._profile_from_dict(str(telegram_user_id), profile)

    def save(self, profile: UserProfile) -> None:
        with self._lock:
            data = self._read()
            data[str(profile.telegram_user_id)] = asdict(profile)
            self._write(data)

    def _read(self) -> dict[str, dict[str, str]]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, data: dict[str, dict[str, str]]) -> None:
        tmp_path = self.path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(self.path)

    @staticmethod
    def _profile_from_dict(user_id: str, data: dict[str, str]) -> UserProfile:
        return UserProfile(
            telegram_user_id=int(user_id),
            solana_public_key=data["solana_public_key"],
            agent_public_key=data["agent_public_key"],
            agent_private_key=data["agent_private_key"],
        )


class NotificationStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._lock = RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("{}", encoding="utf-8")

    def seen(self, telegram_user_id: int, notification_key: str) -> bool:
        with self._lock:
            data = self._read()
            return notification_key in data.get(str(telegram_user_id), [])

    def mark_seen(self, telegram_user_id: int, notification_key: str) -> None:
        with self._lock:
            data = self._read()
            user_id = str(telegram_user_id)
            seen_keys = data.setdefault(user_id, [])
            if notification_key not in seen_keys:
                seen_keys.append(notification_key)
                data[user_id] = seen_keys[-1000:]
                self._write(data)

    def _read(self) -> dict[str, list[str]]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, data: dict[str, list[str]]) -> None:
        tmp_path = self.path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(self.path)
