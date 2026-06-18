"""Proxy configuration and rotating Requests sessions."""

import random
from typing import Optional
from urllib.parse import urlparse

import requests
import yaml

_PROXY_SCHEMES = {"http", "https", "socks4", "socks4a", "socks5", "socks5h"}
_MAX_FAILURES = 3
_RETRYABLE_STATUSES = {403, 407, 408, 429}
_SCHOLAR_BLOCK_MARKERS = (
    "form[action*='/sorry/']",
    "/sorry/",
    "input name=\"captcha\"",
    "input name='captcha'",
    "g-recaptcha",
    "gs_captcha_ccl",
    "our systems have detected unusual traffic",
    "not a robot",
    "to continue, please type the characters",
)


class ProxyConfigError(ValueError):
    """Raised when a proxy configuration file is invalid."""


def _normalise_url(url: str) -> str:
    if not isinstance(url, str):
        raise ProxyConfigError("Proxy URLs must be strings.")
    url = str(url).strip()
    if not url:
        raise ProxyConfigError("Proxy URLs must be non-empty strings.")
    if "://" in url:
        return url
    if ":" in url and url.split(":", 1)[0].lower() in _PROXY_SCHEMES:
        scheme, rest = url.split(":", 1)
        return f"{scheme}://{rest}"
    return f"http://{url}"


def _normalise_endpoint(raw: dict, proxy_id: str) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ProxyConfigError(f"Proxy group '{proxy_id}' entries must be mappings.")

    endpoint = {
        scheme: _normalise_url(url)
        for scheme, url in raw.items()
        if scheme in {"http", "https"} and url
    }
    if not endpoint:
        raise ProxyConfigError(
            f"Proxy group '{proxy_id}' entries must define http or https."
        )
    if "http" not in endpoint:
        endpoint["http"] = endpoint["https"]
    if "https" not in endpoint:
        endpoint["https"] = endpoint["http"]
    return endpoint


class ProxyPool:
    """Round-robin proxy IDs, randomly choosing an endpoint within each ID."""

    def __init__(self, groups: list[tuple[str, list[dict[str, str]]]]) -> None:
        if not groups:
            raise ProxyConfigError("Proxy configuration must contain at least one group.")
        self.groups = groups
        self._index = 0
        self._failures = {proxy_id: 0 for proxy_id, _ in groups}
        self._disabled: set[str] = set()

    def next(self) -> dict[str, str]:
        """Return the next active proxy endpoint."""
        _, endpoint = self.next_proxy()
        return endpoint

    def next_proxy(self) -> tuple[str, dict[str, str]]:
        """Return ``(proxy_id, endpoint)`` for the next active proxy."""
        for _ in range(len(self.groups)):
            proxy_id, endpoints = self.groups[self._index]
            self._index = (self._index + 1) % len(self.groups)
            if proxy_id in self._disabled:
                continue
            endpoint = random.choice(endpoints)
            return proxy_id, dict(endpoint)
        raise requests.exceptions.ProxyError("All configured proxies are unavailable.")

    def mark_success(self, proxy_id: str) -> None:
        self._failures[proxy_id] = 0

    def mark_failure(self, proxy_id: str, reason: str) -> None:
        if proxy_id in self._disabled:
            return
        failures = self._failures.get(proxy_id, 0) + 1
        self._failures[proxy_id] = failures
        if failures >= _MAX_FAILURES:
            self._disabled.add(proxy_id)
            print(f"    [proxy] {proxy_id} disabled after {failures} failures: {reason}")
        else:
            print(f"    [proxy] {proxy_id} failed ({failures}/{_MAX_FAILURES}): {reason}")

    def all_disabled(self) -> bool:
        return len(self._disabled) == len(self.groups)


def _is_retryable_status(status_code: int) -> bool:
    return status_code in _RETRYABLE_STATUSES or 500 <= status_code < 600


def _is_scholar_url(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return False
    return host == "scholar.google.com" or host.endswith(".scholar.google.com")


def _looks_like_scholar_block(url: str, resp: requests.Response) -> bool:
    if not _is_scholar_url(resp.url or url):
        return False
    try:
        text = resp.text.lower()
    except Exception:
        return False
    return any(marker in text for marker in _SCHOLAR_BLOCK_MARKERS)


def _failure_reason(url: str, resp: requests.Response) -> str:
    if _looks_like_scholar_block(url, resp):
        return "Google Scholar CAPTCHA/block page"
    return f"HTTP {resp.status_code}"


def _proxy_failed_response(url: str, resp: requests.Response) -> bool:
    return _is_retryable_status(resp.status_code) or _looks_like_scholar_block(url, resp)


def _all_proxies_error(last_failure: str) -> requests.exceptions.ProxyError:
    message = "All configured proxies are unavailable."
    if last_failure:
        message += f" Last failure: {last_failure}"
    return requests.exceptions.ProxyError(message)


class RotatingProxySession(requests.Session):
    """Requests session that injects a rotated proxy into each request."""

    def __init__(self, proxy_pool: ProxyPool) -> None:
        super().__init__()
        self.proxy_pool = proxy_pool

    def request(self, method, url, **kwargs):
        if kwargs.get("proxies") is not None:
            return super().request(method, url, **kwargs)

        last_failure = ""
        last_exception = None
        while True:
            try:
                proxy_id, proxies = self.proxy_pool.next_proxy()
            except requests.exceptions.ProxyError as e:
                if last_exception:
                    raise _all_proxies_error(last_failure) from last_exception
                raise _all_proxies_error(last_failure) from e

            request_kwargs = dict(kwargs)
            request_kwargs["proxies"] = proxies
            try:
                resp = super().request(method, url, **request_kwargs)
            except requests.RequestException as e:
                last_failure = str(e)
                last_exception = e
                self.proxy_pool.mark_failure(proxy_id, last_failure)
                if self.proxy_pool.all_disabled():
                    raise _all_proxies_error(last_failure) from e
                continue

            if _proxy_failed_response(url, resp):
                last_failure = _failure_reason(url, resp)
                resp.close()
                self.proxy_pool.mark_failure(proxy_id, last_failure)
                if self.proxy_pool.all_disabled():
                    raise _all_proxies_error(last_failure)
                continue

            self.proxy_pool.mark_success(proxy_id)
            return resp


def load_proxy_pool(path: str) -> ProxyPool:
    """Load a proxy pool from a YAML configuration file."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except OSError as e:
        raise ProxyConfigError(f"Could not read proxy config '{path}': {e}") from e
    except yaml.YAMLError as e:
        raise ProxyConfigError(f"Invalid proxy config YAML '{path}': {e}") from e

    if not isinstance(raw, dict) or not raw:
        raise ProxyConfigError("Proxy config must be a non-empty mapping.")

    groups = []
    for proxy_id, entries in raw.items():
        if not isinstance(entries, list) or not entries:
            raise ProxyConfigError(
                f"Proxy group '{proxy_id}' must be a non-empty list."
            )
        endpoints = [_normalise_endpoint(entry, str(proxy_id)) for entry in entries]
        groups.append((str(proxy_id), endpoints))
    return ProxyPool(groups)


def make_session(proxy_pool: Optional[ProxyPool] = None) -> requests.Session:
    """Create a Requests session, optionally backed by rotating proxies."""
    if proxy_pool is None:
        return requests.Session()
    return RotatingProxySession(proxy_pool)
