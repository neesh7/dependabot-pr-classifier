"""Deterministic tagging: DUPLICATE / STALE_CANDIDATE / CURRENT / UNKNOWN.

Runs before any AI. Repo is the isolation boundary: duplicates are only
detected within the same (repo, manifest_path, dependency).
"""

from collections.abc import Callable

from src.classifier.versions import is_newer
from src.schemas import ClassifiedPR, DuplicateOverlap, PRRecord, UpdateAssessment

LatestLookup = Callable[[str, str], str | None]                      # (ecosystem, pkg) -> ver
CveLookup = Callable[[list[tuple[str, str, str]]], dict[tuple[str, str, str], list[str]]]


def _find_duplicates(records: list[PRRecord]) -> dict[int, list[DuplicateOverlap]]:
    """Per PR: which of its deps are also bumped by a newer open PR (same repo+manifest)."""
    by_key: dict[tuple, list[PRRecord]] = {}
    for rec in records:
        for upd in rec.updates:
            by_key.setdefault((rec.repo, rec.manifest_path, upd.dependency), []).append(rec)

    overlaps: dict[int, list[DuplicateOverlap]] = {}
    for (_, _, dep), recs in by_key.items():
        if len({r.pr_number for r in recs}) < 2:
            continue
        newest = max(recs, key=lambda r: r.pr_number)
        for rec in recs:
            if rec.pr_number != newest.pr_number:
                overlaps.setdefault(rec.pr_number, []).append(
                    DuplicateOverlap(dependency=dep, newer_pr=newest.pr_number))
    return overlaps


def classify(records: list[PRRecord], latest_lookup: LatestLookup,
             cve_lookup: CveLookup) -> list[ClassifiedPR]:
    overlaps = _find_duplicates(records)

    # one OSV batch for every target version across all PRs
    cve_queries = [(rec.ecosystem or "", upd.dependency, upd.to_ver or "")
                   for rec in records for upd in rec.updates]
    cves = cve_lookup(cve_queries)

    classified = []
    for rec in records:
        assessments = []
        for upd in rec.updates:
            latest = latest_lookup(rec.ecosystem, upd.dependency) if rec.ecosystem else None
            assessments.append(UpdateAssessment(
                dependency=upd.dependency,
                from_ver=upd.from_ver,
                to_ver=upd.to_ver,
                latest_ver=latest,
                is_behind=bool(latest and upd.to_ver and is_newer(latest, upd.to_ver)),
                target_cves=cves.get((rec.ecosystem or "", upd.dependency, upd.to_ver or ""), []),
            ))

        pr_overlaps = overlaps.get(rec.pr_number, [])
        fully_duplicated = (bool(rec.updates)
                            and len(pr_overlaps) == len(rec.updates))
        if fully_duplicated:
            status = "DUPLICATE"
        elif any(a.is_behind or a.target_cves for a in assessments):
            status = "STALE_CANDIDATE"
        elif assessments and all(a.latest_ver for a in assessments):
            status = "CURRENT"
        else:
            status = "UNKNOWN"  # unparseable PR or registry data unavailable

        classified.append(ClassifiedPR(record=rec, status=status,
                                       duplicate_overlaps=pr_overlaps,
                                       assessments=assessments))
    return classified
