# KnowledgeBase-S

KnowledgeBase-S organizes source material through document instances in folders and derives articles, summaries, and other knowledge from that material. The underlying authority model is documented in [Source / Folder Revision](docs/revision-source-folders.md).

## Language

**Document instance**:
A folder entry representing a user's organization of a raw asset. Multiple document instances may reference the same raw asset; copying an entry does not automatically generate an article.

**Source item**:
A record of material received through a source, carrying provenance, processing state, and any explicit regeneration request. It may be linked to a document instance.

**Document intake**:
The creation and linking of raw assets, document instances, and source items when material arrives through a manual or connector source. It owns source identity, database commit, and cleanup of a newly saved file when persistence fails.

**Document lifecycle**:
The rules governing processing, retry, regeneration, archive, and permanent deletion of a document instance and its associated source items. A document lifecycle module concentrates those rules and the coordinated persistence they require.

## Agreed refactor scope

The document lifecycle refactor preserves current status meanings and user workflows. It concentrates transition rules, locking, and coordinated writes, with fixes for partial updates and concurrency failures under the existing rules.

The lifecycle module owns permanent deletion end to end: eligibility checks, coordinated database deletion and tombstones, followed by shared-file protection and cleanup after commit. It returns the operation result and cleanup warnings, preserving the existing retry behavior for failed cleanup.

The module also owns single and batch lifecycle operations. Batch coordination includes deletion previews and confirmation checks, per-document outcomes, and one worker trigger per source after regeneration requests are persisted. Each document retains its own transaction so one failure does not roll back successful operations on other documents.

Lifecycle verification uses disposable PostgreSQL locally and in CI. Publishing the affected application and ingestion-worker images requires the lifecycle test gate to pass, including rollback, concurrency, batch partial failures, and cleanup after commit.

Separating processing state from organizational state, or changing the meaning of `pending` for copied documents, is deferred. The implemented interface and acceptance checks are in [Document lifecycle refactor](docs/document-lifecycle-refactor.md).
