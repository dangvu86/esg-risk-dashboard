"use client";

import { useState, useEffect, useMemo } from "react";

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

const TYPE_LABELS = { E: "Môi trường", S: "Xã hội", G: "Quản trị" };

const DATA_URL = "/api/events";
const FUNCTION_URL = process.env.NEXT_PUBLIC_FUNCTION_URL || "https://us-central1-ta-tracking-api.cloudfunctions.net/esg_scan";

export default function Home() {
  const [events, setEvents] = useState<EsgEvent[]>([]);
  const [ticker, setTicker] = useState("");
  const [type, setType] = useState("");
  const [severity, setSeverity] = useState("");
  const [loading, setLoading] = useState(true);
  const [triggering, setTriggering] = useState(false);
  const [scanTicker, setScanTicker] = useState("auto");
  const [allCompanies, setAllCompanies] = useState<Company[]>([]);
  const [lang, setLang] = useState<"vi" | "en">("vi");

  useEffect(() => {
    fetch(DATA_URL).then(r => r.json()).then(setEvents).catch(console.error).finally(() => setLoading(false));
    fetch("/api/tickers").then(r => r.json()).then(setAllCompanies).catch(console.error);
  }, []);

  const filtered = useMemo(() => events.filter(e =>
    (!ticker || e.ticker === ticker) &&
    (!type || e.type === type) &&
    (!severity || e.severity === severity)
  ), [events, ticker, type, severity]);

  const tickers = useMemo(() => [...new Set(events.map(e => e.ticker))].sort(), [events]);

  const counts = useMemo(() => ({
    total: filtered.length,
    e: filtered.filter(e => e.type === "E").length,
    s: filtered.filter(e => e.type === "S").length,
    g: filtered.filter(e => e.type === "G").length,
    high: filtered.filter(e => e.severity === "Cao").length,
    med: filtered.filter(e => e.severity === "Trung bình").length,
  }), [filtered]);

  const handleTrigger = async () => {
    setTriggering(true);
    try {
      const param = scanTicker === "auto" ? "mode=auto" : `tickers=${scanTicker}`;
      const res = await fetch(`${FUNCTION_URL}?${param}`);
      const data = await res.json();
      alert(`Scan xong: ${data.tickers_scanned} công ty, ${data.new_events} events mới`);
      // Refresh data
      const updated = await fetch(DATA_URL).then(r => r.json());
      setEvents(updated);
    } catch (err) { alert(`Lỗi: ${err}`); }
    finally { setTriggering(false); }
  };

  if (loading) return <div className="flex items-center justify-center min-h-screen text-gray-500">Đang tải...</div>;

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
            {triggering ? "Đang scan..." : "Scan ngay"}
          </button>
        </div>
      </div>

      {/* Summary Cards */}
      <div className="grid grid-cols-2 md:grid-cols-6 gap-3 mb-6">
        <Card label="Tổng events" value={counts.total} color="bg-gray-50" />
        <Card label="Môi trường (E)" value={counts.e} color="bg-green-50" />
        <Card label="Xã hội (S)" value={counts.s} color="bg-orange-50" />
        <Card label="Quản trị (G)" value={counts.g} color="bg-blue-50" />
        <Card label="Severity Cao" value={counts.high} color="bg-red-50" />
        <Card label="Trung bình" value={counts.med} color="bg-yellow-50" />
      </div>

      {/* Filters */}
      <div className="flex flex-wrap gap-3 mb-4">
        <select value={ticker} onChange={e => setTicker(e.target.value)}
          className="border rounded-lg px-3 py-2 text-sm">
          <option value="">Tất cả mã CK</option>
          {tickers.map(t => <option key={t} value={t}>{t}</option>)}
        </select>
        <select value={type} onChange={e => setType(e.target.value)}
          className="border rounded-lg px-3 py-2 text-sm">
          <option value="">Tất cả loại</option>
          <option value="E">E - Môi trường</option>
          <option value="S">S - Xã hội</option>
          <option value="G">G - Quản trị</option>
        </select>
        <select value={severity} onChange={e => setSeverity(e.target.value)}
          className="border rounded-lg px-3 py-2 text-sm">
          <option value="">Tất cả mức độ</option>
          <option value="Cao">Cao</option>
          <option value="Trung bình">Trung bình</option>
        </select>
        {(ticker || type || severity) && (
          <button onClick={() => { setTicker(""); setType(""); setSeverity(""); }}
            className="text-sm text-blue-600 hover:underline">Xóa filter</button>
        )}
      </div>

      {/* Table */}
      <div className="overflow-x-auto border rounded-lg">
        <table className="w-full text-sm">
          <thead className="bg-gray-50">
            <tr>
              <th className="px-3 py-3 text-left font-medium">Mã CK</th>
              <th className="px-3 py-3 text-left font-medium">Công ty</th>
              <th className="px-3 py-3 text-left font-medium">Loại</th>
              <th className="px-3 py-3 text-left font-medium">Ngày</th>
              <th className="px-3 py-3 text-left font-medium">Tóm tắt</th>
              <th className="px-3 py-3 text-left font-medium">Mức độ</th>
              <th className="px-3 py-3 text-left font-medium">Nguồn</th>
            </tr>
          </thead>
          <tbody className="divide-y">
            {filtered.length === 0 ? (
              <tr><td colSpan={7} className="px-3 py-8 text-center text-gray-400">Không có dữ liệu</td></tr>
            ) : filtered.map((e, i) => (
              <tr key={i} className="hover:bg-gray-50">
                <td className="px-3 py-2 font-medium">{e.ticker}</td>
                <td className="px-3 py-2">{e.company}</td>
                <td className="px-3 py-2">
                  <span className={`px-2 py-0.5 rounded text-xs font-medium ${TYPE_COLORS[e.type]}`}>
                    {e.type} - {TYPE_LABELS[e.type]}
                  </span>
                </td>
                <td className="px-3 py-2 whitespace-nowrap">{e.date}</td>
                <td className="px-3 py-2 max-w-md">{lang === "en" ? (e.summary_en || e.summary) : e.summary}</td>
                <td className="px-3 py-2">
                  <span className={`font-medium ${e.severity === "Cao" ? "text-red-600" : "text-orange-500"}`}>
                    {e.severity}
                  </span>
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

      <p className="mt-4 text-xs text-gray-400">Tổng: {filtered.length} events | Data from Google Cloud Storage</p>
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
