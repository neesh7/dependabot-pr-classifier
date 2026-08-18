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
from src.log import log_event
from src.schemas import DependencyUpdate, PRRecord, RunStats


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
            prs = fetch_dependabot_prs(client, repo, config.github_api_url)
            records.extend(build_record(repo, pr) for pr in prs)
            log_event("collected", repo=repo, open_prs=len(prs))
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


def run(config: Config | None = None, no_ai: bool = False) -> str:
    """Full pipeline: collect → classify → AI verdicts → digest + audit log.

    Returns the digest Markdown. This is what the Azure Function timer calls.
    """
    from pathlib import Path

    from src.report.digest import post_to_teams, render_digest
    from src.storage.audit_log import AuditLog

    config = config or load_config()
    records = collect(config)
    registry, osv = RegistryClient(), OSVClient()
    results = classify(records, registry.latest_stable, osv.query_batch)
    counts: dict[str, int] = {}
    for c in results:
        counts[c.status] = counts.get(c.status, 0) + 1
    log_event("classified", **counts)

    stats = None if no_ai else run_ai(results, config)

    audit = AuditLog(config.audit_log_path)
    for cp in results:
        audit.append(cp)

    digest = render_digest(results, stats)
    out = Path(config.digest_output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(digest, encoding="utf-8")
    log_event("digest_written", path=str(out), prs=len(results))
    if config.teams_webhook_url:
        post_to_teams(config.teams_webhook_url, digest)
        log_event("teams_posted")
    return digest


def run_ai(results: list, config: Config) -> RunStats:
    """Attach model verdicts to STALE_CANDIDATEs, via the cross-repo cache."""
    stale = [c for c in results if c.status == "STALE_CANDIDATE"]
    if not stale:
        return RunStats()
    from src.llm.llm_client import LLMClient
    from src.storage.verdict_cache import VerdictCache

    cache = VerdictCache(config.verdict_cache_path)
    llm = LLMClient(config)
    hits = 0
    for cp in stale:
        key = VerdictCache.key_for(cp)
        if cached := cache.get(key):
            cp.verdict, cp.verdict_meta = cached
            hits += 1
        else:
            cp.verdict, cp.verdict_meta = llm.analyze(cp)
            cache.put(key, cp.verdict, cp.verdict_meta)
    stats = RunStats(stale_analyzed=len(stale), cache_hits=hits, api_calls=llm.api_calls,
                     input_tokens=llm.input_tokens, output_tokens=llm.output_tokens,
                     model=config.model_default)
    log_event("ai_stage", stale=len(stale), cache_hits=hits, api_calls=llm.api_calls,
              input_tokens=llm.input_tokens, output_tokens=llm.output_tokens,
              total_tokens=stats.total_tokens)
    return stats


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Dependabot AI triage")
    parser.add_argument("--collect-only", action="store_true",
                        help="collect PRs and dump JSON, no classification")
    parser.add_argument("--grouped", action="store_true",
                        help="output grouped by (repo, ecosystem, manifest, dependency)")
    parser.add_argument("--no-ai", action="store_true",
                        help="deterministic classification only, zero LLM calls")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):  # Windows consoles default to cp1252
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if args.grouped or args.collect_only:
        records = collect(load_config())
        if args.grouped:
            print(json.dumps(group_records(records), indent=2))
        else:
            print(json.dumps([r.model_dump() for r in records], indent=2))
        return

    digest = run(no_ai=args.no_ai)
    print(digest)


if __name__ == "__main__":
    main()
