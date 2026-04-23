"""
Controversy classifier for ESG events with severity == "Cao".

LLM provider is configurable via environment variables (no code change needed):
  LLM_PROVIDER  - one of: groq, cerebras, openrouter, deepseek, openai, mistral, gemini
                  (optional; auto-picks first provider with an API key in env,
                   priority: groq > cerebras > openrouter > deepseek > openai > mistral > gemini)
  LLM_MODEL     - model id override (optional; uses provider's default_model otherwise)
  <PROVIDER>_API_KEY - key for whichever provider is selected
                       e.g. GROQ_API_KEY, CEREBRAS_API_KEY, GEMINI_API_KEY

To add a new provider: add an entry to PROVIDERS below. All OpenAI-compatible
providers share the same schema — only url/key_env/default_model change.

Output per event: {level, cg_indicator, justification, confidence}
  level         ∈ {"Major", "Minor", "No"}
  cg_indicator  ∈ {1, 2, 3, 4, None}  (only set when type == "G")
  justification 2-sentence English
  confidence    int 0-100

Definitions embedded in prompt from CONTROVERSY_PLAN.md.
"""

import json
import os
import time
import urllib.request
import urllib.error
from datetime import datetime

from rss_fetcher import get_revenue_for_year


JINA_READER_BASE = "https://r.jina.ai/"
VALID_LEVELS = {"Major", "Minor", "No"}
ARTICLE_BODY_MAX_CHARS = 6000
SCALE_RULE_THRESHOLD_PCT = 20


# --- LLM provider registry ---------------------------------------------------
# schema "openai"  = POST messages, Bearer key; common OpenAI-compatible API
# schema "gemini"  = POST contents, key in query string; Google Gemini API
# sleep = seconds between calls to stay under that provider's free-tier RPM.

PROVIDERS = {
    "groq":       {"url": "https://api.groq.com/openai/v1/chat/completions",
                   "key_env": "GROQ_API_KEY",       "default_model": "llama-3.3-70b-versatile",
                   "schema": "openai", "sleep": 20},  # free-tier TPM 12K caps ~3.2 calls/min at 3.5K token prompts
    "cerebras":   {"url": "https://api.cerebras.ai/v1/chat/completions",
                   "key_env": "CEREBRAS_API_KEY",   "default_model": "llama-3.3-70b",
                   "schema": "openai", "sleep": 2},
    "openrouter": {"url": "https://openrouter.ai/api/v1/chat/completions",
                   "key_env": "OPENROUTER_API_KEY", "default_model": "deepseek/deepseek-r1:free",
                   "schema": "openai", "sleep": 3},
    "deepseek":   {"url": "https://api.deepseek.com/v1/chat/completions",
                   "key_env": "DEEPSEEK_API_KEY",   "default_model": "deepseek-chat",
                   "schema": "openai", "sleep": 2},
    "openai":     {"url": "https://api.openai.com/v1/chat/completions",
                   "key_env": "OPENAI_API_KEY",     "default_model": "gpt-4o-mini",
                   "schema": "openai", "sleep": 1},
    "mistral":    {"url": "https://api.mistral.ai/v1/chat/completions",
                   "key_env": "MISTRAL_API_KEY",    "default_model": "mistral-small-latest",
                   "schema": "openai", "sleep": 1},
    "gemini":     {"url": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                   "key_env": "GEMINI_API_KEY",     "default_model": "gemini-2.5-flash-lite",
                   "schema": "gemini", "sleep": 4},
}

AUTO_PICK_ORDER = ["groq", "cerebras", "openrouter", "deepseek", "openai", "mistral", "gemini"]


def resolve_provider():
    """Resolve active provider from env. Returns dict with name, url, key, model, schema, sleep.
    Returns None if no provider has a key in env.
    """
    name = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if name:
        if name not in PROVIDERS:
            print(f"  Classifier: unknown LLM_PROVIDER='{name}', must be one of {list(PROVIDERS)}")
            return None
        candidates = [name]
    else:
        candidates = AUTO_PICK_ORDER

    for n in candidates:
        cfg = PROVIDERS[n]
        key = os.environ.get(cfg["key_env"], "").strip()
        if not key:
            continue
        model = os.environ.get("LLM_MODEL", "").strip() or cfg["default_model"]
        return {
            "name": n, "key": key, "model": model,
            "url": cfg["url"], "schema": cfg["schema"], "sleep": cfg["sleep"],
        }
    return None


def _decode_google_news_url(url):
    """Decode Google News redirect URL to real article URL.
    Returns decoded URL string or None on failure.
    """
    if not url or "news.google.com" not in url:
        return url  # already a real URL or empty
    try:
        from googlenewsdecoder import gnewsdecoder
        result = gnewsdecoder(url, interval=1)
        if result.get("status") and result.get("decoded_url"):
            return result["decoded_url"]
    except Exception as e:
        print(f"    Decoder failed: {type(e).__name__}: {e}")
    return None


def fetch_article_body(url):
    """Fetch article body via Jina Reader. Returns markdown text or None.
    Handles Google News redirect URLs by decoding them first.
    """
    if not url:
        return None

    real_url = _decode_google_news_url(url)
    if not real_url:
        return None

    jina_url = JINA_READER_BASE + real_url
    try:
        req = urllib.request.Request(jina_url, headers={
            "Accept": "text/plain",
            "User-Agent": "Mozilla/5.0",
        })
        with urllib.request.urlopen(req, timeout=45) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        if len(body) > ARTICLE_BODY_MAX_CHARS:
            body = body[:ARTICLE_BODY_MAX_CHARS] + "\n...[truncated]"
        return body
    except urllib.error.HTTPError as e:
        print(f"    Jina {e.code} on {real_url[:80]}")
        return None
    except Exception as e:
        print(f"    Jina failed: {type(e).__name__}: {e}")
        return None

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


def _build_request(provider, prompt):
    """Build (url, payload_bytes, headers, extract_fn) for the chosen provider."""
    if provider["schema"] == "openai":
        url = provider["url"]
        payload = json.dumps({
            "model": provider["model"],
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
        }).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {provider['key']}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",  # some providers (Groq) block default urllib UA via Cloudflare
        }
        extract = lambda r: r["choices"][0]["message"]["content"]
        return url, payload, headers, extract

    if provider["schema"] == "gemini":
        url = provider["url"].format(model=provider["model"]) + f"?key={provider['key']}"
        payload = json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.0, "responseMimeType": "application/json"},
        }).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        extract = lambda r: r["candidates"][0]["content"]["parts"][0]["text"]
        return url, payload, headers, extract

    raise ValueError(f"unknown schema: {provider['schema']}")


def _call_llm(provider, prompt, retries=3):
    """Call the configured LLM provider. Returns parsed JSON dict or None on failure."""
    url, payload, headers, extract = _build_request(provider, prompt)
    label = f"{provider['name']}/{provider['model']}"

    for attempt in range(retries):
        req = urllib.request.Request(url, data=payload, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            return json.loads(extract(result))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if e.code in (429, 503) and attempt < retries - 1:
                wait = 30 * (attempt + 1)
                print(f"  {label} {e.code}, retry in {wait}s...")
                time.sleep(wait)
                continue
            print(f"  {label} API error {e.code}: {body[:200]}")
            return None
        except Exception as e:
            if attempt < retries - 1:
                print(f"  {label} error: {e}, retry in 10s...")
                time.sleep(10)
                continue
            print(f"  {label} failed: {type(e).__name__}: {e}")
            return None
    return None


def _validate(parsed, event_type):
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


def _event_year(event):
    """Extract year as int from event['date'] ('YYYY-MM-DD...'). Fallback: current year."""
    raw = (event.get("date") or "").strip()
    if len(raw) >= 4 and raw[:4].isdigit():
        return int(raw[:4])
    return datetime.now().year


def _revenue_display(per_year, event_year):
    """Build (revenue_year_str, revenue_display_str) for prompt injection."""
    picked = get_revenue_for_year(per_year, event_year)
    if picked is None:
        return ("n/a", "unavailable")
    year_used, rev = picked
    return (str(year_used), f"{rev:,.0f} billion VND")


def classify_event(event, provider, today, revenues=None):
    """Classify a single event. Returns validated dict or None.
    provider: dict from resolve_provider() — carries url/key/model/schema.
    revenues: {ticker: {year_int: revenue_billion_vnd}} used for 20% scale rule.
    """
    article_body = fetch_article_body(event.get("url", ""))
    if article_body:
        print(f"    Article body: {len(article_body)} chars")
    else:
        print(f"    Article body: unavailable (title-only classification)")
        article_body = "(not available — classify from title/source only)"

    event_year = _event_year(event)
    per_year = (revenues or {}).get(event.get("ticker", ""), {})
    revenue_year, revenue_display = _revenue_display(per_year, event_year)
    print(f"    Revenue: {revenue_display} (year {revenue_year}, event year {event_year})")

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
    parsed = _call_llm(provider, prompt)
    if parsed is None:
        return None
    return _validate(parsed, event.get("type", ""))


def classify_events(events, api_key=None, revenues=None, provider=None):
    """Classify a list of events. Returns list of same length;
    failed / invalid entries → None in that position.
    revenues: {ticker: {year: revenue_billion_vnd}} for 20% scale rule (optional).
    provider: dict from resolve_provider(); auto-resolved from env if omitted.
    api_key: deprecated — kept for backwards compat; if set + no provider, treated as Gemini key.
    """
    if not events:
        return []

    if provider is None:
        if api_key:  # legacy call site passing raw Gemini key
            os.environ.setdefault("GEMINI_API_KEY", api_key)
        provider = resolve_provider()

    if not provider:
        print("  Classifier: no LLM provider configured (set e.g. GROQ_API_KEY in env), skipping")
        return [None] * len(events)

    print(f"  Classifier: provider={provider['name']} model={provider['model']}")
    today = datetime.now().strftime("%Y-%m-%d")
    sleep_s = provider["sleep"]
    results = []

    for i, event in enumerate(events):
        print(f"  Classifier: event {i+1}/{len(events)} [{event.get('ticker','?')} {event.get('type','?')}]...")
        result = classify_event(event, provider, today, revenues=revenues)
        if result:
            print(f"    → {result['level']} (confidence={result['confidence']})")
        else:
            print(f"    → FAILED (skipped)")
        results.append(result)

        if i + 1 < len(events):
            time.sleep(sleep_s)

    return results
