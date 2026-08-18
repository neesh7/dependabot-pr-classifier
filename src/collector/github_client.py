"""GitHub GraphQL client: fetch open Dependabot PRs with pagination."""

import httpx

PUBLIC_API_ROOT = "https://api.github.com"
# GraphQL returns bot logins without the "[bot]" suffix that REST uses
DEPENDABOT_LOGINS = {"dependabot", "dependabot[bot]"}
PAGE_SIZE = 50

PR_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: %d, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        title
        body
        url
        createdAt
        mergeable
        baseRefName
        author { login }
        labels(first: 10) { nodes { name } }
        files(first: 20) { nodes { path } }
        commits(last: 1) {
          nodes { commit { statusCheckRollup { state } } }
        }
      }
    }
  }
}
""" % PAGE_SIZE


def graphql_url(api_url: str = "") -> str:
    """GraphQL endpoint for github.com, or for a GitHub Enterprise server root."""
    return f"{api_url.rstrip('/')}/api/graphql" if api_url else f"{PUBLIC_API_ROOT}/graphql"


def rest_url(api_url: str = "") -> str:
    """REST base for github.com, or for a GitHub Enterprise server root."""
    return f"{api_url.rstrip('/')}/api/v3" if api_url else PUBLIC_API_ROOT


def make_client(token: str) -> httpx.Client:
    """Authenticated client; an empty token means unauthenticated (public, rate-limited)."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.Client(headers=headers, timeout=30)


def _graphql(client: httpx.Client, url: str, query: str, variables: dict) -> dict:
    resp = client.post(url, json={"query": query, "variables": variables})
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"GraphQL errors: {data['errors']}")
    return data["data"]


def _is_dependabot(pr: dict) -> bool:
    return (pr.get("author") or {}).get("login") in DEPENDABOT_LOGINS


def fetch_dependabot_prs(client: httpx.Client, repo: str, api_url: str = "") -> list[dict]:
    """All open Dependabot PRs in a repo, paginated. api_url targets a GHE server."""
    owner, name = repo.split("/", 1)
    url = graphql_url(api_url)
    prs, cursor = [], None
    while True:
        data = _graphql(client, url, PR_QUERY,
                        {"owner": owner, "name": name, "cursor": cursor})
        page = data["repository"]["pullRequests"]
        prs.extend(pr for pr in page["nodes"] if _is_dependabot(pr))
        if not page["pageInfo"]["hasNextPage"]:
            return prs
        cursor = page["pageInfo"]["endCursor"]
