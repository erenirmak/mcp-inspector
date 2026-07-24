"""
Lightweight tool search engine using TF-IDF sparse vectors.

No external dependencies — uses only Python stdlib (re, math, collections).
Builds an inverted index over tool names + descriptions from the server registry
and returns the top-k most relevant tools for a natural-language query.

Thread-safety: all public methods are guarded by an RLock so rebuild() and
search() can safely be called from different threads simultaneously.
"""

import math
import re
import threading
from collections import Counter, defaultdict

# ── text preprocessing ──────────────────────────────────────────────

_STOP_WORDS = frozenset(
    "a an and are as at be but by for from has have he her his how i if in into is it its"
    " my no not of on or our she so that the their them then there these they this to was we"
    " what when which who will with you your can do does did get got let may might must need"
    " shall should would could".split()
)


def _tokenize(text: str) -> list[str]:
    """Lowercase, strip non-alpha, remove stop words."""
    tokens = re.findall(r"[a-z0-9_]+", text.lower())
    return [t for t in tokens if t not in _STOP_WORDS and len(t) > 1]


# ── TF-IDF index ────────────────────────────────────────────────────


class ToolSearchIndex:
    """
    A simple TF-IDF inverted index for MCP tool search.

    Each "document" is the concatenation of a tool's name + description.
    The index is rebuilt from scratch whenever `rebuild()` is called with
    the latest tool list from the server registry.

    All public methods are thread-safe.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._docs: list[dict] = []
        self._doc_tokens: list[list[str]] = []
        self._idf: dict[str, float] = {}
        self._tfidf: list[dict[str, float]] = []
        self._ready = False

    @property
    def ready(self) -> bool:
        with self._lock:
            return self._ready and len(self._docs) > 0

    @property
    def doc_count(self) -> int:
        with self._lock:
            return len(self._docs)

    def rebuild(self, tools: list[dict]):
        """
        Rebuild the index from a fresh list of tool dicts.
        Each tool dict is expected to have at least 'name' and optionally 'description'.
        Atomically replaces the old index so concurrent searches always see
        either the old or the new index, never a partial state.
        """
        # Build everything outside the lock first
        docs = list(tools)
        doc_tokens = []
        for tool in tools:
            name = tool.get("name", "")
            desc = tool.get("description", "") or ""
            # Name weighted 3x to boost exact name matches
            text = f"{name} {name} {name} {desc}"
            doc_tokens.append(_tokenize(text))

        idf = _build_idf(doc_tokens)
        tfidf = _build_tfidf(doc_tokens, idf)

        # Swap in atomically
        with self._lock:
            self._docs = docs
            self._doc_tokens = doc_tokens
            self._idf = idf
            self._tfidf = tfidf
            self._ready = True

    def search(self, query: str, top_k: int = 4) -> list[dict]:
        """Return the top-k tools most relevant to the query string."""
        with self._lock:
            if not self._ready or not self._docs:
                return []
            # Take local snapshots so the lock can be released during computation
            idf = self._idf
            tfidf = list(self._tfidf)
            docs = list(self._docs)

        q_tokens = _tokenize(query)
        if not q_tokens:
            return []

        q_tf = Counter(q_tokens)
        max_tf = max(q_tf.values())
        q_vec: dict[str, float] = {}
        for term, count in q_tf.items():
            if term in idf:
                q_vec[term] = (0.5 + 0.5 * count / max_tf) * idf[term]

        if not q_vec:
            return []

        q_norm = math.sqrt(sum(v * v for v in q_vec.values()))
        scores: list[tuple[float, int]] = []
        for idx, doc_vec in enumerate(tfidf):
            dot = sum(q_vec.get(t, 0) * doc_vec.get(t, 0) for t in q_vec)
            d_norm = math.sqrt(sum(v * v for v in doc_vec.values())) if doc_vec else 0
            if d_norm == 0 or q_norm == 0:
                continue
            scores.append((dot / (q_norm * d_norm), idx))

        scores.sort(key=lambda x: x[0], reverse=True)
        results = []
        for score, idx in scores[:top_k]:
            entry = dict(docs[idx])
            entry["_relevance_score"] = round(score, 4)
            results.append(entry)
        return results


# ── module-level helpers (pure functions, no shared state) ───────────────────


def _build_idf(doc_tokens: list[list[str]]) -> dict[str, float]:
    n = len(doc_tokens)
    if n == 0:
        return {}
    df: dict[str, int] = defaultdict(int)
    for tokens in doc_tokens:
        for term in set(tokens):
            df[term] += 1
    return {term: math.log(n / count) for term, count in df.items()}


def _build_tfidf(doc_tokens: list[list[str]], idf: dict[str, float]) -> list[dict[str, float]]:
    result = []
    for tokens in doc_tokens:
        if not tokens:
            result.append({})
            continue
        tf = Counter(tokens)
        max_tf = max(tf.values())
        vec = {term: (0.5 + 0.5 * count / max_tf) * idf[term] for term, count in tf.items() if term in idf}
        result.append(vec)
    return result


# Module-level singleton
tool_index = ToolSearchIndex()
