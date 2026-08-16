"""Latest stable version lookups per ecosystem, with per-run cache and retries."""

import sys
from urllib.parse import quote

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from src.classifier.versions import is_prerelease, sort_key


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    return (isinstance(exc, httpx.HTTPStatusError)
            and (exc.response.status_code == 429 or exc.response.status_code >= 500))


class RegistryClient:
    def __init__(self, client: httpx.Client | None = None):
        self._client = client or httpx.Client(timeout=20, follow_redirects=True)
        self._cache: dict[tuple[str, str], str | None] = {}

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=10),
           retry=retry_if_exception(_retryable), reraise=True)
    def _get_json(self, url: str) -> dict | list:
        resp = self._client.get(url)
        resp.raise_for_status()
        return resp.json()

    def latest_stable(self, ecosystem: str, package: str) -> str | None:
        """Latest non-prerelease version, or None if unknown/unsupported."""
        key = (ecosystem, package.lower())
        if key not in self._cache:
            lookup = getattr(self, f"_latest_{ecosystem}", None)
            try:
                self._cache[key] = lookup(package) if lookup else None
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    print(f"registry lookup failed for {ecosystem}:{package}: {exc}",
                          file=sys.stderr)
                self._cache[key] = None
        return self._cache[key]

    def _latest_pip(self, package: str) -> str | None:
        data = self._get_json(f"https://pypi.org/pypi/{quote(package)}/json")
        return data["info"]["version"]

    def _latest_npm(self, package: str) -> str | None:
        # scoped packages need the / encoded: @types/node -> @types%2Fnode
        data = self._get_json(f"https://registry.npmjs.org/{quote(package, safe='@')}/latest")
        return data["version"]

    def _latest_nuget(self, package: str) -> str | None:
        data = self._get_json(
            f"https://api.nuget.org/v3-flatcontainer/{package.lower()}/index.json")
        stable = [v for v in data["versions"] if not is_prerelease(v)]
        return max(stable, key=sort_key) if stable else None
