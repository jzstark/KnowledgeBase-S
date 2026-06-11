import ast
import unittest
from pathlib import Path


SERVER = Path(__file__).resolve().parents[1] / "kb_mcp" / "server.py"


class KbMcpServerContractTests(unittest.TestCase):
    def test_phase_c_tools_are_exposed(self):
        tree = ast.parse(SERVER.read_text(encoding="utf-8"))
        tool_names: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            for dec in node.decorator_list:
                if (
                    isinstance(dec, ast.Call)
                    and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr == "tool"
                ):
                    tool_names.add(node.name)

        self.assertTrue(
            {
                "kb_search",
                "kb_get_node",
                "kb_get_nodes_batch",
                "kb_get_related",
                "kb_timeline",
                "kb_compare",
                "kb_cite",
                "kb_summarize_corpus",
            }.issubset(tool_names)
        )

    def test_adapter_uses_public_api_prefix(self):
        source = SERVER.read_text(encoding="utf-8")

        self.assertIn('KB_PUBLIC_PREFIX = os.environ.get("KB_PUBLIC_PREFIX", "/api/kb/v1")', source)
        self.assertNotIn('"/api/kb/search"', source)
        self.assertNotIn('"/api/kb/node/', source)


if __name__ == "__main__":
    unittest.main()
