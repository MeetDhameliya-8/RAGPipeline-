# GastroAGI TASK11 — Flowchart Pipelines

## Pipeline A — PDF to Clinical Knowledge

```mermaid
flowchart TD
    A[Clinical PDF Intake] --> B[Document Registration<br/>doc_id, checksum, DOI, source metadata]
    B --> C[PDF Extraction & Cleaning<br/>PyMuPDF, headers/footers cleanup, reading order]
    C --> D{Extraction Reliable?}
    D -- No --> Q1[Quarantine / Manual Review<br/>log extraction issue]
    D -- Yes --> E[Section Mapping & Semantic Segmentation]
    E --> F[Azure AI Clinical Chunk Generation]
    F --> G[Pydantic Schema Validation<br/>Topic, Subtopic, Section, Reference, Original Text]
    G --> H{Schema Valid?}
    H -- No --> Q2[Reject / Retry / Review]
    H -- Yes --> I[Clinical Source-Grounding Validation<br/>claims, stats, population, recommendations]
    I --> J{Clinically Supported?}
    J -- No --> Q3[Reject / Flag Unsupported Claims]
    J -- Yes --> K[Duplicate & Coverage Checks]
    K --> L{Quality Gates Passed?}
    L -- No --> Q4[Reject / Manual Correction]
    L -- Yes --> M[Review Package Creation<br/>chunks + evidence spans + warnings]
    M --> N{Human Approval?}
    N -- No --> Q5[Hold / Revise / Reject]
    N -- Yes --> O[Approved Chunk Staging<br/>stable chunk IDs, versioning]
    O --> P[Authenticated GastroAGI Ingestion Service]
    P --> R[Embedding Generation + Metadata Binding]
    R --> S[ChromaDB Indexing<br/>inactive candidate version]
    S --> T[Retrieval Verification Tests]
    T --> U{Retrieval Verified?}
    U -- No --> Q6[Rollback / Keep Previous Active Version]
    U -- Yes --> V[Publish Active Searchable Knowledge Version]
```

---

## Pipeline B — Clinical RAG Retrieval

```mermaid
flowchart TD
    A[Clinician Question] --> B[GastroAGI Chat API]
    B --> C[Query Understanding<br/>intent, disease, modality, clinical scope]
    C --> D[Query Embedding]
    D --> E[Vector Search in ChromaDB<br/>approved active chunks only]
    E --> F[Metadata Filtering<br/>topic, date, source, approval status]
    F --> G[Evidence Ranking & Relevance Scoring]
    G --> H{Sufficient Relevant Evidence?}
    H -- No --> X[Return Limited-Evidence Response<br/>state uncertainty]
    H -- Yes --> I[Build Grounded Context Package<br/>top chunks + references]
    I --> J[Azure AI Answer Synthesis]
    J --> K[Answer Validation<br/>source support, claim consistency, citation integrity]
    K --> L{Answer Supported?}
    L -- No --> Y[Return Guarded Response / Ask for Clarification]
    L -- Yes --> M[Final Clinical Answer with References]
```

---

## Pipeline C — Error Handling and Recovery

```mermaid
flowchart TD
    A[Pipeline Event / Failure] --> B{Failure Type?}

    B -- Config/Auth --> C[Validate env vars, endpoint, deployment, API key]
    C --> C1[Fail Closed + Sanitized Error Report]

    B -- Azure Rate Limit / Quota --> D[Read error code / Retry-After]
    D --> D1{Transient?}
    D1 -- Yes --> D2[Bounded Retry with Backoff]
    D1 -- No --> D3[Stop Job + Report Quota/Billing Issue]

    B -- Extraction Failure --> E[Flag PDF page/section corruption]
    E --> E1[Quarantine PDF + Manual Review]

    B -- Invalid JSON / Schema --> F[Reject model output]
    F --> F1[Retry Within Safe Limits or Send to Review]

    B -- Unsupported Clinical Claims --> G[Mark chunk unsafe]
    G --> G1[Reject Chunk + Evidence Warning]

    B -- Duplicate Document / Chunk --> H[Check checksum, DOI, chunk IDs]
    H --> H1[Skip Duplicate or Version Update]

    B -- Ingestion Failure --> I[Stop before publish]
    I --> I1[Retain Prior Active Version]

    B -- Retrieval Verification Failure --> J[Rollback candidate index]
    J --> J1[Preserve Existing Searchable Version]

    C1 --> Z[Audit Log + Review Queue]
    D2 --> Z
    D3 --> Z
    E1 --> Z
    F1 --> Z
    G1 --> Z
    H1 --> Z
    I1 --> Z
    J1 --> Z
```

---

## Pipeline D — Codex Implementation Roadmap

```mermaid
flowchart TD
    A[Milestone 1<br/>Repository Inspection & Architecture Map] --> B[Milestone 2<br/>Azure Provider Setup & Secure Config]
    B --> C[Milestone 3<br/>Clinical Chunk Generator Stabilization]
    C --> D[Milestone 4<br/>Schema Validation + Source-Grounding Layer]
    D --> E[Milestone 5<br/>Human Review Package Workflow]
    E --> F[Milestone 6<br/>Approved-only Ingestion Service]
    F --> G[Milestone 7<br/>Embedding + ChromaDB Indexing]
    G --> H[Milestone 8<br/>Retrieval Verification & Publication]

    A --> A1[Inspect AGENTS.md, code, tests, reports]
    B --> B1[Verify endpoint, deployment, API version, env loading]
    C --> C1[Test PDF -> valid 5-field chunks]
    D --> D1[Reject malformed / unsupported content]
    E --> E1[Generate auditable review bundle]
    F --> F1[Stable IDs, idempotent ingestion, duplicates handling]
    G --> G1[Bind embeddings to approved chunk metadata]
    H --> H1[Run clinical retrieval tests before go-live]
```

---

## Optional High-Level End-to-End View

```mermaid
flowchart LR
    A[PDF Corpus] --> B[Extraction]
    B --> C[Azure Chunk Generation]
    C --> D[Validation]
    D --> E[Human Approval]
    E --> F[Ingestion]
    F --> G[Embeddings]
    G --> H[ChromaDB]
    H --> I[Retrieval]
    I --> J[Grounded Clinical Answer]
```
