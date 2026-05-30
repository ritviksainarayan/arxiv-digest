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
    # generic filler that makes bad phrase endpoints
    "after", "before", "toward", "towards", "recent", "various", "several",
    "many", "potential", "possible", "significant", "important", "novel",
    "understanding", "trend", "trends", "decreasing", "increasing", "formed",
    "theoretical", "prediction", "predictions", "estimate", "estimates",
}

TOKEN_RE = re.compile(r"[a-z][a-z0-9\-]{2,}")

# arXiv classification taxonomy. ADS dumps these category names into the same
# `keyword` field as real research keywords, so we must filter them out — they
# are useless as digest keywords (they'd match almost everything).
ARXIV_CATEGORY_KEYWORDS = {
    "astrophysics",
    "astrophysics of galaxies",
    "cosmology and nongalactic astrophysics",
    "earth and planetary astrophysics",
    "high energy astrophysical phenomena",
    "instrumentation and methods for astrophysics",
    "solar and stellar astrophysics",
    "general relativity and quantum cosmology",
    "atmospheric and oceanic physics",
    "machine learning",
    "artificial intelligence",
    "computational physics",
    "data analysis, statistics and probability",
    "fluid dynamics",
    "plasma physics",
    "space physics",
    "nuclear theory",
    "nuclear experiment",
    "computer science",
    "physics",
    "mathematics",
}


def is_arxiv_category(kw: str) -> bool:
    """True if an ADS keyword is actually an arXiv category/taxonomy name."""
    k = kw.strip().lower()
    leaf = re.split(r"\s+-\s+", k)[-1].strip()  # "Astrophysics - Foo" -> "foo"
    if k in ARXIV_CATEGORY_KEYWORDS or leaf in ARXIV_CATEGORY_KEYWORDS:
        return True
    # Any leaf ending in these is an astro category, even ones not listed above.
    return leaf.endswith("astrophysics") or leaf.endswith("astrophysical phenomena")


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


# Split text into clauses so n-grams don't span sentence/punctuation
# boundaries (which produces nonsense like "light curves discuss").
CLAUSE_SPLIT_RE = re.compile(r"[.;:,!?()\[\]{}\"]| - ")


def document_freq(texts: list, nmin: int = 1, nmax: int = 3) -> Counter:
    """Count, for each n-gram, how many documents contain it (df).

    N-grams are generated per-clause so they never cross sentence or
    punctuation boundaries.
    """
    df = Counter()
    for text in texts:
        grams = set()
        for clause in CLAUSE_SPLIT_RE.split(text.lower()):
            tokens = TOKEN_RE.findall(clause)
            grams.update(ngrams(tokens, nmin, nmax))
        df.update(grams)
    return df


def count_in_texts(phrase: str, texts_lower: list) -> int:
    """How many of the given (lowercased) texts contain the phrase."""
    p = phrase.lower()
    return sum(1 for t in texts_lower if p in t)


def yake_keywords(texts: list, top_n: int = 40, max_ngram: int = 3):
    """Extract keyword phrases from a set of texts using YAKE.

    Returns an ordered list of phrases (best first), or None if YAKE is not
    installed so the caller can fall back to plain n-gram frequency.
    """
    try:
        import yake
    except ImportError:
        return None

    corpus = "\n".join(texts)
    if not corpus.strip():
        return []

    extractor = yake.KeywordExtractor(
        lan="en",
        n=max_ngram,         # up to 3-word phrases
        dedupLim=0.9,        # drop near-duplicate phrases
        top=top_n * 3,       # over-fetch; caller filters/dedups down
    )
    # YAKE returns (phrase, score); lower score == more relevant.
    ranked = sorted(extractor.extract_keywords(corpus), key=lambda kv: kv[1])

    out = []
    for phrase, _score in ranked:
        phrase = phrase.strip()
        words = [w.lower() for w in phrase.split()]
        # Keep multi-word phrases only — single words are too generic, and YAKE's
        # 1-grams are mostly noise for this use.
        if len(words) < 2:
            continue
        # Drop phrases bounded by a stopword, or spanning a conjunction (these are
        # almost always sentence fragments like "events and magnetic").
        if words[0] in STOPWORDS or words[-1] in STOPWORDS:
            continue
        if any(w in ("and", "or", "but", "with", "for", "from", "that", "which") for w in words):
            continue
        out.append(phrase)
    return out


def ads_keyword_freq(papers: list) -> Counter:
    """Author-curated ADS `keyword` field, counted by document frequency.

    arXiv category/taxonomy names are excluded — they are not useful keywords.
    """
    df = Counter()
    for p in papers:
        kws = {
            k.strip().lower()
            for k in (p.get("keyword") or [])
            if k and k.strip() and not is_arxiv_category(k)
        }
        df.update(kws)
    return df
