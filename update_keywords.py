#!/usr/bin/env python3
"""
Auto-update the topic keyword list from your (and close collaborators') papers.

Strategy
--------
1. Pull every paper authored by the PRIORITY_ORCIDS defined in topic_digest.py
   (you + your close collaborators) from NASA ADS: titles, abstracts, and the
   curated ADS `keyword` field.
2. Pull a background sample of recent astro-ph.SR / astro-ph.EP papers.
3. Score candidate phrases (1-3 word n-grams) by how over-represented they are
   in YOUR corpus vs. the background, using weighted log-odds. Author-supplied
   ADS keywords get a bonus, since they are already curated buzzwords.
4. Append the top new phrases to keywords.json -> "auto_keywords"
   (APPEND-ONLY: curated seed lists are never touched, existing auto keywords
   are kept).

Run with ADS_API_KEY set. Set DRY_RUN=1 to print results without writing.
"""

import os
import re
import json
import math
from collections import Counter
from datetime import datetime, timedelta, timezone

import requests

from topic_digest import PRIORITY_ORCIDS, PRIORITY_AUTHOR_NAMES, KEYWORDS_FILE

ADS_API_URL = "https://api.adsabs.harvard.edu/v1/search/query"

# --- Tunables (override via env) ---
MAX_NEW_KEYWORDS = int(os.environ.get("MAX_NEW_KEYWORDS", "25"))
MIN_NGRAM = 1
MAX_NGRAM = 3
MIN_USER_DOCS = int(os.environ.get("MIN_USER_DOCS", "2"))   # free-text phrase must appear in >= this many of your papers
BACKGROUND_DAYS = int(os.environ.get("BACKGROUND_DAYS", "180"))
BACKGROUND_ROWS = int(os.environ.get("BACKGROUND_ROWS", "400"))
ADS_KEYWORD_BONUS = 2.5  # log-odds bonus for author-curated ADS keywords

# Generic English + science-paper filler words we don't want as buzzwords.
STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "then", "of", "to", "in", "on",
    "for", "with", "by", "as", "at", "from", "into", "over", "under", "between",
    "is", "are", "was", "were", "be", "been", "being", "this", "that", "these",
    "those", "we", "our", "us", "it", "its", "their", "they", "such", "than",
    "can", "may", "might", "will", "would", "could", "should", "also", "which",
    "while", "when", "where", "how", "what", "who", "all", "both", "each",
    "more", "most", "some", "any", "no", "not", "only", "other", "due", "via",
    # science-paper filler
    "using", "used", "use", "results", "result", "show", "shows", "shown",
    "present", "presented", "study", "studies", "data", "new", "find", "found",
    "observations", "observation", "observed", "analysis", "based", "however",
    "within", "two", "three", "first", "second", "well", "approximately",
    "respectively", "paper", "work", "method", "methods", "model", "models",
    "value", "values", "different", "similar", "high", "low", "large", "small",
    "order", "set", "case", "number", "time", "given", "obtained", "derive",
    "derived", "suggest", "suggests", "indicate", "indicates", "consistent",
}

TOKEN_RE = re.compile(r"[a-z][a-z0-9\-]{2,}")


def ads_search(api_key: str, q: str, fl: str, rows: int, sort: str = "date desc") -> list:
    headers = {"Authorization": f"Bearer {api_key}"}
    params = {"q": q, "fl": fl, "rows": rows, "sort": sort}
    r = requests.get(ADS_API_URL, headers=headers, params=params, timeout=90)
    r.raise_for_status()
    return r.json().get("response", {}).get("docs", [])


def fetch_user_papers(api_key: str) -> list:
    """Papers by you + close collaborators (ORCID, with author-name fallback)."""
    clauses = [f"orcid:{o}" for o in PRIORITY_ORCIDS]
    for names in PRIORITY_AUTHOR_NAMES.values():
        for name in names:
            clauses.append(f'author:"{name}"')
    q = "(" + " OR ".join(clauses) + ")"
    docs = ads_search(api_key, q, fl="title,abstract,keyword", rows=300)
    print(f"  Found {len(docs)} papers in your/collaborators' corpus")
    return docs


def fetch_background_papers(api_key: str) -> list:
    end = datetime.now()
    start = end - timedelta(days=BACKGROUND_DAYS)
    date_range = f"[{start.strftime('%Y-%m-%d')} TO {end.strftime('%Y-%m-%d')}]"
    q = (
        '(arxiv_class:"astro-ph.SR" OR arxiv_class:"astro-ph.EP") '
        f"AND entdate:{date_range}"
    )
    docs = ads_search(api_key, q, fl="title,abstract", rows=BACKGROUND_ROWS)
    print(f"  Found {len(docs)} background astro-ph.SR/EP papers")
    return docs


def paper_text(paper: dict) -> str:
    title = (paper.get("title") or [""])[0] or ""
    abstract = paper.get("abstract") or ""
    return f"{title}. {abstract}"


def ngrams(tokens: list, nmin: int, nmax: int):
    """Yield clean n-grams: no stopword at either end, all tokens length >= 3."""
    for n in range(nmin, nmax + 1):
        for i in range(len(tokens) - n + 1):
            gram = tokens[i:i + n]
            if gram[0] in STOPWORDS or gram[-1] in STOPWORDS:
                continue
            yield " ".join(gram)


def document_freq(texts: list) -> Counter:
    """Count, for each n-gram, how many documents contain it (df)."""
    df = Counter()
    for text in texts:
        tokens = TOKEN_RE.findall(text.lower())
        df.update(set(ngrams(tokens, MIN_NGRAM, MAX_NGRAM)))
    return df


def weighted_log_odds(f_u: int, n_u: int, f_b: int, n_b: int, alpha: float = 0.5) -> float:
    """Log-odds that a phrase belongs to the user corpus vs. background."""
    return (
        math.log((f_u + alpha) / (n_u - f_u + alpha))
        - math.log((f_b + alpha) / (n_b - f_b + alpha))
    )


def collect_ads_keywords(papers: list) -> Counter:
    """Author-curated ADS `keyword` field, counted by document frequency."""
    df = Counter()
    for p in papers:
        kws = {k.strip().lower() for k in (p.get("keyword") or []) if k and k.strip()}
        df.update(kws)
    return df


def mine_keywords(api_key: str) -> list:
    print("Fetching your corpus...")
    user_papers = fetch_user_papers(api_key)
    if not user_papers:
        print("  No user papers found; nothing to mine.")
        return []

    print("Fetching background corpus...")
    bg_papers = fetch_background_papers(api_key)

    user_texts = [paper_text(p) for p in user_papers]
    bg_texts = [paper_text(p) for p in bg_papers]
    n_u, n_b = len(user_texts), max(1, len(bg_texts))

    user_df = document_freq(user_texts)
    bg_df = document_freq(bg_texts)
    ads_kw_df = collect_ads_keywords(user_papers)

    # Candidate set: free-text phrases meeting the doc-frequency floor, plus
    # every author-curated ADS keyword that appears in >= 1 of your papers.
    candidates = {p for p, c in user_df.items() if c >= MIN_USER_DOCS}
    candidates |= {p for p, c in ads_kw_df.items() if c >= 1}

    scored = []
    for phrase in candidates:
        f_u = user_df.get(phrase, ads_kw_df.get(phrase, 1))
        f_b = bg_df.get(phrase, 0)
        score = weighted_log_odds(f_u, n_u, f_b, n_b)
        if phrase in ads_kw_df:
            score += ADS_KEYWORD_BONUS
        scored.append((score, phrase))

    scored.sort(reverse=True)
    return [phrase for _, phrase in scored]


def update_keywords_file(ranked: list, dry_run: bool = False) -> list:
    data = json.loads(KEYWORDS_FILE.read_text())

    existing = {
        k.lower()
        for k in (
            data.get("seed_topic_keywords", [])
            + data.get("seed_high_value_keywords", [])
            + data.get("auto_keywords", [])
        )
    }

    new = []
    for phrase in ranked:
        if phrase.lower() in existing:
            continue
        new.append(phrase)
        existing.add(phrase.lower())
        if len(new) >= MAX_NEW_KEYWORDS:
            break

    print(f"\nProposed {len(new)} new keyword(s):")
    for kw in new:
        print(f"  + {kw}")

    if dry_run:
        print("\nDRY_RUN=1 -> keywords.json not modified.")
        return new

    if new:
        data["auto_keywords"] = data.get("auto_keywords", []) + new
        data["auto_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        KEYWORDS_FILE.write_text(json.dumps(data, indent=2) + "\n")
        print(f"\nWrote {len(new)} new keyword(s) to {KEYWORDS_FILE.name}")
    else:
        print("\nNo new keywords to add.")
    return new


def main():
    api_key = os.environ.get("ADS_API_KEY")
    if not api_key:
        raise ValueError("ADS_API_KEY environment variable is required")

    dry_run = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
    ranked = mine_keywords(api_key)
    update_keywords_file(ranked, dry_run=dry_run)


if __name__ == "__main__":
    main()
