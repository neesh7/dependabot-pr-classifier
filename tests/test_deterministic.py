from src.classifier.deterministic import classify
from src.classifier.versions import is_major_gap, is_newer, is_prerelease, normalize
from src.schemas import DependencyUpdate, PRRecord


# ── version comparison ────────────────────────────────────────────────────

def test_numeric_not_string_comparison():
    assert is_newer("1.10.0", "1.9.0")       # string compare would say 1.9.0 wins
    assert is_newer("10.0.0", "9.99.99")


def test_equal_after_padding():
    assert not is_newer("2.31", "2.31.0")
    assert not is_newer("2.31.0", "2.31")


def test_requirement_operators_normalized():
    assert normalize("~=2.31") == "2.31"
    assert normalize("^1.2.3") == "1.2.3"
    assert normalize("v4.17.21") == "4.17.21"


def test_prerelease_detection():
    assert is_prerelease("3.0.0-beta.1")
    assert is_prerelease("2.0.0rc1")
    assert not is_prerelease("3.1.13")


def test_stable_newer_than_its_prerelease():
    assert is_newer("3.0.0", "3.0.0-beta.1")


def test_major_gap():
    assert is_major_gap("2.8.16", "3.1.13")
    assert not is_major_gap("13.0.1", "13.0.4")


# ── classifier ────────────────────────────────────────────────────────────

def make_record(pr_number: int, dep: str, to_ver: str, *, repo="o/r",
                manifest="app/app.csproj", updates=None, age_days=5) -> PRRecord:
    updates = updates or [DependencyUpdate(dependency=dep, from_ver="1.0.0", to_ver=to_ver)]
    return PRRecord(
        repo=repo, pr_number=pr_number, title=f"Bump {dep}", url="http://x",
        base_ref="main", age_days=age_days, mergeable="MERGEABLE", ci_status="SUCCESS",
        ecosystem="nuget", manifest_path=manifest, dependency=dep,
        from_ver="1.0.0", to_ver=to_ver, updates=updates,
    )


def no_cves(queries):
    return {q: [] for q in queries}


def test_duplicate_all_but_newest():
    records = [
        make_record(10, "Redis", "2.0.0"),
        make_record(12, "Redis", "3.0.0"),
    ]
    results = {c.record.pr_number: c for c in
               classify(records, lambda e, p: "3.0.0", no_cves)}
    assert results[10].status == "DUPLICATE"
    assert results[10].duplicate_overlaps[0].newer_pr == 12
    assert results[12].status == "CURRENT"


def test_monorepo_same_dep_two_manifests_not_duplicate():
    records = [
        make_record(10, "lodash", "4.17.21", manifest="frontend/package.json"),
        make_record(11, "lodash", "4.17.21", manifest="admin/package.json"),
    ]
    results = classify(records, lambda e, p: "4.17.21", no_cves)
    assert all(c.status == "CURRENT" for c in results)
    assert all(not c.duplicate_overlaps for c in results)


def test_stale_when_registry_ahead():
    records = [make_record(10, "axios", "1.6.0")]
    [c] = classify(records, lambda e, p: "1.7.2", no_cves)
    assert c.status == "STALE_CANDIDATE"
    assert c.assessments[0].is_behind
    assert c.assessments[0].latest_ver == "1.7.2"


def test_grouped_stale_if_any_member_behind():
    records = [make_record(10, "group", "x", updates=[
        DependencyUpdate(dependency="a", from_ver="1.0", to_ver="2.0"),
        DependencyUpdate(dependency="b", from_ver="1.0", to_ver="1.5"),
    ])]
    latest = {"a": "2.0", "b": "1.9"}
    [c] = classify(records, lambda e, p: latest[p], no_cves)
    assert c.status == "STALE_CANDIDATE"
    behind = [a.dependency for a in c.assessments if a.is_behind]
    assert behind == ["b"]


def test_cve_on_target_version_is_stale_signal():
    records = [make_record(10, "urllib3", "1.26.4")]

    def cves(queries):
        return {q: ["GHSA-xxxx-1234"] for q in queries}

    [c] = classify(records, lambda e, p: "1.26.4", cves)
    assert c.status == "STALE_CANDIDATE"
    assert c.assessments[0].target_cves == ["GHSA-xxxx-1234"]


def test_unknown_when_registry_unavailable():
    records = [make_record(10, "internal-pkg", "1.0.0")]
    [c] = classify(records, lambda e, p: None, no_cves)
    assert c.status == "UNKNOWN"


def test_partial_group_overlap_is_not_duplicate():
    grouped = make_record(10, "group", "x", updates=[
        DependencyUpdate(dependency="Redis", from_ver="2.8", to_ver="2.13"),
        DependencyUpdate(dependency="Npgsql", from_ver="8.0", to_ver="8.0.9"),
    ])
    single = make_record(16, "Redis", "3.1.13", updates=[
        DependencyUpdate(dependency="Redis", from_ver="2.8", to_ver="3.1.13"),
    ])
    latest = {"Redis": "3.1.13", "Npgsql": "8.0.9"}
    results = {c.record.pr_number: c for c in
               classify([grouped, single], lambda e, p: latest[p], no_cves)}
    # group shares Redis with newer #16 but Npgsql is its own -> not a full duplicate
    assert results[10].status == "STALE_CANDIDATE"  # its Redis 2.13 is behind 3.1.13
    assert [o.dependency for o in results[10].duplicate_overlaps] == ["Redis"]
    assert results[16].status == "CURRENT"
