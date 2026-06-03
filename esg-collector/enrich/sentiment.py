"""
Filter out positive/neutral ESG events flagged by keyword filter.

Problem: keyword_classifier catches keywords like "tử vong" / "ô nhiễm" etc,
but some titles mention these in a positive context (e.g., charity for victims,
new anti-pollution investment). These should NOT count as ESG risks.

Input:  list of events that passed keyword filter.
Output: subset where LLM agrees the title describes a *negative ESG risk*.

Batches 5 events per LLM call to minimize quota usage.
Uses the same provider registry as enrich.llm.
"""

import time

from enrich.llm import resolve_provider, call_llm


BATCH_SIZE = 5

FILTER_PROMPT = """You are an ESG risk analyst screening Vietnamese news titles for a company risk dashboard.

Goal: filter out ONLY clearly positive/unrelated titles. When in doubt, label "risk" (we prefer false positives over missing real issues).

Analyze each title INDEPENDENTLY — do not let the label of one title influence another. Each title is a standalone decision.

DEFAULT: label "risk" unless you are CONFIDENT the title is NOT about the company having a negative ESG event.

Label "risk" (keep) includes — do NOT drop these:
- Any mention of being fined, penalized, prosecuted, inspected, audited, investigated, sanctioned ("bị xử phạt", "bị phạt", "truy thu thuế", "bị thanh tra", "bị khởi tố", "bị truy tố", "bị bắt", "vi phạm", "sai phạm", "khởi tố")
- Any inspection / audit CONCLUSION that finds issues ("kết luận thanh tra chỉ ra", "tồn tại", "chưa tuân thủ", "sai phạm", "lỗ hổng")
- Labor accidents, worker deaths/injuries AT the company's facility
- Pollution, spills, environmental violations BY the company
- Governance issues: executive arrests, fraud, conflict of interest, bond violations
- Bonds/securities issued by the company that are under investigation ("trái phiếu bị thanh tra", "vi phạm trái phiếu")
- The company's response to its own violations ("công ty X nói gì về kết luận thanh tra")

Label "not_risk" (drop) only in CLEAR cases:
- Pure charity / disaster relief / CSR ("hỗ trợ gia đình nạn nhân", "quỹ thiện tâm", "ủng hộ", "tài trợ") — even if title mentions accident keyword
- Executive/Chairman STATEMENTS about sustainability, renewable energy, environment commitment (e.g., "Chủ tịch X: Tận dụng tài nguyên tái tạo thay vì gia tăng ô nhiễm" is a POSITIVE CSR message, not a pollution incident)
- Investment IN prevention / safety / green tech with no mention of prior violation
- Company receives an award or positive recognition
- Robbery/theft where the company is merely the victim location (e.g., "cướp ngân hàng X bị bắt") — unless the company staff were perpetrators
- Generic commentary not tied to a specific company incident (e.g., "Làm gì nếu bị lừa đảo, BIDV cảnh báo" — this is advice, not a BIDV incident)
- Divestment, M&A, share/bond issuance, earnings announcement WITHOUT any regulatory issue mentioned (including "thoái vốn", "chào bán trái phiếu", "giảm cổ phiếu", "phát hành", "đóng cửa ở mức cao")

Events to classify:
{events}

Return a JSON object with key "labels" containing an array of strings ("risk" or "not_risk") in the same order as input. Only the JSON, no explanation.
"""


def _extract_labels(parsed, expected_len):
    if isinstance(parsed, list):
        lst = parsed
    elif isinstance(parsed, dict):
        lst = parsed.get("labels") or next(
            (v for v in parsed.values() if isinstance(v, list)), None)
    else:
        return None
    if not isinstance(lst, list) or len(lst) != expected_len:
        return None
    out = []
    for s in lst:
        s = (s or "").strip().lower() if isinstance(s, str) else ""
        out.append("risk" if s == "risk" else "not_risk" if s == "not_risk" else "risk")
    return out


def filter_negative(events, provider=None):
    """Return subset of events whose titles describe real negative ESG risks.
    Falls back to keeping all events if provider unavailable or call fails.
    """
    if not events:
        return []

    provider = provider or resolve_provider()
    if not provider:
        print("  Sentiment: no LLM provider configured, skipping (keeping all)")
        return list(events)

    print(f"  Sentiment: provider={provider['name']} model={provider['model']} n={len(events)}")
    sleep_s = provider["sleep"]
    kept = []

    for chunk_start in range(0, len(events), BATCH_SIZE):
        chunk = events[chunk_start:chunk_start + BATCH_SIZE]
        events_text = "\n".join(
            f'{i+1}. [{e.get("ticker","?")}/{e.get("type","?")}] {e.get("summary","")}'
            for i, e in enumerate(chunk)
        )
        prompt = FILTER_PROMPT.format(events=events_text)

        print(f"  Sentiment: chunk {chunk_start//BATCH_SIZE + 1} ({len(chunk)} events)...")
        parsed = call_llm(provider, prompt)
        labels = _extract_labels(parsed, len(chunk)) if parsed else None

        if labels is None:
            print(f"    chunk failed, keeping all {len(chunk)} events in chunk")
            kept.extend(chunk)
        else:
            dropped = 0
            for e, lab in zip(chunk, labels):
                if lab == "risk":
                    kept.append(e)
                else:
                    dropped += 1
                    print(f"    DROP [{e.get('ticker')}] {e.get('summary','')[:90]}")
            print(f"    kept {len(chunk)-dropped}/{len(chunk)}")

        if chunk_start + BATCH_SIZE < len(events):
            time.sleep(sleep_s)

    print(f"  Sentiment: {len(kept)}/{len(events)} events kept as real risks")
    return kept
