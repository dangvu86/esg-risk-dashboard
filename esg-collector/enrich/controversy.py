"""
Controversy classifier for ESG events with severity == "Cao".

Ported from cloud-function/controversy_classifier.py.
Key differences from the original:
  - No Jina/URL fetching — body is passed in directly from the DB.
  - classify_events() removed — the runner calls classify_event() per row.
  - LLM calls delegated to enrich.llm (resolve_provider, call_llm).
  - Revenue lookup via enrich.revenue.get_revenue_for_year.

Output per event: {level, cg_indicator, justification, confidence}
  level         in {"Major", "Minor", "No"}
  cg_indicator  in {1, 2, 3, 4, None}  (only set when type == "G")
  justification 2-sentence English
  confidence    int 0-100
"""

from __future__ import annotations

from datetime import datetime

from enrich.llm import call_llm  # noqa: F401 — exposed for monkeypatching in tests
from enrich.revenue import get_revenue_for_year


VALID_LEVELS = {"Major", "Minor", "No"}
ARTICLE_BODY_MAX_CHARS = 6000
SCALE_RULE_THRESHOLD_PCT = 20


CLASSIFY_PROMPT = """You are an ESG analyst specializing in Vietnamese corporate controversies.

EVENT TO CLASSIFY:
- Ticker: {ticker}
- Company: {company}
- Type: {type}  ("E" = Environmental, "S" = Social, "G" = Governance)
- Date: {date}
- Title (VI): {summary}
- Title (EN): {summary_en}
- Source: {source}

ARTICLE BODY (markdown from source, may include navigation/boilerplate — focus on article content):
{article_body}

COMPANY SCALE (for 20% rule):
- Event year: {event_year}
- Company annual revenue (reporting year {revenue_year}): {revenue_display}

DEFINITIONS (apply strictly):

For E and S events:
- Major = public report in past 5 years + NO evidence of resolution + material consequences for community/worker/environment
- Minor = (i) legal action / non-compliance WITHOUT material consequences (poor mgmt control), OR (ii) legacy major (5-10 years old) without resolution
- No = (i) no reports in past 10 years, OR (ii) evidence of resolution exists

Material consequences markers (E/S):
- Deaths / injuries to workers or community
- Wide-scope pollution with community impact
- Operation suspension / license revocation
- Fines > 1 billion VND
- Central government agency involvement (Bộ TN&MT, Bộ Công an, Tổng cục)
- Criminal prosecution

Resolution markers (VN keywords): "đã đóng phạt", "đã khắc phục", "vụ án đình chỉ", "Tòa kết luận", "tái bổ nhiệm", "đã giải quyết"

For G events, first map to one of 4 CG indicators:
1. Bribery, corruption, business ethics  (keywords: hối lộ, tham nhũng)
2. Accurate financial reporting          (keywords: sai lệch BCTC, audit qualified, late disclosure UBCKNN)
3. Tax behavior                           (keywords: trốn thuế, truy thu thuế, tax dispute)
4. Shareholder rights / governance breach (keywords: xung đột lợi ích, gia đình trị, vi phạm UBCKNN)

Then apply the same 3-level scheme. Material in G context = criminal prosecution, license revocation, executive arrest, fine > 500M VND, dispossession of shareholder rights.
Resolution in G context = đã đóng phạt, vụ án đình chỉ, re-issued corrected financials, tái bổ nhiệm.

SCALE CONSOLIDATION RULE (project-level → corporate level, apply AFTER base level is determined):
The assigned level must reflect corporate-level impact. If the article body makes clear the event affects only a specific subsidiary/plant/project/factory (not the whole corporation) AND EITHER:
  (a) the affected unit's revenue/scale appears to be LESS than 20% of the parent company's annual revenue shown above, OR
  (b) the article indicates the parent company owns LESS than 20% of the affected entity,
then downgrade Major → Minor. Do NOT downgrade Minor → No under this rule.

Keep the base level unchanged if: affected unit's share of revenue/ownership is unclear, OR the event is corporate-wide (e.g., parent company fined, board-level action), OR revenue data shows "unavailable".

Applies in principle to E, S, and G events. Note G controversies (board prosecution, financial reporting fraud, corporate tax disputes) are almost always corporate-level — only downgrade if the article clearly scopes the event to a minority-owned subsidiary.

TODAY'S DATE: {today}  (use to compute past 5y / 10y windows from event date)

REASONING STEPS (perform internally before answering):
1. Determine event recency (past 5y? 5-10y? >10y?)
2. Search for resolution markers in title/source/body
3. Assess material consequences from title/body
4. For G: identify which of 4 CG indicators (use it to construct the justification)
5. Decide base level using the 3-level scheme from E&S definitions (applies to all types)
6. Apply scale consolidation rule if article clearly shows project-level scope
7. Compute confidence (high if clear-cut, low if borderline)

JUSTIFICATION FORMAT (exactly 2 sentences in English):
- Sentence 1: WHAT happened + WHICH definition/indicator matched
- Sentence 2: WHY this level (cite material consequences, resolution status, scale)

OUTPUT JSON ONLY (no other text, no markdown fences):
{{
  "level": "Major" | "Minor" | "No",
  "cg_indicator": 1 | 2 | 3 | 4 | null,
  "justification": "<exactly 2 sentences in English>",
  "confidence": <integer 0-100>
}}"""


def _validate(parsed: dict, event_type: str) -> dict | None:
    """Validate parsed LLM output. Returns sanitized dict or None if invalid."""
    if not isinstance(parsed, dict):
        return None
    level = parsed.get("level")
    if level not in VALID_LEVELS:
        return None
    justification = parsed.get("justification", "")
    if not isinstance(justification, str) or not justification.strip():
        return None
    # Rough 2-sentence check: look for at least one ". " separator
    if justification.count(". ") < 1 and not justification.rstrip().endswith("."):
        return None

    cg = parsed.get("cg_indicator")
    if event_type == "G":
        if cg not in (1, 2, 3, 4):
            cg = None
    else:
        cg = None

    confidence = parsed.get("confidence", 0)
    try:
        confidence = int(confidence)
    except (TypeError, ValueError):
        confidence = 0
    confidence = max(0, min(100, confidence))

    return {
        "level": level,
        "cg_indicator": cg,
        "justification": justification.strip(),
        "confidence": confidence,
    }


def _event_year(event: dict) -> int:
    """Extract year as int from event['date'] ('YYYY-MM-DD...'). Fallback: current year."""
    raw = (event.get("date") or "").strip()
    if len(raw) >= 4 and raw[:4].isdigit():
        return int(raw[:4])
    return datetime.now().year


def _revenue_display(per_year: dict, event_year: int) -> tuple[str, str]:
    """Build (revenue_year_str, revenue_display_str) for prompt injection."""
    picked = get_revenue_for_year(per_year, event_year)
    if picked is None:
        return ("n/a", "unavailable")
    year_used, rev = picked
    return (str(year_used), f"{rev:,.0f} billion VND")


def classify_event(event: dict, provider: dict, today: str, *, body: str, revenues: dict | None = None) -> dict | None:
    """Classify a single ESG event. Returns validated dict or None.

    Args:
        event:     Row dict with keys: ticker, company, type, date, summary, summary_en, source.
        provider:  Provider dict (name, model, sleep, ...) — used directly for call_llm.
        today:     ISO date string "YYYY-MM-DD" for the recency window computation.
        body:      Article body text (from DB). Truncated to ARTICLE_BODY_MAX_CHARS.
        revenues:  {ticker: {year_int: revenue_billion_vnd}} for the 20% scale rule.
    """
    article_body = (body or "").strip()
    if len(article_body) > ARTICLE_BODY_MAX_CHARS:
        article_body = article_body[:ARTICLE_BODY_MAX_CHARS] + "\n...[truncated]"
    if not article_body:
        article_body = "(not available — classify from title/source only)"

    event_year = _event_year(event)
    per_year = (revenues or {}).get(event.get("ticker", ""), {})
    revenue_year, revenue_display = _revenue_display(per_year, event_year)

    prompt = CLASSIFY_PROMPT.format(
        ticker=event.get("ticker", ""),
        company=event.get("company", ""),
        type=event.get("type", ""),
        date=event.get("date", ""),
        summary=event.get("summary", ""),
        summary_en=event.get("summary_en", ""),
        source=event.get("source", ""),
        article_body=article_body,
        event_year=event_year,
        revenue_year=revenue_year,
        revenue_display=revenue_display,
        today=today,
    )
    parsed = call_llm(provider, prompt)
    if parsed is None:
        return None
    return _validate(parsed, event.get("type", ""))
