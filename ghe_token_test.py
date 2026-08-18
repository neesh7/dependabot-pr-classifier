"""Check that GITHUB_TOKEN works and can actually read the configured repos.

Two checks, in the order they fail usefully:

  1. Token check     — who does this token authenticate as, and what can it do?
  2. Repo discovery  — can it read each repo in REPOS, and see Dependabot PRs there?

The token is never printed. Exit code is 0 only if both checks pass.

    python ghe_token_test.py                    # checks every repo in REPOS
    python ghe_token_test.py -r owner/name      # checks one repo (repeatable)

For GitHub Enterprise, set GITHUB_API_URL to the server root, e.g.
GITHUB_API_URL=https://ghe.example.com — the /api/graphql and /api/v3 paths are
derived from it. Unset means github.com.
"""

import argparse
import os
import sys

import httpx
from dotenv import load_dotenv

from src.collector.github_client import make_client

OK, BAD = "PASS", "FAIL"

VIEWER_QUERY = "query { viewer { login } rateLimit { remaining resetAt } }"

REPO_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    nameWithOwner
    isPrivate
    viewerPermission
    pullRequests(states: OPEN, first: 100) {
      totalCount
      nodes { author { login } }
    }
  }
}
"""

DEPENDABOT_LOGINS = {"dependabot", "dependabot[bot]"}


def endpoints() -> tuple[str, str]:
    """(graphql_url, rest_url) for github.com or a GitHub Enterprise server."""
    root = os.getenv("GITHUB_API_URL", "").strip().rstrip("/")
    if not root:
        return "https://api.github.com/graphql", "https://api.github.com"
    return f"{root}/api/graphql", f"{root}/api/v3"


NO_ACCESS = ("repo does not exist, or the token cannot see it "
             "(private repo without `repo` scope, or no access granted)")


def _diagnose(exc: Exception) -> str:
    """Map the usual GitHub failures onto the thing you actually have to fix."""
    # a missing/invisible repo comes back as a GraphQL error, not a 404
    if "Could not resolve to a Repository" in str(exc):
        return NO_ACCESS
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 401:
            return "401 Bad credentials — the token is wrong, expired, or revoked"
        if code == 403:
            body = exc.response.text[:200]
            if "SAML" in body or "SSO" in body:
                return ("403 — the token needs SSO authorisation for this org "
                        "(Settings > Developer settings > PAT > Configure SSO)")
            return f"403 Forbidden — token lacks the required scope, or rate limited: {body}"
        if code == 404:
            return f"404 — {NO_ACCESS}"
        return f"HTTP {code}: {exc.response.text[:200]}"
    if isinstance(exc, httpx.ConnectError):
        return f"cannot reach the server — check GITHUB_API_URL and network ({exc})"
    return f"{type(exc).__name__}: {str(exc)[:200]}"


def _graphql(client: httpx.Client, url: str, query: str, variables: dict | None = None) -> dict:
    resp = client.post(url, json={"query": query, "variables": variables or {}})
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError("; ".join(e.get("message", "?") for e in data["errors"]))
    return data["data"]


# ── check 1: is the token valid, and as whom? ─────────────────────────────

def check_token(client: httpx.Client, graphql_url: str, rest_url: str) -> bool:
    print("1. Token check")
    try:
        data = _graphql(client, graphql_url, VIEWER_QUERY)
    except Exception as exc:
        print(f"   {BAD} {_diagnose(exc)}")
        return False

    viewer, limit = data["viewer"]["login"], data["rateLimit"]
    print(f"   {OK} authenticated as {viewer!r}")
    print(f"        rate limit: {limit['remaining']} remaining, resets {limit['resetAt']}")

    # scopes only exist for classic PATs; fine-grained tokens omit the header
    try:
        resp = client.get(f"{rest_url}/user")
        scopes = resp.headers.get("x-oauth-scopes")
        if scopes is None:
            print("        scopes: not reported (fine-grained token or GitHub App)")
        else:
            granted = [s.strip() for s in scopes.split(",") if s.strip()]
            print(f"        scopes: {', '.join(granted) or '(none)'}")
            if "repo" not in granted and "public_repo" not in granted:
                print("        note: no `repo` scope — private repos will 404 in check 2")
    except Exception as exc:  # non-fatal: the GraphQL call already proved the token
        print(f"        scopes: could not read ({type(exc).__name__})")
    return True


# ── check 2: can it actually read the repos we scan? ──────────────────────

def check_repo(client: httpx.Client, graphql_url: str, repo: str) -> bool:
    if "/" not in repo:
        print(f"   {BAD} {repo}: expected owner/name")
        return False
    owner, name = repo.split("/", 1)
    try:
        data = _graphql(client, graphql_url, REPO_QUERY, {"owner": owner, "name": name})
    except Exception as exc:
        print(f"   {BAD} {repo}: {_diagnose(exc)}")
        return False

    r = data.get("repository")
    if r is None:
        print(f"   {BAD} {repo}: not visible to this token (404 / no access)")
        return False

    prs = r["pullRequests"]
    bot = sum(1 for pr in prs["nodes"]
              if (pr.get("author") or {}).get("login") in DEPENDABOT_LOGINS)
    visibility = "private" if r["isPrivate"] else "public"
    print(f"   {OK} {r['nameWithOwner']}: {visibility}, permission "
          f"{r['viewerPermission'] or 'NONE'}, {prs['totalCount']} open PRs "
          f"({bot} from Dependabot)")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-r", "--repo", action="append",
                        help="repo to check as owner/name (repeatable); defaults to REPOS")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):  # Windows consoles default to cp1252
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    load_dotenv()
    token = os.getenv("GITHUB_TOKEN", "")
    if not token:
        print(f"{BAD} GITHUB_TOKEN is not set (see .env.example)")
        return 1

    graphql_url, rest_url = endpoints()
    repos = args.repo or [r.strip() for r in os.getenv("REPOS", "").split(",") if r.strip()]

    print(f"Endpoint: {graphql_url}")
    print(f"Token:    {len(token)} chars, ending {token[-4:]}\n")

    with make_client(token) as client:
        token_ok = check_token(client, graphql_url, rest_url)

        print("\n2. Repo discovery check")
        if not token_ok:
            print("   skipped — the token itself is not usable")
            repos_ok = False
        elif not repos:
            print(f"   {BAD} no repos to check: set REPOS or pass --repo owner/name")
            repos_ok = False
        else:
            repos_ok = all([check_repo(client, graphql_url, r) for r in repos])

    ok = token_ok and repos_ok
    print("\nToken is good for this pipeline." if ok else
          "\nFix the failures above before running the pipeline.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
