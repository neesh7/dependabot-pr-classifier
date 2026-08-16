"""Orchestrator: collect → (classify → analyze → report, later phases).

Usage: python -m src.main [--collect-only] [--grouped]
Default (no flags): collect + deterministic classification.
"""

import argparse
import json
import sys
from datetime import datetime, timezone

from src.classifier.deterministic import classify
from src.classifier.osv_client import OSVClient
from src.classifier.registry_client import RegistryClient
from src.collector import pr_parser
from src.collector.github_client import fetch_dependabot_prs, make_client
from src.config import Config, load_config
from src.schemas import DependencyUpdate, PRRecord


def _age_days(created_at: str) -> int:
    created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - created).days


def _ci_status(pr: dict) -> str:
    commits = pr["commits"]["nodes"]
    rollup = commits[0]["commit"]["statusCheckRollup"] if commits else None
    return rollup["state"] if rollup else "UNKNOWN"


def build_record(repo: str, pr: dict) -> PRRecord:
    title, body = pr["title"], pr.get("body") or ""
    labels = [l["name"] for l in pr["labels"]["nodes"]]
    files = [f["path"] for f in pr["files"]["nodes"]]

    parsed = pr_parser.parse_title(title)
    ecosystem, manifest_path = pr_parser.detect_ecosystem(files)
    updates = pr_parser.parse_body_updates(body)
    if parsed.dependency and not updates:
        updates = [DependencyUpdate(dependency=parsed.dependency,
                                    from_ver=parsed.from_ver, to_ver=parsed.to_ver)]
    meta = pr_parser.extract_body_meta(body)

    return PRRecord(
        repo=repo,
        pr_number=pr["number"],
        title=title,
        url=pr["url"],
        base_ref=pr["baseRefName"],
        age_days=_age_days(pr["createdAt"]),
        mergeable=pr["mergeable"],
        ci_status=_ci_status(pr),
        labels=labels,
        is_security=pr_parser.is_security(labels, body),
        is_grouped=parsed.kind == "group",
        files=files,
        ecosystem=ecosystem,
        manifest_path=manifest_path or parsed.directory,
        dependency=parsed.dependency,
        from_ver=parsed.from_ver,
        to_ver=parsed.to_ver,
        group_name=parsed.group_name,
        updates=updates,
        body_excerpt=body[:2000],
        **meta,
    )


def collect(config: Config) -> list[PRRecord]:
    records = []
    with make_client(config.github_token) as client:
        for repo in config.repos:
            prs = fetch_dependabot_prs(client, repo)
            records.extend(build_record(repo, pr) for pr in prs)
            print(f"{repo}: {len(prs)} open Dependabot PRs", file=sys.stderr)
    return records


def group_records(records: list[PRRecord]) -> dict:
    """(repo) → "ecosystem|manifest|dependency" → [PR summaries]. Duplicate-detection input."""
    grouped: dict = {}
    for rec in records:
        for upd in rec.updates or [DependencyUpdate(dependency="?")]:
            key = f"{rec.ecosystem}|{rec.manifest_path}|{upd.dependency}"
            grouped.setdefault(rec.repo, {}).setdefault(key, []).append({
                "pr_number": rec.pr_number,
                "from_ver": upd.from_ver,
                "to_ver": upd.to_ver,
                "age_days": rec.age_days,
                "url": rec.url,
            })
    return grouped


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Dependabot AI triage")
    parser.add_argument("--collect-only", action="store_true",
                        help="collect PRs and dump JSON, no classification")
    parser.add_argument("--grouped", action="store_true",
                        help="output grouped by (repo, ecosystem, manifest, dependency)")
    args = parser.parse_args(argv)

    records = collect(load_config())
    if args.grouped:
        print(json.dumps(group_records(records), indent=2))
    elif args.collect_only:
        print(json.dumps([r.model_dump() for r in records], indent=2))
    else:
        registry, osv = RegistryClient(), OSVClient()
        results = classify(records, registry.latest_stable, osv.query_batch)
        counts: dict[str, int] = {}
        for c in results:
            counts[c.status] = counts.get(c.status, 0) + 1
        print(f"classified: {counts}", file=sys.stderr)
        print(json.dumps([c.model_dump() for c in results], indent=2))


if __name__ == "__main__":
    main()
