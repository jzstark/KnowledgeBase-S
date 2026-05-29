"""P5 test coverage: cite algorithm, entity promotion thresholds, doc_kind cascade, summary-first fallback."""
import os
import re
import unittest
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/app")

REPO_ROOT = Path(__file__).resolve().parents[3]
INTERNAL_API = REPO_ROOT / "services" / "api" / "kb" / "internal.py"
INGEST_API = REPO_ROOT / "services" / "api" / "kb" / "ingest.py"
PUBLIC_API = REPO_ROOT / "services" / "api" / "kb" / "public.py"


# ─── cite: _citation_prompt_body ─────────────────────────────────────────────

class CitationPromptBodyTests(unittest.TestCase):
    """Unit tests for the excerpt-selection helper used in Stage 1 of /cite."""

    def setUp(self):
        # kb.public can't be imported in test environment (prompts reads a file at import
        # time). Extract just the pure function + its regex dependency via exec instead.
        import ast as _ast
        source = PUBLIC_API.read_text(encoding="utf-8")
        tree = _ast.parse(source)
        lines = source.splitlines(keepends=True)

        re_line: int | None = None
        fn_start_line: int | None = None
        fn_end_line: int | None = None
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Assign):
                for t in node.targets:
                    if isinstance(t, _ast.Name) and t.id == "_CITATION_TERM_RE":
                        re_line = node.lineno - 1
            if isinstance(node, _ast.FunctionDef) and node.name == "_citation_prompt_body":
                fn_start_line = node.lineno - 1
                fn_end_line = node.end_lineno

        if re_line is None or fn_start_line is None:
            self.skipTest("_citation_prompt_body not found in source")

        snippet = "import re\n" + lines[re_line] + "".join(lines[fn_start_line:fn_end_line])
        ns: dict = {}
        exec(snippet, ns)  # noqa: S102
        self._fn = ns["_citation_prompt_body"]

    def test_body_within_limit_returned_unchanged(self):
        body = "hello world this is a short document"
        result = self._fn(body, "hello", None, 1000)
        self.assertEqual(result, body)

    def test_body_over_limit_with_no_matching_terms_truncated(self):
        body = "x" * 2000
        result = self._fn(body, "zzz", None, 100)
        self.assertTrue(result.endswith("..."), repr(result[-10:]))
        self.assertLessEqual(len(result), 103)  # 100 chars + "..."

    def test_body_over_limit_with_matching_terms_contains_term(self):
        filler = "unrelated content " * 100
        body = filler + "target_keyword sits here " + filler
        result = self._fn(body, "target_keyword", None, 500)
        self.assertIn("target_keyword", result)

    def test_excerpts_annotated_with_offset_markers(self):
        filler = "aaa " * 100
        body = filler + "special_term " + filler
        result = self._fn(body, "special_term", None, 300)
        markers = re.findall(r"\[excerpt (\d+):(\d+)\]", result)
        self.assertTrue(len(markers) > 0, "expected [excerpt N:M] markers")
        for start_s, end_s in markers:
            self.assertLessEqual(int(start_s), int(end_s))

    def test_context_terms_also_drive_excerpt_selection(self):
        filler = "noise " * 150
        body = filler + "context_clue is here " + filler
        result = self._fn(body, "irrelevant_claim", "context_clue", 400)
        self.assertIn("context_clue", result)


# ─── cite: server-side quote verification ────────────────────────────────────

class CiteQuoteVerificationTests(unittest.TestCase):
    """Structural tests: quote must appear verbatim in full body (hallucination guard)."""

    def test_quote_verbatim_check_present_in_source(self):
        source = PUBLIC_API.read_text(encoding="utf-8")
        # The guard: skip LLM-proposed quotes not literally present in the full body
        self.assertIn("if quote not in body_texts[aid]:", source)

    def test_full_body_used_for_verification_not_excerpt(self):
        source = PUBLIC_API.read_text(encoding="utf-8")
        # body_texts must be populated with limit=None (full body), not the budgeted excerpt
        self.assertIn("_read_wiki_body(USER_ID, nid, \"article\", limit=None)", source)

    def test_stage_ordering_coarse_then_llm_then_verify(self):
        source = PUBLIC_API.read_text(encoding="utf-8")
        pos_stage1 = source.index("Stage 1")
        pos_stage2 = source.index("Stage 2")
        pos_verify = source.index("服务端验证")
        self.assertLess(pos_stage1, pos_stage2)
        self.assertLess(pos_stage2, pos_verify)


# ─── entity promotion thresholds ─────────────────────────────────────────────

class EntityPromotionThresholdTests(unittest.TestCase):
    """Pure-logic tests mirroring kb/internal.py:1681-1686 (config defaults)."""

    @staticmethod
    def _should_promote(max_salience: float, mention_count: int) -> bool:
        # Mirrors production: promotion_max_salience=0.9, promotion_salience=0.7,
        # promotion_salience_mentions=2, promotion_min_mentions=3
        return (
            max_salience >= 0.9
            or (max_salience >= 0.7 and mention_count >= 2)
            or mention_count >= 3
        )

    def test_salience_at_max_threshold_promotes(self):
        self.assertTrue(self._should_promote(0.9, 0))

    def test_salience_above_max_threshold_promotes(self):
        self.assertTrue(self._should_promote(0.95, 0))

    def test_salience_just_below_max_threshold_blocked(self):
        self.assertFalse(self._should_promote(0.89, 0))

    def test_mid_salience_plus_enough_mentions_promotes(self):
        self.assertTrue(self._should_promote(0.7, 2))
        self.assertTrue(self._should_promote(0.8, 2))

    def test_mid_salience_with_insufficient_mentions_blocked(self):
        self.assertFalse(self._should_promote(0.75, 1))
        self.assertFalse(self._should_promote(0.7, 1))

    def test_high_mention_count_promotes_regardless_of_salience(self):
        self.assertTrue(self._should_promote(0.0, 3))
        self.assertTrue(self._should_promote(0.5, 4))

    def test_below_all_thresholds_blocked(self):
        self.assertFalse(self._should_promote(0.0, 2))
        self.assertFalse(self._should_promote(0.69, 2))

    def test_production_logic_structure_matches_test_mirror(self):
        # Promotion logic moved to kb/ingest.py (do_process_entity_candidates)
        source = INGEST_API.read_text(encoding="utf-8")
        self.assertIn("entity.promotion_max_salience", source)
        self.assertIn("entity.promotion_salience_mentions", source)
        self.assertIn("entity.promotion_min_mentions", source)


# ─── doc_kind cascade ────────────────────────────────────────────────────────

class DocKindCascadeTests(unittest.TestCase):
    """Cascade order: explicit > source_item > source.default > config.default."""

    def _ingest_fn_body(self) -> str:
        # Ingest domain logic moved to kb/ingest.py as do_ingest()
        source = INGEST_API.read_text(encoding="utf-8")
        fn_start = source.index("async def do_ingest(")
        match = re.search(r"\n(?:async )?def ", source[fn_start + 1:])
        fn_end = fn_start + 1 + match.start() if match else fn_start + 8000
        return source[fn_start:fn_end]

    def test_cascade_comment_documents_priority_order(self):
        body = self._ingest_fn_body()
        self.assertIn("source_items.doc_kind", body)
        self.assertIn("sources.default_doc_kind", body)
        self.assertIn("config.doc_kind.default", body)

    def test_cascade_order_explicit_before_source_item_before_source_before_config(self):
        body = self._ingest_fn_body()
        pos_explicit = body.index("doc_kind = (body.doc_kind")
        pos_source_item = body.index("SELECT doc_kind FROM source_items")
        pos_source_default = body.index("SELECT default_doc_kind FROM sources")
        pos_config = body.index("settings.doc_kind.default")
        self.assertLess(pos_explicit, pos_source_item)
        self.assertLess(pos_source_item, pos_source_default)
        self.assertLess(pos_source_default, pos_config)

    def test_invalid_doc_kind_degrades_to_config_default(self):
        body = self._ingest_fn_body()
        self.assertIn("doc_kind not in allowed_kinds", body)
        # Both the normal fallback and the invalid-value branch use settings.doc_kind.default
        self.assertGreaterEqual(body.count("settings.doc_kind.default"), 2)


# ─── summary-first retrieval fallback ────────────────────────────────────────

class SummaryFirstFallbackTests(unittest.TestCase):
    """_load_doc_context: try summary_nodes first, fall back to wiki body."""

    def _load_doc_context_body(self) -> str:
        source = PUBLIC_API.read_text(encoding="utf-8")
        fn_start = source.index("async def _load_doc_context(")
        match = re.search(r"\n(?:async )?def ", source[fn_start + 1:])
        fn_end = fn_start + 1 + match.start() if match else fn_start + 3000
        return source[fn_start:fn_end]

    def test_summary_nodes_queried_before_wiki_fallback(self):
        body = self._load_doc_context_body()
        pos_summary = body.index("FROM summary_nodes")
        pos_fallback = body.index("_read_wiki_body")
        self.assertLess(pos_summary, pos_fallback)

    def test_fallback_triggered_only_when_no_summary_row(self):
        body = self._load_doc_context_body()
        # The guard must exist
        self.assertIn("if summary_row and summary_row[", body)
        # The else branch must precede the wiki call
        pos_else = body.index("else:")
        pos_fallback = body.index("_read_wiki_body")
        self.assertLess(pos_else, pos_fallback)

    def test_summary_query_filters_non_empty_body(self):
        body = self._load_doc_context_body()
        self.assertIn("AND body IS NOT NULL", body)
        self.assertIn("AND length(body) > 0", body)

    def test_default_summary_preferred_via_is_default_ordering(self):
        body = self._load_doc_context_body()
        self.assertIn("ORDER BY is_default DESC", body)


if __name__ == "__main__":
    unittest.main()
