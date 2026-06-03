"""
Vietnamese -> English translator for ESG article titles.
Port of cloud-function/translator.py — titles-only scope.
Batches multiple titles per request to save quota (BATCH_SIZE=30).
"""

import time

from enrich.llm import resolve_provider, call_llm


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


def translate_titles(titles, provider=None):
    """Translate list of Vietnamese article titles to English.
    Returns list of same length; failed entries fall back to original VN text.
    provider: optional pre-resolved provider dict (for testing/injection).
    """
    if not titles:
        return []

    provider = provider or resolve_provider()
    if not provider:
        print("  Translator: no LLM provider configured (set e.g. GROQ_API_KEY), skipping")
        return list(titles)

    print(f"  Translator: provider={provider['name']} model={provider['model']}")
    results = list(titles)
    sleep_s = provider["sleep"]

    for chunk_start in range(0, len(titles), BATCH_SIZE):
        chunk = titles[chunk_start:chunk_start + BATCH_SIZE]
        titles_text = "\n".join(f"{i+1}. {s}" for i, s in enumerate(chunk))
        prompt = TRANSLATE_PROMPT.format(titles=titles_text)

        print(f"  Translator: chunk {chunk_start//BATCH_SIZE + 1} ({len(chunk)} titles)...")
        parsed = call_llm(provider, prompt)
        translated = _extract_translations(parsed, len(chunk)) if parsed else None

        if translated:
            for i, en in enumerate(translated):
                if en:
                    results[chunk_start + i] = en
        else:
            print(f"  Translator: chunk failed or length mismatch, keeping VN")

        if chunk_start + BATCH_SIZE < len(titles):
            time.sleep(sleep_s)

    return results
