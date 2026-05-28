import ast
import importlib
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/app")


REPO_ROOT = Path(__file__).resolve().parents[3]
PUBLIC_API = REPO_ROOT / "services" / "api" / "kb" / "public.py"


class KbPublicContractTests(unittest.TestCase):
    def test_phase_c_routes_are_exposed(self):
        tree = ast.parse(PUBLIC_API.read_text(encoding="utf-8"))
        routes: set[tuple[str, str]] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                func = dec.func
                if not (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "router"
                    and func.attr in {"get", "post"}
                    and dec.args
                    and isinstance(dec.args[0], ast.Constant)
                ):
                    continue
                routes.add((func.attr.upper(), dec.args[0].value))

        self.assertEqual(
            {
                ("GET", "/search"),
                ("GET", "/nodes/{node_id}"),
                ("POST", "/nodes/batch"),
                ("GET", "/nodes/{node_id}/related"),
                ("GET", "/timeline"),
                ("POST", "/compare"),
                ("POST", "/cite"),
                ("POST", "/summarize_corpus"),
            },
            routes,
        )

    def test_search_allows_keyword_only_matches(self):
        source = PUBLIC_API.read_text(encoding="utf-8")

        self.assertIn("OR n.title ILIKE $2", source)
        self.assertIn("OR n.abstract ILIKE $2", source)
        self.assertIn("OR s.body ILIKE $2", source)
        self.assertIn("COALESCE(\n                     CASE", source)

    def test_cite_uses_budgeted_excerpts_but_verifies_against_full_wiki_body(self):
        source = PUBLIC_API.read_text(encoding="utf-8")

        self.assertIn("def _citation_prompt_body", source)
        self.assertIn('limit=None', source)
        self.assertIn("LLM 输入按预算从全文抽取相关窗口；quote 验证使用全文", source)

    def test_compare_context_is_summary_first_for_articles(self):
        source = PUBLIC_API.read_text(encoding="utf-8")

        self.assertIn("FROM summary_nodes", source)
        self.assertIn("WHERE summary_of = :id", source)
        self.assertIn('body_text = _read_wiki_body(USER_ID, node_id, "article", limit=body_chars)', source)


class WikiBodyTests(unittest.TestCase):
    def test_limit_none_returns_full_body_without_ellipsis(self):
        try:
            wiki = importlib.import_module("kb.wiki")
        except ModuleNotFoundError as exc:
            self.skipTest(f"optional dependency not installed: {exc.name}")
        old_user_data_dir = wiki.USER_DATA_DIR
        with tempfile.TemporaryDirectory() as tmp:
            try:
                wiki.USER_DATA_DIR = Path(tmp)
                path = wiki.wiki_file_path("default", "art_test", "article")
                path.parent.mkdir(parents=True)
                path.write_text("---\nid: art_test\n---\n\n# Title\n\n" + "x" * 20, encoding="utf-8")

                self.assertEqual(wiki.read_wiki_body("default", "art_test", "article", limit=None), "x" * 20)
                self.assertEqual(wiki.read_wiki_body("default", "art_test", "article", limit=5), "xxxxx...")
            finally:
                wiki.USER_DATA_DIR = old_user_data_dir


if __name__ == "__main__":
    unittest.main()
