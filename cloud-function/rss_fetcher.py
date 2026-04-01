"""
RSS fetcher for ESG news from Google News.
Reused logic from prepare_search.py skill script.
"""

import csv
import time
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

KEYWORD_GROUPS = {
    "E": "ô nhiễm OR xả thải OR môi trường OR khí thải",
    "S": "tai nạn OR tử vong OR đình công OR an toàn lao động",
    "G": "vi phạm OR xử phạt OR UBCKNN OR khởi tố OR thanh tra",
}


def load_companies(csv_path="Top100.csv"):
    """Load ticker -> company name mapping from CSV."""
    companies = {}
    path = Path(csv_path)
    if not path.exists():
        print(f"CSV not found: {csv_path}")
        return companies
    with open(path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ticker = row.get("Mã CK", "").strip()
            name = row.get("Tên Công ty", "").strip()
            if ticker and name:
                companies[ticker] = name
    print(f"Loaded {len(companies)} companies from CSV")
    return companies


def build_rss_url(company_name, keywords, after_date, before_date):
    """Build Google News RSS search URL."""
    query = f'intitle:"{company_name}" {keywords} after:{after_date} before:{before_date}'
    params = urllib.parse.urlencode({
        "q": query,
        "hl": "vi",
        "gl": "VN",
        "ceid": "VN:vi",
    })
    return f"https://news.google.com/rss/search?{params}"


def fetch_rss(url, retries=3):
    """Fetch RSS feed and return raw XML string."""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status == 200:
                    return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 30 * (attempt + 1)
                print(f"Rate limited (429), waiting {wait}s...")
                time.sleep(wait)
            else:
                print(f"HTTP {e.code} for {url[:80]}...")
                return None
        except Exception as e:
            print(f"RSS fetch error: {e}")
            if attempt < retries - 1:
                time.sleep(10)
    return None


def parse_rss_xml(xml_string):
    """Parse RSS XML and extract items."""
    items = []
    try:
        root = ET.fromstring(xml_string)
        for item in root.findall(".//item"):
            title = item.findtext("title", "").strip()
            link = item.findtext("link", "").strip()
            source_el = item.find("source")
            source = source_el.text.strip() if source_el is not None and source_el.text else ""
            pub_date_raw = item.findtext("pubDate", "").strip()

            parsed_date = ""
            if pub_date_raw:
                for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S"):
                    try:
                        dt = datetime.strptime(pub_date_raw[:25] if "GMT" not in pub_date_raw else pub_date_raw, fmt)
                        parsed_date = dt.strftime("%Y-%m-%d")
                        break
                    except ValueError:
                        continue
                if not parsed_date:
                    parsed_date = pub_date_raw[:10]

            if title:
                items.append({
                    "title": title,
                    "source": source,
                    "date": parsed_date,
                    "google_link": link,
                })
    except ET.ParseError as e:
        print(f"XML parse error: {e}")
    return items


def generate_date_chunks(start_date, end_date, months=6):
    """Split date range into chunks of N months."""
    chunks = []
    current = start_date
    while current < end_date:
        chunk_end = current + timedelta(days=months * 30)
        if chunk_end > end_date:
            chunk_end = end_date
        chunks.append((current.strftime("%Y-%m-%d"), chunk_end.strftime("%Y-%m-%d")))
        current = chunk_end
    return chunks


def fetch_company_news(company_name, days_back=7, delay=1.0):
    """Fetch RSS for one company across all keyword groups.

    days_back=7 for weekly update, days_back=1825 for 5-year batch.
    Automatically chunks long date ranges into 6-month pieces.
    """
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days_back)

    # Short range: single query per group. Long range: chunk by 6 months.
    if days_back <= 60:
        chunks = [(start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"))]
    else:
        chunks = generate_date_chunks(start_date, end_date, months=6)

    print(f"  Date range: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')} ({len(chunks)} chunks)")

    all_items = []
    total_queries = len(KEYWORD_GROUPS) * len(chunks)
    done = 0

    for group_key, keywords in KEYWORD_GROUPS.items():
        for after_date, before_date in chunks:
            done += 1
            url = build_rss_url(company_name, keywords, after_date, before_date)
            xml = fetch_rss(url)
            if xml:
                items = parse_rss_xml(xml)
                for item in items:
                    item["keyword_group"] = group_key
                all_items.extend(items)
                print(f"  [{done}/{total_queries}] {group_key} {after_date}~{before_date}: {len(items)} items")
            else:
                print(f"  [{done}/{total_queries}] {group_key} {after_date}~{before_date}: failed/empty")
            time.sleep(delay)

    # Dedup by exact title
    seen = set()
    unique = []
    for item in all_items:
        key = item["title"].lower().strip()
        if key not in seen:
            seen.add(key)
            unique.append(item)

    print(f"  Total: {len(all_items)} raw → {len(unique)} unique")
    return unique
