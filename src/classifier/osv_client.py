"""OSV.dev batch lookup: known vulnerability IDs for exact (ecosystem, package, version)."""

import sys

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from src.classifier.registry_client import _retryable
from src.classifier.versions import normalize

OSV_BATCH_URL = "https://api.osv.dev/v1/querybatch"

# our ecosystem slugs -> OSV ecosystem names
OSV_ECOSYSTEMS = {
    "pip": "PyPI",
    "npm": "npm",
    "nuget": "NuGet",
    "maven": "Maven",
    "gomod": "Go",
    "cargo": "crates.io",
    "bundler": "RubyGems",
    "github-actions": "GitHub Actions",
}

Query = tuple[str, str, str]  # (ecosystem, package, version)


class OSVClient:
    def __init__(self, client: httpx.Client | None = None):
        self._client = client or httpx.Client(timeout=20)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=10),
           retry=retry_if_exception(_retryable), reraise=True)
    def _post(self, payload: dict) -> dict:
        resp = self._client.post(OSV_BATCH_URL, json=payload)
        resp.raise_for_status()
        return resp.json()

    def query_batch(self, items: list[Query]) -> dict[Query, list[str]]:
        """Vulnerability IDs (GHSA/CVE) per queried version. Missing/unsupported -> []."""
        supported = [i for i in items if i[0] in OSV_ECOSYSTEMS and i[2]]
        result: dict[Query, list[str]] = {i: [] for i in items}
        if not supported:
            return result
        payload = {"queries": [
            {"package": {"name": pkg, "ecosystem": OSV_ECOSYSTEMS[eco]},
             "version": normalize(ver)}
            for eco, pkg, ver in supported
        ]}
        try:
            data = self._post(payload)
        except httpx.HTTPError as exc:
            print(f"OSV batch query failed: {exc}", file=sys.stderr)
            return result
        for item, res in zip(supported, data.get("results", [])):
            result[item] = [v["id"] for v in (res or {}).get("vulns", [])]
        return result
