"""
Env-switchable LLM provider registry for the ESG enrich pipeline.

Provider is configurable via environment variables (no code change needed):
  LLM_PROVIDER  - one of: groq, cerebras, openrouter, deepseek, openai, mistral, gemini
                  (optional; auto-picks first provider with an API key in env,
                   priority: groq > cerebras > openrouter > deepseek > openai > mistral > gemini)
  LLM_MODEL     - model id override (optional; uses provider's default_model otherwise)
  <PROVIDER>_API_KEY - key for whichever provider is selected
                       e.g. GROQ_API_KEY, CEREBRAS_API_KEY, GEMINI_API_KEY

To add a new provider: add an entry to PROVIDERS below. All OpenAI-compatible
providers share the same schema — only url/key_env/default_model change.

Ported verbatim from cloud-function/controversy_classifier.py.
Jina/decoder helpers (fetch_article_body, _decode_google_news_url) are NOT included.
"""

import json
import os
import time
import urllib.request
import urllib.error


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
            print(f"  LLM: unknown LLM_PROVIDER='{name}', must be one of {list(PROVIDERS)}")
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


# Public alias used by downstream stage modules
call_llm = _call_llm
