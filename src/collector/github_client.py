"""GitHub GraphQL client: fetch open Dependabot PRs with pagination."""

import httpx

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"
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


def make_client(token: str) -> httpx.Client:
    return httpx.Client(headers={"Authorization": f"Bearer {token}"}, timeout=30)


def _graphql(client: httpx.Client, query: str, variables: dict) -> dict:
    resp = client.post(GITHUB_GRAPHQL_URL, json={"query": query, "variables": variables})
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"GraphQL errors: {data['errors']}")
    return data["data"]


def _is_dependabot(pr: dict) -> bool:
    return (pr.get("author") or {}).get("login") in DEPENDABOT_LOGINS


def fetch_dependabot_prs(client: httpx.Client, repo: str) -> list[dict]:
    """All open Dependabot PRs in a repo, paginated."""
    owner, name = repo.split("/", 1)
    prs, cursor = [], None
    while True:
        data = _graphql(client, PR_QUERY, {"owner": owner, "name": name, "cursor": cursor})
        page = data["repository"]["pullRequests"]
        prs.extend(pr for pr in page["nodes"] if _is_dependabot(pr))
        if not page["pageInfo"]["hasNextPage"]:
            return prs
        cursor = page["pageInfo"]["endCursor"]
