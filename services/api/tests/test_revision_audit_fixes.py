import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
DATABASE = REPO_ROOT / "services" / "api" / "database.py"
INTERNAL_API = REPO_ROOT / "services" / "api" / "kb" / "internal.py"
PUBLIC_API = REPO_ROOT / "services" / "api" / "kb" / "public.py"
KB_TOOLS = REPO_ROOT / "services" / "api" / "kb_tools.py"
SOURCES = REPO_ROOT / "services" / "api" / "routers" / "sources.py"
INGESTION_PIPELINE = REPO_ROOT / "services" / "ingestion-worker" / "pipeline.py"
IMAGE_SOURCE = REPO_ROOT / "services" / "ingestion-worker" / "sources" / "image.py"
PDF_SOURCE = REPO_ROOT / "services" / "ingestion-worker" / "sources" / "pdf.py"


class RevisionAuditFixTests(unittest.TestCase):
    def test_database_removes_legacy_part_of_edges_and_chat_tables(self):
        source = DATABASE.read_text(encoding="utf-8")
        self.assertIn("DELETE FROM knowledge_edges WHERE relation_type = 'part_of'", source)
        self.assertIn("DROP TABLE IF EXISTS chat_messages", source)
        self.assertIn("DROP TABLE IF EXISTS chat_sessions", source)
        self.assertIn("metadata JSONB DEFAULT '{}'", source)
        self.assertNotIn("knowledge_edges ADD COLUMN IF NOT EXISTS description", source)

    def test_internal_embedding_writes_record_embedding_model(self):
        # Ingest logic moved to kb/ingest.py; embedding_model column is always written
        ingest = (REPO_ROOT / "services" / "api" / "kb" / "ingest.py").read_text(encoding="utf-8")
        self.assertIn("embedding_model", ingest)
        self.assertIn(":embedding_model", ingest)

    def test_searches_use_entity_canonical_name(self):
        public = PUBLIC_API.read_text(encoding="utf-8")
        tools = KB_TOOLS.read_text(encoding="utf-8")
        self.assertIn("LEFT JOIN entity_nodes en ON en.node_id = n.id", public)
        self.assertIn("COALESCE(en.canonical_name, n.title) AS title", public)
        self.assertIn("LEFT JOIN entity_nodes en ON en.node_id = n.id", tools)
        self.assertIn("COALESCE(en.canonical_name, n.title) AS title", tools)

    def test_source_item_doc_kind_patch_exists_and_syncs_node(self):
        source = SOURCES.read_text(encoding="utf-8")
        self.assertIn('class SourceItemUpdate', source)
        self.assertIn('@router.patch("/source-items/{item_id}")', source)
        self.assertIn("SET doc_kind = :doc_kind", source)
        self.assertIn("FROM article_nodes an", source)

    def test_source_items_materialize_folder_document_instances(self):
        source = SOURCES.read_text(encoding="utf-8")
        self.assertIn("async def _ensure_document_instance_for_source_item", source)
        self.assertIn("INSERT INTO raw_assets", source)
        self.assertIn("INSERT INTO document_instances", source)
        self.assertIn("SET document_instance_id = :document_instance_id", source)
        self.assertIn("UPDATE document_instances", source)

    def test_worker_text_access_is_guarded(self):
        for path in (INGESTION_PIPELINE, IMAGE_SOURCE, PDF_SOURCE):
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("content[0].text", source, path.name)
            self.assertIn("getattr(", source, path.name)

    def test_worker_models_are_config_driven(self):
        self.assertIn("models.image_ocr", IMAGE_SOURCE.read_text(encoding="utf-8"))
        self.assertIn("models.image_cleanup", IMAGE_SOURCE.read_text(encoding="utf-8"))
        self.assertIn("models.pdf_cleanup", PDF_SOURCE.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
