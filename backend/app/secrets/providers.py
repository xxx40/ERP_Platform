import base64
from hashlib import sha256
import json
from pathlib import Path
from threading import RLock
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
import httpx


class LocalEncryptedSecretProvider:
    provider_id = "local-fernet"

    def __init__(self, path: Path, master_key: str) -> None:
        if len(master_key) < 16:
            raise ValueError("local secret master key must contain at least 16 characters")
        key = base64.urlsafe_b64encode(sha256(master_key.encode("utf-8")).digest())
        self.cipher = Fernet(key)
        self.path = path
        self.lock = RLock()

    def put(self, name: str, value: str) -> dict:
        if not name.strip() or not value:
            raise ValueError("secret name and value are required")
        with self.lock:
            payload = self._read()
            secret_id = uuid4().hex
            payload[secret_id] = {
                "name": name.strip(),
                "ciphertext": self.cipher.encrypt(value.encode("utf-8")).decode("ascii"),
            }
            self._write(payload)
        return self.describe(secret_id)

    def get(self, secret_id: str) -> str:
        with self.lock:
            item = self._read().get(secret_id)
        if item is None:
            raise KeyError(secret_id)
        try:
            return self.cipher.decrypt(item["ciphertext"].encode("ascii")).decode("utf-8")
        except (InvalidToken, KeyError) as exc:
            raise ValueError("secret cannot be decrypted with the active master key") from exc

    def describe(self, secret_id: str) -> dict:
        with self.lock:
            item = self._read().get(secret_id)
        if item is None:
            raise KeyError(secret_id)
        return {
            "secret_id": secret_id,
            "name": item["name"],
            "masked": "********",
            "provider": self.provider_id,
        }

    def list(self) -> list[dict]:
        with self.lock:
            ids = sorted(self._read())
        return [self.describe(secret_id) for secret_id in ids]

    def delete(self, secret_id: str) -> None:
        with self.lock:
            payload = self._read()
            if secret_id not in payload:
                raise KeyError(secret_id)
            del payload[secret_id]
            self._write(payload)

    def health(self) -> bool:
        return True

    def _read(self) -> dict:
        if not self.path.is_file():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, payload: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(self.path)


class VaultSecretProvider:
    provider_id = "vault-http"

    def __init__(self, base_url: str, token: str, timeout: float = 5) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(self, method: str, path: str, payload: dict | None = None):
        response = httpx.request(
            method,
            f"{self.base_url}{path}",
            headers={"X-Vault-Token": self.token},
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json() if response.content else {}

    def put(self, name: str, value: str) -> dict:
        result = self._request("POST", "/v1/erp-assistant/secrets", {"name": name, "value": value})
        return {**result, "masked": "********", "provider": self.provider_id}

    def get(self, secret_id: str) -> str:
        return str(self._request("GET", f"/v1/erp-assistant/secrets/{secret_id}")["value"])

    def list(self) -> list[dict]:
        return [
            {**item, "masked": "********", "provider": self.provider_id}
            for item in self._request("GET", "/v1/erp-assistant/secrets").get("items", [])
        ]

    def delete(self, secret_id: str) -> None:
        self._request("DELETE", f"/v1/erp-assistant/secrets/{secret_id}")

    def health(self) -> bool:
        try:
            self._request("GET", "/v1/sys/health")
            return True
        except Exception:
            return False
