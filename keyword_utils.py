#!/usr/bin/env python3
"""
Shared text-mining helpers for keyword extraction.

Used by both update_keywords.py (monthly buzzword mining vs. a background
corpus) and topic_digest.py (harvesting candidate keywords from the day's
papers to suggest in the email).
"""

import re
from collections import Counter

# Generic English + science-paper filler words we never want as buzzwords.
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


def paper_text(paper: dict) -> str:
    """Title + abstract as a single lowercased-able string."""
    title = (paper.get("title") or [""])[0] or ""
    abstract = paper.get("abstract") or ""
    return f"{title}. {abstract}"


def ngrams(tokens: list, nmin: int = 1, nmax: int = 3):
    """Yield clean n-grams: no stopword at either end."""
    for n in range(nmin, nmax + 1):
        for i in range(len(tokens) - n + 1):
            gram = tokens[i:i + n]
            if gram[0] in STOPWORDS or gram[-1] in STOPWORDS:
                continue
            yield " ".join(gram)


def document_freq(texts: list, nmin: int = 1, nmax: int = 3) -> Counter:
    """Count, for each n-gram, how many documents contain it (df)."""
    df = Counter()
    for text in texts:
        tokens = TOKEN_RE.findall(text.lower())
        df.update(set(ngrams(tokens, nmin, nmax)))
    return df


def ads_keyword_freq(papers: list) -> Counter:
    """Author-curated ADS `keyword` field, counted by document frequency."""
    df = Counter()
    for p in papers:
        kws = {k.strip().lower() for k in (p.get("keyword") or []) if k and k.strip()}
        df.update(kws)
    return df
