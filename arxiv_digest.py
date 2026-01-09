#!/usr/bin/env python3
"""
UW-Madison Astro-ph arXiv Digest (ADS Version)

Queries NASA ADS for astronomy papers from the past week with
UW-Madison affiliated authors.

Key fixes vs prior version:
- Broader ADS query (no longer requires "Wisconsin" to appear in aff)
- Uses ADS-side astronomy filter (fq=database:astronomy)
- UW-Madison affiliation matcher no longer rejects "UW–Madison" just because
  the word "Wisconsin" is missing
- More robust author↔aff mapping (handles missing/misaligned aff lists)
"""

import requests
import smtplib
import ssl
import os
import re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from collections import defaultdict
from itertools import zip_longest


# ADS API configuration
ADS_API_URL = "https://api.adsabs.harvard.edu/v1/search/query"

# Other UW system campuses to exclude
OTHER_UW_CAMPUSES = [
    "milwaukee", "green bay", "la crosse", "eau claire", "oshkosh",
    "parkside", "platteville", "river falls", "stevens point",
    "stout", "superior", "whitewater"
]

# Hints that often indicate "UW" means University of Washington (false positive risk)
UWASH_HINTS = [
    "university of washington",
    "uw seattle",
    "seattle, wa",
    "seattle wa",
    "washington, seattle",
    "dept. of astronomy, university of washington",
]

# Patterns for identifying UW-Madison affiliations
# NOTE: We intentionally allow "UW-Madison" even if "Wisconsin" isn't spelled out.
UW_MADISON_PATTERNS = [
    r"\buw[\s\-–—]*madison\b",
    r"\buniversity of wisconsin[\s,\-–—]*madison\b",
    r"\buniv\.?\s*(of\s*)?wisconsin[\s,\-–—]*madison\b",
    r"\bu\.?\s*of\s*w\.?[\s,\-–—]*madison\b",
    r"\bwisconsin[\s,\-–—]*madison\b",
    # common institute variants (optional but helps recall)
    r"\bspace science and engineering center\b.*\bmadison\b",
    r"\bssec\b.*\bmadison\b",
]

UW_MADISON_REGEX = re.compile("|".join(UW_MADISON_PATTERNS), re.IGNORECASE)


def is_uw_madison_affiliation(affiliation: str) -> bool:
    """
    Check if an affiliation string indicates UW-Madison.

    Accepts:
    - UW–Madison / UW Madison
    - University of Wisconsin–Madison (many punctuation variants)
    - Univ. of Wisconsin, Madison
    - etc.

    Excludes:
    - Other UW system campuses (Milwaukee, etc.)
    - University of Washington / Seattle contexts
    """
    if not affiliation:
        return False

    aff_lower = affiliation.lower()

    # Exclude University of Washington contexts (common false positives when "UW" appears)
    if any(h in aff_lower for h in UWASH_HINTS):
        return False

    # Exclude other UW system campuses (e.g., Milwaukee)
    if any(campus in aff_lower for campus in OTHER_UW_CAMPUSES):
        return False

    # Positive match against explicit UW–Madison / U Wisconsin–Madison patterns
    return bool(UW_MADISON_REGEX.search(affiliation))


def query_ads(api_key: str, days_back: int = 7, rows: int = 500, debug: bool = False) -> list:
    """
    Query ADS for recent astronomy papers that likely include UW-Madison authors.

    Strategy:
    1) Use an OR-based aff query to increase recall (doesn't require "Wisconsin")
    2) Use ADS-side filtering to astronomy database via fq=database:astronomy
    3) Filter down precisely in Python using is_uw_madison_affiliation()
    """

    end_date = datetime.now()
    start_date = end_date - timedelta(days=days_back)
    date_range = f"[{start_date:%Y-%m-%d} TO {end_date:%Y-%m-%d}]"

    # Broader recall query: many UW-Madison affiliations omit the literal "Wisconsin"
    # Quoted phrases improve precision; OR improves recall.
    query = (
        f'entdate:{date_range} AND ('
        f'aff:"University of Wisconsin" OR '
        f'aff:"University of Wisconsin - Madison" OR '
        f'aff:"University of Wisconsin–Madison" OR '
        f'aff:"UW-Madison" OR '
        f'aff:"UW Madison" OR '
        f'aff:"Univ of Wisconsin"'
        f')'
    )

    headers = {
        "Authorization": f"Bearer {api_key}",
    }

    # Use ADS-side astronomy filter; this is more reliable than guessing journals from bibcode.
    params = {
        "q": query,
        "fq": "database:astronomy",
        "fl": "title,author,aff,abstract,bibcode,identifier,keyword,pubdate,arxiv_class,bibstem,doctype",
        "rows": rows,
        "sort": "date desc",
    }

    if debug:
        print(f"DEBUG: ADS query: {query}")
        print(f"DEBUG: ADS fq: {params['fq']}")

    response = requests.get(ADS_API_URL, headers=headers, params=params, timeout=30)
    response.raise_for_status()

    data = response.json()
    papers = data.get("response", {}).get("docs", [])

    if debug:
        print(f"DEBUG: Raw ADS results: {len(papers)}")
        for p in papers[:15]:
            title = (p.get("title") or ["?"])[0]
            print(f"\n  Title: {title[:80]}")
            print(f"  pubdate: {p.get('pubdate')}")
            print(f"  doctype: {p.get('doctype')}")
            print(f"  arxiv_class: {p.get('arxiv_class')}")
            affs = p.get("aff") or []
            for i, aff in enumerate(affs[:5]):
                print(f"  aff[{i}]: {(aff or '')[:120]}")

    # Final filter: confirm UW-Madison affiliations in the returned set
    confirmed_papers = []
    for paper in papers:
        uw_authors = get_uw_authors(paper)
        if uw_authors:
            confirmed_papers.append(paper)

    if debug:
        print(f"\nDEBUG: After UW-Madison filter: {len(confirmed_papers)}")

    return confirmed_papers


def get_arxiv_id(paper: dict) -> str:
    """Extract arXiv ID from ADS paper record."""
    identifiers = paper.get("identifier", []) or []
    for ident in identifiers:
        if isinstance(ident, str) and ident.startswith("arXiv:"):
            return ident.replace("arXiv:", "")
    return None


def get_arxiv_url(paper: dict) -> str:
    """Get arXiv URL for a paper."""
    arxiv_id = get_arxiv_id(paper)
    if arxiv_id:
        return f"https://arxiv.org/abs/{arxiv_id}"
    bibcode = paper.get("bibcode", "")
    return f"https://ui.adsabs.harvard.edu/abs/{bibcode}"


def get_arxiv_category(paper: dict) -> str:
    """Get primary arXiv category (if present)."""
    classes = paper.get("arxiv_class", []) or []
    if classes:
        return classes[0]
    return "astronomy"


def get_uw_authors(paper: dict) -> list:
    """
    Extract UW-Madison affiliated authors from a paper.

    Robust to:
    - aff list shorter than author list
    - missing aff entries
    - occasional author↔aff misalignment

    If we can’t reliably map author-by-author but see UW–Madison in any affiliation,
    return a sentinel so the paper is still included.
    """
    authors = paper.get("author", []) or []
    affs = paper.get("aff", []) or []

    uw_authors = []
    for author, aff in zip_longest(authors, affs, fillvalue=""):
        if author and aff and is_uw_madison_affiliation(aff):
            uw_authors.append(author)

    # Fallback: include paper if ANY affiliation looks UW-Madison
    if not uw_authors:
        if any(is_uw_madison_affiliation(a) for a in affs if a):
            return ["(UW–Madison affiliation present; per-author mapping unavailable)"]

    return uw_authors


def format_paper_html(paper: dict) -> str:
    """Format a single paper as HTML."""
    title = (paper.get("title") or ["Untitled"])[0]
    authors = paper.get("author", []) or []
    abstract = paper.get("abstract", "No abstract available.")
    url = get_arxiv_url(paper)
    category = get_arxiv_category(paper)
    uw_authors = get_uw_authors(paper)

    author_str = ", ".join(authors[:10]) + (f" et al. ({len(authors)} authors)" if len(authors) > 10 else "")
    uw_str = ", ".join(uw_authors) if uw_authors else "Unknown"

    if len(abstract) > 500:
        abstract = abstract[:500] + "..."

    return f"""
    <div style="margin-bottom: 20px; padding: 15px; border-left: 3px solid #c5050c; background-color: #f9f9f9;">
        <h3 style="margin: 0 0 8px 0;">
            <a href="{url}" style="color: #0479a8; text-decoration: none;">{title}</a>
        </h3>
        <p style="margin: 0 0 8px 0; color: #c5050c; font-size: 14px;">
            <strong>UW-Madison:</strong> {uw_str}
        </p>
        <p style="margin: 0 0 8px 0; color: #666; font-size: 14px;">
            <strong>All Authors:</strong> {author_str}
        </p>
        <p style="margin: 0 0 8px 0; color: #666; font-size: 14px;">
            <strong>Category:</strong> {category}
        </p>
        <p style="margin: 0; font-size: 14px; line-height: 1.5;">
            {abstract}
        </p>
    </div>
    """


def format_paper_text(paper: dict) -> str:
    """Format a single paper as plain text."""
    title = (paper.get("title") or ["Untitled"])[0]
    authors = paper.get("author", []) or []
    abstract = paper.get("abstract", "No abstract available.")
    url = get_arxiv_url(paper)
    category = get_arxiv_category(paper)
    uw_authors = get_uw_authors(paper)

    author_str = ", ".join(authors[:10]) + (f" et al. ({len(authors)} authors)" if len(authors) > 10 else "")
    uw_str = ", ".join(uw_authors) if uw_authors else "Unknown"

    if len(abstract) > 500:
        abstract = abstract[:500] + "..."

    return f"""
{title}
{'-' * min(len(title), 80)}
UW-Madison: {uw_str}
All Authors: {author_str}
Category: {category}
Link: {url}

{abstract}

"""


def create_email_content(papers: list, days_back: int) -> tuple:
    """Create HTML and plain text email content."""
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days_back)
    date_range = f"{start_date.strftime('%B %d')} to {end_date.strftime('%B %d, %Y')}"

    if not papers:
        subject = "UW-Madison Astro-ph Digest: No papers this week"
        html = f"""
        <html>
        <body style="font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px;">
            <h1 style="color: #c5050c; border-bottom: 2px solid #c5050c; padding-bottom: 10px;">
                UW-Madison Astro-ph Digest
            </h1>
            <p style="color: #666;">Papers from {date_range}</p>
            <p>No papers with UW-Madison affiliated authors were found in ADS (astronomy database) for this window.</p>
        </body>
        </html>
        """
        text = f"UW-Madison Astro-ph Digest\n{date_range}\n\nNo papers found this week."
        return subject, html, text

    subject = f"UW-Madison Astro-ph Digest: {len(papers)} paper{'s' if len(papers) != 1 else ''} this week"

    by_category = defaultdict(list)
    for paper in papers:
        category = get_arxiv_category(paper)
        by_category[category].append(paper)

    html_papers = ""
    for cat in sorted(by_category.keys()):
        cat_papers = by_category[cat]
        html_papers += f'<h2 style="color: #333; margin-top: 30px;">{cat} ({len(cat_papers)})</h2>'
        for paper in cat_papers:
            html_papers += format_paper_html(paper)

    html = f"""
    <html>
    <body style="font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px;">
        <h1 style="color: #c5050c; border-bottom: 2px solid #c5050c; padding-bottom: 10px;">
            UW-Madison Astro-ph Digest
        </h1>
        <p style="color: #666;">Papers from {date_range}</p>
        <p style="font-size: 18px;"><strong>{len(papers)}</strong> paper{"s" if len(papers) != 1 else ""} with UW-Madison affiliated authors</p>
        {html_papers}
        <hr style="margin-top: 40px; border: none; border-top: 1px solid #ddd;">
        <p style="color: #999; font-size: 12px;">
            This digest is automatically generated using NASA ADS (astronomy database filter).
        </p>
    </body>
    </html>
    """

    text_papers = ""
    for cat in sorted(by_category.keys()):
        cat_papers = by_category[cat]
        text_papers += f"\n{'=' * 60}\n{cat} ({len(cat_papers)})\n{'=' * 60}\n"
        for paper in cat_papers:
            text_papers += format_paper_text(paper)

    text = f"""UW-Madison Astro-ph Digest
{date_range}

{len(papers)} paper{"s" if len(papers) != 1 else ""} with UW-Madison affiliated authors
{text_papers}
"""

    return subject, html, text


def send_email(subject: str, html_content: str, text_content: str):
    """Send the digest email."""
    smtp_server = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    sender_email = os.environ["SENDER_EMAIL"]
    sender_password = os.environ["SENDER_PASSWORD"]
    recipient_email = os.environ["RECIPIENT_EMAIL"]

    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = sender_email
    message["To"] = recipient_email

    message.attach(MIMEText(text_content, "plain"))
    message.attach(MIMEText(html_content, "html"))

    context = ssl.create_default_context()
    with smtplib.SMTP(smtp_server, smtp_port) as server:
        server.starttls(context=context)
        server.login(sender_email, sender_password)
        server.sendmail(sender_email, recipient_email, message.as_string())

    print(f"Email sent successfully to {recipient_email}")


def main():
    """Main function to run the digest."""
    api_key = os.environ.get("ADS_API_KEY")
    if not api_key:
        raise ValueError("ADS_API_KEY environment variable is required")

    days_back = int(os.environ.get("DAYS_BACK", "7"))
    debug = os.environ.get("DEBUG", "").lower() in ("1", "true", "yes")

    print(f"Querying ADS for UW-Madison astronomy papers from the last {days_back} days...")
    papers = query_ads(api_key, days_back=days_back, debug=debug)
    print(f"Found {len(papers)} papers with UW-Madison affiliations")

    for paper in papers:
        title = (paper.get("title") or ["Untitled"])[0]
        uw_authors = get_uw_authors(paper)
        print(f"  - {title[:70]}...")
        print(f"    UW authors: {', '.join(uw_authors)}")

    subject, html, text = create_email_content(papers, days_back)

    if os.environ.get("SENDER_EMAIL"):
        send_email(subject, html, text)
    else:
        print("\nEmail credentials not configured. Email content:")
        print(text)


if __name__ == "__main__":
    main()
