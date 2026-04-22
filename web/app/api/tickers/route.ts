import { NextResponse } from "next/server";

const DATA_URL = "https://storage.googleapis.com/esg-risk-dashboard/top100.json";

export async function GET() {
  const res = await fetch(DATA_URL, { next: { revalidate: 86400 } }); // cache 1 day
  const data = await res.json();
  return NextResponse.json(data);
}
