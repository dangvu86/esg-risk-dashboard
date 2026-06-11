"""Vet alias pools against the article corpus to catch false-positive aliases
BEFORE they pollute matching ("Hoàn Vũ"→NVL matched 660 Miss-Universe articles;
"Thời trang CAO"→PNJ matched the generic phrase "thời trang cao cấp").

Method (the same measurement used in the 2026-06-11 manual cleanup):
for each alias, scan title+description+sapo of every article (word-bounded,
case-insensitive) and compute the ANCHOR CO-OCCURRENCE RATIO — the share of
alias-hit articles that also mention another alias of the same company. A
genuine brand/subsidiary alias co-occurs with its parent's name in most
coverage; a collision alias almost never does.

Verdicts:
  PASS    hits == 0 (no evidence either way — harmless today), or
          ratio >= REVIEW_RATIO, or hits < FAIL_MIN_HITS with ratio >= FAIL_RATIO
  REVIEW  FAIL_RATIO <= ratio < REVIEW_RATIO  (eyeball the printed samples)
  FAIL    hits >= FAIL_MIN_HITS and ratio < FAIL_RATIO  (collision — remove)

Known blind spots (measured on the 2026-06-11 full-corpus run — read FAILs
with these in mind before removing anything):
  - DOMINANT BRANDS: a company's main brand (VietinBank/CTG, Vietnam
    Airlines/HVN, VEAM/VEA) rarely co-occurs with its ticker or formal name —
    low ratio does NOT mean collision. `names` entries therefore never FAIL
    (capped at REVIEW) and --apply never touches them.
  - SELF-NEWSWORTHY SUBSIDIARIES: a real subsidiary that makes its own news
    (Núi Pháo/MSN, NT2/POW, Bảo hiểm Quân Đội/MBB) also shows a low ratio.
    Always read the samples before trusting a subsidiaries FAIL.

The vetted alias's own ticker code and any alias already in the stoplist /
blocked-contexts config are skipped.

Requires the articles DB locally (download from GCS first):
  gcloud storage cp gs://esg-scan-data/state/articles.db <dir>/articles.db

CLI (report-only by default; --apply rewrites the alias JSONs minus FAILs):
  ESG_DATA_DIR=<dir> python -m alias_builder.alias_vet --tickers NVL KDH
  ESG_DATA_DIR=<dir> python -m alias_builder.alias_vet --all
  ESG_DATA_DIR=<dir> python -m alias_builder.alias_vet --tickers NVL --apply
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

from config import settings

# An alias FAILs (auto-remove with --apply) only on solid evidence: enough
# hit volume to judge, and near-zero co-occurrence with the company's other
# aliases. Between FAIL_RATIO and REVIEW_RATIO a human reads the samples.
FAIL_MIN_HITS = 5
FAIL_RATIO = 0.10
REVIEW_RATIO = 0.50
SAMPLES_PER_ALIAS = 3
_ALIAS_FIELDS = ("names", "subsidiaries", "projects")


@dataclass
class Verdict:
    ticker: str
    alias: str
    field: str
    hits: int
    cooccur: int
    verdict: str            # PASS | REVIEW | FAIL
    samples: list[str] = field(default_factory=list)

    @property
    def ratio(self) -> float:
        return self.cooccur / self.hits if self.hits else 0.0


def _bounded(term: str) -> re.Pattern:
    return re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE | re.UNICODE)


def _load_texts(db_path) -> list[str]:
    """title+description+sapo per article — the fields the matcher trusts most."""
    conn = sqlite3.connect(db_path)
    try:
        return [" | ".join(filter(None, row))
                for row in conn.execute("SELECT title, description, sapo FROM articles")]
    finally:
        conn.close()


def _load_pool(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _skip_set() -> set[str]:
    """Surfaces already handled elsewhere — vetting them is redundant."""
    out: set[str] = set()
    for p in (settings.AMBIGUOUS_ALIASES_PATH,
              getattr(settings, "BLOCKED_CONTEXTS_PATH", None)):
        try:
            if p:
                out |= {str(s).strip().upper() for s in json.loads(Path(p).read_text(encoding="utf-8"))}
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    return out


def vet_ticker(ticker: str, texts: list[str], aliases_dir: Path | None = None) -> list[Verdict]:
    aliases_dir = aliases_dir or settings.ALIASES_DIR
    pool = _load_pool(aliases_dir / f"{ticker}.json")
    skip = _skip_set()

    candidates: list[tuple[str, str]] = []   # (alias, field)
    seen: set[str] = set()
    for f in _ALIAS_FIELDS:
        for a in pool.get(f) or []:
            a = (a or "").strip()
            if not a or a.lower() in seen or a.upper() == ticker.upper() or a.upper() in skip:
                continue
            seen.add(a.lower())
            candidates.append((a, f))
    if not candidates:
        return []

    # Anchors: every OTHER alias of this company (+ company_name). A hit
    # article that contains any anchor is evidence the alias is genuine.
    all_aliases = {a for a, _ in candidates}
    company_name = (pool.get("company_name") or "").strip()
    if company_name:
        all_aliases.add(company_name)
    all_aliases.add(ticker.upper())

    alias_rx = {a: _bounded(a) for a, _ in candidates}
    anchor_rx = {a: _bounded(a) for a in all_aliases}

    out: list[Verdict] = []
    for alias, f in candidates:
        rx = alias_rx[alias]
        # Anchors = every other alias. Longer forms that CONTAIN the vetted
        # alias ("CTCP Vi La" ⊃ "Vi La") deliberately stay in: an article
        # naming the full form is genuine evidence for the short form.
        anchors = [anchor_rx[a] for a in all_aliases if a.lower() != alias.lower()]
        hits = cooccur = 0
        samples: list[str] = []
        for t in texts:
            if not rx.search(t):
                continue
            hits += 1
            if any(a.search(t) for a in anchors):
                cooccur += 1
            elif len(samples) < SAMPLES_PER_ALIAS:
                samples.append(t[:110])      # show the NON-co-occurring ones
        ratio = cooccur / hits if hits else 0.0
        if hits == 0 or ratio >= REVIEW_RATIO:
            verdict = "PASS"
        elif f != "names" and hits >= FAIL_MIN_HITS and ratio < FAIL_RATIO:
            verdict = "FAIL"
        else:
            # includes `names` entries with low ratio: a dominant brand
            # legitimately appears without its ticker/formal name (see the
            # blind-spots note in the module docstring) — human review only.
            verdict = "REVIEW"
        out.append(Verdict(ticker, alias, f, hits, cooccur, verdict, samples))
    return out


def apply_fails(ticker: str, verdicts: list[Verdict], aliases_dir: Path | None = None) -> int:
    """Rewrite <TICKER>.json without the FAIL aliases. Returns count removed."""
    aliases_dir = aliases_dir or settings.ALIASES_DIR
    fails = {v.alias for v in verdicts if v.verdict == "FAIL"}
    if not fails:
        return 0
    path = aliases_dir / f"{ticker}.json"
    pool = _load_pool(path)
    removed = 0
    for f in _ALIAS_FIELDS:
        before = pool.get(f) or []
        after = [a for a in before if a not in fails]
        removed += len(before) - len(after)
        pool[f] = after
    path.write_text(json.dumps(pool, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return removed


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tickers", nargs="+", help="tickers to vet")
    ap.add_argument("--all", action="store_true", help="vet every alias file")
    ap.add_argument("--db", default=None, help="articles DB path (default: settings.DB_PATH)")
    ap.add_argument("--apply", action="store_true",
                    help="remove FAIL aliases from the JSON files (REVIEW is never auto-removed)")
    args = ap.parse_args()
    if not args.tickers and not args.all:
        ap.error("pass --tickers or --all")

    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    db = Path(args.db) if args.db else settings.DB_PATH
    if not Path(db).exists():
        raise SystemExit(f"articles DB not found at {db} — download it from GCS first "
                         "(see module docstring)")
    print(f"loading corpus from {db} …")
    texts = _load_texts(db)
    print(f"{len(texts):,} articles loaded")

    tickers = args.tickers or sorted(p.stem for p in settings.ALIASES_DIR.glob("*.json"))
    totals = {"PASS": 0, "REVIEW": 0, "FAIL": 0}
    for tk in tickers:
        tk = tk.upper()
        try:
            verdicts = vet_ticker(tk, texts)
        except FileNotFoundError:
            print(f"{tk}: no alias file — skipped")
            continue
        flagged = [v for v in verdicts if v.verdict != "PASS"]
        for v in verdicts:
            totals[v.verdict] += 1
        if not flagged:
            print(f"{tk}: {len(verdicts)} aliases, all PASS")
            continue
        print(f"{tk}: {len(verdicts)} aliases — {len(flagged)} flagged")
        for v in flagged:
            print(f"  [{v.verdict}] {v.alias!r} ({v.field}): {v.hits} hits, "
                  f"co-occurrence {v.cooccur}/{v.hits} = {v.ratio:.0%}")
            for s in v.samples:
                print(f"      · {s}")
        if args.apply:
            n = apply_fails(tk, verdicts)
            if n:
                print(f"  --apply: removed {n} FAIL alias(es) from {tk}.json")
    print(f"\nsummary: {totals['PASS']} PASS / {totals['REVIEW']} REVIEW / {totals['FAIL']} FAIL")
    if totals["FAIL"] and not args.apply:
        print("re-run with --apply to remove the FAILs (then rebuild image + rematch)")


if __name__ == "__main__":
    main()
