import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

const DATA_URL = "https://storage.googleapis.com/esg-risk-dashboard/esg_events.json";

export async function GET() {
  const res = await fetch(DATA_URL, { cache: "no-store" });
  const data = await res.json();
  return NextResponse.json(data, {
    headers: { "Cache-Control": "no-store, no-cache, must-revalidate" },
  });
}
