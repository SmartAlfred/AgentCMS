"""Content policy service (#17).

Evaluates content against a configurable, ordered set of policy rules on
every write.  Rules are loaded per-site from ``content_policies`` and
evaluated in ``priority`` order.  Each rule has a ``kind`` (check type)
and a ``severity`` (allow / warn / flag / block).

Checks implemented:
* ``blocked_terms`` — literal or regex terms from a per-site list
* ``link_density`` — outbound-link-per-300-words ratio
* ``prompt_injection`` — heuristics for injection / SEO spam
* ``duplicate`` — simhash near-duplicate detection (shared with #15)
* ``external_hook`` — POST to a configured URL (fail-open on timeout)

Design rules from the ticket:
* ``flag`` → content stored, not publishable until a human clears it
* ``block`` → write rejected with 422 ``CONTENT_POLICY_BLOCKED``, naming
  the rule; agents get a ``hint`` but never the raw blocklist
* silent drops are forbidden: agents always receive a reason
* external-hook timeout (3 s) never blocks and records an audit note
"""

from __future__ import annotations

import base64
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.models.content_policy import ContentPolicy, ModerationDecision
from app.models.post import Post

logger = logging.getLogger("app.content_policy")

# ---------------------------------------------------------------------------
# Default seed list (obvious spam categories)
# ---------------------------------------------------------------------------

DEFAULT_BLOCKED_TERMS: list[str] = [
    "buy now",
    "click here",
    "free money",
    "make money fast",
    "get rich quick",
    "affiliate link",
    "crypto airdrop",
    "guaranteed returns",
    "risk-free profit",
    "act now",
    "limited time offer",
    "congratulations you won",
    "winner announcement",
]

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

LINK_DENSITY_WARN_PER_300 = 5
LINK_DENSITY_BLOCK_PER_300 = 10
EXTERNAL_HOOK_TIMEOUT_SECONDS = 3.0

# ---------------------------------------------------------------------------
# Prompt-injection / SEO-spam patterns
# ---------------------------------------------------------------------------

_INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"ignore\s+(previous|all|above|prior)\s+(instructions?|prompts?|rules?)", re.I),
    re.compile(r"disregard\s+(previous|all|above|prior)\s+(instructions?|prompts?|rules?)", re.I),
    re.compile(r"you\s+are\s+now\s+(a|an)\s+", re.I),
    re.compile(r"new\s+instructions?:", re.I),
]

_HIDDEN_TEXT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"[\u200b\u200c\u200d\ufeff]"),  # zero-width chars
    re.compile(r"<!--[\s\S]*?-->"),  # HTML comments in markdown
    re.compile(r"<(?:div|span)\s[^>]*(?:display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0)", re.I),
]

_CAPS_RATIO_THRESHOLD = 0.6
_MIN_CAPS_LENGTH = 20
_BASE64_PATTERN = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")
_LINK_FARM_PATTERN = re.compile(r"\[url\]", re.I)


# ---------------------------------------------------------------------------
# Check result
# ---------------------------------------------------------------------------


@dataclass
class PolicyCheckResult:
    """Outcome of a single policy check."""

    rule_name: str
    rule_kind: str
    outcome: str  # allow / warn / flag / block
    evidence: str | None = None
    matched_post_id: uuid.UUID | None = None


@dataclass
class PolicyEvaluation:
    """Aggregated result of running all policy checks."""

    allowed: bool = True
    worst_outcome: str = "allow"
    checks: list[PolicyCheckResult] = field(default_factory=list)
    blocking_check: PolicyCheckResult | None = None

    def add(self, result: PolicyCheckResult) -> None:
        self.checks.append(result)
        if result.outcome == "block" and self.worst_outcome != "block":
            self.worst_outcome = "block"
            self.allowed = False
            self.blocking_check = result
        elif result.outcome == "flag" and self.worst_outcome not in ("block", "flag"):
            self.worst_outcome = "flag"


# ---------------------------------------------------------------------------
# Normalise content before analysis
# ---------------------------------------------------------------------------


def _normalise_for_analysis(body_md: str) -> str:
    """Strip zero-width characters, HTML comments, and collapse whitespace.

    The ticket requires that zero-width-character and HTML-comment payloads
    are detected and **normalised before analysis**.
    """
    text = body_md
    # Remove zero-width characters
    text = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", text)
    # Remove HTML comments
    text = re.sub(r"<!--[\s\S]*?-->", "", text)
    # Remove hidden HTML elements
    text = re.sub(
        r"<(?:div|span)\s[^>]*(?:display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0)[^>]*>[\s\S]*?</(?:div|span)>",
        "",
        text,
        flags=re.I,
    )
    # Collapse multiple newlines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_blocked_terms(
    body_md: str,
    blocked_terms: list[str],
    *,
    site_id: uuid.UUID,
    session: Session,
) -> list[PolicyCheckResult]:
    """Check for blocked literal terms and regex patterns."""
    results: list[PolicyCheckResult] = []
    normalised = _normalise_for_analysis(body_md.lower())

    for term in blocked_terms:
        term = term.strip()
        if not term:
            continue

        # Try as regex first, fall back to literal
        matched = False
        evidence_match: str | None = None
        try:
            pattern = re.compile(term, re.I)
            m = pattern.search(normalised)
            if m:
                matched = True
                # Grab surrounding context for evidence
                start = max(0, m.start() - 20)
                end = min(len(normalised), m.end() + 20)
                evidence_match = f"...{normalised[start:end].strip()}..."
        except re.error:
            # Not a valid regex, treat as literal
            if term.lower() in normalised:
                matched = True
                idx = normalised.index(term.lower())
                start = max(0, idx - 20)
                end = min(len(normalised), idx + len(term) + 20)
                evidence_match = f"...{normalised[start:end].strip()}..."

        if matched:
            results.append(
                PolicyCheckResult(
                    rule_name=f"blocked_term:{term[:64]}",
                    rule_kind="blocked_terms",
                    outcome="block",
                    evidence=evidence_match,
                )
            )

    return results


def _check_link_density(body_md: str, is_agent_actor: bool) -> list[PolicyCheckResult]:
    """Check outbound link density per 300 words."""
    results: list[PolicyCheckResult] = []
    # Count words
    words = re.findall(r"\w+", body_md)
    word_count = len(words)
    if word_count == 0:
        return results

    # Count outbound links (markdown format)
    links = re.findall(r"(?<!!)\[([^\]]*)\]\(([^)]+)\)", body_md)
    # Also count raw URLs not inside markdown links
    raw_urls = re.findall(r"(?<!\]\()(https?://\S+)", body_md)
    link_count = len(links) + len(raw_urls)

    if link_count == 0:
        return results

    links_per_300 = (link_count / word_count) * 300

    if links_per_300 > LINK_DENSITY_BLOCK_PER_300 and is_agent_actor:
        results.append(
            PolicyCheckResult(
                rule_name="link_density",
                rule_kind="link_density",
                outcome="block",
                evidence=f"{link_count} links / {word_count} words ({links_per_300:.1f} per 300 words)",
            )
        )
    elif links_per_300 > LINK_DENSITY_WARN_PER_300:
        outcome = "block" if is_agent_actor else "flag"
        results.append(
            PolicyCheckResult(
                rule_name="link_density",
                rule_kind="link_density",
                outcome=outcome,
                evidence=f"{link_count} links / {word_count} words ({links_per_300:.1f} per 300 words)",
            )
        )

    return results


def _check_prompt_injection(body_md: str) -> list[PolicyCheckResult]:
    """Detect prompt-injection and SEO-spam heuristics."""
    results: list[PolicyCheckResult] = []
    normalised = _normalise_for_analysis(body_md)

    # Prompt injection patterns
    for pattern in _INJECTION_PATTERNS:
        m = pattern.search(normalised)
        if m:
            start = max(0, m.start() - 20)
            end = min(len(normalised), m.end() + 20)
            results.append(
                PolicyCheckResult(
                    rule_name="prompt_injection",
                    rule_kind="prompt_injection",
                    outcome="block",
                    evidence=f"matched: {normalised[start:end].strip()!r}",
                )
            )
            break  # One hit is enough

    # Hidden text markers (zero-width chars, HTML comments)
    for pattern in _HIDDEN_TEXT_PATTERNS:
        m = pattern.search(body_md)  # Check original, not normalised
        if m:
            results.append(
                PolicyCheckResult(
                    rule_name="hidden_text",
                    rule_kind="prompt_injection",
                    outcome="flag",
                    evidence=f"hidden text marker detected at position {m.start()}",
                )
            )
            break

    # Excessive ALL-CAPS words
    words = re.findall(r"[A-Z]{2,}", normalised)
    caps_words = [w for w in words if len(w) >= 3]
    total_words = len(re.findall(r"\w+", normalised))
    if (
        total_words > 0
        and len(caps_words) / max(total_words, 1) > _CAPS_RATIO_THRESHOLD
        and len(caps_words) > 5
    ):
        results.append(
            PolicyCheckResult(
                rule_name="excessive_caps",
                rule_kind="prompt_injection",
                outcome="flag",
                evidence=f"{len(caps_words)} ALL-CAPS words out of {total_words} total",
            )
        )

    # Base64 blobs
    b64_matches = _BASE64_PATTERN.findall(normalised)
    for match in b64_matches:
        # Only flag if it decodes to something meaningful
        try:
            decoded = base64.b64decode(match)
            if len(decoded) > 20:
                results.append(
                    PolicyCheckResult(
                        rule_name="base64_blob",
                        rule_kind="prompt_injection",
                        outcome="flag",
                        evidence=(
                            f"base64 blob detected ({len(match)} chars, decodes to {len(decoded)} bytes)"
                        ),
                    )
                )
                break
        except Exception:
            pass

    # Link farms [url]
    if _LINK_FARM_PATTERN.search(normalised):
        results.append(
            PolicyCheckResult(
                rule_name="link_farm",
                rule_kind="prompt_injection",
                outcome="block",
                evidence="[url] link farm pattern detected",
            )
        )

    # Walls of keywords (high ratio of unique words to total words in short text)
    if total_words > 0 and total_words < 200:
        unique_words = len(set(re.findall(r"\w+", normalised.lower())))
        ratio = unique_words / total_words
        if ratio > 0.9 and total_words > 30:
            results.append(
                PolicyCheckResult(
                    rule_name="keyword_wall",
                    rule_kind="prompt_injection",
                    outcome="flag",
                    evidence=f"{unique_words} unique / {total_words} total words (ratio {ratio:.2f})",
                )
            )

    return results


def _check_duplicate(
    body_md: str,
    site_id: uuid.UUID,
    session: Session,
    existing_post_id: uuid.UUID | None = None,
) -> list[PolicyCheckResult]:
    """Near-duplicate detection using simhash (shared with #15 validation)."""
    from app.services.validation import _simhash, _similarity

    results: list[PolicyCheckResult] = []
    normalised = _normalise_for_analysis(body_md)

    recent_query = session.query(Post.id, Post.body_md).filter(
        Post.site_id == site_id,
        Post.deleted_at.is_(None),
    )
    if existing_post_id is not None:
        recent_query = recent_query.filter(Post.id != existing_post_id)
    recent_posts = recent_query.order_by(Post.created_at.desc()).limit(50).all()

    current_hash = _simhash(normalised)
    for recent_id, recent_body in recent_posts:
        if not recent_body:
            continue
        sim = _similarity(current_hash, _simhash(recent_body))
        if sim >= 0.85:  # Stricter than validation warning threshold (0.65)
            results.append(
                PolicyCheckResult(
                    rule_name="duplicate_detection",
                    rule_kind="duplicate",
                    outcome="flag",
                    evidence=f"simhash similarity {sim:.0%} with post {recent_id}",
                    matched_post_id=recent_id,
                )
            )
            break

    return results


def _check_external_hook(
    body_md: str,
    hook_url: str,
) -> list[PolicyCheckResult]:
    """POST to an external classifier (fail-open on timeout or error)."""
    results: list[PolicyCheckResult] = []
    try:
        resp = httpx.post(
            hook_url,
            json={"body_md": body_md},
            timeout=EXTERNAL_HOOK_TIMEOUT_SECONDS,
        )
        if resp.status_code >= 400:
            results.append(
                PolicyCheckResult(
                    rule_name="external_hook_error",
                    rule_kind="external_hook",
                    outcome="flag",
                    evidence=f"external hook returned {resp.status_code}",
                )
            )
        else:
            data = resp.json()
            verdict = data.get("verdict", "allow")
            if verdict in ("flag", "block"):
                results.append(
                    PolicyCheckResult(
                        rule_name="external_hook",
                        rule_kind="external_hook",
                        outcome=verdict,
                        evidence=data.get("reason", "external classifier flagged content"),
                    )
                )
    except httpx.TimeoutException:
        results.append(
            PolicyCheckResult(
                rule_name="external_hook_timeout",
                rule_kind="external_hook",
                outcome="flag",
                evidence="external classifier timed out (fail-open)",
            )
        )
    except Exception:
        results.append(
            PolicyCheckResult(
                rule_name="external_hook_error",
                rule_kind="external_hook",
                outcome="flag",
                evidence="external classifier unreachable (fail-open)",
            )
        )

    return results


# ---------------------------------------------------------------------------
# Main evaluation entry point
# ---------------------------------------------------------------------------


def evaluate_content_policy(
    session: Session,
    *,
    site_id: uuid.UUID,
    body_md: str,
    is_agent_actor: bool = True,
    existing_post_id: uuid.UUID | None = None,
    exclude_post_id: uuid.UUID | None = None,
    audit_ctx: dict[str, Any] | None = None,
) -> PolicyEvaluation:
    """Run all active content policies for a site against the given body.

    Returns a ``PolicyEvaluation`` with the aggregated outcome.  Does NOT
    persist anything — the caller decides what to do with the result.
    """
    evaluation = PolicyEvaluation()

    # Load active policies for this site, ordered by priority
    policies = (
        session.query(ContentPolicy)
        .filter(ContentPolicy.site_id == site_id, ContentPolicy.enabled.is_(True))
        .order_by(ContentPolicy.priority.asc(), ContentPolicy.created_at.asc())
        .all()
    )

    blocked_terms: list[str] = list(DEFAULT_BLOCKED_TERMS)
    hook_url: str | None = None

    # Collect config from policies
    for policy in policies:
        config = policy.config or {}
        if policy.kind == "blocked_terms":
            custom_terms = config.get("terms", [])
            if isinstance(custom_terms, list):
                blocked_terms.extend(custom_terms)
        elif policy.kind == "external_hook":
            url = config.get("url")
            if isinstance(url, str) and url:
                hook_url = url

    # Always run built-in checks regardless of policy configuration
    # 1. Blocked terms (always runs with seed list + per-site terms)
    for result in _check_blocked_terms(body_md, blocked_terms, site_id=site_id, session=session):
        evaluation.add(result)

    # 2. Link density
    for result in _check_link_density(body_md, is_agent_actor):
        evaluation.add(result)

    # 3. Prompt injection / SEO spam heuristics
    for result in _check_prompt_injection(body_md):
        evaluation.add(result)

    # 4. Duplicate detection
    for result in _check_duplicate(body_md, site_id, session, existing_post_id or exclude_post_id):
        evaluation.add(result)

    # 5. External hook (optional, only if configured)
    if hook_url:
        for result in _check_external_hook(body_md, hook_url):
            evaluation.add(result)

    return evaluation


def record_moderation_decision(
    session: Session,
    *,
    post_id: uuid.UUID,
    check: PolicyCheckResult,
    policy_id: uuid.UUID | None = None,
) -> ModerationDecision:
    """Persist a moderation decision for a flagged or blocked write."""
    decision = ModerationDecision(
        id=uuid.uuid4(),
        post_id=post_id,
        policy_id=policy_id,
        rule_name=check.rule_name,
        rule_kind=check.rule_kind,
        outcome=check.outcome,
        evidence=check.evidence,
        matched_post_id=check.matched_post_id,
    )
    session.add(decision)
    session.flush()
    return decision


def list_flagged_decisions(
    session: Session,
    *,
    site_id: uuid.UUID | None = None,
    cleared: bool | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[list[ModerationDecision], str | None]:
    """List moderation decisions for the Security dashboard."""
    query = session.query(ModerationDecision)

    if site_id is not None:
        query = query.join(Post, ModerationDecision.post_id == Post.id).filter(Post.site_id == site_id)
    if cleared is not None:
        query = query.filter(ModerationDecision.cleared == cleared)

    if cursor:
        try:
            cursor_id = uuid.UUID(cursor)
            query = query.filter(ModerationDecision.id < cursor_id)
        except ValueError:
            pass

    query = query.order_by(ModerationDecision.created_at.desc(), ModerationDecision.id.desc())
    items = query.limit(limit + 1).all()

    next_cursor: str | None = None
    if len(items) > limit:
        last = items[-2]
        next_cursor = str(last.id)
        items = items[:limit]

    return items, next_cursor


def clear_decision(
    session: Session,
    decision_id: uuid.UUID,
    *,
    actor_id: uuid.UUID,
) -> ModerationDecision:
    """Clear a flagged decision (human action)."""
    decision = session.query(ModerationDecision).filter(ModerationDecision.id == decision_id).first()
    if decision is None:
        from app.domain.errors import NotFoundError

        raise NotFoundError(f"Moderation decision '{decision_id}' not found.")
    decision.cleared = True
    decision.cleared_by_actor_id = actor_id
    from datetime import UTC, datetime

    decision.cleared_at = datetime.now(UTC)
    session.flush()
    return decision


def block_decision(
    session: Session,
    decision_id: uuid.UUID,
) -> ModerationDecision:
    """Escalate a flagged decision to blocked."""
    decision = session.query(ModerationDecision).filter(ModerationDecision.id == decision_id).first()
    if decision is None:
        from app.domain.errors import NotFoundError

        raise NotFoundError(f"Moderation decision '{decision_id}' not found.")
    decision.blocked = True
    decision.outcome = "block"
    session.flush()
    return decision


def is_post_publishable(session: Session, post_id: uuid.UUID) -> bool:
    """Check if a post has any un-cleared flag/block decisions."""
    count = (
        session.query(ModerationDecision)
        .filter(
            ModerationDecision.post_id == post_id,
            ModerationDecision.cleared.is_(False),
            ModerationDecision.outcome.in_(["flag", "block"]),
        )
        .count()
    )
    return count == 0
