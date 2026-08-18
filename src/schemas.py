"""Pydantic models: the data contracts between pipeline stages.

DependencyUpdate — one dependency bump (a grouped PR has several)
ParsedTitle      — what pr_parser extracts from a PR title
PRRecord         — collector output (one open Dependabot PR, fully structured)
Verdict          — the model's structured answer for a STALE_CANDIDATE (Phase 3)
"""

from typing import Literal

from pydantic import BaseModel, Field, ValidationError  # noqa: F401  (re-exported)


class DependencyUpdate(BaseModel):
    dependency: str
    from_ver: str | None = None
    to_ver: str | None = None


class ParsedTitle(BaseModel):
    kind: Literal["bump", "requirement", "group", "unknown"]
    dependency: str | None = None
    from_ver: str | None = None
    to_ver: str | None = None
    directory: str | None = None  # the "in /frontend" suffix, when present
    group_name: str | None = None
    update_count: int | None = None


class PRRecord(BaseModel):
    repo: str
    pr_number: int
    title: str
    url: str
    base_ref: str
    age_days: int = Field(ge=0)
    mergeable: Literal["MERGEABLE", "CONFLICTING", "UNKNOWN"]
    ci_status: Literal["SUCCESS", "FAILURE", "PENDING", "ERROR", "EXPECTED", "UNKNOWN"]
    labels: list[str] = []
    is_security: bool = False
    is_grouped: bool = False
    files: list[str] = []
    # parsed fields
    ecosystem: str | None = None
    manifest_path: str | None = None
    dependency: str | None = None  # None for grouped PRs — see `updates`
    from_ver: str | None = None
    to_ver: str | None = None
    group_name: str | None = None
    updates: list[DependencyUpdate] = []  # every dep in the PR (1 for plain bumps)
    # body extracts
    release_notes_excerpt: str = ""
    changelog_links: list[str] = []
    compatibility_score_url: str | None = None
    body_excerpt: str = ""


class UpdateAssessment(BaseModel):
    """Staleness check for one dependency bump inside a PR."""

    dependency: str
    from_ver: str | None = None
    to_ver: str | None = None
    latest_ver: str | None = None  # None = registry lookup unavailable
    is_behind: bool = False        # latest_ver > to_ver
    target_cves: list[str] = []    # known vulns in the PR's TARGET version


class DuplicateOverlap(BaseModel):
    dependency: str
    newer_pr: int


class VerdictMeta(BaseModel):
    """Provenance for one AI verdict (goes to the audit log)."""

    model: str
    prompt_version: str
    cache_hit: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    tool_iterations: int = 0


class RunStats(BaseModel):
    """Per-run AI accounting, surfaced in the digest for token/cost monitoring."""

    stale_analyzed: int = 0
    cache_hits: int = 0       # verdicts reused from cache (cost 0 tokens this run)
    api_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class ClassifiedPR(BaseModel):
    """Deterministic classifier output for one PR; verdict added for stale ones."""

    record: PRRecord
    status: Literal["DUPLICATE", "STALE_CANDIDATE", "CURRENT", "UNKNOWN"]
    duplicate_overlaps: list[DuplicateOverlap] = []  # deps also bumped by a newer PR
    assessments: list[UpdateAssessment] = []
    verdict: "Verdict | None" = None          # only for STALE_CANDIDATE
    verdict_meta: VerdictMeta | None = None


class Verdict(BaseModel):
    """Schema the model must return. Parsed with model_validate_json; one retry on failure."""

    verdict: Literal["SUPERSEDED", "STILL_VALID", "NEEDS_HUMAN"]
    recommended_action: str  # "recreate to 4.18.2" | "merge as-is" | "manual review"
    risk_of_newer_version: Literal["low", "medium", "high"]
    breaking_changes_in_gap: list[str] = []
    additional_cves_fixed: list[str] = []
    reasoning: str = Field(description="2-3 sentences max")


ClassifiedPR.model_rebuild()  # resolve the forward ref to Verdict
