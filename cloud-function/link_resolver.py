"""
Resolve Google News RSS redirect links to real article URLs.

googlenewsdecoder requires external HTTP calls which fail on Cloud Functions
due to SSL issues. For now, we skip link resolution and store google_link
as-is. The dashboard can try client-side redirect or show source name only.
"""


def resolve_links(items, delay=0):
    """Pass through - copy google_link to url field without resolving."""
    for item in items:
        # Store the Google News link directly - it still redirects to real article in browser
        item["url"] = item.get("google_link", "")
    return items
