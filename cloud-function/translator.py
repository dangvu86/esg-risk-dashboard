"""
Vietnamese -> English translator for ESG event summaries.
Uses the same LLM provider registry as controversy_classifier (env-switchable).
Batches multiple summaries per request to save quota.
"""

import json
import os
import time
import urllib.request
import urllib.error

from controversy_classifier import resolve_provider


BATCH_SIZE = 30

TRANSLATE_PROMPT = """Translate these Vietnamese ESG news titles to natural, concise English — Google Translate style.

RULES:
1. Proper-noun handling (match Google Translate behavior):
   - Vietnamese person names: transliterate to ASCII without diacritics. "Phạm Nhật Vượng" → "Pham Nhat Vuong".
   - Vietnamese company/brand names: use their common English form if widely known, otherwise transliterate. "Hòa Phát" → "Hoa Phat"; "Hóa chất Đức Giang" → "Duc Giang Chemicals"; "Vingroup" stays "Vingroup".
   - Government agencies and ministries: translate to English (e.g., "Bộ Tài nguyên và Môi trường" → "Ministry of Natural Resources and Environment"; "UBCKNN" → "State Securities Commission").
   - Place names: transliterate. "Dung Quất" → "Dung Quat"; "Hải Dương" → "Hai Duong".
2. Preserve ticker codes (DGC, HPG, VIC), numbers, dates, and currency units (tỷ VND → billion VND, triệu VND → million VND).
3. Keep it concise — strip trailing source names like "- CafeF" if present.

Return a JSON object with key "translations" containing an array of strings in the same order as input. Only the JSON, no explanation.

Vietnamese titles:
{titles}"""


def _build_request(provider, prompt):
    """Mirror of controversy_classifier._build_request — kept inline to avoid a circular import."""
    if provider["schema"] == "openai":
        url = provider["url"]
        payload = json.dumps({
            "model": provider["model"],
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {provider['key']}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
        }
        extract = lambda r: r["choices"][0]["message"]["content"]
        return url, payload, headers, extract

    if provider["schema"] == "gemini":
        url = provider["url"].format(model=provider["model"]) + f"?key={provider['key']}"
        payload = json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
        }).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        extract = lambda r: r["candidates"][0]["content"]["parts"][0]["text"]
        return url, payload, headers, extract

    raise ValueError(f"unknown schema: {provider['schema']}")


def _call_llm(provider, prompt, retries=3):
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
                print(f"  Translator {label} {e.code}, retry in {wait}s...")
                time.sleep(wait)
                continue
            print(f"  Translator {label} API error {e.code}: {body[:200]}")
            return None
        except Exception as e:
            if attempt < retries - 1:
                print(f"  Translator {label} error: {e}, retry in 10s...")
                time.sleep(10)
                continue
            print(f"  Translator {label} failed: {type(e).__name__}: {e}")
            return None
    return None


def _extract_translations(parsed, expected_len):
    """Both schemas are instructed to return an object with a 'translations' array.
    Some models may return a bare list instead — accept both shapes.
    """
    if isinstance(parsed, list):
        lst = parsed
    elif isinstance(parsed, dict):
        lst = parsed.get("translations") or parsed.get("results") or next(
            (v for v in parsed.values() if isinstance(v, list)), None)
    else:
        return None
    if not isinstance(lst, list) or len(lst) != expected_len:
        return None
    return [s.strip() if isinstance(s, str) else "" for s in lst]


def translate_summaries(summaries, api_key=None):
    """Translate list of Vietnamese summaries to English.
    Returns list of same length; failed entries fall back to original VN text.
    api_key: deprecated — kept for backwards compat; registry resolves from env.
    """
    if not summaries:
        return []

    provider = resolve_provider()
    if not provider:
        print("  Translator: no LLM provider configured (set e.g. GROQ_API_KEY), skipping")
        return list(summaries)

    print(f"  Translator: provider={provider['name']} model={provider['model']}")
    results = list(summaries)
    sleep_s = provider["sleep"]

    for chunk_start in range(0, len(summaries), BATCH_SIZE):
        chunk = summaries[chunk_start:chunk_start + BATCH_SIZE]
        titles_text = "\n".join(f"{i+1}. {s}" for i, s in enumerate(chunk))
        prompt = TRANSLATE_PROMPT.format(titles=titles_text)

        print(f"  Translator: chunk {chunk_start//BATCH_SIZE + 1} ({len(chunk)} titles)...")
        parsed = _call_llm(provider, prompt)
        translated = _extract_translations(parsed, len(chunk)) if parsed else None

        if translated:
            for i, en in enumerate(translated):
                if en:
                    results[chunk_start + i] = en
        else:
            print(f"  Translator: chunk failed or length mismatch, keeping VN")

        if chunk_start + BATCH_SIZE < len(summaries):
            time.sleep(sleep_s)

    return results
