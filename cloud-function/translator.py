"""
Vietnamese -> English translator for ESG event summaries.
Uses Gemini 2.5 Flash-Lite (free tier: 15 RPM, 1000 RPD).
Batches multiple summaries per request to save quota.
"""

import json
import os
import time
import urllib.request
import urllib.error


GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent"

BATCH_SIZE = 30

TRANSLATE_PROMPT = """Translate these Vietnamese ESG news titles to concise English. Keep proper nouns (company names, government agencies, places) accurate. Preserve numbers and currency units.

Return a JSON array of strings in the same order. Only the JSON, no explanation.

Vietnamese titles:
{titles}"""


def _call_gemini(prompt, api_key, retries=3):
    """Single Gemini call. Returns parsed JSON list or None on failure."""
    url = f"{GEMINI_API_URL}?key={api_key}"
    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }).encode("utf-8")

    for attempt in range(retries):
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            text = result["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if e.code in (429, 503) and attempt < retries - 1:
                wait = 30 * (attempt + 1)
                print(f"  Translator {e.code}, retry in {wait}s...")
                time.sleep(wait)
                continue
            print(f"  Translator API error {e.code}: {body[:200]}")
            return None
        except Exception as e:
            if attempt < retries - 1:
                print(f"  Translator error: {e}, retry in 10s...")
                time.sleep(10)
                continue
            print(f"  Translator failed: {type(e).__name__}: {e}")
            return None
    return None


def translate_summaries(summaries, api_key=None):
    """Translate list of Vietnamese summaries to English.
    Returns list of same length; failed entries fall back to original VN text.
    """
    if not summaries:
        return []

    api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        print("  Translator: GEMINI_API_KEY not set, skipping translation")
        return list(summaries)

    results = list(summaries)

    for chunk_start in range(0, len(summaries), BATCH_SIZE):
        chunk = summaries[chunk_start:chunk_start + BATCH_SIZE]
        titles_text = "\n".join(f"{i+1}. {s}" for i, s in enumerate(chunk))
        prompt = TRANSLATE_PROMPT.format(titles=titles_text)

        print(f"  Translator: chunk {chunk_start//BATCH_SIZE + 1} ({len(chunk)} titles)...")
        translated = _call_gemini(prompt, api_key)

        if translated and isinstance(translated, list) and len(translated) == len(chunk):
            for i, en in enumerate(translated):
                if isinstance(en, str) and en.strip():
                    results[chunk_start + i] = en.strip()
        else:
            print(f"  Translator: chunk failed or length mismatch, keeping VN")

        if chunk_start + BATCH_SIZE < len(summaries):
            time.sleep(4)  # 15 RPM = 4s between calls

    return results
