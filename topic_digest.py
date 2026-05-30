#!/usr/bin/env python3
"""
Daily Topic-Based Astro-ph Digest (SR/EP only)

Queries NASA ADS for recent papers matching research interests.
Robust to ADS query-length limits by batching keyword queries and merging results.
Priority ORCID authors are pinned to the top of the digest.
"""

import os
import re
import ssl
import time
import json
import random
import smtplib
from pathlib import Path
from collections import Counter
from urllib.parse import urlencode
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication

import requests

from keyword_utils import (
    paper_text,
    document_freq,
    ads_keyword_freq,
    yake_keywords,
    count_in_texts,
    is_arxiv_category,
)


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
    "0009-0006-4294-6760",  # Adam Distler
    "0009-0001-9841-0846",  # Erin Motherway
]

# Map priority ORCIDs to expected author names (fallback if ORCID not claimed)
PRIORITY_AUTHOR_NAMES = {
    "0009-0007-0488-5685": ["narayan, ritvik", "narayan, ritvik sai"],
    "0000-0001-7493-7419": ["soares-furtado, melinda"],
    "0009-0001-1360-8547": ["sheffler, julia"],
    "0000-0001-7246-5438": ["vanderburg, andrew"],
    "0009-0006-4294-6760": ["distler, adam"],
    "0009-0001-9841-0846": ["motherway, erin"]
}


# Topic keywords to search for (also used for relevance scoring).
# These are the embedded fallback used only if keywords.json is missing/unreadable.
DEFAULT_TOPIC_KEYWORDS = [
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

DEFAULT_HIGH_VALUE_KEYWORDS = [
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

# ---------------------------------------------------------------------------
# Keyword loading: curated "seed_*" lists (kept forever) + "auto_keywords"
# appended monthly by update_keywords.py. See keywords.json.
# ---------------------------------------------------------------------------
KEYWORDS_FILE = Path(__file__).with_name("keywords.json")


def load_keywords() -> tuple[list, list]:
    """Return (topic_keywords, high_value_keywords) from keywords.json.

    Falls back to the embedded DEFAULT_* lists if the file is missing or
    unreadable. Auto-mined buzzwords are merged into the topic-tier list.
    """
    try:
        data = json.loads(KEYWORDS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = {}

    seed_topic = data.get("seed_topic_keywords") or DEFAULT_TOPIC_KEYWORDS
    auto = data.get("auto_keywords") or []
    selected = data.get("selected_keywords") or []
    high = data.get("seed_high_value_keywords") or DEFAULT_HIGH_VALUE_KEYWORDS

    # Merge seeds + auto-mined + email-selected, de-duplicated, order preserved.
    topic = list(dict.fromkeys([*seed_topic, *auto, *selected]))
    return topic, high


TOPIC_KEYWORDS, HIGH_VALUE_KEYWORDS = load_keywords()

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
            {abstract} <a href="{url}" style="color: #0479a8; text-decoration: none;">[read more]</a>
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
# PDF attachments (top-N by relevance)
# -----------------------
PDF_TOP_N = int(os.environ.get("PDF_TOP_N", "5"))
PDF_MAX_MB = float(os.environ.get("PDF_MAX_MB", "8"))          # per-file cap
PDF_TOTAL_MAX_MB = float(os.environ.get("PDF_TOTAL_MAX_MB", "20"))  # total cap (Gmail allows ~25)
PDF_USER_AGENT = "arxiv-digest/1.0 (https://github.com/ritviksainarayan/arxiv-digest)"


def _pdf_filename(paper: dict) -> str:
    """The paper's title as the PDF filename (sanitized for the filesystem)."""
    title = (paper.get("title", ["paper"])[0] or "paper").strip()
    # Replace characters that are illegal/awkward in filenames, collapse spaces.
    name = re.sub(r'[\\/:*?"<>|]+', " ", title)
    name = re.sub(r"\s+", " ", name).strip()
    name = name[:180].rstrip(". ")  # keep well under filesystem name limits
    if not name:
        name = get_arxiv_id(paper) or paper.get("bibcode", "paper")
    return f"{name}.pdf"


def fetch_pdf(paper: dict) -> bytes | None:
    """Download a paper's PDF from arXiv. Returns bytes or None on any failure."""
    arxiv_id = get_arxiv_id(paper)
    if not arxiv_id:
        return None
    url = f"https://arxiv.org/pdf/{arxiv_id}"
    try:
        r = requests.get(url, headers={"User-Agent": PDF_USER_AGENT}, timeout=90)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"  PDF fetch failed for {arxiv_id}: {e}")
        return None

    content = r.content
    if not content.startswith(b"%PDF-"):
        print(f"  Skipping {arxiv_id}: response was not a PDF ({len(content)} bytes)")
        return None
    if len(content) > PDF_MAX_MB * 1024 * 1024:
        print(f"  Skipping {arxiv_id}: {len(content) / 1e6:.1f} MB exceeds {PDF_MAX_MB} MB cap")
        return None
    return content


def collect_top_pdfs(sorted_papers: list, top_n: int = PDF_TOP_N) -> list:
    """Fetch up to top_n PDFs, respecting per-file and total size caps.

    Returns a list of (filename, bytes) tuples.
    """
    attachments = []
    total = 0
    for paper in sorted_papers[:top_n]:
        data = fetch_pdf(paper)
        if not data:
            continue
        if total + len(data) > PDF_TOTAL_MAX_MB * 1024 * 1024:
            print(f"  Total PDF size cap ({PDF_TOTAL_MAX_MB} MB) reached; stopping.")
            break
        attachments.append((_pdf_filename(paper), data))
        total += len(data)
        print(f"  Attached {attachments[-1][0]} ({len(data) / 1e6:.1f} MB)")
        time.sleep(1)  # be polite to arXiv
    return attachments


# -----------------------
# Suggested keywords (harvested from the day's papers)
# -----------------------
GH_REPO = os.environ.get("GH_REPO", "ritviksainarayan/arxiv-digest")
SUGGEST_TOP_N = int(os.environ.get("SUGGEST_TOP_N", "15"))
# Minimum papers a free-text phrase must appear in to be suggested. ADS-curated
# keywords bypass this (they're already vetted). Raise to be stricter.
FREETEXT_MIN_PAPERS = int(os.environ.get("FREETEXT_MIN_PAPERS", "3"))


def _norm_key(phrase: str) -> str:
    """Normalize a phrase for dup-matching: lowercase + de-pluralize last word.

    So "open clusters" matches existing "open cluster", "m dwarfs" matches
    "m dwarf", etc.
    """
    words = phrase.lower().split()
    if words and words[-1].endswith("s") and len(words[-1]) > 3:
        words[-1] = words[-1][:-1]
    return " ".join(words)


def _is_trackable(phrase: str, existing_lower: set, existing_norm: set) -> bool:
    """True if a phrase is a usable new keyword (not tracked, not a category)."""
    p = phrase.lower()
    if p in existing_lower or _norm_key(phrase) in existing_norm:
        return False
    if is_arxiv_category(phrase):
        return False
    return len(p) >= 3


def harvest_candidate_keywords(papers: list, existing_lower: set, top_n: int = SUGGEST_TOP_N) -> list:
    """Suggest new keyword phrases from today's papers, NOT already tracked.

    Sources, in priority order:
      1. Author-curated ADS keywords (UAT thesaurus terms — clean and specific).
      2. YAKE-extracted phrases from the day's titles + abstracts (statistical
         keyword extraction; far less noisy than raw n-grams).
      3. Plain n-gram document frequency — only as a fallback if YAKE isn't
         installed.

    Returns a list of (phrase, n_papers) tuples, best first. Excludes anything
    already tracked (singular/plural aware) and arXiv category names.
    """
    texts = [paper_text(p) for p in papers]
    texts_lower = [t.lower() for t in texts]
    ads = ads_keyword_freq(papers)       # already excludes arXiv categories
    existing_norm = {_norm_key(e) for e in existing_lower}

    ordered = []  # candidate phrases, best first, de-duplicated by norm key
    seen_norm = set()

    def add(phrase):
        nk = _norm_key(phrase)
        if nk in seen_norm or not _is_trackable(phrase, existing_lower, existing_norm):
            return
        seen_norm.add(nk)
        ordered.append(phrase)

    # 1. ADS keywords first, most-common across today's papers first.
    for phrase, _c in ads.most_common():
        add(phrase)

    # 2. YAKE phrases (or n-gram fallback).
    yake_phrases = yake_keywords(texts, top_n=top_n * 3)
    if yake_phrases is None:
        # Fallback: multi-word n-grams seen in >= FREETEXT_MIN_PAPERS papers.
        df = document_freq(texts)
        fallback = sorted(
            ((c, p) for p, c in df.items() if " " in p and c >= FREETEXT_MIN_PAPERS),
            reverse=True,
        )
        for _c, phrase in fallback:
            add(phrase)
    else:
        for phrase in yake_phrases:
            add(phrase)

    # Drop a phrase fully contained within an already-accepted longer one.
    accepted, out = [], []
    for phrase in ordered:
        if any(phrase != a and phrase in a for a in accepted):
            continue
        accepted.append(phrase)
        out.append((phrase, count_in_texts(phrase, texts_lower)))
        if len(out) >= top_n:
            break
    return out


def build_keyword_issue_url(candidates: list, date_str: str) -> str:
    """A GitHub 'new issue' URL pre-filled with a checklist of candidates.

    Checking boxes on the resulting issue triggers the Keyword Selection
    workflow, which appends the ticked keywords to keywords.json.
    """
    title = f"Keyword selection: {date_str}"
    body_lines = [
        "Tick the keywords you want to add to your digest, then the",
        "**Keyword Selection** workflow will add them automatically.",
        "",
    ]
    body_lines += [f"- [ ] {kw}" for kw, _ in candidates]
    body_lines += [
        "",
        "<!-- keyword-selection: do not change the title -->",
    ]
    # NOTE: no `labels=` param — GitHub 404s a prefilled new-issue link if the
    # label doesn't already exist in the repo. The workflow matches on the issue
    # title prefix instead, so a label is not needed.
    query = urlencode({
        "title": title,
        "body": "\n".join(body_lines),
    })
    return f"https://github.com/{GH_REPO}/issues/new?{query}"


def format_suggestions_html(candidates: list, date_str: str) -> str:
    if not candidates:
        return ""
    url = build_keyword_issue_url(candidates, date_str)
    chips = "".join(
        f'<span style="display:inline-block; background:#eef4fb; color:#0479a8; '
        f'border:1px solid #cfe2f3; border-radius:12px; padding:3px 10px; '
        f'margin:3px 4px 3px 0; font-size:13px;">{kw} '
        f'<span style="color:#999;">×{count}</span></span>'
        for kw, count in candidates
    )
    return f"""
    <div style="margin-top: 40px; padding: 18px; background:#fafcff; border:1px dashed #cfe2f3; border-radius:12px;">
        <h2 style="margin:0 0 6px 0; color:#0479a8; font-size:18px;">🔎 New keywords from today's papers</h2>
        <p style="margin:0 0 12px 0; color:#666; font-size:14px;">
            Terms appearing today that aren't in your list yet. Pick the ones worth tracking:
        </p>
        <div style="margin-bottom:14px;">{chips}</div>
        <a href="{url}"
           style="display:inline-block; background:#0479a8; color:white; text-decoration:none;
                  padding:10px 18px; border-radius:8px; font-size:14px; font-weight:bold;">
            ✅ Select keywords to add →
        </a>
        <p style="margin:12px 0 0 0; color:#999; font-size:12px;">
            Opens a pre-filled GitHub issue. Tick the boxes you want and the rest is automatic.
        </p>
    </div>
    """


def format_suggestions_text(candidates: list, date_str: str) -> str:
    if not candidates:
        return ""
    url = build_keyword_issue_url(candidates, date_str)
    lines = ["", "=" * 60, "NEW KEYWORDS FROM TODAY'S PAPERS (not in your list yet):", "=" * 60]
    lines += [f"  - {kw}  (x{count})" for kw, count in candidates]
    lines += ["", f"Select which to add: {url}", ""]
    return "\n".join(lines)


# -----------------------
# Email creation + sending
# -----------------------
def create_email_content(papers: list[dict], days_back: int, attach_count: int = 0,
                         candidates: list | None = None) -> tuple[str, str, str]:
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days_back)
    date_range = f"{start_date.strftime('%B %d')} - {end_date.strftime('%B %d, %Y')}"

    suggest_html = format_suggestions_html(candidates or [], end_date.strftime("%Y-%m-%d"))
    suggest_text = format_suggestions_text(candidates or [], end_date.strftime("%Y-%m-%d"))

    sorted_papers = sort_papers(papers)

    attach_note_html = (
        f'<p style="margin: 8px 0 0 0; font-size: 14px; color: #0479a8;">'
        f'📎 Top {attach_count} paper{"s" if attach_count != 1 else ""} attached as PDF.</p>'
        if attach_count else ""
    )
    attach_note_text = (
        f"\n📎 Top {attach_count} paper{'s' if attach_count != 1 else ''} attached as PDF.\n"
        if attach_count else ""
    )

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
            {attach_note_html}
        </div>

        {html_papers}

        {suggest_html}

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
{attach_note_text}
{'=' * 60}
{text_papers}
{suggest_text}
{'=' * 60}
{treasure_title}
{'=' * 60}
{treasure_content}
"""
    return subject, html, text


def send_email(subject: str, html_content: str, text_content: str, attachments: list | None = None):
    smtp_server = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    sender_email = os.environ["SENDER_EMAIL"]
    sender_password = os.environ["SENDER_PASSWORD"]
    recipient_email = os.environ["RECIPIENT_EMAIL"]

    # "mixed" root so we can carry both the text/html body and PDF attachments.
    message = MIMEMultipart("mixed")
    message["Subject"] = subject
    message["From"] = sender_email
    message["To"] = recipient_email

    body = MIMEMultipart("alternative")
    body.attach(MIMEText(text_content, "plain"))
    body.attach(MIMEText(html_content, "html"))
    message.attach(body)

    for filename, data in (attachments or []):
        part = MIMEApplication(data, _subtype="pdf")
        # RFC 2231 tuple form (charset, lang, value) so non-ASCII titles in the
        # filename are encoded as pure-ASCII `filename*=utf-8''...`. Passing a raw
        # non-ASCII str here corrupts the message and makes Gmail drop body parts.
        part.add_header("Content-Disposition", "attachment", filename=("utf-8", "", filename))
        message.attach(part)

    context = ssl.create_default_context()
    with smtplib.SMTP(smtp_server, smtp_port) as server:
        server.starttls(context=context)
        server.login(sender_email, sender_password)
        server.sendmail(sender_email, recipient_email, message.as_string())

    n = len(attachments or [])
    print(f"Email sent successfully to {recipient_email}" + (f" with {n} PDF attachment(s)" if n else ""))


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

    # Fetch the top-N papers (by the same ranking used in the email) as PDFs.
    attachments = []
    if papers and os.environ.get("SENDER_EMAIL") and PDF_TOP_N > 0:
        print(f"\nFetching top {PDF_TOP_N} PDFs from arXiv...")
        attachments = collect_top_pdfs(sort_papers(papers), PDF_TOP_N)

    # Suggest new keywords from today's papers (excluding ones already tracked).
    # Try the LLM first; fall back to ADS-keyword harvesting if it's unavailable.
    existing_lower = {k.lower() for k in (TOPIC_KEYWORDS + HIGH_VALUE_KEYWORDS)}
    candidates = harvest_candidate_keywords(papers, existing_lower) if papers else []
    if candidates:
        print(f"\nSuggesting {len(candidates)} new candidate keyword(s) from today's papers:")
        for kw, c in candidates:
            print(f"  ? {kw} (x{c})")

    subject, html, text = create_email_content(
        papers, days_back, attach_count=len(attachments), candidates=candidates
    )

    if os.environ.get("SENDER_EMAIL"):
        send_email(subject, html, text, attachments=attachments)
    else:
        print("\nEmail credentials not configured. Email content:")
        print(text)


if __name__ == "__main__":
    main()
