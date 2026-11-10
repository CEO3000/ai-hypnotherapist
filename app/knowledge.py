"""Loads the practice knowledge base and retrieves library notes for script writing.

Exclusion is structural, not just a prompt instruction:
- an ingested note file is retrievable only if every book_catalog.md row that references it
  is tagged core-craft (not excluded, caution or mislabeled); files the catalog does not
  mention are blocked (fail closed);
- inside allowed files, any section whose heading path marks it as excluded, flagged,
  skipped or cautionary is dropped before indexing.

Books the practice adds from the admin panel (books.py) join the library once the practice approves their notes:
they are checked by the AI for suitability first, and the same section filter applies to them.
"""

import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import snowballstemmer

CATALOG_PATH = "knowledge_base/book_catalog.md"
INGESTED_DIR = "knowledge_base/ingested"
ETHICS_PATH = "knowledge_base/index.md"
ETHICS_HEADING = "Ethical frame"

PRESENTING_ISSUES = (
    "smoking-cessation",
    "weight-management",
    "anxiety-and-stress",
    "confidence-and-self-esteem",
    "sleep-and-insomnia",
    "pain-management",
    "phobias-and-fears",
    "habit-control-general",
)
PRESENTING_ISSUE_FILES = [f"knowledge_base/techniques/presenting-issues/{p}.md" for p in PRESENTING_ISSUES]

SCRIPT_CORE_FILES = [
    "knowledge_base/techniques/screening-and-safety.md",
    "knowledge_base/techniques/emotional-pivot-reprogramming.md",
    "templates/script_structure.md",
    "knowledge_base/techniques/suggestion-language.md",
    "knowledge_base/techniques/ego-strengthening-and-emergence.md",
    "knowledge_base/techniques/inductions.md",
    "knowledge_base/techniques/deepeners.md",
]
# Read for script writing when present, never required: the clinician's own voice and signature moves. A required
# file that is missing stops the whole app, so a server that does not have this one yet still starts normally.
SCRIPT_OPTIONAL_FILES = [
    "knowledge_base/techniques/practitioner-session-craft.md",
]
CONSULTATION_CORE_FILES = [
    "knowledge_base/techniques/screening-and-safety.md",
    "knowledge_base/techniques/emotional-pivot-reprogramming.md",
    *PRESENTING_ISSUE_FILES,
]

EXCLUDED_HEADING = re.compile(
    r"exclu|not adopted|not extracted|not used|flag|skip|do not use|\bcaution\b|caveat"
    r"|ethical (?:filter|flag|note)",
    re.IGNORECASE,
)
ADDED_PREFIX = "library"  # source paths of the books the practice added: library/<file>.md
INGESTED_REF = re.compile(r"ingested/([A-Za-z0-9._-]+\.md)")
HEADING_LINE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
MAX_CHUNK_CHARS = 3500

STOPWORDS = frozenset(
    "a an and are as at be been but by can could did do does for from had has have he her his how i if in "
    "into is it its me my no not of on or our she so than that the their them then there these they this "
    "to too up us was we were what when where which who why will with would you your".split()
)
_stemmer = snowballstemmer.stemmer("english")


class KnowledgeBaseError(RuntimeError):
    pass


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


# --- catalog -----------------------------------------------------------------


@dataclass(frozen=True)
class CatalogRow:
    file: str
    tag: str
    status: str
    ingested_notes: tuple[str, ...]

    @property
    def allowed(self) -> bool:
        tag = self.tag.replace("*", "").strip().lower()
        return tag.startswith("core-craft") and not any(w in tag for w in ("exclu", "caution", "mislabel"))


def parse_catalog(text: str) -> list[CatalogRow]:
    rows = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) != 3:
            # Can't tell which cell is the tag, so anything it references is blocked.
            rows.append(CatalogRow(cells[0], "malformed", stripped, tuple(INGESTED_REF.findall(stripped))))
            continue
        file, tag, status = cells
        if file.lower() == "file" or set(file) <= set("-: "):
            continue
        rows.append(CatalogRow(file, tag, status, tuple(INGESTED_REF.findall(f"{tag} {status}"))))
    return rows


def classify_ingested(rows: list[CatalogRow], ingested_files: list[str]) -> tuple[set[str], dict[str, str]]:
    verdicts: dict[str, list[bool]] = {}
    for row in rows:
        for ref in row.ingested_notes:
            verdicts.setdefault(ref, []).append(row.allowed)
    allowed: set[str] = set()
    blocked: dict[str, str] = {}
    for name in ingested_files:
        found = verdicts.get(name)
        if not found:
            blocked[name] = "not referenced in book_catalog.md"
        elif not all(found):
            blocked[name] = "catalog tag is not core-craft (excluded, caution or mislabeled)"
        else:
            allowed.add(name)
    return allowed, blocked


# --- chunking ----------------------------------------------------------------


@dataclass(frozen=True)
class Chunk:
    id: str
    source_path: str
    heading: str
    text: str

    @property
    def sha(self) -> str:
        return sha(self.text)


def _split_long(body: str, limit: int) -> list[str]:
    if len(body) <= limit:
        return [body]
    parts: list[str] = []
    current = ""
    for para in re.split(r"\n\s*\n", body):
        para = para.strip()
        if not para:
            continue
        while len(para) > limit:
            cut = para.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            if current:
                parts.append(current)
                current = ""
            parts.append(para[:cut].strip())
            para = para[cut:].strip()
        if current and len(current) + 2 + len(para) > limit:
            parts.append(current)
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current:
        parts.append(current)
    return parts


def chunk_markdown(relpath: str, text: str) -> tuple[list[Chunk], list[str]]:
    """Split a note file at headings; returns (kept chunks, headings of dropped sections)."""
    chunks: list[Chunk] = []
    dropped: list[str] = []
    stack: list[tuple[int, str]] = []
    buffer: list[str] = []
    in_code = False

    def flush() -> None:
        body = "\n".join(buffer).strip()
        buffer.clear()
        if not body:
            return
        path = [heading for _, heading in stack]
        label = " > ".join(path) if path else Path(relpath).stem
        if any(EXCLUDED_HEADING.search(heading) for heading in path):
            dropped.append(label)
            return
        for part in _split_long(body, MAX_CHUNK_CHARS):
            chunks.append(Chunk(f"{relpath}#{len(chunks)}", relpath, label, part))

    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
        match = None if in_code else HEADING_LINE.match(line)
        if match:
            flush()
            level = len(match.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, match.group(2)))
        else:
            buffer.append(line)
    flush()
    return chunks, dropped


def extract_section(text: str, heading_prefix: str) -> str:
    lines = text.splitlines()
    for i, line in enumerate(lines):
        match = HEADING_LINE.match(line)
        if match and match.group(2).lower().startswith(heading_prefix.lower()):
            level = len(match.group(1))
            body = [line]
            for following in lines[i + 1:]:
                nxt = HEADING_LINE.match(following)
                if nxt and len(nxt.group(1)) <= level:
                    break
                body.append(following)
            return "\n".join(body).strip()
    return ""


# --- retrieval -----------------------------------------------------------------


def tokenize(text: str) -> list[str]:
    words = [w for w in re.findall(r"[a-z]+(?:'[a-z]+)?", text.lower()) if len(w) > 1 and w not in STOPWORDS]
    return _stemmer.stemWords(words)


class BM25:
    def __init__(self, documents: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.term_freqs = [Counter(doc) for doc in documents]
        self.lengths = [len(doc) for doc in documents]
        count = len(documents)
        self.avg_length = (sum(self.lengths) / count) if count else 0.0
        doc_freq: Counter = Counter()
        for doc in documents:
            doc_freq.update(set(doc))
        self.idf = {term: math.log(1 + (count - n + 0.5) / (n + 0.5)) for term, n in doc_freq.items()}

    def scores(self, query: list[str]) -> list[float]:
        terms = set(query)
        results = []
        for tf, length in zip(self.term_freqs, self.lengths):
            norm = self.k1 * (1 - self.b + self.b * length / self.avg_length) if self.avg_length else self.k1
            results.append(sum(
                self.idf[t] * tf[t] * (self.k1 + 1) / (tf[t] + norm) for t in terms if t in tf
            ))
        return results


@dataclass
class KnowledgeBase:
    root: Path
    documents: dict[str, str]
    ethics_frame: str
    chunks: list[Chunk]
    blocked_files: dict[str, str]
    dropped_sections: list[str]
    index: BM25

    @classmethod
    def load(cls, root: Path, added_dir: Path | None = None) -> "KnowledgeBase":
        """added_dir: the approved notes of books the practice added (one .md file per book)."""
        documents = {}
        for rel in sorted(set(SCRIPT_CORE_FILES) | set(CONSULTATION_CORE_FILES)):
            path = root / rel
            if not path.is_file():
                raise KnowledgeBaseError(f"Required knowledge file is missing: {path}")
            documents[rel] = path.read_text(encoding="utf-8")
        for rel in SCRIPT_OPTIONAL_FILES:
            if (root / rel).is_file():
                documents[rel] = (root / rel).read_text(encoding="utf-8")

        for required in (ETHICS_PATH, CATALOG_PATH):
            if not (root / required).is_file():
                raise KnowledgeBaseError(f"Required knowledge file is missing: {root / required}")
        ethics = extract_section((root / ETHICS_PATH).read_text(encoding="utf-8"), ETHICS_HEADING)
        if not ethics:
            raise KnowledgeBaseError(f"'{ETHICS_HEADING}' section not found in {root / ETHICS_PATH}")

        rows = parse_catalog((root / CATALOG_PATH).read_text(encoding="utf-8"))
        ingested = sorted(p.name for p in (root / INGESTED_DIR).glob("*.md"))
        allowed, blocked = classify_ingested(rows, ingested)

        chunks: list[Chunk] = []
        dropped: list[str] = []
        for name in sorted(allowed):
            rel = f"{INGESTED_DIR}/{name}"
            kept, removed = chunk_markdown(rel, (root / rel).read_text(encoding="utf-8"))
            chunks.extend(kept)
            dropped.extend(f"{rel}: {heading}" for heading in removed)
        for path in sorted(added_dir.glob("*.md")) if added_dir and added_dir.is_dir() else []:
            rel = f"{ADDED_PREFIX}/{path.name}"
            kept, removed = chunk_markdown(rel, path.read_text(encoding="utf-8"))
            chunks.extend(kept)
            dropped.extend(f"{rel}: {heading}" for heading in removed)

        index = BM25([tokenize(f"{c.heading}\n{c.text}") for c in chunks])
        return cls(root, documents, ethics, chunks, blocked, dropped, index)

    def search(self, query: str, k: int = 10, per_source: int = 3, char_budget: int = 30000) -> list[Chunk]:
        terms = tokenize(query)
        if not terms or not self.chunks:
            return []
        scores = self.index.scores(terms)
        ranked = sorted(range(len(self.chunks)), key=lambda i: scores[i], reverse=True)
        picked: list[Chunk] = []
        per: Counter = Counter()
        used = 0
        for i in ranked:
            if scores[i] <= 0 or len(picked) >= k:
                break
            chunk = self.chunks[i]
            if per[chunk.source_path] >= per_source or used + len(chunk.text) > char_budget:
                continue
            picked.append(chunk)
            per[chunk.source_path] += 1
            used += len(chunk.text)
        return picked

    def consultation_reference(self) -> list[tuple[str, str]]:
        return [(f"{ETHICS_PATH}#ethical-frame", self.ethics_frame)] + [
            (rel, self.documents[rel]) for rel in CONSULTATION_CORE_FILES
        ]

    def script_reference(self, category: str) -> list[tuple[str, str]]:
        files = list(SCRIPT_CORE_FILES) + [rel for rel in SCRIPT_OPTIONAL_FILES if rel in self.documents]
        if category in PRESENTING_ISSUES:
            files.append(f"knowledge_base/techniques/presenting-issues/{category}.md")
        return [(f"{ETHICS_PATH}#ethical-frame", self.ethics_frame)] + [
            (rel, self.documents[rel]) for rel in files
        ]


def render_documents(docs: list[tuple[str, str]], wrapper: str) -> str:
    body = "\n\n".join(f'<document path="{path}">\n{text.strip()}\n</document>' for path, text in docs)
    return f"<{wrapper}>\n{body}\n</{wrapper}>"


def render_library_notes(chunks: list[Chunk]) -> str:
    if not chunks:
        return "<library_notes>\n(no library notes matched this client)\n</library_notes>"
    body = "\n\n".join(
        f'<note source="{c.source_path}" section="{c.heading}">\n{c.text}\n</note>' for c in chunks
    )
    return f"<library_notes>\n{body}\n</library_notes>"


class KnowledgeStore:
    """Reloads the knowledge base whenever any of its markdown files change on disk (a book approved or removed
    in the admin panel included)."""

    def __init__(self, root: Path, added_dir: Path | None = None):
        self.root = root
        self.added_dir = added_dir
        self._kb: KnowledgeBase | None = None
        self._fingerprint: tuple | None = None

    def _current_fingerprint(self) -> tuple:
        files = [*(self.root / "knowledge_base").rglob("*.md"), *(self.root / "templates").rglob("*.md")]
        if self.added_dir and self.added_dir.is_dir():
            files += list(self.added_dir.glob("*.md"))
        return tuple(sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in files))

    def get(self) -> KnowledgeBase:
        fingerprint = self._current_fingerprint()
        if self._kb is None or fingerprint != self._fingerprint:
            self._kb = KnowledgeBase.load(self.root, self.added_dir)
            self._fingerprint = fingerprint
        return self._kb
