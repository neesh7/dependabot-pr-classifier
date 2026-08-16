"""Parse Dependabot PR titles, bodies, and file lists into structured fields."""

import re

from src.schemas import DependencyUpdate, ParsedTitle

# Optional conventional-commit prefix Dependabot adds when configured, e.g. "chore(deps): "
_PREFIX_RE = re.compile(r"^\w+(\([^)]*\))?[:!]\s+")

# dot-separated components; must not end on a dot (bodies write "to 13.0.4.")
_VER = r"[~=^<>!]*[\w+-]+(?:\.[\w+-]+)*"

_BUMP_RE = re.compile(
    rf"^[Bb]umps? (?P<dep>\S+) from (?P<from_ver>{_VER}) to (?P<to_ver>{_VER})(?: in (?P<dir>\S+))?$"
)
_REQUIREMENT_RE = re.compile(
    rf"^[Uu]pdates? (?P<dep>\S+) requirement from (?P<from_ver>{_VER}) to (?P<to_ver>{_VER})(?: in (?P<dir>\S+))?$"
)
_GROUP_RE = re.compile(
    r"^[Bb]umps? the (?P<group>\S+) group"
    r"(?: in (?P<dir>\S+))?"
    r"(?: across \d+ director(?:y|ies))?"
    r" with (?P<count>\d+) updates?"
)

# Matches the per-dependency lines Dependabot writes in bodies, in all its phrasings:
#   "Bumps [lodash](https://...) from 4.17.20 to 4.17.21."
#   "Updates `axios` from 1.5.0 to 1.6.0"
#   "Updated [Newtonsoft.Json](https://...) from 13.0.1 to 13.0.4."
_BODY_UPDATE_RE = re.compile(
    rf"\b(?:Bumps?|Updates?d?) \[?`?(?P<dep>[A-Za-z0-9@_./-]+)`?\]?(?:\([^)\s]+\))?"
    rf" from `?(?P<from_ver>{_VER})`? to `?(?P<to_ver>{_VER})`?"
)

_COMPAT_BADGE_RE = re.compile(
    r"https://dependabot-badges\.githubapp\.com/badges/compatibility_score\?[^)\s\]]+"
)
_CHANGELOG_LINK_RE = re.compile(r"https://[^\s)\]]*(?:changelog|CHANGELOG|/releases)[^\s)\]]*")
_SECURITY_BODY_RE = re.compile(r"(?i)security (?:advisor|fix|update|vulnerabilit)")

# (filename predicate, ecosystem) — manifests first, lockfiles as fallback
_MANIFEST_RULES = [
    (lambda n: n.endswith((".csproj", ".fsproj", ".vbproj"))
     or n in {"packages.config", "Directory.Packages.props", "Directory.Build.props"}, "nuget"),
    (lambda n: n == "package.json", "npm"),
    (lambda n: (n.startswith("requirements") and n.endswith(".txt"))
     or n in {"Pipfile", "pyproject.toml", "setup.py", "setup.cfg"}, "pip"),
    (lambda n: n == "pom.xml", "maven"),
    (lambda n: n in {"build.gradle", "build.gradle.kts"}, "gradle"),
    (lambda n: n == "go.mod", "gomod"),
    (lambda n: n == "Gemfile", "bundler"),
    (lambda n: n == "Cargo.toml", "cargo"),
    (lambda n: n.startswith("Dockerfile"), "docker"),
]
_LOCKFILE_RULES = [
    (lambda n: n in {"package-lock.json", "yarn.lock", "pnpm-lock.yaml"}, "npm"),
    (lambda n: n in {"Pipfile.lock", "poetry.lock", "uv.lock"}, "pip"),
    (lambda n: n == "packages.lock.json", "nuget"),
    (lambda n: n == "go.sum", "gomod"),
    (lambda n: n == "Gemfile.lock", "bundler"),
    (lambda n: n == "Cargo.lock", "cargo"),
]


def parse_title(title: str) -> ParsedTitle:
    title = _PREFIX_RE.sub("", title.strip()).rstrip(".")
    if m := _GROUP_RE.match(title):
        return ParsedTitle(kind="group", group_name=m["group"], directory=m["dir"],
                           update_count=int(m["count"]))
    if m := _BUMP_RE.match(title):
        return ParsedTitle(kind="bump", dependency=m["dep"], from_ver=m["from_ver"],
                           to_ver=m["to_ver"], directory=m["dir"])
    if m := _REQUIREMENT_RE.match(title):
        return ParsedTitle(kind="requirement", dependency=m["dep"], from_ver=m["from_ver"],
                           to_ver=m["to_ver"], directory=m["dir"])
    return ParsedTitle(kind="unknown")


def parse_body_updates(body: str) -> list[DependencyUpdate]:
    """Per-dependency bumps stated in the body (grouped PRs list one line per dep).

    Strips <details> blocks first — release notes inside them repeat
    'Bumps X from A to B' phrases that aren't this PR's updates.
    """
    head = re.sub(r"<details>.*?</details>", "", body, flags=re.DOTALL)
    seen: dict[str, DependencyUpdate] = {}
    for m in _BODY_UPDATE_RE.finditer(head):
        if m["dep"] not in seen:
            seen[m["dep"]] = DependencyUpdate(dependency=m["dep"], from_ver=m["from_ver"],
                                              to_ver=m["to_ver"])
    return list(seen.values())


def extract_body_meta(body: str) -> dict:
    notes = ""
    if m := re.search(r"<summary>Release notes</summary>(.*?)</details>", body, re.DOTALL):
        notes = m.group(1).strip()[:1500]
    badge = _COMPAT_BADGE_RE.search(body)
    links = list(dict.fromkeys(_CHANGELOG_LINK_RE.findall(body)))[:5]
    return {
        "release_notes_excerpt": notes,
        "changelog_links": links,
        "compatibility_score_url": badge.group(0) if badge else None,
    }


def detect_ecosystem(files: list[str]) -> tuple[str | None, str | None]:
    """(ecosystem, manifest_path) from the PR's changed files."""
    names = [(path, path.rsplit("/", 1)[-1]) for path in files]
    for path, name in names:
        if path.startswith(".github/workflows/"):
            return "github-actions", path
    for rules in (_MANIFEST_RULES, _LOCKFILE_RULES):
        for path, name in names:
            for predicate, ecosystem in rules:
                if predicate(name):
                    return ecosystem, path
    return None, None


def is_security(labels: list[str], body: str) -> bool:
    if any("security" in label.lower() for label in labels):
        return True
    head = re.sub(r"<details>.*?</details>", "", body, flags=re.DOTALL)
    head = re.sub(r"https?://\S+", "", head)  # docs URLs mention "security-vulnerabilities"
    return bool(_SECURITY_BODY_RE.search(head))
