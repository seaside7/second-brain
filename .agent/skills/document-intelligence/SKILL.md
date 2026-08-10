# Document Intelligence

Read-only document discovery, indexing, and retrieval from Google Drive. Answers natural-language questions with source citations.

## Architecture

```
Google Drive (configurable folder)
    ↓
DocumentConnector (fetch file list + download)
    ↓
DocumentParser (extract text from PDF/DOCX/TXT/MD/GDocs)
    ↓
DocumentStore (index with metadata + full text)
    ↓
DocumentRetriever (search by keywords/NL query)
    ↓
LLM (DeepSeek/Claude) → answer with source references
```

## Configuration

Set in workspace config: `.agent/workspaces/<workspace>/documents.json`

```json
{
  "source": "google_drive",
  "folder_id": "YOUR_GOOGLE_DRIVE_FOLDER_ID",
  "shared_drive_id": null,
  "supported_types": ["pdf", "docx", "txt", "md", "google-docs"],
  "index_path": "journal/state/document_index.json"
}
```

## Usage

```bash
# Index/sync documents from configured folder
python .agent/skills/document-intelligence/scripts/doc_engine.py sync

# Search documents
python .agent/skills/document-intelligence/scripts/doc_engine.py search --query "API migration"

# Ask a question (LLM-powered answer with source citation)
python .agent/skills/document-intelligence/scripts/doc_engine.py ask --query "What does the contract say about SLA?"

# List indexed documents
python .agent/skills/document-intelligence/scripts/doc_engine.py list

# Show document details
python .agent/skills/document-intelligence/scripts/doc_engine.py show --file-id <drive_file_id>
```

## Safety

- READ-ONLY: never modifies, deletes, or shares Drive files
- No approval needed for any operation
- Failures are non-fatal (logged, processing continues)
