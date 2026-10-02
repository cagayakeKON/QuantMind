"""J-Quants V2 client with pagination and a conservative Standard rate budget."""

from __future__ import annotations

import os
import time

import requests


class JQuantsClient:
    def __init__(self, api_key: str | None = None, *, session=None, interval=0.6):
        key = api_key or os.getenv("JQUANTS_API_KEY", "")
        if not key.strip():
            raise ValueError("Configure JQUANTS_API_KEY in the backend environment")
        self._key = key.strip()
        self._session = session or requests.Session()
        self._interval = max(0.6, float(interval))
        self._next = 0.0

    def rows(self, endpoint: str, params: dict | None = None) -> list[dict]:
        if endpoint not in {
            "/equities/master",
            "/equities/bars/daily",
            "/markets/calendar",
            "/equities/valuation",
            "/indices/bars/daily/topix",
        }:
            raise ValueError("Unsupported J-Quants endpoint")
        query = dict(params or {})
        rows, seen = [], set()
        while True:
            payload = self._get(endpoint, query)
            data = payload.get("data")
            if not isinstance(data, list):
                raise ValueError(f"Invalid J-Quants data response: {endpoint}")
            rows.extend(data)
            key = payload.get("pagination_key")
            if not key:
                return rows
            if key in seen:
                raise ValueError(f"Repeated J-Quants pagination key: {endpoint}")
            seen.add(key)
            query["pagination_key"] = key

    def _get(self, endpoint, params):
        for attempt in range(6):
            delay = self._next - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._next = time.monotonic() + self._interval
            try:
                response = self._session.get(
                    "https://api.jquants.com/v2" + endpoint,
                    params=params,
                    headers={"x-api-key": self._key},
                    timeout=(10, 120),
                )
            except requests.RequestException:
                if attempt == 5:
                    raise RuntimeError(
                        f"J-Quants network failure: {endpoint}"
                    ) from None
                self._next = time.monotonic() + min(30, 2**attempt)
                continue
            if response.status_code == 429:
                try:
                    cooldown = min(
                        180, max(60, float(response.headers.get("Retry-After", 60)))
                    )
                except ValueError:
                    cooldown = 60
                # Split long cooldowns to keep each individual wait bounded.
                while cooldown > 0:
                    wait = min(30, cooldown)
                    time.sleep(wait)
                    cooldown -= wait
                continue
            if response.status_code >= 500:
                self._next = time.monotonic() + min(30, 2**attempt)
                continue
            if response.status_code != 200:
                # Never include response bodies, keys or request headers in errors.
                raise RuntimeError(f"J-Quants HTTP {response.status_code}: {endpoint}")
            return response.json()
        raise RuntimeError(f"J-Quants retry budget exhausted: {endpoint}")
