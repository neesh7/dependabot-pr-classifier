"""Read-only tools for the model tool-use loop. Errors are returned as strings
so the model can adapt instead of the run crashing.

Schema is the OpenAI function-calling shape: {"type": "function", "function": {...}}."""

import re

import httpx

from src.classifier.versions import sort_key

MAX_FILE_CHARS = 20_000
MAX_NOTES_CHARS = 6_000

def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": required, "additionalProperties": False},
    }}


TOOL_DEFINITIONS = [
    _tool(
        "fetch_release_notes",
        "Fetch GitHub release notes for a package covering the gap between "
        "two versions. Use to judge breaking changes and fixes in the gap.",
        {
            "package": {"type": "string"},
            "ecosystem": {"type": "string", "enum": ["npm", "pip", "nuget"]},
            "from_ver": {"type": "string", "description": "lower bound (exclusive)"},
            "to_ver": {"type": "string", "description": "upper bound (inclusive)"},
        },
        ["package", "ecosystem", "from_ver", "to_ver"],
    ),
    _tool(
        "lookup_cve",
        "Look up one vulnerability by ID (CVE-... or GHSA-...) on OSV.dev.",
        {"vuln_id": {"type": "string"}},
        ["vuln_id"],
    ),
    _tool(
        "fetch_file_from_repo",
        "Fetch one file from a GitHub repo (e.g. CHANGELOG.md). "
        "repo is 'owner/name'. Read-only, size-capped.",
        {"repo": {"type": "string"}, "path": {"type": "string"}},
        ["repo", "path"],
    ),
]

_GITHUB_URL_RE = re.compile(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git|/|$)")


class ToolExecutor:
    def __init__(self, github_token: str):
        self._http = httpx.Client(timeout=20, follow_redirects=True)
        self._gh = httpx.Client(timeout=20, follow_redirects=True,
                                headers={"Authorization": f"Bearer {github_token}"})

    def execute(self, name: str, args: dict) -> str:
        try:
            handler = getattr(self, f"_{name}", None)
            if handler is None:
                return f"Unknown tool: {name}"
            return handler(**args)
        except Exception as exc:  # tool errors go back to the model as text
            return f"Tool error: {exc}"

    # ── fetch_release_notes ───────────────────────────────────────────────

    def _resolve_github_repo(self, ecosystem: str, package: str) -> str | None:
        try:
            if ecosystem == "npm":
                data = self._http.get(
                    f"https://registry.npmjs.org/{package}/latest").json()
                url = (data.get("repository") or {}).get("url", "")
            elif ecosystem == "pip":
                data = self._http.get(f"https://pypi.org/pypi/{package}/json").json()
                info = data["info"]
                urls = list((info.get("project_urls") or {}).values())
                url = next((u for u in urls + [info.get("home_page") or ""]
                            if u and "github.com" in u), "")
            elif ecosystem == "nuget":
                # nuspec of any version carries the repository/project URL
                idx = self._http.get(
                    f"https://api.nuget.org/v3-flatcontainer/{package.lower()}/index.json"
                ).json()
                ver = idx["versions"][-1]
                nuspec = self._http.get(
                    f"https://api.nuget.org/v3-flatcontainer/{package.lower()}/{ver}/"
                    f"{package.lower()}.nuspec").text
                m = re.search(r'(?:repository[^>]*url|projectUrl)[="<>]*([^"<]+)', nuspec)
                url = m.group(1) if m else ""
            else:
                return None
        except httpx.HTTPError:
            return None
        m = _GITHUB_URL_RE.search(url or "")
        return m.group(1) if m else None

    def _fetch_release_notes(self, package: str, ecosystem: str,
                             from_ver: str, to_ver: str) -> str:
        repo = self._resolve_github_repo(ecosystem, package)
        if not repo:
            return f"No GitHub repository could be resolved for {ecosystem}:{package}."
        resp = self._gh.get(f"https://api.github.com/repos/{repo}/releases",
                            params={"per_page": 40})
        resp.raise_for_status()
        releases = resp.json()

        lo, hi = sort_key(from_ver), sort_key(to_ver)

        def in_gap(tag: str) -> bool:
            ver = re.sub(r"^[^\d]*", "", tag)  # v1.2.3 / release-1.2.3 -> 1.2.3
            if not ver:
                return False
            return lo < sort_key(ver) <= hi

        picked = [r for r in releases if in_gap(r.get("tag_name", ""))] or releases[:5]
        out = []
        for r in picked:
            body = (r.get("body") or "").strip()
            out.append(f"## {r.get('name') or r.get('tag_name')}\n{body[:1200]}")
        text = "\n\n".join(out)[:MAX_NOTES_CHARS]
        return text or f"No release notes found for {package} {from_ver}..{to_ver}."

    # ── lookup_cve ────────────────────────────────────────────────────────

    def _lookup_cve(self, vuln_id: str) -> str:
        resp = self._http.get(f"https://api.osv.dev/v1/vulns/{vuln_id}")
        resp.raise_for_status()
        v = resp.json()
        severity = ", ".join(s.get("score", "") for s in v.get("severity", []))
        return (f"{v.get('id')}: {v.get('summary', '')}\n"
                f"Severity: {severity or 'unspecified'}\n"
                f"{(v.get('details') or '')[:1500]}")

    # ── fetch_file_from_repo ──────────────────────────────────────────────

    def _fetch_file_from_repo(self, repo: str, path: str) -> str:
        resp = self._gh.get(f"https://api.github.com/repos/{repo}/contents/{path}",
                            headers={"Accept": "application/vnd.github.raw+json"})
        resp.raise_for_status()
        return resp.text[:MAX_FILE_CHARS]
