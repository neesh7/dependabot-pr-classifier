"""Version comparison across ecosystems. Never string-compare versions."""

import re

from packaging.version import InvalidVersion, Version

_REQ_PREFIX = re.compile(r"^[~=^<>!\s]+")
_PAD = 4  # pad release tuples so "2.31" == "2.31.0"


def normalize(ver: str) -> str:
    """Strip requirement operators (~=2.31 → 2.31) and leading v."""
    return _REQ_PREFIX.sub("", ver.strip()).lstrip("vV")


def sort_key(ver: str) -> tuple:
    """Uniform comparable key for PEP440 and semver-ish strings alike."""
    v = normalize(ver)
    core, _, pre = v.partition("-")
    try:
        parsed = Version(core)
        release = parsed.release
        prerelease = parsed.is_prerelease or bool(pre)
    except InvalidVersion:
        release = tuple(int(p) if p.isdigit() else 0 for p in core.split("."))
        prerelease = bool(pre)
    padded = tuple(release) + (0,) * (_PAD - len(release))
    return (padded[:_PAD], 0 if prerelease else 1, pre)


def is_newer(a: str, b: str) -> bool:
    """True if version a is strictly newer than b."""
    return sort_key(a) > sort_key(b)


def is_prerelease(ver: str) -> bool:
    v = normalize(ver)
    try:
        return Version(v).is_prerelease
    except InvalidVersion:
        return "-" in v


def is_major_gap(a: str, b: str) -> bool:
    """True if a and b differ in major version (Phase 3 escalation signal)."""
    return sort_key(a)[0][0] != sort_key(b)[0][0]
