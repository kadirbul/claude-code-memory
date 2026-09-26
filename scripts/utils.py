"""Shared utilities for the personal knowledge base."""

import hashlib
import json
import re
from pathlib import Path

from config import (
    CONCEPTS_DIR,
    CONNECTIONS_DIR,
    DAILY_DIR,
    INDEX_FILE,
    KNOWLEDGE_DIR,
    LOG_FILE,
    PROJECTS_DIR,
    QA_DIR,
    STATE_FILE,
)


# ── State management ──────────────────────────────────────────────────

def load_state() -> dict:
    """Load persistent state from state.json."""
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"ingested": {}, "query_count": 0, "last_lint": None, "total_cost": 0.0}


def save_state(state: dict) -> None:
    """Save state to state.json."""
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ── File hashing ──────────────────────────────────────────────────────

def file_hash(path: Path) -> str:
    """SHA-256 hash of a file (first 16 hex chars)."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


# ── Slug / naming ─────────────────────────────────────────────────────

def slugify(text: str) -> str:
    """Convert text to a filename-safe slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")


# ── Wikilink helpers ──────────────────────────────────────────────────

def extract_wikilinks(content: str) -> list[str]:
    """Extract all [[wikilinks]] from markdown content."""
    return re.findall(r"\[\[([^\]]+)\]\]", content)


def wiki_article_exists(link: str) -> bool:
    """Check if a wikilinked article exists on disk."""
    path = KNOWLEDGE_DIR / f"{link}.md"
    return path.exists()


# ── Wiki content helpers ──────────────────────────────────────────────

def read_wiki_index() -> str:
    """Read the knowledge base index file."""
    if INDEX_FILE.exists():
        return INDEX_FILE.read_text(encoding="utf-8")
    return "# Knowledge Base Index\n\n| Article | Summary | Compiled From | Updated |\n|---------|---------|---------------|---------|"


def read_all_wiki_content() -> str:
    """Read index + all wiki articles into a single string for context."""
    parts = [f"## INDEX\n\n{read_wiki_index()}"]

    for subdir in [CONCEPTS_DIR, CONNECTIONS_DIR, QA_DIR]:
        if not subdir.exists():
            continue
        for md_file in sorted(subdir.glob("*.md")):
            rel = md_file.relative_to(KNOWLEDGE_DIR)
            content = md_file.read_text(encoding="utf-8")
            parts.append(f"## {rel}\n\n{content}")

    return "\n\n---\n\n".join(parts)


def list_wiki_articles() -> list[Path]:
    """List all wiki article files.

    PROJECTS_DIR was missing from this list until 2026-09-26. Everything that walks the
    wiki goes through here - the "existing articles" list the compiler is told to search
    before writing, and near_duplicate_pairs() - so an omitted directory was invisible to
    BOTH: a log revisiting a projects/ topic got a fresh concepts/ article instead of an
    update, which is the exact duplication mechanism the search rule exists to stop.
    """
    articles = []
    for subdir in [CONCEPTS_DIR, CONNECTIONS_DIR, PROJECTS_DIR, QA_DIR]:
        if subdir.exists():
            articles.extend(sorted(subdir.glob("*.md")))
    return articles


def list_raw_files() -> list[Path]:
    """List all daily log files."""
    if not DAILY_DIR.exists():
        return []
    return sorted(DAILY_DIR.glob("*.md"))


def near_duplicate_pairs(threshold: float = 0.5) -> list[tuple[str, str, float]]:
    """Article pairs whose SLUGS overlap enough that they are probably one topic
    filed twice.

    WHY: the compiler mints a fresh article for a concept it has already written,
    under reworded titles, so the corpus accretes synonym piles that make recall
    ambiguous - a lookup returns six articles saying the same thing and neither the
    reader nor an agent can tell which is current. Measured 2026-09-25 across 603
    articles: 101 pairs over this threshold, 8 exact word-for-word permutations
    (`ssrf-unvalidated-urlopen-scheme` / `urlopen-ssrf-unvalidated-scheme`), and
    SEVEN articles on the One Knowledge Plane programme alone.

    This does not fix anything - it makes the problem VISIBLE. compile.py prints the
    count on every run, so the number is a trend the nightly log carries instead of
    something nobody notices until a human counts files by hand. It reports on slugs,
    not contents: cheap, and the failure it catches is precisely a naming failure.

    Overlap is Jaccard - |shared| / |union| - so a pair scores highly only when the two
    slugs are mostly the SAME words, not merely when one contains the other.

    The default threshold is 0.5, not the 0.6 used with the old measure: Jaccard is
    strictly harsher, and at 0.6 it MISSED the pm2-resurrect trio entirely (measured
    2026-09-26: 0 of 3 articles implicated at 0.6, all 3 at 0.5) while still catching
    the baseline-preserving and one-knowledge-plane piles. A threshold is only
    meaningful against the measure it was tuned for.

    Two caveats for whoever reads the printed number. It counts PAIRS, which is quadratic
    in pile size (k articles = k(k-1)/2 pairs), so one bad night can move it by dozens.
    And it is NOT comparable to the "101 pairs" quoted historically, which used the
    shorter-slug denominator. Treat it as a trend against its own baseline.
    """
    # Key on the path RELATIVE to knowledge/, not the bare filename: the same slug can
    # exist in concepts/ AND connections/, and reporting just the filename both collapses
    # those into a meaningless self-pair and leaves the reader unable to find the files.
    return duplicate_pairs_among(
        [p.relative_to(KNOWLEDGE_DIR).as_posix() for p in list_wiki_articles()],
        threshold,
    )


def duplicate_pairs_among(slugs: list[str], threshold: float = 0.5) -> list[tuple[str, str, float]]:
    """The pure half of near_duplicate_pairs(), split out so it can be TESTED.

    The corpus lives under knowledge/, which is gitignored, so a test asserting against
    the real wiki passes only on this machine and - worse - goes red the moment someone
    actually collapses a duplicate pile, i.e. it fails for doing the right thing. This
    function takes the slugs, so a test can pin an exact expected pair set on a fixture.
    """
    tokenised: list[tuple[str, set[str]]] = []
    for slug in slugs:
        stem = slug.rsplit("/", 1)[-1].removesuffix(".md")
        words = {w for w in re.split(r"[-_]", stem) if len(w) > 2}
        if words:
            tokenised.append((slug, words))

    pairs: list[tuple[str, str, float]] = []
    for i, (name_a, words_a) in enumerate(tokenised):
        for name_b, words_b in tokenised[i + 1:]:
            # Jaccard, NOT |shared| / |shorter|. The shorter-slug denominator scores a
            # perfect 1.0 whenever one slug's tokens are a subset of a longer one's,
            # however much longer - which saturated the metric (56 pairs at exactly 1.00
            # on 2026-09-26) and filled the printed top-5 with non-duplicates.
            overlap = len(words_a & words_b) / len(words_a | words_b)
            if overlap >= threshold:
                pairs.append((name_a, name_b, round(overlap, 2)))
    return sorted(pairs, key=lambda t: -t[2])


# ── Index helpers ─────────────────────────────────────────────────────

def count_inbound_links(target: str, exclude_file: Path | None = None) -> int:
    """Count how many wiki articles link to a given target."""
    count = 0
    for article in list_wiki_articles():
        if article == exclude_file:
            continue
        content = article.read_text(encoding="utf-8")
        if f"[[{target}]]" in content:
            count += 1
    return count


def get_article_word_count(path: Path) -> int:
    """Count words in an article, excluding YAML frontmatter."""
    content = path.read_text(encoding="utf-8")
    # Strip frontmatter
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            content = content[end + 3:]
    return len(content.split())


def build_index_entry(rel_path: str, summary: str, sources: str, updated: str) -> str:
    """Build a single index table row."""
    link = rel_path.replace(".md", "")
    return f"| [[{link}]] | {summary} | {sources} | {updated} |"
