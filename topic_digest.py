#!/usr/bin/env python3
"""
Daily Topic-Based Astro-ph Digest (SR/EP only)

Queries NASA ADS for recent papers matching research interests.
Robust to ADS query-length limits by batching keyword queries and merging results.
Priority ORCID authors are pinned to the top of the digest.
"""

import os
import ssl
import random
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests


# -----------------------
# ADS API configuration
# -----------------------
ADS_API_URL = "https://api.adsabs.harvard.edu/v1/search/query"

# Priority ORCIDs - papers by these authors appear at the top
PRIORITY_ORCIDS = [
    "0009-0007-0488-5685",  # Ritvik Sai Narayan
    "0000-0001-7493-7419",  # Melinda Soares-Furtado
    "0009-0001-1360-8547",  # Julia Sheffler
    "0000-0001-7246-5438",  # Andrew Vanderburg
]

# Map priority ORCIDs to expected author names (fallback if ORCID not claimed)
PRIORITY_AUTHOR_NAMES = {
    "0009-0007-0488-5685": ["narayan, ritvik", "narayan, ritvik sai"],
    "0000-0001-7493-7419": ["soares-furtado, melinda"],
    "0009-0001-1360-8547": ["sheffler, julia"],
    "0000-0001-7246-5438": ["vanderburg, andrew"],
}


# Topic keywords to search for (also used for relevance scoring)
TOPIC_KEYWORDS = [
    "open cluster",
    "MESA",
    "NGC 188",
    "m dwarf",
    "gyrochronology",
    "stellar rotation",
    "exoplanet age",
    "planetary engulfment",
    "free-floating planet",
    "planet engulfment",
    "engulfment",
    "young stars",
    "TESS photometry",
    "stellar age",
    "rotational evolution",
    "starspot",
    "chromospheric activity",
    "Ursa Major",
    "Hyades",
    "Upper Sco",
    "gyrochronological",
    "age estimate",
    "age constraint",
    "lithium depletion",
    "lithium abundance",
    "lithium",
    "stellar pollution",
    "chemical abundance",
    "convective zone",
    "convective envelope",
    "transiting planet",
    "transiting exoplanet",
    "high-precision radial velocity",
    "asteroseismology",
    # Roman + exoplanets
    "Nancy Grace Roman Space Telescope",
    "Roman Space Telescope",
    "Roman wide field instrument",
    "Roman photometry",
    "debris disk",
    "transit survey",
    "transit search",
    "transit injection-recovery",
    "completeness",
    "planet validation",
    "joint transit RV fit",
    "radial velocity follow-up",
    "RV mass",
    "mass-radius relation",
    "occurrence rate",
    "planet demographics",
    "multi-planet system",
    "TTV",
    "Rossiter-McLaughlin",
    "spin-orbit",
    "obliquity",
    "transmission spectroscopy",
    "emission spectroscopy",
    "atmospheric retrieval",
    "clouds and hazes",
    "metallicity",
    "escape",
    "photoevaporation",
    "core-powered mass loss",
]

HIGH_VALUE_KEYWORDS = [
    "hydrodynamic simulation",
    "exoplanet discovery",
    "common envelope",
    "gyrochronology",
    "planetary engulfment",
    "planet engulfment",
    "engulfment",
    "lithium depletion",
    "lithium abundance",
    "stellar age",
    "young planet",
    "stellar pollution",
    "exoplanet yield",
]

# How many keywords per ADS query (keeps q length safely small)
KEYWORDS_PER_QUERY = int(os.environ.get("KEYWORDS_PER_QUERY", "12"))

# Silly encouraging welcome messages
WELCOME_MESSAGES = [
    "🏃‍♀️ Step by step, paper by paper. You're literally ascending while reading about the cosmos. Iconic.",
    "⭐ Here you are, staying up-to-date on your literature review. Nice work!",
    "🚀 Cardio + astro-ph = an insane form of multitasking. You're a rockstar.",
    "🌟 Fun fact: reading papers on a stepmill burns mass, just like a star. You're basically a main sequence queen.",
    "💪 Other people scroll Instagram at the gym. You read about stellar evolution. We are not the same.",
    "🔭 Your heart rate is up, your knowledge is expanding. The universe is proud of you.",
    "✨ Every step you take is one step closer to tenure and one step up the stepmill. Synergy!",
    "🌙 The Moon's escape velocity is 2.38 km/s. Your's must be higher because NOTHING can stop you.",
    "⚡ You're generating more power than a brown dwarf right now. Keep climbing!",
    "🍕 You could be eating pizza in bed. But no. You're on the stepmill. Reading about LITHIUM DEPLETION.",
]

BOTTOM_TREASURES = [
    ("🏁 FINISH LINE", "You crossed it. There's no medal. There's no ceremony. There's just the quiet satisfaction of knowing you read an entire digest while climbing to nowhere."),
    ("🏆 ACHIEVEMENT UNLOCKED", "'+1 Literature Awareness' - You have gained 50 XP in the skill 'Keeping Up With The Field.' Only 9,950 more XP until you feel caught up!"),
    ("🌈 WHOLESOME MOMENT", "Hey. Genuinely. It's hard to keep up with the literature while doing everything else. The fact that you're trying means a lot. You're doing great. 💜"),
]


# -----------------------
# Utility helpers
# -----------------------
def unique_preserve(seq):
    seen = set()
    out = []
    for x in seq:
        if x not in seen:
            out.append(x)
            seen.add(x)
    return out


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def _parse_pubdate(paper: dict) -> datetime:
    s = (paper.get("pubdate") or "")[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except Exception:
        return datetime.min


# -----------------------
# ADS query building
# -----------------------
def _ads_quote(kw: str) -> str:
    # Keep phrases safe inside quotes
    kw = kw.replace('"', '\\"')
    return f'"{kw}"'


def build_query(days_back: int, keywords_subset: list[str]) -> str:
    """
    Build a short ADS query:
      - restrict to astro-ph.SR or astro-ph.EP
      - restrict by entdate window
      - require a match in the ADS 'abs' combo field (title+abstract+keyword)
    """
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days_back)
    date_range = f"[{start_date.strftime('%Y-%m-%d')} TO {end_date.strftime('%Y-%m-%d')}]"

    # ADS "abs" is a combo field (title+abstract+keyword), so this is much shorter
    # than repeating (title OR abs) for every keyword.
    kw_terms = " OR ".join(_ads_quote(k) for k in keywords_subset)
    kw_clause = f"abs:({kw_terms})"

    sr_ep_only = '(arxiv_class:"astro-ph.SR" OR arxiv_class:"astro-ph.EP")'

    return f"{sr_ep_only} AND {kw_clause} AND entdate:{date_range}"


def query_ads(api_key: str, q: str, rows: int = 200) -> list[dict]:
    """GET from ADS search API."""
    headers = {
        "Authorization": f"Bearer {api_key}",
    }
    params = {
        "q": q,
        "fl": "title,author,aff,abstract,bibcode,identifier,keyword,pubdate,arxiv_class,orcid_pub,orcid_user,orcid_other",
        "rows": rows,
        "sort": "date desc",
    }
    r = requests.get(ADS_API_URL, headers=headers, params=params, timeout=60)
    r.raise_for_status()
    return r.json().get("response", {}).get("docs", [])


def query_topic_papers(api_key: str, days_back: int = 1, rows: int = 500) -> list[dict]:
    """
    Run multiple ADS queries over keyword chunks and merge by bibcode.
    """
    kws = unique_preserve(TOPIC_KEYWORDS)
    merged = {}
    per_query_rows = max(50, rows // max(1, (len(kws) + KEYWORDS_PER_QUERY - 1) // KEYWORDS_PER_QUERY))

    for subset in chunked(kws, KEYWORDS_PER_QUERY):
        q = build_query(days_back, subset)
        docs = query_ads(api_key, q, rows=per_query_rows)
        for d in docs:
            bc = d.get("bibcode")
            if bc:
                merged[bc] = d

    return list(merged.values())


# -----------------------
# ORCID + relevance
# -----------------------

def normalize_name(name: str) -> str:
    """Normalize author name for matching (Last, First M. -> last first)"""
    return name.lower().replace(",", "").replace(".", "").strip()
    
def get_paper_orcids(paper: dict) -> set:
    orcids = set()
    for field in ("orcid_pub", "orcid_user", "orcid_other"):
        for oid in paper.get(field, []) or []:
            if oid and oid != "-":
                orcids.add(oid)
    return orcids


def has_priority_author(paper: dict) -> bool:
    # First try ORCID matching
    if get_paper_orcids(paper).intersection(PRIORITY_ORCIDS):
        return True
    
    # Fallback to name matching
    authors = paper.get("author", []) or []
    normalized_authors = [normalize_name(a) for a in authors]
    
    for orcid, names in PRIORITY_AUTHOR_NAMES.items():
        for name in names:
            if normalize_name(name) in normalized_authors:
                return True
    
    return False


def get_priority_authors(paper: dict) -> list:
    priority_authors = []
    authors = paper.get("author", []) or []
    
    # Get ORCID-matched authors
    for field in ("orcid_pub", "orcid_user", "orcid_other"):
        orcids = paper.get(field, []) or []
        for i, oid in enumerate(orcids):
            if oid in PRIORITY_ORCIDS and i < len(authors):
                if authors[i] not in priority_authors:
                    priority_authors.append(authors[i])
    
    # Get name-matched authors (fallback)
    normalized_authors = [normalize_name(a) for a in authors]
    for i, norm_name in enumerate(normalized_authors):
        for orcid, names in PRIORITY_AUTHOR_NAMES.items():
            for name in names:
                if normalize_name(name) == norm_name:
                    if authors[i] not in priority_authors:
                        priority_authors.append(authors[i])
    
    return priority_authors


def calculate_relevance_score(paper: dict) -> int:
    score = 0
    title = (paper.get("title", [""])[0] or "").lower()
    abstract = (paper.get("abstract") or "").lower()

    # Pin priority authors strongly (but not automatic "must read")
    if has_priority_author(paper):
        score += 25

    hv = [k.lower() for k in HIGH_VALUE_KEYWORDS]
    hv_set = set(hv)

    for kw in hv:
        if kw in title:
            score += 15
        elif kw in abstract:
            score += 10

    for kw in (k.lower() for k in TOPIC_KEYWORDS):
        if kw in hv_set:
            continue
        if kw in title:
            score += 5
        elif kw in abstract:
            score += 3

    return score


def get_relevance_tier(score: int) -> tuple:
    if score >= 20:
        return ("🔴", "#c5050c", "MUST READ", "#fff0f0")
    if score >= 10:
        return ("🟠", "#e67e00", "RELEVANT", "#fff8f0")
    if score >= 2:
        return ("🟡", "#d4a017", "SOMEWHAT RELEVANT", "#fffef0")
    return ("⚪", "#888888", "GENERAL", "#f9f9f9")


def sort_papers(papers: list[dict]) -> list[dict]:
    """
    Priority-first, then score, then recency.
    (This is what you actually want for "priority sorting".)
    """
    return sorted(
        papers,
        key=lambda p: (has_priority_author(p), calculate_relevance_score(p), _parse_pubdate(p)),
        reverse=True,
    )


# -----------------------
# Formatting helpers
# -----------------------
def get_arxiv_id(paper: dict) -> str | None:
    for ident in paper.get("identifier", []) or []:
        if ident.startswith("arXiv:"):
            return ident.replace("arXiv:", "")
    return None


def get_arxiv_url(paper: dict) -> str:
    arxiv_id = get_arxiv_id(paper)
    if arxiv_id:
        return f"https://arxiv.org/abs/{arxiv_id}"
    bibcode = paper.get("bibcode", "")
    return f"https://ui.adsabs.harvard.edu/abs/{bibcode}"


def get_arxiv_category(paper: dict) -> str:
    classes = paper.get("arxiv_class", []) or []
    return classes[0] if classes else "astro-ph"


def format_paper_html(paper: dict) -> str:
    title = paper.get("title", ["Untitled"])[0]
    authors = paper.get("author", []) or []
    abstract = paper.get("abstract", "No abstract available.") or "No abstract available."
    url = get_arxiv_url(paper)
    category = get_arxiv_category(paper)
    priority_authors = get_priority_authors(paper)

    score = calculate_relevance_score(paper)
    emoji, color, label, bg_color = get_relevance_tier(score)

    author_str = ", ".join(authors[:6]) + (f" + {len(authors) - 6} more" if len(authors) > 6 else "")

    truncated_abstract = abstract
    if len(truncated_abstract) > 400:
        truncated_abstract = truncated_abstract[:400].rsplit(" ", 1)[0] + "..."

    priority_badge = ""
    if priority_authors:
        priority_badge = f"""
            <p style="margin: 0 0 8px 0; color: #c5050c; font-weight: bold; font-size: 14px;">
                ⭐ {", ".join(priority_authors)}
            </p>
        """

    return f"""
    <div style="margin-bottom: 25px; padding: 15px; border-left: 6px solid {color}; background-color: {bg_color};">
        <div style="margin-bottom: 10px;">
            <span style="font-size: 24px; margin-right: 10px;">{emoji}</span>
            <span style="background-color: {color}; color: white; padding: 3px 10px; border-radius: 12px; font-size: 12px; font-weight: bold;">
                {label}
            </span>
            <span style="color: #888; font-size: 12px; margin-left: 10px;">{category}</span>
        </div>
        <h3 style="margin: 0 0 8px 0;">
            <a href="{url}" style="color: #0479a8; text-decoration: none;">{title}</a>
        </h3>
        {priority_badge}
        <p style="margin: 0 0 10px 0; color: #666; font-size: 14px;">
            {author_str}
        </p>
        <p style="margin: 0; font-size: 14px; line-height: 1.5; color: #444;">
            {truncated_abstract} <a href="{url}" style="color: #0479a8; text-decoration: none;">[read more]</a>
        </p>
    </div>
    """


def format_paper_text(paper: dict) -> str:
    title = paper.get("title", ["Untitled"])[0]
    authors = paper.get("author", []) or []
    abstract = paper.get("abstract", "No abstract available.") or "No abstract available."
    url = get_arxiv_url(paper)
    category = get_arxiv_category(paper)
    priority_authors = get_priority_authors(paper)

    score = calculate_relevance_score(paper)
    emoji, _, label, _ = get_relevance_tier(score)

    author_str = ", ".join(authors[:15]) + (f" et al. ({len(authors)} authors)" if len(authors) > 15 else "")
    priority_line = f"⭐ PRIORITY AUTHOR: {', '.join(priority_authors)}\n" if priority_authors else ""

    return f"""
{emoji} [{label}]
{title}
{'-' * min(len(title), 80)}
{priority_line}Authors: {author_str}
Category: {category}
Link: {url}

{abstract}

"""


# -----------------------
# Email creation + sending
# -----------------------
def create_email_content(papers: list[dict], days_back: int) -> tuple[str, str, str]:
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days_back)
    date_range = f"{start_date.strftime('%B %d')} - {end_date.strftime('%B %d, %Y')}"

    sorted_papers = sort_papers(papers)

    tier_counts = {"🔴": 0, "🟠": 0, "🟡": 0, "⚪": 0}
    for p in papers:
        emoji, _, _, _ = get_relevance_tier(calculate_relevance_score(p))
        tier_counts[emoji] += 1

    welcome = random.choice(WELCOME_MESSAGES)
    treasure_title, treasure_content = random.choice(BOTTOM_TREASURES)

    if not papers:
        subject = "Astro-ph Topic Digest: No papers today"
        html = f"""
        <html><body style="font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px;">
            <h1 style="color: #0479a8; border-bottom: 2px solid #0479a8; padding-bottom: 10px;">Daily Astro-ph Topic Digest</h1>
            <div style="background-color: #f0f8ff; padding: 15px; border-radius: 10px; margin-bottom: 20px; font-size: 16px;">
                {welcome}
            </div>
            <p style="color: #666;">Papers from {date_range}</p>
            <p>No papers matching your interests were found today. Rest day for your brain! 🧘</p>
        </body></html>
        """
        text = f"Daily Astro-ph Topic Digest\n{date_range}\n\n{welcome}\n\nNo papers found today."
        return subject, html, text

    subject = f"Astro-ph Digest: {tier_counts['🔴']}🔴 {tier_counts['🟠']}🟠 {tier_counts['🟡']}🟡 ({len(papers)} total)"

    html_papers = "".join(format_paper_html(p) for p in sorted_papers)
    html = f"""
    <html>
    <body style="font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px;">
        <h1 style="color: #0479a8; border-bottom: 2px solid #0479a8; padding-bottom: 10px;">
            Daily Astro-ph Topic Digest
        </h1>

        <div style="background-color: #f0f8ff; padding: 15px; border-radius: 10px; margin-bottom: 20px; font-size: 16px; line-height: 1.5;">
            {welcome}
        </div>

        <p style="color: #666;">Papers from {date_range}</p>

        <div style="background-color: #f5f5f5; padding: 15px; border-radius: 10px; margin-bottom: 25px;">
            <p style="margin: 0; font-size: 18px;">
                <strong>{len(papers)} papers</strong> today:
                <span style="margin-left: 15px;">🔴 {tier_counts['🔴']} must-read</span>
                <span style="margin-left: 10px;">🟠 {tier_counts['🟠']} relevant</span>
                <span style="margin-left: 10px;">🟡 {tier_counts['🟡']} interesting</span>
                <span style="margin-left: 10px;">⚪ {tier_counts['⚪']} general</span>
            </p>
        </div>

        {html_papers}

        <div style="margin-top: 50px; padding: 20px; background: #5b5fc7; border-radius: 15px; color: white; text-align: center;">
            <h2 style="margin: 0 0 10px 0;">{treasure_title}</h2>
            <p style="margin: 0; font-size: 14px; line-height: 1.6;">
                {treasure_content}
            </p>
        </div>

        <hr style="margin-top: 40px; border: none; border-top: 1px solid #ddd;">
        <p style="color: #999; font-size: 12px;">
            This digest is automatically generated using NASA ADS. Keep climbing! 🏔️
        </p>
    </body>
    </html>
    """

    text_papers = "".join(format_paper_text(p) for p in sorted_papers)
    text = f"""Daily Astro-ph Topic Digest
{date_range}

{welcome}

{len(papers)} papers today:
  🔴 {tier_counts['🔴']} must-read
  🟠 {tier_counts['🟠']} relevant
  🟡 {tier_counts['🟡']} interesting
  ⚪ {tier_counts['⚪']} general

{'=' * 60}
{text_papers}

{'=' * 60}
{treasure_title}
{'=' * 60}
{treasure_content}
"""
    return subject, html, text


def send_email(subject: str, html_content: str, text_content: str):
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


# -----------------------
# Main
# -----------------------
def main():
    api_key = os.environ.get("ADS_API_KEY")
    if not api_key:
        raise ValueError("ADS_API_KEY environment variable is required")

    days_back = int(os.environ.get("DAYS_BACK", "1"))

    print(f"Querying ADS for topic-relevant papers from the last {days_back} days...")
    print(f"Priority ORCIDs: {PRIORITY_ORCIDS}")
    print(f"Batch size: {KEYWORDS_PER_QUERY} keywords/query")

    papers = query_topic_papers(api_key, days_back=days_back, rows=500)
    print(f"Found {len(papers)} unique papers (merged across batches)")

    # Count by tier
    tier_counts = {"🔴": 0, "🟠": 0, "🟡": 0, "⚪": 0}
    for p in papers:
        emoji, _, _, _ = get_relevance_tier(calculate_relevance_score(p))
        tier_counts[emoji] += 1

    print(f"  🔴 {tier_counts['🔴']} must-read")
    print(f"  🟠 {tier_counts['🟠']} relevant")
    print(f"  🟡 {tier_counts['🟡']} interesting")
    print(f"  ⚪ {tier_counts['⚪']} general")

    subject, html, text = create_email_content(papers, days_back)

    if os.environ.get("SENDER_EMAIL"):
        send_email(subject, html, text)
    else:
        print("\nEmail credentials not configured. Email content:")
        print(text)


if __name__ == "__main__":
    main()
