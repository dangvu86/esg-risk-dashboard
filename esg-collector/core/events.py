"""Event clustering: group matched articles that report the same real-world
event, per docs/superpowers/specs/2026-06-08-event-clustering-dedup-design.md.

Two articles are the same event iff: same ticker AND published within
`window_days` of each other AND (Jaccard of their normalised title token-sets
>= `jaccard_min` OR they share a rare significant token — a proper-noun-like
token such as "PECC3" / "Phiệt" that appears in few of the ticker's titles).
Clusters are connected components of that pairwise graph, so a long-running
saga chained by <=window_days hops is ONE event (intended).

Pure and deterministic: no DB, no network, no clock — reusable by both
pipeline/export.py (one card per cluster) and the enrich runner (one LLM
verdict per cluster, members inherit).
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date

from core.title import normalise

# Normalised (diacritic-stripped) Vietnamese news stopwords. Deliberately
# NOT included: "vu" (doubles as the surname Vũ), "the" (Thế), "an" (án).
_STOPWORDS = {
    "va", "cua", "cho", "voi", "tai", "sau", "truoc", "ve", "da", "dang",
    "se", "la", "lai", "trong", "ngoai", "khi", "den", "tu", "mot", "hai",
    "ba", "bon", "nhieu", "loat", "gan", "hon", "nay", "thang", "ngay",
    "ong", "bi", "duoc", "do", "vi", "sao", "khong", "cung", "con",
    "nguoi", "cong", "ty", "co", "phan", "ctcp", "tnhh", "mtv", "doanh",
    "nghiep", "ngan", "hang", "chung", "khoan", "trieu", "ti", "dong",
    "vnd", "usd", "moi", "len", "gi", "noi", "nói", "sau", "nua", "them",
}


def _tokens(title: str) -> frozenset[str]:
    return frozenset(
        t for t in normalise(title or "").split()
        if len(t) >= 2 and t not in _STOPWORDS and not t.isdigit()
    )


def _pub_date(article: dict) -> date | None:
    s = (article.get("published_at") or "")[:10]
    try:
        y, m, d = s.split("-")
        return date(int(y), int(m), int(d))
    except (ValueError, AttributeError):
        return None


def _jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    return inter / (len(a) + len(b) - inter)


class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def cluster_events(articles, window_days: int = 10, jaccard_min: float = 0.45,
                   rare_max_ratio: float = 0.7) -> list[list[dict]]:
    """Group article dicts (article_id, ticker, title, published_at) into
    same-event clusters. Each cluster is sorted earliest-first, so
    `cluster[0]` is the representative. Ticker is a hard boundary."""
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for a in articles:
        by_ticker[(a.get("ticker") or "").upper()].append(a)

    out: list[list[dict]] = []
    for ticker in sorted(by_ticker):
        items = by_ticker[ticker]
        n = len(items)
        toks = [_tokens(a.get("title") or "") for a in items]
        dates = [_pub_date(a) for a in items]
        df = Counter(t for ts in toks for t in ts)
        tkr_lc = ticker.lower()

        def _rare(t: str) -> bool:
            # Proper-noun-ish: long enough, shared by >=2 but few titles, and
            # never the company token itself (present in nearly every title).
            return (len(t) >= 4 and t != tkr_lc and df[t] >= 2
                    and df[t] / n <= rare_max_ratio)

        uf = _UnionFind(n)
        for i in range(n):
            if not toks[i]:
                continue
            for j in range(i + 1, n):
                if not toks[j]:
                    continue
                di, dj = dates[i], dates[j]
                if (di is None) != (dj is None):
                    continue          # dated never merges with dateless
                if di is not None and abs((di - dj).days) > window_days:
                    continue
                shared = toks[i] & toks[j]
                if not shared:
                    continue
                if _jaccard(toks[i], toks[j]) >= jaccard_min or \
                        any(_rare(t) for t in shared):
                    uf.union(i, j)

        comps: dict[int, list[int]] = defaultdict(list)
        for i in range(n):
            comps[uf.find(i)].append(i)
        far_future = date.max
        clusters = []
        for members in comps.values():
            members.sort(key=lambda i: (dates[i] or far_future,
                                        str(items[i].get("article_id"))))
            clusters.append([items[i] for i in members])
        clusters.sort(key=lambda c: (_pub_date(c[0]) or far_future,
                                     str(c[0].get("article_id"))))
        out.extend(clusters)
    return out


def event_key(cluster: list[dict]) -> str:
    """Stable id for a cluster: ticker + representative article_id."""
    rep = cluster[0]
    return f"{(rep.get('ticker') or '').upper()}:{rep.get('article_id')}"
