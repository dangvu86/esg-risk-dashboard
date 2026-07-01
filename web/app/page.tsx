"use client";

import { useState, useEffect, useMemo, useCallback } from "react";
import {
  type EsgEvent,
  type Company,
  type Lang,
  type Filters,
  type SortKey,
  type Pillar,
  type Severity,
  type Fund,
  filterEvents,
  sortEvents,
  paginate,
  mergeTickers,
  computeStats,
  pickHeadline,
  severityLabel,
  controversyLabel,
  PAGE_SIZE,
} from "@/lib/esg";

const PILLAR_CLASS: Record<Pillar, string> = { E: "group-E", S: "group-S", G: "group-G" };
const SEVERITY_CLASS: Record<Severity, string> = { Cao: "sev-high", "Trung bình": "sev-med" };
const CONTROVERSY_CLASS: Record<"Major" | "Minor" | "No", string> = {
  Major: "badge ctrl-Major",
  Minor: "badge ctrl-Minor",
  No: "badge ctrl-No",
};

// Minimal bilingual UI string table.
const T = {
  tag: { en: "📰 ESG events", vi: "📰 Sự kiện ESG" },
  title: { en: "ESG controversy events", vi: "Sự kiện rủi ro ESG" },
  loading: { en: "Loading…", vi: "Đang tải…" },
  error: { en: "Couldn't load events.", vi: "Không tải được dữ liệu." },
  retry: { en: "Retry", vi: "Thử lại" },
  empty: { en: "No matching events", vi: "Không có sự kiện phù hợp" },
  search: { en: "🔍 Search headline…", vi: "🔍 Tìm tiêu đề…" },
  allTickers: { en: "All tickers", vi: "Tất cả ticker" },
  allPillars: { en: "All pillars", vi: "Tất cả nhóm" },
  allSeverity: { en: "All severity", vi: "Tất cả mức độ" },
  high: { en: "High", vi: "Cao" },
  medium: { en: "Medium", vi: "Trung bình" },
  allControversy: { en: "All controversy", vi: "Tất cả tranh cãi" },
  notClassified: { en: "Not classified", vi: "Chưa phân loại" },
  allFunds: { en: "All portfolios", vi: "Tất cả danh mục" },
  clear: { en: "Clear", vi: "Xoá lọc" },
  subtitle: { en: "events · sorted by most recent", vi: "sự kiện · sắp xếp theo ngày mới nhất" },
  perPage: { en: "/ page", vi: "/ trang" },
  statEvents: { en: "Events", vi: "Sự kiện" },
  statHigh: { en: "High severity", vi: "Mức Cao" },
  statMajor: { en: "Major controversy", vi: "Tranh cãi Major" },
  statCompanies: { en: "Companies", vi: "Công ty" },
  statEnv: { en: "Environment", vi: "Môi trường" },
  statSoc: { en: "Social", vi: "Xã hội" },
  statGov: { en: "Governance", vi: "Quản trị" },
  statFiltered: { en: "stats follow active filters", vi: "thống kê theo bộ lọc đang chọn" },
  colDate: { en: "Date", vi: "Ngày" },
  colCompany: { en: "Company", vi: "Công ty" },
  colHeadline: { en: "Headline", vi: "Tiêu đề" },
  colPillar: { en: "Pillar", vi: "Nhóm" },
  colSeverity: { en: "Severity", vi: "Mức độ" },
  colControversy: { en: "Controversy", vi: "Tranh cãi" },
  colJustification: { en: "Justification", vi: "Lý do" },
  colSource: { en: "Source", vi: "Nguồn" },
  prev: { en: "← Prev", vi: "← Trước" },
  next: { en: "Next →", vi: "Sau →" },
  footer: {
    en: "English translations auto-generated · Original VN preserved · Falls back to VN if translation pending",
    vi: "Bản tiếng Anh dịch tự động · Bản gốc VN luôn giữ nguyên · Hiện bản VN nếu chưa dịch",
  },
} as const;

const SORT_LABELS: Record<SortKey, { en: string; vi: string }> = {
  date_desc: { en: "Sort: Newest", vi: "Sắp xếp: Mới nhất" },
  date_asc: { en: "Sort: Oldest", vi: "Sắp xếp: Cũ nhất" },
  ticker_asc: { en: "Sort: Ticker A→Z", vi: "Sắp xếp: Ticker A→Z" },
};

const EMPTY_FILTERS: Filters = { ticker: "", pillar: "", severity: "", controversy: "", fund: "", query: "" };

export default function Home() {
  const [events, setEvents] = useState<EsgEvent[]>([]);
  const [companies, setCompanies] = useState<Company[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  const [lang, setLang] = useState<Lang>("en");
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  const [sortKey, setSortKey] = useState<SortKey>("date_desc");
  const [page, setPage] = useState(1);

  // Keep <html lang> in sync for accessibility.
  useEffect(() => {
    document.documentElement.lang = lang;
  }, [lang]);

  const load = useCallback(() => {
    fetch("/api/events")
      .then((r) => {
        if (!r.ok) throw new Error("events");
        return r.json();
      })
      .then((data: EsgEvent[]) => setEvents(data))
      .catch(() => setError(true))
      .finally(() => setLoading(false));
    // Tickers are best-effort; failure degrades to event-derived tickers.
    fetch("/api/tickers")
      .then((r) => (r.ok ? r.json() : []))
      .then((data: Company[]) => setCompanies(data))
      .catch(() => setCompanies([]));
  }, []);

  // Fetch once on mount. Initial state is already loading:true / error:false,
  // so the effect itself sets no state synchronously.
  useEffect(() => {
    load();
  }, [load]);

  // Any change to a filter, the query, or the sort returns to page 1.
  function updateFilters(patch: Partial<Filters>) {
    setFilters((f) => ({ ...f, ...patch }));
    setPage(1);
  }
  function updateSort(key: SortKey) {
    setSortKey(key);
    setPage(1);
  }
  function clearAll() {
    setFilters(EMPTY_FILTERS);
    setSortKey("date_desc");
    setPage(1);
  }

  const tickerOptions = useMemo(() => mergeTickers(companies, events), [companies, events]);
  const processed = useMemo(
    () => sortEvents(filterEvents(events, filters), sortKey),
    [events, filters, sortKey],
  );
  const pageData = useMemo(() => paginate(processed, page, PAGE_SIZE), [processed, page]);
  const stats = useMemo(() => computeStats(processed), [processed]);

  const isFiltered = Object.values(filters).some(Boolean);

  const start = processed.length === 0 ? 0 : (pageData.page - 1) * PAGE_SIZE + 1;
  const end = Math.min(pageData.page * PAGE_SIZE, processed.length);

  // Numbered labels (can't be static T entries because they interpolate counts).
  const total = processed.length.toLocaleString();
  const headerCount =
    lang === "en"
      ? `events · page ${pageData.page} of ${pageData.totalPages}`
      : `sự kiện · trang ${pageData.page}/${pageData.totalPages}`;
  const showingRange =
    lang === "en"
      ? `Showing ${start.toLocaleString()}–${end.toLocaleString()} of ${total}`
      : `Hiện ${start.toLocaleString()}–${end.toLocaleString()} / ${total}`;
  const pageOf =
    lang === "en"
      ? `Page ${pageData.page} of ${pageData.totalPages}`
      : `Trang ${pageData.page}/${pageData.totalPages}`;

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-screen subtle-text">
        {T.loading[lang]}
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex flex-col items-center justify-center min-h-screen gap-4">
        <p className="subtle-text">{T.error[lang]}</p>
        <button
          className="pill-btn-light"
          onClick={() => {
            setLoading(true);
            setError(false);
            load();
          }}
        >
          {T.retry[lang]}
        </button>
      </div>
    );
  }

  return (
    <>
      {/* Nav pill */}
      <div className="max-w-[1600px] mx-auto px-6 pt-6">
        <nav className="pill-nav flex items-center justify-between h-14 px-4">
          <div className="flex items-center gap-3 pl-2">
            <div className="w-7 h-7 rounded-full gradient-accent flex items-center justify-center">
              <div className="w-3 h-3 bg-white rounded-full" />
            </div>
            <span className="font-bold text-base">ESG Controversy</span>
          </div>
          <div className="lang-toggle">
            <button className={`lang-btn ${lang === "en" ? "active" : ""}`} onClick={() => setLang("en")}>EN</button>
            <button className={`lang-btn ${lang === "vi" ? "active" : ""}`} onClick={() => setLang("vi")}>VI</button>
          </div>
        </nav>
      </div>

      <div className="max-w-[1600px] mx-auto px-6 py-10">
        {/* Header */}
        <div className="mb-7">
          <span className="pill-tag mb-4 inline-block">{T.tag[lang]}</span>
          <h1 className="display-h1">{T.title[lang]}</h1>
          <p className="subtle-text mt-2 text-sm">
            {processed.length.toLocaleString()} {T.subtitle[lang]}
          </p>
        </div>

        {/* Stats row — reflects the active filters (full dataset when unfiltered) */}
        <div className="grid grid-cols-2 sm:grid-cols-4 xl:grid-cols-7 gap-3 mb-6">
          {[
            { label: T.statEvents[lang], value: stats.total, accent: "stat-plain" },
            { label: `E · ${T.statEnv[lang]}`, value: stats.byPillar.E, accent: "stat-E" },
            { label: `S · ${T.statSoc[lang]}`, value: stats.byPillar.S, accent: "stat-S" },
            { label: `G · ${T.statGov[lang]}`, value: stats.byPillar.G, accent: "stat-G" },
            { label: T.statHigh[lang], value: stats.high, accent: "stat-high" },
            { label: T.statMajor[lang], value: stats.major, accent: "stat-high" },
            { label: T.statCompanies[lang], value: stats.companies, accent: "stat-plain" },
          ].map((c) => (
            <div key={c.label} className="stat-card">
              <div className={`stat-value ${c.accent}`}>{c.value.toLocaleString()}</div>
              <div className="stat-label">{c.label}</div>
            </div>
          ))}
        </div>
        {isFiltered ? (
          <p className="subtle-text text-xs -mt-4 mb-5">↑ {T.statFiltered[lang]}</p>
        ) : null}

        {/* Filter bar */}
        <div className="flex flex-wrap items-center gap-3 mb-5">
          <input
            className="filter-input flex-1 min-w-[220px]"
            placeholder={T.search[lang]}
            value={filters.query}
            onChange={(e) => updateFilters({ query: e.target.value })}
          />
          <select className="filter-input" value={filters.ticker}
            onChange={(e) => updateFilters({ ticker: e.target.value })}>
            <option value="">{T.allTickers[lang]}</option>
            {tickerOptions.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
          <select className="filter-input" value={filters.pillar}
            onChange={(e) => updateFilters({ pillar: e.target.value as "" | Pillar })}>
            <option value="">{T.allPillars[lang]}</option>
            <option value="E">E</option>
            <option value="S">S</option>
            <option value="G">G</option>
          </select>
          <select className="filter-input" value={filters.severity}
            onChange={(e) => updateFilters({ severity: e.target.value as "" | Severity })}>
            <option value="">{T.allSeverity[lang]}</option>
            <option value="Cao">{T.high[lang]}</option>
            <option value="Trung bình">{T.medium[lang]}</option>
          </select>
          <select className="filter-input" value={filters.controversy}
            onChange={(e) => updateFilters({ controversy: e.target.value as Filters["controversy"] })}>
            <option value="">{T.allControversy[lang]}</option>
            <option value="Major">Major</option>
            <option value="Minor">Minor</option>
            <option value="No">No</option>
            <option value="none">{T.notClassified[lang]}</option>
          </select>
          <select className="filter-input" value={filters.fund}
            onChange={(e) => updateFilters({ fund: e.target.value as "" | Fund })}>
            <option value="">{T.allFunds[lang]}</option>
            <option value="VEF">VEF</option>
          </select>
          <select className="filter-input" value={sortKey}
            onChange={(e) => updateSort(e.target.value as SortKey)}>
            {(Object.keys(SORT_LABELS) as SortKey[]).map((k) => (
              <option key={k} value={k}>{SORT_LABELS[k][lang]}</option>
            ))}
          </select>
          {isFiltered ? (
            <button className="text-xs text-violet-600 hover:underline ml-1" onClick={clearAll}>
              {T.clear[lang]}
            </button>
          ) : null}
        </div>

        {/* Results table */}
        <div className="glass-card overflow-hidden">
          <div className="px-7 py-4 flex justify-between items-center text-sm border-b border-violet-100/50">
            <div>
              <strong>{processed.length.toLocaleString()}</strong> {headerCount}
            </div>
            <div className="subtle-text">{PAGE_SIZE} {T.perPage[lang]}</div>
          </div>

          <div className="overflow-x-auto">
            <table className="results-table w-full text-sm">
              <thead className="text-xs subtle-text uppercase tracking-wider">
                <tr className="border-b border-violet-100">
                  <th className="text-left px-7 py-3 font-semibold">{T.colDate[lang]}</th>
                  <th className="text-left py-3 font-semibold">Ticker</th>
                  <th className="text-left py-3 font-semibold">{T.colCompany[lang]}</th>
                  <th className="text-left py-3 font-semibold">{T.colHeadline[lang]}</th>
                  <th className="text-left py-3 font-semibold">{T.colPillar[lang]}</th>
                  <th className="text-left py-3 font-semibold">{T.colSeverity[lang]}</th>
                  <th className="text-left py-3 font-semibold">{T.colControversy[lang]}</th>
                  <th className="text-left py-3 font-semibold">{T.colJustification[lang]}</th>
                  <th className="text-left pr-7 py-3 font-semibold">{T.colSource[lang]}</th>
                </tr>
              </thead>
              <tbody>
                {pageData.rows.length === 0 ? (
                  <tr><td colSpan={9} className="px-7 py-10 text-center subtle-text">{T.empty[lang]}</td></tr>
                ) : (
                  pageData.rows.map((e, i) => {
                    const h = pickHeadline(e, lang);
                    return (
                      <tr key={`${e.ticker}-${e.date}-${i}`} className="row border-b border-violet-100/30 align-top">
                        <td className="px-7 py-4 tabular-nums subtle-text whitespace-nowrap">{e.date}</td>
                        <td className="py-4"><span className="ticker-chip">{e.ticker}</span></td>
                        <td className="py-4 subtle-text">{e.company}</td>
                        <td className="py-4 font-medium max-w-xl">
                          {h.text}
                          {h.isFallback ? <span className="fallback-tag">(VI · awaiting translation)</span> : null}
                        </td>
                        <td className="py-4"><span className={`badge ${PILLAR_CLASS[e.type]}`}>{e.type}</span></td>
                        <td className="py-4">
                          <span className={SEVERITY_CLASS[e.severity]}>
                            {severityLabel(e.severity, lang)}
                          </span>
                        </td>
                        <td className="py-4">
                          {e.controversy_level
                            ? <span className={CONTROVERSY_CLASS[e.controversy_level]}>{controversyLabel(e.controversy_level)}</span>
                            : <span className="subtle-text text-xs">—</span>}
                        </td>
                        <td className="py-4 text-xs subtle-text max-w-[280px]">
                          {e.controversy_justification || "—"}
                        </td>
                        <td className="pr-7 py-4 subtle-text">
                          {e.url
                            ? <a href={e.url} target="_blank" rel="noopener noreferrer" className="text-violet-600 hover:underline">{e.source} ↗</a>
                            : e.source}
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>

          {/* Pagination footer */}
          <div className="px-7 py-4 flex justify-between items-center text-sm border-t border-violet-100/50">
            <span className="subtle-text">{showingRange}</span>
            <div className="flex items-center gap-2">
              <button className="pill-btn-light" disabled={pageData.page <= 1}
                onClick={() => setPage((p) => p - 1)}>{T.prev[lang]}</button>
              <span className="subtle-text text-xs px-2">{pageOf}</span>
              <button className="pill-btn-light" disabled={pageData.page >= pageData.totalPages}
                onClick={() => setPage((p) => p + 1)}>{T.next[lang]}</button>
            </div>
          </div>
        </div>

        <div className="mt-6 text-xs subtle-text text-center">{T.footer[lang]}</div>
      </div>
    </>
  );
}
