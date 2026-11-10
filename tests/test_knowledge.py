from pathlib import Path

import pytest

from app.knowledge import (
    KnowledgeBase,
    KnowledgeBaseError,
    KnowledgeStore,
    chunk_markdown,
    classify_ingested,
    parse_catalog,
)
from tests.support import EXCLUDED_MARKERS, write

REAL_KB = Path("knowledge")


def all_text(kb: KnowledgeBase) -> str:
    return "\n".join(f"{c.heading}\n{c.text}" for c in kb.chunks)


def test_only_core_craft_listed_files_are_indexed(kb_root):
    kb = KnowledgeBase.load(kb_root)
    assert {c.source_path for c in kb.chunks} == {"knowledge_base/ingested/good-book.md"}
    assert set(kb.blocked_files) == {"bad-book.md", "mislabeled.md", "caution-book.md", "unlisted.md",
                                     "malformed-ref.md"}
    assert kb.blocked_files["unlisted.md"] == "not referenced in book_catalog.md"


def test_flagged_sections_are_dropped_but_legitimate_ones_kept(kb_root):
    kb = KnowledgeBase.load(kb_root)
    text = all_text(kb)
    for marker in EXCLUDED_MARKERS:
        assert marker not in text
    assert "SMOKING_MARKER" in text
    assert "CAUTIONARY_OK_MARKER" in text  # "cautionary-tale" is not a caution flag
    assert any("Techniques flagged and excluded" in d for d in kb.dropped_sections)


def test_search_never_returns_excluded_material(kb_root):
    kb = KnowledgeBase.load(kb_root)
    for query in ("smoking", "covert smoking thing", "BAD_MARKER smoking", "flagged excluded skipped"):
        for chunk in kb.search(query, k=50, per_source=50):
            for marker in EXCLUDED_MARKERS:
                assert marker not in chunk.text
    top = kb.search("smoking triggers coffee break")
    assert top and "SMOKING_MARKER" in top[0].text


def test_ethics_frame_is_extracted_without_other_sections(kb_root):
    kb = KnowledgeBase.load(kb_root)
    assert "ETHICS_MARKER" in kb.ethics_frame
    assert "NOT_ETHICS_MARKER" not in kb.ethics_frame


def test_references_include_expected_documents(kb_root):
    kb = KnowledgeBase.load(kb_root)
    consult = "\n".join(text for _, text in kb.consultation_reference())
    assert "SCREENING-AND-SAFETY_MARKER" in consult and "ISSUE_smoking-cessation_MARKER" in consult
    script = kb.script_reference("smoking-cessation")
    paths = [p for p, _ in script]
    assert "templates/script_structure.md" in paths
    assert "knowledge_base/techniques/presenting-issues/smoking-cessation.md" in paths
    assert "knowledge_base/techniques/presenting-issues/weight-management.md" not in paths
    assert len(kb.script_reference("other")) == len(script) - 1


def test_missing_required_file_fails_loudly(kb_root):
    (kb_root / "knowledge_base/techniques/screening-and-safety.md").unlink()
    with pytest.raises(KnowledgeBaseError, match="screening-and-safety"):
        KnowledgeBase.load(kb_root)


def test_store_reloads_when_files_change(kb_root):
    store = KnowledgeStore(kb_root)
    first = store.get()
    assert store.get() is first
    write(kb_root, "knowledge_base/ingested/good-book.md", "# Good Book\n\n## New technique\n\nNEW_MARKER\n")
    second = store.get()
    assert second is not first
    assert "NEW_MARKER" in all_text(second)


def test_file_referenced_by_both_allowed_and_excluded_rows_is_blocked():
    rows = parse_catalog(
        "| File | Tag | Status |\n|---|---|---|\n"
        "| A.pdf | core-craft | see `ingested/shared.md` |\n"
        "| B.pdf | excluded | see `ingested/shared.md` |\n"
    )
    allowed, blocked = classify_ingested(rows, ["shared.md"])
    assert allowed == set()
    assert "shared.md" in blocked


def test_code_blocks_do_not_create_headings():
    chunks, dropped = chunk_markdown("x.md", "# T\n\n## Real\n\ntext\n\n```\n## Excluded fake heading\n```\nmore\n")
    assert dropped == []
    assert "## Excluded fake heading" in chunks[0].text


def test_long_sections_are_split():
    body = "\n\n".join(["word " * 200] * 10)
    chunks, _ = chunk_markdown("x.md", f"# T\n\n## Long\n\n{body}")
    assert len(chunks) > 1
    assert all(len(c.text) <= 3500 for c in chunks)


@pytest.mark.skipif(not REAL_KB.is_dir(), reason="the practice knowledge base is not on this machine")
def test_real_knowledge_base_blocks_the_excluded_books():
    kb = KnowledgeBase.load(REAL_KB)
    assert {"nlp-textbook.md", "hypnotic-language-formula.md"} <= set(kb.blocked_files)
    indexed = {c.source_path.rsplit("/", 1)[-1] for c in kb.chunks}
    assert not indexed & set(kb.blocked_files)
    headings = " ".join(c.heading.lower() for c in kb.chunks)
    for word in ("excluded", "flagged", "not adopted", "skipped"):
        assert word not in headings
    assert len(kb.chunks) > 100


CRAFT_NOTE = "knowledge_base/techniques/practitioner-session-craft.md"


def test_the_clinicians_craft_note_reaches_script_writing_only_when_present(kb_root):
    absent = KnowledgeBase.load(kb_root)  # a server that does not have the file yet still starts
    assert CRAFT_NOTE not in [p for p, _ in absent.script_reference("smoking-cessation")]
    write(kb_root, CRAFT_NOTE, "# Craft\n\nCRAFT_MARKER\n")
    kb = KnowledgeBase.load(kb_root)
    paths = [p for p, _ in kb.script_reference("smoking-cessation")]
    assert paths.index(CRAFT_NOTE) > paths.index("knowledge_base/techniques/deepeners.md")  # after the required notes
    assert "CRAFT_MARKER" not in "\n".join(text for _, text in kb.consultation_reference())  # never the consultant's
    assert "CRAFT_MARKER" not in all_text(kb)  # and not searchable library material


def test_store_picks_up_the_craft_note_when_it_is_added(kb_root):
    store = KnowledgeStore(kb_root)
    assert CRAFT_NOTE not in [p for p, _ in store.get().script_reference("other")]
    write(kb_root, CRAFT_NOTE, "# Craft\n\nCRAFT_MARKER\n")
    assert CRAFT_NOTE in [p for p, _ in store.get().script_reference("other")]
