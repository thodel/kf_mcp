#!/usr/bin/env python3
"""
corpus.py — what makes this repo Königsfelden rather than one of the others.

embeddings.py is vendored byte-identical across the fleet (see ch-h-bot
docs/MCP_PROVIDER_PATTERN.md); whatever is particular to this corpus lives
here instead, so that the shared file can stay a checksum.

Since thodel/kf_mcp#11 embed_db.py and vectorstore.py are the shared files too
(byte-identical with eperiodica_mcp). What they need from this corpus is the
interface at the end of this file: DB_ENV / DEFAULT_DB / PARENT_TABLE,
iter_documents(conn, limit) yielding (id, head, text), and clean(text). The
index itself did not change in the move: `chunks.entry_id` became `parent_id`
(migrate_parent_id.py), the head on every passage is built by the same rule as
before, and all 6,621 passages were measured row-identical (ch-h-bot#555), so
the vectors stayed.
"""

from __future__ import annotations

import os
import re
import unicodedata

# The shared embeddings.py reads KF_EMBED_MODEL, KF_CHUNK_CHARS, ... only
# if it knows the prefix. Set before it is imported; the Dockerfile sets it too.
os.environ.setdefault("MCP_ENV_PREFIX", "KF")

import embeddings as emb  # noqa: E402

# Qwen3-Embedding is instruction-aware: queries carry a task instruction and
# documents do not. This one names the corpus; the shared default only says
# "Swiss history".
QUERY_PREFIX = os.environ.get(
    "KF_EMBED_QUERY_PREFIX",
    "Instruct: Given a question about the medieval and early modern monastery of "
    "Königsfelden, retrieve passages from charters and administrative records "
    "that answer it\nQuery: ",
)


# ── Transcription cleanup: resolve abbreviations to their expanded reading ────
#
# The edition transcribes diplomatically. Segment boundaries are marked with ✳
# (82,062 of them), and every abbreviated word is written as the raw manuscript
# form immediately followed by the editor's expansion:
#
#     "un̄ und"           the raw form is a whole word            -> und
#     "Hein r₎ rich"      the raw form continues a fragment       -> Heinrich
#     "stif tˀin terin"                                           -> stifterin
#     "Diz ist dˀ der"                                            -> Diz ist der
#
# Embedded as-is, this is close to unusable: names arrive split in half, every
# abbreviated word is doubled, and 82k segment markers punctuate the text at
# random. Embedding the expanded reading is the decision this implements.

SEGMENT_MARKER = "✳"
# Expansion bracket, abbreviation hook, combining macron — the three ways this
# edition marks a raw form.
_MARKER_RE = re.compile("[₎ˀ\u0304]")
_PUNCT = ",.;:()[]«»\"\'"


def has_marker(token: str) -> bool:
    """True when a token is a raw manuscript form rather than a reading."""
    return bool(_MARKER_RE.search(token))


def build_vocab(texts) -> dict:
    """For each token, how often it is a fragment versus a word in its own right.

    Deciding whether an expansion joins the token before it needs to know
    whether that token is already a word: "ist" is, "Hein" and "stif" are not.

    Counting total occurrences does not separate them — "Hein" recurs
    constantly, because "Heinrich" is abbreviated the same way every time — and
    neither does a rare/common threshold, because "Hein" is also a name on its
    own. What separates them is the *ratio*: "ist" appears overwhelmingly in
    ordinary positions, "Hein" almost only before a raw form. The corpus
    answers the question itself, which beats a word list for a language with no
    settled orthography.

    Returns ``{token: (n_fragment, n_solo)}``.
    """
    counts: dict[str, list[int]] = {}
    for text in texts:
        tokens = (text or "").replace(SEGMENT_MARKER, " ").split()
        for i, token in enumerate(tokens):
            if has_marker(token):
                continue
            bare = token.strip(_PUNCT).lower()
            if not bare:
                continue
            slot = counts.setdefault(bare, [0, 0])
            followed = i + 1 < len(tokens) and has_marker(tokens[i + 1])
            slot[0 if followed else 1] += 1
    return {k: tuple(v) for k, v in counts.items()}


def _is_wordlike(token: str) -> bool:
    """Alphabetic, tolerating this edition's combining diacritics.

    ``str.isalpha()`` is False for a combining mark, so "Oͤs" (O + U+0364 + s)
    would otherwise never be recognised as a word fragment.
    """
    return bool(token) and all(
        c.isalpha() or unicodedata.combining(c) for c in token)


def clean_entry_text(text: str, vocab: dict) -> str:
    """Transcription reduced to its expanded reading (see the note above)."""
    tokens = (text or "").replace(SEGMENT_MARKER, " ").split()
    out: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if has_marker(token):
            if i + 1 < len(tokens):
                expansion = tokens[i + 1]
                previous = out[-1] if out else ""
                bare = previous.strip(_PUNCT).lower()
                fragment, solo = vocab.get(bare, (0, 0))
                if _is_wordlike(bare) and fragment > solo:
                    out[-1] = previous + expansion
                else:
                    out.append(expansion)
                i += 2
            else:
                i += 1        # trailing raw form with no expansion after it
            continue
        out.append(token)
        i += 1
    return re.sub(r"\s+([,.;:])", r"\1", " ".join(out)).strip()


# ── What the shared embed_db.py asks of a corpus ─────────────────────────────

DB_ENV = "KF_DB"
DEFAULT_DB = "/data/kf.db"
PARENT_TABLE = "entries"

# The expansion vocabulary of the whole corpus. iter_documents builds it before
# its first yield, and clean() reads it: the shared build_chunks calls
# iter_documents before the first clean, so the order is right. Before the
# first iter_documents it is empty, and clean() then never joins a fragment.
_VOCAB: dict = {}


def head_line(title, year) -> str:
    """The first line of every passage of an entry: `title year`.

    Verbatim what the old embed_db.py wrote (no strip, no separator beyond the
    one space, a missing title or year simply absent) — the stored passages
    were embedded with exactly this line, and db.passage_body cuts exactly this
    line off a hit's snippet. One function, so there is one rule.
    """
    return " ".join(str(x) for x in (title, year) if x)


def clean(text: str) -> str:
    """The expanded reading of one entry, with the vocabulary of the whole corpus."""
    return clean_entry_text(text, _VOCAB)


def iter_documents(conn, limit=None):
    """Yield (entry_id, head, text_raw) for every entry that has no chunks yet.

    The vocabulary comes first and comes from the WHOLE table, whatever
    ``limit`` says: whether "Hein" is a fragment or a name needs every
    occurrence of the token, not the ones in this batch. That is how the old
    embed_db.py did it and how ch-h-bot#555 measured the move (6,621 of 6,621
    passages row-identical).

    Only entries without chunks are yielded, as before: a rerun on a fully
    chunked database cleans nothing, and ``--limit`` means "this many
    not-yet-chunked entries", a trial run on a sample.
    """
    global _VOCAB
    _VOCAB = build_vocab(
        t for (t,) in conn.execute(
            f"SELECT text_raw FROM {PARENT_TABLE} WHERE text_raw IS NOT NULL"))
    rows = conn.execute(
        f"""
        SELECT e.id, e.title, e.year, e.text_raw
        FROM {PARENT_TABLE} e
        WHERE e.text_raw IS NOT NULL AND e.text_raw <> ''
          AND NOT EXISTS (SELECT 1 FROM chunks c WHERE c.parent_id = e.id)
        ORDER BY e.id
        """ + (" LIMIT ?" if limit else ""),
        (limit,) if limit else ()).fetchall()
    for entry_id, title, year, text in rows:
        yield entry_id, head_line(title, year), text


def embed_query(query: str, model: str = emb.DEFAULT_MODEL, client=None) -> list[float]:
    """Embed one search query with this corpus's instruction prefix."""
    vectors = emb.embed_texts([QUERY_PREFIX + query], model=model, client=client)
    if not vectors:
        raise emb.EmbeddingError("the embedding service returned no vector for the query")
    return vectors[0]
