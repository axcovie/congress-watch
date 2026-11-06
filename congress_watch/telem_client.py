"""Thin HTTP client for the Telem router API (search, fetch, monitors).

Endpoints and payload shapes follow https://docs.telem.ai/ and
https://docs.telem.ai/resources/monitors/.
"""
from __future__ import annotations

import os
from typing import Any, Iterable

import httpx

DEFAULT_BASE_URL = "https://router.telem.ai"


class TelemError(RuntimeError):
    pass


def extract_results(data: dict) -> list[dict]:
    """Pull results from a search-shaped response (search, monitor poll, webhook).

    Handles a top-level `results` list and/or per-provider
    `preprocessor_runs[*].output_payload.results`, de-duplicated by URL.
    """
    seen: set[str] = set()
    out: list[dict] = []

    def add(items, provider=None):
        for r in items or []:
            if not isinstance(r, dict):
                continue
            url = r.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            out.append({**r, "provider": r.get("provider") or provider})

    add(data.get("results"))
    for run in data.get("preprocessor_runs") or data.get("by_provider") or []:
        payload = run.get("output_payload") or run
        add(payload.get("results"), run.get("preprocessor_name") or run.get("provider"))
    return out


def has_warning(data, name: str) -> bool:
    """True if `name` appears as a warning anywhere in the response: a truthy
    key, an item in a warnings list, or a dict whose code/type equals it."""
    if isinstance(data, dict):
        if data.get(name):
            return True
        if data.get("code") == name or data.get("type") == name:
            return True
        return any(has_warning(v, name) for v in data.values() if isinstance(v, (dict, list)))
    if isinstance(data, list):
        return any(x == name or has_warning(x, name) for x in data)
    return False


class TelemClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 90.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("TELEM_API_KEY")
        if not self.api_key:
            raise TelemError("TELEM_API_KEY is not set. Get a key at https://app.telem.ai")
        base = (base_url or os.environ.get("TELEM_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._http = httpx.Client(
            base_url=base,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
            transport=transport,
        )

    # ------------------------------------------------------------------ core
    def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        resp = self._http.request(method, path, **kwargs)
        if resp.status_code == 204:
            return {}
        if resp.status_code >= 400:
            raise TelemError(f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:500]}")
        try:
            return resp.json()
        except ValueError as exc:  # pragma: no cover - defensive
            raise TelemError(f"{method} {path} returned non-JSON body") from exc

    def close(self) -> None:
        self._http.close()

    # ---------------------------------------------------------------- search
    def search(
        self,
        query: str,
        num_results: int = 5,
        auto_routing: str | None = "accuracy",
        providers: list[str] | None = None,
    ) -> list[dict]:
        """Run a search and return de-duplicated results across providers."""
        search: dict[str, Any] = {"num_results": num_results}
        if providers:
            search["providers"] = {"include": providers}
        elif auto_routing:
            search["auto_routing"] = auto_routing
        data = self._request(
            "POST", "/v1/search", json={"user_input": {"query": query}, "search": search}
        )
        return extract_results(data)

    # ----------------------------------------------------------------- fetch
    def fetch(self, urls: Iterable[str], tier: str = "default") -> dict[str, dict]:
        """Fetch pages as clean markdown. Returns {url: fetched_result}."""
        urls = list(urls)
        if not urls:
            return {}
        data = self._request(
            "POST", "/v1/fetch", json={"urls": urls, "options": {"tier": tier}}
        )
        out: dict[str, dict] = {}
        for run in data.get("preprocessor_runs") or []:
            for r in (run.get("output_payload") or {}).get("fetched_results") or []:
                if r.get("url"):
                    out[r["url"]] = r
        return out

    # -------------------------------------------------------------- monitors
    def create_monitor(
        self,
        query: str,
        frequency: str = "1d",
        include_domains: list[str] | None = None,
        ttl: str | None = "30d",
        webhook_url: str | None = None,
        search_num_results: int | None = None,
        metadata: dict | None = None,
    ) -> dict:
        options: dict[str, Any] = {"frequency": frequency}
        if include_domains:
            options["include_domains"] = include_domains[:20]
        if search_num_results:
            options["search"] = {"num_results": search_num_results}
        body: dict[str, Any] = {"query": query, "options": options}
        if ttl:
            body["ttl"] = ttl
        if webhook_url:
            body["webhook_url"] = webhook_url
        if metadata:
            body["metadata"] = metadata
        return self._request("POST", "/v1/monitors", json=body)

    def poll_monitor(self, monitor_id: str) -> dict:
        return self._request("POST", f"/v1/monitors/{monitor_id}/poll")

    def get_monitor(self, monitor_id: str) -> dict:
        return self._request("GET", f"/v1/monitors/{monitor_id}")

    def list_monitors(self, limit: int = 200) -> list[dict]:
        out: list[dict] = []
        cursor = None
        while True:
            params: dict[str, Any] = {"limit": limit}
            if cursor:
                params["cursor"] = cursor
            data = self._request("GET", "/v1/monitors", params=params)
            items = data.get("data") or data.get("monitors") or data.get("items") or []
            out.extend(items)
            cursor = data.get("next_cursor")
            if not cursor:
                return out

    def update_monitor(self, monitor_id: str, **fields: Any) -> dict:
        return self._request("PATCH", f"/v1/monitors/{monitor_id}", json=fields)

    def delete_monitor(self, monitor_id: str) -> None:
        self._request("DELETE", f"/v1/monitors/{monitor_id}")
