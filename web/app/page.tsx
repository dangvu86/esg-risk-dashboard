"use client";

import { useState, useEffect, useMemo } from "react";

type ControversyLevel = "Major" | "Minor" | "No" | "";

interface EsgEvent {
  ticker: string;
  company: string;
  type: "E" | "S" | "G";
  date: string;
  summary: string;
  summary_en?: string;
  severity: "Cao" | "Trung bình";
  source: string;
  url: string;
  controversy_level?: ControversyLevel;
  controversy_justification?: string;
  controversy_classified_at?: string;
}

interface Company {
  ticker: string;
  company: string;
}

const TYPE_COLORS = {
  E: "bg-green-100 text-green-800",
  S: "bg-orange-100 text-orange-800",
  G: "bg-blue-100 text-blue-800",
};

const SEVERITY_LABEL: Record<string, string> = {
  Cao: "High",
  "Trung bình": "Medium",
};

const CONTROVERSY_STYLES: Record<string, string> = {
  Major: "bg-red-100 text-red-800 border border-red-200",
  Minor: "bg-orange-100 text-orange-800 border border-orange-200",
  No: "bg-green-100 text-green-800 border border-green-200",
};

const DATA_URL = "/api/events";
const FUNCTION_URL = process.env.NEXT_PUBLIC_FUNCTION_URL || "https://us-central1-ta-tracking-api.cloudfunctions.net/esg_scan";

export default function Home() {
  const [events, setEvents] = useState<EsgEvent[]>([]);
  const [ticker, setTicker] = useState("");
  const [type, setType] = useState("");
  const [severity, setSeverity] = useState("");
  const [controversy, setControversy] = useState("");
  const [loading, setLoading] = useState(true);
  const [triggering, setTriggering] = useState(false);
  const [scanTicker, setScanTicker] = useState("auto");
  const [allCompanies, setAllCompanies] = useState<Company[]>([]);
  const [lang, setLang] = useState<"vi" | "en">("en");

  useEffect(() => {
    fetch(DATA_URL).then(r => r.json()).then(setEvents).catch(console.error).finally(() => setLoading(false));
    fetch("/api/tickers").then(r => r.json()).then(setAllCompanies).catch(console.error);
  }, []);

  const filtered = useMemo(() => events.filter(e =>
    (!ticker || e.ticker === ticker) &&
    (!type || e.type === type) &&
    (!severity || e.severity === severity) &&
    (!controversy || (controversy === "none" ? !e.controversy_level : e.controversy_level === controversy))
  ), [events, ticker, type, severity, controversy]);

  const tickers = useMemo(() => [...new Set(events.map(e => e.ticker))].sort(), [events]);

  const counts = useMemo(() => ({
    total: filtered.length,
    e: filtered.filter(e => e.type === "E").length,
    s: filtered.filter(e => e.type === "S").length,
    g: filtered.filter(e => e.type === "G").length,
    high: filtered.filter(e => e.severity === "Cao").length,
    med: filtered.filter(e => e.severity === "Trung bình").length,
    major: filtered.filter(e => e.controversy_level === "Major").length,
    minor: filtered.filter(e => e.controversy_level === "Minor").length,
    no: filtered.filter(e => e.controversy_level === "No").length,
  }), [filtered]);

  const handleTrigger = async () => {
    setTriggering(true);
    try {
      const param = scanTicker === "auto" ? "mode=auto" : `tickers=${scanTicker}`;
      const res = await fetch(`${FUNCTION_URL}?${param}`);
      const data = await res.json();
      alert(`Done: ${data.tickers_scanned} companies, ${data.new_events} new events`);
      const updated = await fetch(DATA_URL).then(r => r.json());
      setEvents(updated);
    } catch (err) { alert(`Error: ${err}`); }
    finally { setTriggering(false); }
  };

  if (loading) return <div className="flex items-center justify-center min-h-screen text-gray-500">Loading...</div>;

  return (
    <main className="max-w-7xl mx-auto px-4 py-8">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold">ESG Risk Dashboard</h1>
        <div className="flex items-center gap-2">
          <div className="inline-flex border rounded-lg overflow-hidden text-sm">
            <button onClick={() => setLang("vi")}
              className={`px-3 py-2 ${lang === "vi" ? "bg-blue-600 text-white" : "bg-white text-gray-700 hover:bg-gray-50"}`}>
              VI
            </button>
            <button onClick={() => setLang("en")}
              className={`px-3 py-2 ${lang === "en" ? "bg-blue-600 text-white" : "bg-white text-gray-700 hover:bg-gray-50"}`}>
              EN
            </button>
          </div>
          <select value={scanTicker} onChange={e => setScanTicker(e.target.value)}
            className="border rounded-lg px-3 py-2 text-sm">
            <option value="auto">All</option>
            {allCompanies.map(c => <option key={c.ticker} value={c.ticker}>{c.ticker} - {c.company}</option>)}
          </select>
          <button onClick={handleTrigger} disabled={triggering}
            className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 text-sm">
            {triggering ? "Scanning..." : "Scan now"}
          </button>
        </div>
      </div>

      {/* Summary Cards */}
      <div className="grid grid-cols-2 md:grid-cols-6 gap-3 mb-3">
        <Card label="Total events" value={counts.total} color="bg-gray-50" />
        <Card label="Environment (E)" value={counts.e} color="bg-green-50" />
        <Card label="Social (S)" value={counts.s} color="bg-orange-50" />
        <Card label="Governance (G)" value={counts.g} color="bg-blue-50" />
        <Card label="High severity" value={counts.high} color="bg-red-50" />
        <Card label="Medium severity" value={counts.med} color="bg-yellow-50" />
      </div>
      <div className="grid grid-cols-3 gap-3 mb-6">
        <Card label="Major controversy" value={counts.major} color="bg-red-100" />
        <Card label="Minor controversy" value={counts.minor} color="bg-orange-100" />
        <Card label="No controversy" value={counts.no} color="bg-green-100" />
      </div>

      {/* Filters */}
      <div className="flex flex-wrap gap-3 mb-4">
        <select value={ticker} onChange={e => setTicker(e.target.value)}
          className="border rounded-lg px-3 py-2 text-sm">
          <option value="">All tickers</option>
          {tickers.map(t => <option key={t} value={t}>{t}</option>)}
        </select>
        <select value={type} onChange={e => setType(e.target.value)}
          className="border rounded-lg px-3 py-2 text-sm">
          <option value="">All types</option>
          <option value="E">E - Environment</option>
          <option value="S">S - Social</option>
          <option value="G">G - Governance</option>
        </select>
        <select value={severity} onChange={e => setSeverity(e.target.value)}
          className="border rounded-lg px-3 py-2 text-sm">
          <option value="">All severity</option>
          <option value="Cao">High</option>
          <option value="Trung bình">Medium</option>
        </select>
        <select value={controversy} onChange={e => setControversy(e.target.value)}
          className="border rounded-lg px-3 py-2 text-sm">
          <option value="">All controversy</option>
          <option value="Major">Major</option>
          <option value="Minor">Minor</option>
          <option value="No">No</option>
          <option value="none">Not classified</option>
        </select>
        {(ticker || type || severity || controversy) && (
          <button onClick={() => { setTicker(""); setType(""); setSeverity(""); setControversy(""); }}
            className="text-sm text-blue-600 hover:underline">Clear filters</button>
        )}
      </div>

      {/* Table */}
      <div className="overflow-x-auto border rounded-lg">
        <table className="w-full text-sm table-fixed min-w-[1200px]">
          <colgroup>
            <col className="w-[70px]" />
            <col className="w-[140px]" />
            <col className="w-[60px]" />
            <col className="w-[100px]" />
            <col />
            <col className="w-[90px]" />
            <col className="w-[110px]" />
            <col />
            <col className="w-[140px]" />
          </colgroup>
          <thead className="bg-gray-50">
            <tr>
              <th className="px-3 py-3 text-left font-medium">Ticker</th>
              <th className="px-3 py-3 text-left font-medium">Company</th>
              <th className="px-3 py-3 text-left font-medium">Type</th>
              <th className="px-3 py-3 text-left font-medium">Date</th>
              <th className="px-3 py-3 text-left font-medium">Summary</th>
              <th className="px-3 py-3 text-left font-medium">Severity</th>
              <th className="px-3 py-3 text-left font-medium">Controversy</th>
              <th className="px-3 py-3 text-left font-medium">Justification</th>
              <th className="px-3 py-3 text-left font-medium">Source</th>
            </tr>
          </thead>
          <tbody className="divide-y">
            {filtered.length === 0 ? (
              <tr><td colSpan={9} className="px-3 py-8 text-center text-gray-400">No data</td></tr>
            ) : filtered.map((e, i) => (
              <tr key={i} className="hover:bg-gray-50 align-top">
                <td className="px-3 py-2 font-medium">{e.ticker}</td>
                <td className="px-3 py-2">{e.company}</td>
                <td className="px-3 py-2">
                  <span className={`px-2 py-0.5 rounded text-xs font-medium ${TYPE_COLORS[e.type]}`}>
                    {e.type}
                  </span>
                </td>
                <td className="px-3 py-2 whitespace-nowrap">{e.date}</td>
                <td className="px-3 py-2 break-words">{lang === "en" ? (e.summary_en || e.summary) : e.summary}</td>
                <td className="px-3 py-2">
                  <span className={`font-medium ${e.severity === "Cao" ? "text-red-600" : "text-orange-500"}`}>
                    {SEVERITY_LABEL[e.severity] ?? e.severity}
                  </span>
                </td>
                <td className="px-3 py-2">
                  {e.controversy_level ? (
                    <span
                      className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${CONTROVERSY_STYLES[e.controversy_level] ?? "bg-gray-100 text-gray-600"}`}
                    >
                      {e.controversy_level}
                    </span>
                  ) : (
                    <span className="text-gray-300">—</span>
                  )}
                </td>
                <td className="px-3 py-2 break-words">
                  {e.controversy_justification ? (
                    <p className="text-xs text-gray-600 leading-snug">{e.controversy_justification}</p>
                  ) : (
                    <span className="text-gray-300">—</span>
                  )}
                </td>
                <td className="px-3 py-2">
                  {e.url ? <a href={e.url} target="_blank" rel="noopener noreferrer"
                    className="text-blue-600 hover:underline">{e.source}</a> : e.source}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="mt-4 text-xs text-gray-400">Total: {filtered.length} events | Data from Google Cloud Storage</p>
    </main>
  );
}

function Card({ label, value, color }: { label: string; value: number; color: string }) {
  return (
    <div className={`${color} rounded-lg p-3`}>
      <p className="text-xs text-gray-500">{label}</p>
      <p className="text-2xl font-bold">{value}</p>
    </div>
  );
}
