import json
from pathlib import Path

import pytest

from src.collector.pr_parser import (
    detect_ecosystem,
    extract_body_meta,
    is_security,
    parse_body_updates,
    parse_title,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ── title variants ────────────────────────────────────────────────────────

def test_simple_bump():
    p = parse_title("Bump lodash from 4.17.20 to 4.17.21")
    assert p.kind == "bump"
    assert (p.dependency, p.from_ver, p.to_ver) == ("lodash", "4.17.20", "4.17.21")
    assert p.directory is None


def test_bump_with_manifest_path():
    p = parse_title("Bump lodash from 4.17.20 to 4.17.21 in /frontend")
    assert p.kind == "bump"
    assert p.dependency == "lodash"
    assert p.directory == "/frontend"


def test_requirement_update():
    p = parse_title("Update requests requirement from ~=2.25 to ~=2.31")
    assert p.kind == "requirement"
    assert (p.dependency, p.from_ver, p.to_ver) == ("requests", "~=2.25", "~=2.31")


def test_grouped():
    p = parse_title("Bump the npm_and_yarn group with 3 updates")
    assert p.kind == "group"
    assert p.group_name == "npm_and_yarn"
    assert p.update_count == 3
    assert p.dependency is None


def test_grouped_with_path():
    p = parse_title("Bump the pip group in /backend with 2 updates")
    assert p.kind == "group"
    assert (p.group_name, p.directory, p.update_count) == ("pip", "/backend", 2)


def test_grouped_across_directories():
    p = parse_title("Bump the npm_and_yarn group across 2 directories with 4 updates")
    assert p.kind == "group"
    assert (p.group_name, p.update_count) == ("npm_and_yarn", 4)


def test_conventional_commit_prefix():
    p = parse_title("chore(deps): bump axios from 1.5.0 to 1.6.0")
    assert p.kind == "bump"
    assert (p.dependency, p.from_ver, p.to_ver) == ("axios", "1.5.0", "1.6.0")


def test_scoped_npm_package():
    p = parse_title("Bump @types/node from 20.1.0 to 20.2.0")
    assert p.dependency == "@types/node"


def test_github_action_dependency():
    p = parse_title("Bump actions/checkout from 3 to 4")
    assert (p.dependency, p.from_ver, p.to_ver) == ("actions/checkout", "3", "4")


def test_unparseable_title():
    assert parse_title("Fix typo in README").kind == "unknown"


# ── real fixtures ─────────────────────────────────────────────────────────

def test_real_grouped_pr():
    fx = load_fixture("pr_grouped_nuget.json")
    p = parse_title(fx["title"])
    assert p.kind == "group"
    assert p.group_name == "worker-minor-patch"
    assert p.update_count == 3

    updates = parse_body_updates(fx["body"])
    assert {u.dependency for u in updates} == {"Newtonsoft.Json", "Npgsql", "StackExchange.Redis"}
    by_dep = {u.dependency: u for u in updates}
    assert by_dep["Newtonsoft.Json"].from_ver == "13.0.1"
    assert by_dep["Newtonsoft.Json"].to_ver == "13.0.4"
    assert by_dep["StackExchange.Redis"].to_ver == "2.13.17"

    assert detect_ecosystem(fx["files"]) == ("nuget", "worker/Worker.csproj")
    assert not is_security(fx["labels"], fx["body"])


def test_real_major_bump_pr():
    fx = load_fixture("pr_major_bump_nuget.json")
    p = parse_title(fx["title"])
    assert p.kind == "bump"
    assert (p.dependency, p.from_ver, p.to_ver) == ("StackExchange.Redis", "2.8.16", "3.1.13")

    meta = extract_body_meta(fx["body"])
    assert meta["compatibility_score_url"] is not None
    assert "compatibility_score" in meta["compatibility_score_url"]
    # body-derived updates must match the title
    updates = parse_body_updates(fx["body"])
    assert updates[0].dependency == "StackExchange.Redis"
    assert updates[0].to_ver == "3.1.13"
    assert not is_security(fx["labels"], fx["body"])


# ── body parsing ──────────────────────────────────────────────────────────

def test_body_updates_ignore_release_notes():
    body = (
        "Updates `axios` from 1.5.0 to 1.6.0\n"
        "<details><summary>Release notes</summary>\n"
        "Bumps something-unrelated from 0.1 to 0.2\n"
        "</details>\n"
        "Updates `lodash` from 4.17.20 to 4.17.21\n"
    )
    updates = parse_body_updates(body)
    assert [u.dependency for u in updates] == ["axios", "lodash"]


def test_release_notes_excerpt():
    fx = load_fixture("pr_grouped_nuget.json")
    meta = extract_body_meta(fx["body"])
    assert "13.0.4" in meta["release_notes_excerpt"]
    assert len(meta["release_notes_excerpt"]) <= 1500


def test_security_via_label():
    assert is_security(["dependencies", "security"], "")


def test_security_via_body_marker():
    body = "Bumps urllib3 from 1.26.4 to 1.26.18. **This update includes security fixes.**"
    assert is_security([], body)


def test_docs_link_is_not_security_signal():
    body = ("Bumps x from 1 to 2. [score](https://docs.github.com/en/github/"
            "managing-security-vulnerabilities/about-dependabot-security-updates)")
    assert not is_security([], body)


# ── ecosystem detection ───────────────────────────────────────────────────

@pytest.mark.parametrize("files,expected", [
    (["frontend/package.json", "frontend/package-lock.json"], ("npm", "frontend/package.json")),
    (["requirements.txt"], ("pip", "requirements.txt")),
    (["requirements-dev.txt"], ("pip", "requirements-dev.txt")),
    (["worker/Worker.csproj"], ("nuget", "worker/Worker.csproj")),
    ([".github/workflows/ci.yml"], ("github-actions", ".github/workflows/ci.yml")),
    (["yarn.lock"], ("npm", "yarn.lock")),  # lockfile-only PR
    (["go.mod", "go.sum"], ("gomod", "go.mod")),
    (["README.md"], (None, None)),
])
def test_detect_ecosystem(files, expected):
    assert detect_ecosystem(files) == expected
