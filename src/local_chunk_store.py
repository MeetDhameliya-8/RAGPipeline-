"""Local SQLite vector store for generated GastroAGI chunks.

This is a local review/retrieval database, not the production GastroAGI backend.
It stores draft chunks with review-required metadata and deterministic local
embeddings so the BOT page can retrieve relevant chunks without external calls.
"""

from __future__ import annotations

from .diagnostics import stage

from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterator

from .chunk_validator import source_claim_issues
from .providers.azure import AzureChunkGenerator
from .providers.base import ChunkGenerationError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCAL_DB_DIR = PROJECT_ROOT / "local_db"
LOCAL_DB_PATH = LOCAL_DB_DIR / "gastroagi_chunks.sqlite3"
EMBEDDING_DIMENSIONS = 256
EMBEDDING_ALGORITHM = "local_hashing_v1"
TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9-]{2,}")
MIN_EVIDENCE_SCORE = 0.05
MAX_CONTEXT_TERMS = 24
MAX_HISTORY_PROMPT_TOKENS = 600
GENERIC_MATCH_TERMS = {
    "adult", "adults", "care", "clinical", "compare", "comparison", "current",
    "detailed", "discussed", "evidence", "every", "findings", "implications",
    "latest", "main", "management", "patient", "patients", "practical", "receive",
    "recent", "reported", "research", "study", "therapy", "therapeutic", "treated",
    "treatment", "trial", "versus", "disease", "important", "new", "last", "days",
    "after", "available", "show", "shows", "studies", "summarize", "tell",
}
EVIDENCE_DIMENSION_TERMS = {
    "dose", "dosing", "dosage", "mg", "milligram", "milligrams",
    "mortality", "death", "deaths", "survival",
    "adverse", "safety", "harm", "harms", "complication", "complications",
    "efficacy", "response", "remission",
}
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "for", "from",
    "about", "also", "how", "in", "is", "it", "of", "on", "or", "that", "the", "this", "to",
    "with", "what", "when", "where", "which", "who", "why",
    "could", "would", "should", "please", "was", "were", "main",
}

@dataclass(frozen=True)
class StoredChunk:
    chunk_id: str
    source_file: str
    output_dir: str
    chunk_number: int
    topic: str
    subtopic: str
    section: str
    reference: str
    original_text: str
    validation_status: str
    review_status: str

    @property
    def citation_label(self) -> str:
        parts = [self.source_file, f"chunk {self.chunk_number}"]
        if self.section:
            parts.append(self.section)
        if self.reference:
            parts.append(self.reference)
        return " | ".join(parts)


@dataclass(frozen=True)
class StoredHit:
    chunk: StoredChunk
    score: float
    excerpt: str
    matched_terms: tuple[str, ...]


@dataclass(frozen=True)
class PreparedChatTurn:
    question: str
    database: dict[str, Any]
    hits: list[StoredHit]
    evidence_scope: dict[str, Any]
    static_response: dict[str, Any] | None = None
    response_type: str = "evidence_summary"


def init_db(db_path: Path = LOCAL_DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            create table if not exists chunks (
                chunk_id text primary key,
                source_file text not null,
                output_dir text not null,
                chunk_number integer not null,
                topic text not null,
                subtopic text not null,
                section text not null,
                reference text not null,
                original_text text not null,
                validation_status text not null,
                review_status text not null,
                content_hash text not null,
                created_at text not null,
                updated_at text not null
            )
            """
        )
        conn.execute(
            """
            create table if not exists embeddings (
                chunk_id text primary key,
                algorithm text not null,
                dimensions integer not null,
                vector_json text not null,
                foreign key(chunk_id) references chunks(chunk_id) on delete cascade
            )
            """
        )
        conn.execute(
            """
            create table if not exists ingestion_audit (
                id integer primary key autoincrement,
                event_type text not null,
                chunk_id text,
                output_dir text,
                detail_json text not null,
                created_at text not null
            )
            """
        )
        conn.execute("create index if not exists idx_chunks_output_dir on chunks(output_dir)")
        conn.execute("create index if not exists idx_chunks_section on chunks(section)")


def db_status(db_path: Path = LOCAL_DB_PATH) -> dict[str, Any]:
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        chunk_count = conn.execute("select count(*) from chunks").fetchone()[0]
        package_count = conn.execute("select count(distinct output_dir) from chunks").fetchone()[0]
    return {
        "status": "LOCAL_REVIEW_DB_READY",
        "enabled": True,
        "path": str(db_path),
        "chunk_count": chunk_count,
        "package_count": package_count,
        "embedding_algorithm": EMBEDDING_ALGORITHM,
        "dimensions": EMBEDDING_DIMENSIONS,
        "production_backend": False,
        "review_required": True,
    }


def ingest_chunks_file(
    chunks_path: Path,
    validation_status: str,
    db_path: Path = LOCAL_DB_PATH,
) -> dict[str, Any]:
    with stage("sqlite_storage"):
        init_db(db_path)
        validation_status = validation_status.strip().upper()
        if validation_status not in {"PASS", "REVIEW_REQUIRED"}:
            return _audit_only(
                db_path,
                "blocked_failed_validation" if validation_status == "FAILED" else "blocked_unverified_validation",
                chunks_path.parent.name,
                {"reason": "Only PASS or REVIEW_REQUIRED packages can enter the local review DB."},
            )
        chunks = _load_pipeline_chunks(chunks_path, validation_status)
        if not chunks:
            return {"status": "NOT_STORED", "enabled": False, "reason": "No valid chunks were available."}
        now = _utc_now()
        inserted = 0
        updated = 0
        unchanged = 0
        package_name = chunks_path.parent.name
        expected_ids = {chunk.chunk_id for chunk in chunks}
        with sqlite3.connect(db_path) as conn:
            conn.execute("begin immediate")
            existing_ids = {
                row[0] for row in conn.execute(
                    "select chunk_id from chunks where output_dir = ?", (package_name,)
                )
            }
            obsolete_ids = existing_ids - expected_ids
            # Replace a package snapshot atomically; a failed transaction retains its prior contents.
            conn.executemany("delete from embeddings where chunk_id = ?", [(value,) for value in obsolete_ids])
            conn.executemany("delete from chunks where chunk_id = ?", [(value,) for value in obsolete_ids])
            if obsolete_ids:
                conn.execute(
                    "insert into ingestion_audit (event_type, output_dir, detail_json, created_at) values (?, ?, ?, ?)",
                    ("local_review_replace", package_name, json.dumps({"removed": len(obsolete_ids)}), now),
                )
            for chunk in chunks:
                existing = conn.execute(
                    "select content_hash, validation_status, review_status from chunks where chunk_id = ?",
                    (chunk.chunk_id,),
                ).fetchone()
                embedding = conn.execute(
                    "select algorithm, dimensions from embeddings where chunk_id = ?", (chunk.chunk_id,)
                ).fetchone()
                if (
                    existing == (_content_hash(chunk), chunk.validation_status, chunk.review_status)
                    and embedding == (EMBEDDING_ALGORITHM, EMBEDDING_DIMENSIONS)
                ):
                    unchanged += 1
                    continue
                elif existing:
                    updated += 1
                else:
                    inserted += 1
                vector = embed_text(_chunk_embedding_text(chunk))
                conn.execute(
                    """
                    insert into chunks (
                        chunk_id, source_file, output_dir, chunk_number, topic, subtopic,
                        section, reference, original_text, validation_status, review_status,
                        content_hash, created_at, updated_at
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    on conflict(chunk_id) do update set
                        source_file=excluded.source_file,
                        output_dir=excluded.output_dir,
                        chunk_number=excluded.chunk_number,
                        topic=excluded.topic,
                        subtopic=excluded.subtopic,
                        section=excluded.section,
                        reference=excluded.reference,
                        original_text=excluded.original_text,
                        validation_status=excluded.validation_status,
                        review_status=excluded.review_status,
                        content_hash=excluded.content_hash,
                        updated_at=excluded.updated_at
                    """,
                    (
                        chunk.chunk_id,
                        chunk.source_file,
                        chunk.output_dir,
                        chunk.chunk_number,
                        chunk.topic,
                        chunk.subtopic,
                        chunk.section,
                        chunk.reference,
                        chunk.original_text,
                        chunk.validation_status,
                        chunk.review_status,
                        _content_hash(chunk),
                        now,
                        now,
                    ),
                )
                conn.execute(
                    """
                    insert into embeddings (chunk_id, algorithm, dimensions, vector_json)
                    values (?, ?, ?, ?)
                    on conflict(chunk_id) do update set
                        algorithm=excluded.algorithm,
                        dimensions=excluded.dimensions,
                        vector_json=excluded.vector_json
                    """,
                    (chunk.chunk_id, EMBEDDING_ALGORITHM, EMBEDDING_DIMENSIONS, json.dumps(vector)),
                )
                conn.execute(
                    """
                    insert into ingestion_audit (event_type, chunk_id, output_dir, detail_json, created_at)
                    values (?, ?, ?, ?, ?)
                    """,
                    (
                        "local_review_upsert",
                        chunk.chunk_id,
                        chunk.output_dir,
                        json.dumps({"validation_status": validation_status, "review_status": chunk.review_status}),
                        now,
                    ),
                )
        return {
            "status": "LOCAL_REVIEW_DB_STORED",
            "enabled": True,
            "chunks_seen": len(chunks),
            "inserted": inserted,
            "updated": updated,
            "unchanged": unchanged,
            "removed": len(obsolete_ids),
            "output_dir": package_name,
            "review_required": True,
        }


def verify_chunks_file_ingested(
    chunks_path: Path,
    validation_status: str,
    db_path: Path = LOCAL_DB_PATH,
) -> dict[str, Any]:
    """Read back local review DB rows for a chunks.json package after ingestion."""
    with stage("sqlite_readback"):
        init_db(db_path)
        if validation_status.upper() == "FAILED":
            return {
                "status": "READBACK_SKIPPED_FAILED_VALIDATION",
                "verified": False,
                "reason": "FAILED validation packages are intentionally not stored.",
                "expected_chunks": 0,
                "stored_chunks": 0,
            }
        chunks = _load_pipeline_chunks(chunks_path, validation_status)
        expected_ids = [chunk.chunk_id for chunk in chunks]
        if not expected_ids:
            return {
                "status": "READBACK_FAILED",
                "verified": False,
                "reason": "No valid chunks were available for read-back verification.",
                "expected_chunks": 0,
                "stored_chunks": 0,
            }
        placeholders = ",".join("?" for _ in expected_ids)
        with sqlite3.connect(db_path) as conn:
            chunk_rows = conn.execute(
                f"select chunk_id from chunks where chunk_id in ({placeholders})",
                tuple(expected_ids),
            ).fetchall()
            embedding_rows = conn.execute(
                f"select chunk_id from embeddings where chunk_id in ({placeholders}) and algorithm = ? and dimensions = ?",
                (*expected_ids, EMBEDDING_ALGORITHM, EMBEDDING_DIMENSIONS),
            ).fetchall()
            package_ids = {
                row[0] for row in conn.execute(
                    "select chunk_id from chunks where output_dir = ?", (chunks_path.parent.name,)
                )
            }
        stored_ids = {row[0] for row in chunk_rows}
        embedded_ids = {row[0] for row in embedding_rows}
        missing_ids = sorted(set(expected_ids) - stored_ids)
        missing_embeddings = sorted(set(expected_ids) - embedded_ids)
        obsolete_ids = sorted(package_ids - set(expected_ids))
        verified = not missing_ids and not missing_embeddings and not obsolete_ids
        return {
            "status": "READBACK_VERIFIED" if verified else "READBACK_FAILED",
            "verified": verified,
            "expected_chunks": len(expected_ids),
            "stored_chunks": len(stored_ids),
            "embedding_rows": len(embedded_ids),
            "missing_chunk_ids": missing_ids,
            "missing_embedding_ids": missing_embeddings,
            "obsolete_chunk_ids": obsolete_ids,
            "embedding_algorithm": EMBEDDING_ALGORITHM,
            "dimensions": EMBEDDING_DIMENSIONS,
            "output_dir": chunks_path.parent.name,
        }


def retrieve(query: str, top_k: int = 4, db_path: Path = LOCAL_DB_PATH) -> list[StoredHit]:
    init_db(db_path)
    query_tokens = tuple(tokenize(query))
    if not query_tokens:
        return []
    query_vector = embed_text(query)
    if not any(query_vector):
        return []
    hits: list[StoredHit] = []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select c.*, e.vector_json
            from chunks c
            join embeddings e on e.chunk_id = c.chunk_id
            where c.validation_status in ('PASS', 'REVIEW_REQUIRED')
                and e.algorithm = ? and e.dimensions = ?
            """,
            (EMBEDDING_ALGORITHM, EMBEDDING_DIMENSIONS),
        ).fetchall()
    query_terms = set(query_tokens)
    for row in rows:
        chunk = StoredChunk(
            chunk_id=row["chunk_id"],
            source_file=row["source_file"],
            output_dir=row["output_dir"],
            chunk_number=int(row["chunk_number"]),
            topic=row["topic"],
            subtopic=row["subtopic"],
            section=row["section"],
            reference=row["reference"],
            original_text=row["original_text"],
            validation_status=row["validation_status"],
            review_status=row["review_status"],
        )
        chunk_terms = set(tokenize(_chunk_embedding_text(chunk)))
        matched_terms = tuple(sorted(query_terms & chunk_terms))
        if not matched_terms:
            continue
        vector = json.loads(row["vector_json"])
        score = cosine_similarity(query_vector, vector)
        if score <= 0:
            continue
        lexical_boost = min(len(matched_terms) / max(len(query_terms), 1), 1.0) * 0.2
        hits.append(
            StoredHit(
                chunk=chunk,
                score=score + lexical_boost,
                excerpt=extract_relevant_excerpt(chunk.original_text, query_terms),
                matched_terms=matched_terms,
            )
        )
    hits.sort(key=lambda hit: (hit.score, len(hit.matched_terms)), reverse=True)
    return hits[:top_k]


def build_db_rag_response(
    question: str,
    db_path: Path = LOCAL_DB_PATH,
    history: list[dict[str, str]] | None = None,
    use_llm: bool = False,
) -> dict[str, Any]:
    turn = _prepare_chat_turn(question, history or [], db_path)
    if turn.static_response is not None:
        return turn.static_response

    answer_mode = "extractive_synthesis"
    answer = _build_extractive_answer(
        turn.question,
        turn.hits,
        turn.evidence_scope,
        response_type=turn.response_type,
    )
    if use_llm:
        llm_answer = _try_llm_grounded_answer(
            turn.question,
            turn.hits,
            history or [],
            response_type=turn.response_type,
        )
        if llm_answer:
            answer = llm_answer
            answer_mode = "azure_grounded_synthesis"

    return _chat_response_payload(
        "ANSWERED_FROM_LOCAL_REVIEW_DB",
        answer,
        turn.hits,
        turn.database,
        answer_mode=answer_mode,
        evidence_scope=turn.evidence_scope,
        response_type=turn.response_type,
    )


def _prepare_chat_turn(
    question: str,
    history: list[dict[str, str]],
    db_path: Path,
) -> PreparedChatTurn:
    clean_question = " ".join(question.split())
    status = db_status(db_path)
    response_type = _classify_response_type(clean_question, history)
    empty_scope = _evidence_scope(clean_question, [], response_type=response_type)
    if not clean_question:
        return PreparedChatTurn(
            clean_question,
            status,
            [],
            empty_scope,
            _chat_response_payload(
                "INVALID_QUESTION",
                "Please enter a message.",
                [],
                status,
                answer_mode="conversation_control",
                evidence_scope=empty_scope,
                response_type="conversation",
            ),
        )
    conversation_reply = _conversation_reply(clean_question)
    if conversation_reply:
        conversational_type = (
            "greeting"
            if conversation_reply["status"] == "CONVERSATIONAL_GREETING"
            else "acknowledgement"
        )
        return PreparedChatTurn(
            clean_question,
            status,
            [],
            empty_scope,
            _chat_response_payload(
                conversation_reply["status"],
                conversation_reply["answer"],
                [],
                status,
                answer_mode="conversation_control",
                evidence_scope=empty_scope,
                response_type=conversational_type,
            ),
        )
    if _is_obviously_off_topic(clean_question):
        return PreparedChatTurn(
            clean_question,
            status,
            [],
            empty_scope,
            _chat_response_payload(
                "OUT_OF_SCOPE",
                "I’m focused on gastroenterology, hepatology, and related clinical research. Ask me a GI or liver question and I’ll help.",
                [],
                status,
                answer_mode="scope_control",
                evidence_scope=empty_scope,
                response_type="off_topic",
            ),
        )
    if status["chunk_count"] == 0:
        return PreparedChatTurn(
            clean_question,
            status,
            [],
            empty_scope,
            _chat_response_payload(
                "NO_STORED_CHUNKS",
                "I don’t have reviewed clinical evidence available yet. Add and process source PDFs before asking evidence-based questions.",
                [],
                status,
                answer_mode="evidence_gate",
                evidence_scope=empty_scope,
                response_type="evidence_gap",
            ),
        )

    retrieval_query = _clinical_retrieval_query(clean_question, history, response_type)
    retrieval_limit = 30 if _is_comparison_question(clean_question) else 12
    candidates = retrieve(retrieval_query, top_k=retrieval_limit, db_path=db_path)
    relevant_hits = _relevant_hits_for_question(
        clean_question, candidates, retrieval_query=retrieval_query
    )
    if _requested_time_window(clean_question):
        relevant_hits = _hits_within_requested_window(clean_question, relevant_hits)
    hits = _select_answer_hits(
        clean_question,
        relevant_hits,
        response_type=response_type,
    )
    scope = _evidence_scope(clean_question, hits, response_type=response_type)
    if scope["requested_time_window"] and not scope["window_verified"]:
        days = int(scope["requested_time_window"].split("_")[1])
        latest = scope["latest_reference_year"]
        latest_text = f" The newest matching source I can date reliably is from {latest}." if latest else ""
        response = _chat_response_payload(
            "RECENT_RESEARCH_UNAVAILABLE",
            f"I cannot confirm publications from the requested {days}-day window because no qualifying publication date is "
            "available and no authorized current-literature feed is configured."
            + latest_text,
            [],
            status,
            answer_mode="evidence_gate",
            evidence_scope=scope,
            response_type=response_type,
        )
        return PreparedChatTurn(clean_question, status, [], scope, response, response_type)
    if not hits:
        return PreparedChatTurn(
            clean_question,
            status,
            [],
            scope,
            _chat_response_payload(
                "INSUFFICIENT_REVIEWED_EVIDENCE",
                "I don’t have enough verified information to answer that safely. Try narrowing the question to a specific condition, treatment, outcome, or study.",
                [],
                status,
                answer_mode="evidence_gate",
                evidence_scope=scope,
                response_type=response_type,
            ),
        )
    return PreparedChatTurn(
        clean_question,
        status,
        hits,
        scope,
        response_type=response_type,
    )


def _classify_response_type(question: str, history: list[dict[str, str]]) -> str:
    normalized = question.casefold()
    if _contains_unverified_breakthrough_claim(question):
        return "unsupported_claim"
    if history and _looks_like_follow_up(question):
        return "follow_up"
    if _is_recent_question(question):
        return "recent_research"
    if _is_comparison_question(question):
        return "drug_comparison"
    if re.search(r"\b(trial|study|studies|endpoint|endpoints)\b", normalized):
        return "clinical_trial"
    if re.search(
        r"\b(assess|assessed|assessment|consider|considerations|factors|before changing|clinical reasoning)\b",
        normalized,
    ):
        return "clinical_reasoning"
    if re.search(r"\b(explain|definition|define|what is|simple terms|in simple terms)\b", normalized):
        return "general_medical_explanation"
    return "evidence_summary"


def _clinical_retrieval_query(
    question: str,
    history: list[dict[str, str]],
    response_type: str,
) -> str:
    query = _contextual_retrieval_query(question, history)
    normalized = question.casefold()
    if (
        response_type == "clinical_reasoning"
        and "postoperative" in normalized
        and "infliximab" in normalized
    ):
        query += " endoscopic objective pharmacokinetic trough concentration antibodies immunogenicity safety"
    return query


def _contextual_retrieval_query(question: str, history: list[dict[str, str]]) -> str:
    """Expand short follow-up questions with bounded recent clinical context."""
    if not history or not _looks_like_follow_up(question):
        return question
    context_terms: list[str] = []
    saw_user_turn = False
    ignored_terms = GENERIC_MATCH_TERMS | {
        "about", "answer", "current", "discuss", "discussed", "explain", "question",
        "ready", "reviewed", "source", "sure", "topic",
    }
    for item in reversed(history[-6:]):
        role = str(item.get("role", "")).strip()
        if role not in {"user", "assistant"}:
            continue
        content = " ".join(str(item.get("content", "")).split())[:900]
        for token in tokenize(content):
            if token in ignored_terms or token in context_terms:
                continue
            context_terms.append(token)
            if len(context_terms) >= MAX_CONTEXT_TERMS:
                break
        if role == "user":
            saw_user_turn = True
        if saw_user_turn or len(context_terms) >= MAX_CONTEXT_TERMS:
            break
    if not context_terms:
        return question
    return " ".join([question, *context_terms])


def _looks_like_follow_up(question: str) -> bool:
    normalized = re.sub(r"[^a-z0-9\s]", " ", question.lower()).strip()
    tokens = normalized.split()
    if not tokens:
        return False
    starters = (
        "what about", "how about", "and ", "also ", "what were", "what was",
        "main findings", "adverse events", "safety", "limitations", "outcomes",
        "tell me more", "expand on", "elaborate", "clinical implications",
        "practical implications", "why does", "why is", "can you explain that",
        "could you explain that", "explain that", "make that simpler",
    )
    if any(normalized.startswith(starter) for starter in starters):
        return True
    return len(tokens) <= 6 and any(
        token in {"it", "they", "them", "that", "those", "this", "study", "trial", "findings", "outcomes", "safety"}
        for token in tokens
    )


def _has_sufficient_evidence(hit: StoredHit) -> bool:
    return bool(hit.matched_terms) and hit.score >= MIN_EVIDENCE_SCORE


def _has_specific_evidence_match(hit: StoredHit) -> bool:
    return bool(set(hit.matched_terms) - GENERIC_MATCH_TERMS)


def _relevant_hits_for_question(
    question: str,
    hits: list[StoredHit],
    retrieval_query: str | None = None,
) -> list[StoredHit]:
    question_terms = set(tokenize(question))
    resolved_terms = set(tokenize(retrieval_query or question))
    anchor_terms = resolved_terms - GENERIC_MATCH_TERMS
    entity_terms = anchor_terms - EVIDENCE_DIMENSION_TERMS
    required_named_entities = _required_named_entities(question_terms, hits)
    return _rank_hits_for_current_question(
        retrieval_query or question,
        [
            hit
            for hit in hits
            if _has_sufficient_evidence(hit)
            and _has_specific_evidence_match(hit)
            and _matches_question_anchors(hit, entity_terms)
            and _matches_requested_dimension(hit, question_terms)
            and (
                _matches_condition_scope(question_terms, hit)
                or (
                    _is_comparison_question(question)
                    and bool(required_named_entities)
                    and _matches_named_entities(required_named_entities, hit)
                )
            )
            and _matches_named_entities(required_named_entities, hit)
        ],
    )


def _required_named_entities(question_terms: set[str], hits: list[StoredHit]) -> set[str]:
    metadata_terms: set[str] = set()
    for hit in hits:
        metadata_terms.update(
            tokenize(
                " ".join(
                    [hit.chunk.topic, hit.chunk.subtopic, hit.chunk.section, hit.chunk.original_text]
                )
            )
        )
    excluded = GENERIC_MATCH_TERMS | EVIDENCE_DIMENSION_TERMS | {
        "crohn", "crohns", "ulcerative", "colitis", "inflammatory", "bowel", "ibd",
        "adult", "adults", "children", "pediatric", "clinical", "postoperative",
        "induction", "maintenance", "management", "failure", "prevention", "after",
    }
    return (question_terms & metadata_terms) - excluded


def _matches_named_entities(required_terms: set[str], hit: StoredHit) -> bool:
    if not required_terms:
        return True
    metadata_terms = set(
        tokenize(" ".join([hit.chunk.topic, hit.chunk.subtopic, hit.chunk.section, hit.chunk.original_text]))
    )
    return bool(required_terms & metadata_terms)


def _matches_condition_scope(question_terms: set[str], hit: StoredHit) -> bool:
    metadata = " ".join([hit.chunk.topic, hit.chunk.subtopic, hit.chunk.section]).casefold()
    if "ibd" in question_terms:
        return any(term in metadata for term in ("inflammatory bowel disease", "crohn", "ulcerative colitis"))
    if "crohn" in question_terms or "crohns" in question_terms:
        return "crohn" in metadata
    if {"ulcerative", "colitis"} <= question_terms:
        return "ulcerative colitis" in metadata
    return True


def _matches_question_anchors(hit: StoredHit, entity_terms: set[str]) -> bool:
    if not entity_terms:
        return True
    return bool(entity_terms & set(hit.matched_terms))


def _matches_requested_dimension(hit: StoredHit, question_terms: set[str]) -> bool:
    dimension_groups = (
        {"dose", "dosing", "dosage", "mg", "milligram", "milligrams"},
        {"mortality", "death", "deaths", "survival"},
        {"adverse", "safety", "harm", "harms", "complication", "complications"},
        {"efficacy", "response", "remission"},
    )
    matched = set(hit.matched_terms)
    for group in dimension_groups:
        if question_terms & group and not matched & group:
            return False
    return True


def _rank_hits_for_current_question(question: str, hits: list[StoredHit]) -> list[StoredHit]:
    current_terms = set(tokenize(question)) - GENERIC_MATCH_TERMS
    if not current_terms:
        return hits
    ranked = sorted(
        hits,
        key=lambda hit: (len(current_terms & set(hit.matched_terms)), hit.score, len(hit.matched_terms)),
        reverse=True,
    )
    if _is_recent_question(question):
        ranked.sort(
            key=lambda hit: (
                (_reference_date(hit.chunk.reference) or date.min).toordinal(),
                _reference_year(hit.chunk.reference) or 0,
                len(current_terms & set(hit.matched_terms)),
                hit.score,
            ),
            reverse=True,
        )
    return ranked


def _select_answer_hits(
    question: str,
    hits: list[StoredHit],
    response_type: str = "evidence_summary",
) -> list[StoredHit]:
    if response_type == "general_medical_explanation":
        return _select_general_explanation_hits(hits)
    if response_type == "clinical_reasoning":
        return _select_reasoning_hits(hits)
    if response_type == "clinical_trial":
        return _select_trial_hits(hits)
    if response_type == "unsupported_claim":
        primary = [hit for hit in hits if _trial_hit_category(hit) == "primary"]
        return (primary or hits)[:1]
    limit = 6 if _is_comparison_question(question) else 4
    if not _is_comparison_question(question):
        return hits[:limit]
    named_terms = _required_named_entities(set(tokenize(question)), hits)
    selected: list[StoredHit] = []
    if named_terms:
        for term in sorted(named_terms, key=lambda value: question.casefold().find(value)):
            matching = [
                hit
                for hit in hits
                if term
                in set(
                    tokenize(
                        " ".join(
                            [hit.chunk.topic, hit.chunk.subtopic, hit.chunk.section, hit.chunk.original_text]
                        )
                    )
                )
            ]
            matching.sort(
                key=lambda hit: _comparison_entity_priority(hit, term, named_terms),
                reverse=True,
            )
            selected.extend(hit for hit in matching[:2] if hit not in selected)
        return selected[:limit]
    query_terms = set(tokenize(question)) - GENERIC_MATCH_TERMS - EVIDENCE_DIMENSION_TERMS
    term_frequency = {
        term: sum(1 for hit in hits if term in set(hit.matched_terms))
        for term in query_terms
    }
    for term in sorted(query_terms, key=lambda value: (term_frequency.get(value, 0), value)):
        candidate = next(
            (hit for hit in hits if term in set(hit.matched_terms) and hit not in selected),
            None,
        )
        if candidate is not None:
            selected.append(candidate)
        if len(selected) >= limit:
            return selected
    seen_subtopics: set[str] = set()
    seen_subtopics.update(hit.chunk.subtopic.casefold() for hit in selected)
    for hit in hits:
        subtopic = hit.chunk.subtopic.casefold()
        if subtopic in seen_subtopics:
            continue
        selected.append(hit)
        seen_subtopics.add(subtopic)
        if len(selected) >= limit:
            return selected
    for hit in hits:
        if hit not in selected:
            selected.append(hit)
        if len(selected) >= limit:
            break
    return selected


def _select_general_explanation_hits(hits: list[StoredHit]) -> list[StoredHit]:
    if not hits:
        return []
    return [max(hits, key=_general_explanation_priority)]


def _general_explanation_priority(hit: StoredHit) -> tuple[int, float]:
    text = " ".join(
        [hit.chunk.topic, hit.chunk.subtopic, hit.chunk.section, hit.chunk.original_text]
    ).casefold()
    score = 0
    if re.search(r"crohn.{0,40}(?:is|described as).{0,30}chronic", text):
        score += 8
    if "not all adults" in text:
        score += 3
    if any(term in text for term in ("pediatric", "children", "6 to 17", "childhood")):
        score -= 8
    if any(term in text for term in ("postoperative", "resection", "after surgery")):
        score -= 6
    if any(term in text for term in ("anti-tnf failure", "antagonist failure", "refractory")):
        score -= 6
    if "moderate to severe" in text or "moderately to severely" in text:
        score -= 3
    if any(term in hit.chunk.section.casefold() for term in ("baseline", "eligible", "trial design")):
        score -= 3
    return score, hit.score


def _select_reasoning_hits(hits: list[StoredHit]) -> list[StoredHit]:
    selected: list[StoredHit] = []
    categories = (
        ("objective", ("objective", "endoscopic", "defined", "definition")),
        ("pharmacokinetic", ("pharmacokinetic", "trough", "concentration", "antibodies", "immunogenicity")),
        ("risk", ("higher risk", "risk factor", "recurrence")),
        ("safety", ("safety", "infection", "adverse")),
    )
    for _, terms in categories:
        candidate = next(
            (
                hit
                for hit in hits
                if hit not in selected
                and any(term in (hit.chunk.section + " " + hit.chunk.original_text).casefold() for term in terms)
            ),
            None,
        )
        if candidate is not None:
            selected.append(candidate)
    selected.extend(hit for hit in hits if hit not in selected)
    return selected[:4]


def _select_trial_hits(hits: list[StoredHit]) -> list[StoredHit]:
    selected: list[StoredHit] = []
    for category in ("primary", "secondary", "design", "limitations"):
        candidates = [
            hit for hit in hits if hit not in selected and _trial_hit_category(hit) == category
        ]
        candidates.sort(key=lambda hit: _trial_category_priority(hit, category), reverse=True)
        candidate = candidates[0] if candidates else None
        if candidate is not None:
            selected.append(candidate)
    selected.extend(hit for hit in hits if hit not in selected)
    return selected[:4]


def _trial_hit_category(hit: StoredHit) -> str:
    section = hit.chunk.section.casefold()
    text = (hit.chunk.section + " " + hit.chunk.original_text).casefold()
    if any(term in section for term in ("secondary", "exploratory", "nominal", "week 10")):
        return "secondary"
    if "primary endpoint" in section or "primary end point" in section:
        return "primary"
    if "primary endpoint" in text or "primary end point" in text:
        return "primary"
    if any(term in text for term in ("secondary", "exploratory", "nominal", "week 10")):
        return "secondary"
    if any(term in text for term in ("trial design", "study design", "randomized", "enrolled", "eligible")):
        return "design"
    if any(term in text for term in ("limitation", "safety", "adverse", "interpretation")):
        return "limitations"
    return "other"


def _trial_category_priority(hit: StoredHit, category: str) -> tuple[int, float]:
    section = hit.chunk.section.casefold()
    category_terms = {
        "primary": ("primary", "endpoint", "end point"),
        "secondary": ("week 10", "secondary", "exploratory", "nominal"),
        "design": ("design", "participants", "population", "baseline"),
        "limitations": ("limitation", "safety", "adverse"),
    }
    section_score = sum(1 for term in category_terms.get(category, ()) if term in section)
    return section_score, hit.score


def _comparison_hit_priority(hit: StoredHit) -> tuple[int, float]:
    text = (hit.chunk.section + " " + hit.chunk.original_text).casefold()
    outcome_score = sum(
        1
        for term in ("result", "efficacy", "remission", "recurrence", "safety", "outcome", "endpoint")
        if term in text
    )
    return outcome_score, hit.score


def _comparison_entity_priority(
    hit: StoredHit,
    term: str,
    named_terms: set[str],
) -> tuple[int, int, int, float]:
    metadata_terms = set(tokenize(" ".join([hit.chunk.topic, hit.chunk.subtopic, hit.chunk.section])))
    explicit_named_terms = metadata_terms & named_terms
    outcome_score, hit_score = _comparison_hit_priority(hit)
    return (
        int(term in metadata_terms),
        int(not (explicit_named_terms - {term})),
        outcome_score,
        hit_score,
    )


def _is_comparison_question(question: str) -> bool:
    normalized = question.casefold()
    return bool(re.search(r"\b(compare|comparison|versus|vs\.?|difference|better than)\b", normalized))


def _is_recent_question(question: str) -> bool:
    normalized = question.casefold()
    return bool(
        re.search(r"\b(latest|recent|newest|most recent|last\s+\d+\s+days?|past\s+\d+\s+days?)\b", normalized)
    )


def _requested_time_window(question: str) -> str | None:
    normalized = question.casefold()
    match = re.search(r"\b(?:last|past)\s+(\d+)\s+days?\b", normalized)
    return f"last_{match.group(1)}_days" if match else None


def _hits_within_requested_window(question: str, hits: list[StoredHit]) -> list[StoredHit]:
    requested_window = _requested_time_window(question)
    if not requested_window:
        return hits
    days = int(requested_window.split("_")[1])
    today = datetime.now(timezone.utc).date().toordinal()
    cutoff = today - days
    return [
        hit
        for hit in hits
        if (publication_date := _reference_date(hit.chunk.reference)) is not None
        and cutoff <= publication_date.toordinal() <= today
    ]


def _reference_date(reference: str) -> date | None:
    iso_match = re.search(r"\b(20\d{2})-(0[1-9]|1[0-2])-([0-2]\d|3[01])\b", reference)
    if not iso_match:
        return None
    try:
        return date.fromisoformat(iso_match.group(0))
    except ValueError:
        return None


def _reference_year(reference: str) -> int | None:
    years = [int(value) for value in re.findall(r"\b(?:19|20)\d{2}\b", reference)]
    return max(years) if years else None


def _evidence_scope(
    question: str,
    hits: list[StoredHit],
    response_type: str = "evidence_summary",
) -> dict[str, Any]:
    years = [year for hit in hits if (year := _reference_year(hit.chunk.reference)) is not None]
    requested_window = _requested_time_window(question)
    publication_dates = [
        publication_date
        for hit in hits
        if (publication_date := _reference_date(hit.chunk.reference)) is not None
    ]
    window_verified = False
    if requested_window and publication_dates:
        window_verified = bool(_hits_within_requested_window(question, hits))
    population_scopes = sorted({_population_scope(hit) for hit in hits})
    meaningful_population_scopes = {value for value in population_scopes if value != "general"}
    return {
        "corpus_limited": True,
        "date_aware": _is_recent_question(question),
        "latest_reference_year": max(years) if years else None,
        "requested_time_window": requested_window,
        "window_verified": window_verified,
        "comparison": _is_comparison_question(question),
        "population_scopes": population_scopes,
        "population_mismatch": (
            response_type == "drug_comparison" and len(meaningful_population_scopes) > 1
        ),
        "general_explanation_limited": (
            response_type == "general_medical_explanation"
            and bool(hits)
            and all(scope != "general" for scope in population_scopes)
        ),
        "review_required": any(hit.chunk.review_status == "REVIEW_REQUIRED" for hit in hits),
    }


def _population_scope(hit: StoredHit) -> str:
    text = " ".join(
        [hit.chunk.topic, hit.chunk.subtopic, hit.chunk.section, hit.chunk.original_text]
    ).casefold()
    if any(term in text for term in ("pediatric", "children", "6 to 17", "childhood")):
        return "pediatric"
    if any(term in text for term in ("postoperative", "post-operative", "resection", "after surgery")):
        return "adult_postoperative"
    if any(term in text for term in ("anti-tnf failure", "antagonist failure", "refractory", "moderate to severe")):
        return "selected_severe_or_refractory"
    if re.search(r"\badults?\b", text):
        return "adult_other"
    return "general"


def _population_scope_label(hit: StoredHit) -> str:
    labels = {
        "pediatric": "Pediatric (children or adolescents)",
        "adult_postoperative": "Adults after intestinal resection",
        "selected_severe_or_refractory": "Selected severe or treatment-refractory population",
        "adult_other": "Adults",
        "general": "Not clearly specified",
    }
    return labels[_population_scope(hit)]


def _is_obviously_off_topic(question: str) -> bool:
    terms = set(tokenize(question))
    off_topic_terms = {
        "capital", "france", "weather", "football", "soccer", "cricket", "recipe",
        "movie", "music", "president", "programming", "python", "javascript", "joke",
        "smartphone", "phone", "photography", "camera", "laptop", "television", "gaming",
    }
    return bool(terms & off_topic_terms)


def stream_db_rag_response(
    question: str,
    db_path: Path = LOCAL_DB_PATH,
    history: list[dict[str, str]] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield chat response events, using provider token streaming when LLM evidence synthesis runs."""
    turn = _prepare_chat_turn(question, history or [], db_path)
    if turn.static_response is not None:
        yield from _stream_static_response(turn.static_response)
        return

    base_result = _chat_response_payload(
        "ANSWERED_FROM_LOCAL_REVIEW_DB",
        "",
        turn.hits,
        turn.database,
        answer_mode="azure_grounded_synthesis_stream",
        evidence_scope=turn.evidence_scope,
        response_type=turn.response_type,
    )
    yield {
        "type": "meta",
        "grounding_status": base_result["grounding_status"],
        "answer_mode": base_result["answer_mode"],
        "response_type": base_result["response_type"],
    }
    answer_parts: list[str] = []
    try:
        provider = AzureChunkGenerator.from_environment()
        prompt = _grounded_answer_prompt()
        context = _answer_context(
            turn.question,
            turn.hits,
            history or [],
            response_type=turn.response_type,
        )
        for delta in provider.stream_generate(context, "local_review_database", prompt):
            answer_parts.append(delta)
        streamed_answer = "".join(answer_parts)
        cited_answer = _validated_grounded_answer(streamed_answer, turn.hits)
        if cited_answer is None:
            raise ChunkGenerationError("Grounded synthesis returned unresolved citation markers.")
        for delta in answer_parts:
            yield {"type": "delta", "text": delta}
        if cited_answer != streamed_answer:
            yield {"type": "delta", "text": cited_answer[len(streamed_answer):]}
        done_result = {**base_result, "answer": cited_answer, "streaming": "provider_token_stream"}
        yield {"type": "done", "result": _result_without_answer(done_result)}
    except Exception as exc:  # Provider/configuration failures fall back to source excerpts.
        fallback_answer = _build_extractive_answer(
            turn.question,
            turn.hits,
            turn.evidence_scope,
            response_type=turn.response_type,
        )
        fallback_result = _chat_response_payload(
            "ANSWERED_FROM_LOCAL_REVIEW_DB",
            fallback_answer,
            turn.hits,
            turn.database,
            answer_mode="extractive_synthesis_after_stream_error",
            evidence_scope=turn.evidence_scope,
            response_type=turn.response_type,
        )
        message = str(exc) if isinstance(exc, ChunkGenerationError) else "Grounded synthesis was unavailable; using source excerpts."
        yield {"type": "error", "message": message, "recoverable": True}
        yield {"type": "delta", "text": fallback_answer}
        yield {"type": "done", "result": _result_without_answer({**fallback_result, "streaming": "provider_stream_failed_fallback"})}


def _stream_static_response(result: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield {
        "type": "meta",
        "grounding_status": result.get("grounding_status"),
        "answer_mode": result.get("answer_mode"),
        "response_type": result.get("response_type"),
    }
    answer = str(result.get("answer") or "")
    if answer:
        yield {"type": "delta", "text": answer}
    yield {"type": "done", "result": _result_without_answer(result)}


def _result_without_answer(result: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in result.items() if key != "answer"}
def _chat_response_payload(
    grounding_status: str,
    answer: str,
    hits: list[StoredHit],
    database: dict[str, Any],
    answer_mode: str,
    evidence_scope: dict[str, Any] | None = None,
    response_type: str = "evidence_summary",
) -> dict[str, Any]:
    return {
        "grounding_status": grounding_status,
        "answer": answer,
        "answer_mode": answer_mode,
        "response_type": response_type,
        "presentation": {
            "version": 1,
            "format": "safe_markdown",
            "layout": response_type,
        },
        "citations": [_citation_label(index, hit) for index, hit in enumerate(hits, start=1)],
        "citation_details": [_citation_detail(index, hit) for index, hit in enumerate(hits, start=1)],
        "retrieved_chunks": [
            {
                "citation_number": index,
                "chunk_id": hit.chunk.chunk_id,
                "source_file": hit.chunk.source_file,
                "output_dir": hit.chunk.output_dir,
                "validation_status": hit.chunk.validation_status,
                "review_status": hit.chunk.review_status,
                "chunk_number": hit.chunk.chunk_number,
                "topic": hit.chunk.topic,
                "subtopic": hit.chunk.subtopic,
                "section": hit.chunk.section,
                "reference": hit.chunk.reference,
                "score": round(hit.score, 4),
                "matched_terms": list(hit.matched_terms),
                "excerpt": hit.excerpt,
            }
            for index, hit in enumerate(hits, start=1)
        ],
        "database": database,
        "evidence_scope": evidence_scope or _evidence_scope("", hits, response_type=response_type),
    }



def _conversation_reply(question: str) -> dict[str, str] | None:
    normalized = re.sub(r"[^a-z0-9\s]", " ", question.lower())
    tokens = [token for token in normalized.split() if token]
    normalized_text = " ".join(tokens)
    capability_question = bool(
        re.fullmatch(
            r"(?:(?:hi|hello|hey)\s+)?(?:what|how)\s+(?:can|do)\s+you\s+(?:help|do)(?:\s+me)?(?:\s+with)?",
            normalized_text,
        )
    )
    if capability_question:
        return {
            "status": "CONVERSATIONAL_GREETING",
            "answer": (
                "Hi! I'm GastroAGI. I can help you explore gastroenterology and hepatology research, "
                "understand clinical findings, and discuss gastroenterology research. What would you like to explore?"
            ),
        }
    if not tokens or len(tokens) > 5:
        return None
    token_set = set(tokens)
    if "gastroagi" in token_set and len(token_set) > 1:
        token_set = token_set - {"gastroagi"}
    if token_set <= {"hi", "hii", "hello", "hey", "yo", "there"}:
        return {
            "status": "CONVERSATIONAL_GREETING",
            "answer": "Hello! I'm GastroAGI. How can I help you with clinical research today?",
        }
    if token_set <= {"good", "morning", "afternoon", "evening", "hello", "hi"} and "good" in token_set:
        return {
            "status": "CONVERSATIONAL_GREETING",
            "answer": "Good day. I'm ready to help with gastroenterology and hepatology questions.",
        }
    if token_set <= {"thanks", "thank", "you", "appreciate", "appreciated"} and ("thanks" in token_set or "thank" in token_set):
        return {
            "status": "CONVERSATIONAL_ACKNOWLEDGEMENT",
            "answer": "You're welcome. Send another clinical question whenever you're ready.",
        }
    if token_set <= {"ok", "okay", "great", "cool", "fine"}:
        return {
            "status": "CONVERSATIONAL_ACKNOWLEDGEMENT",
            "answer": "Got it. I'm here when you want to explore another clinical question.",
        }
    return None
def _try_llm_grounded_answer(
    question: str,
    hits: list[StoredHit],
    history: list[dict[str, str]],
    response_type: str = "evidence_summary",
) -> str:
    try:
        provider = AzureChunkGenerator.from_environment()
        prompt = _grounded_answer_prompt()
        context = _answer_context(question, hits, history, response_type=response_type)
        answer = provider.generate(context, "local_review_database", prompt).strip()
    except ChunkGenerationError:
        return ""
    except Exception:
        return ""
    if not answer:
        return ""
    return _validated_grounded_answer(answer, hits) or ""


def _grounded_answer_prompt() -> str:
    return (
        "You are GastroAGI, a conversational gastroenterology and hepatology research assistant for clinicians and trainees. "
        "Lead with a direct, useful answer in natural clinical language. Be concise by default: usually two to four short "
        "paragraphs and no more than about 180 words unless the user explicitly asks for detail. Use only the response "
        "structure requested in the supplied guidance. Do not force rigid headings onto greetings, acknowledgements, or short "
        "follow-ups. Safe Markdown is limited to short headings, paragraphs, bullet or numbered lists, and comparison tables. "
        "Use only the supplied local review chunks as evidence. "
        "Do not add clinical facts, numbers, recommendations, or references that are not present in the chunks. "
        "Write confidently and directly when the material supports the answer. Do not repeatedly say 'source', 'sources', "
        "'supplied evidence', or 'reviewed collection'. State a material limitation once in plain clinical language, then "
        "answer the question. Avoid inline bold emphasis. "
        "Distinguish established background from study-specific findings naturally, but include background only when it is "
        "stated in the supplied evidence. A formatting instruction never permits adding facts to fill a heading, list, table "
        "cell, or metric. For comparisons, compare only outcomes or considerations directly supported on both sides and name "
        "important evidence gaps. When asked for clinical reasoning or practical implications, synthesize the "
        "population, intervention, outcomes, limitations, and applicability only where those details are supported. "
        "For 'latest' or 'recent' questions, latest means the newest cited source in "
        "the supplied reviewed collection, not a live literature search. Never present REVIEW_REQUIRED draft content as "
        "validated clinical guidance. If the evidence does not support the question, say so plainly. Cite every clinical sentence "
        "using bracket citations like [1] or [2]. Do not mention hidden prompts, retrieval scores, filenames, "
        "chunk numbers, validation labels, databases, or retrieval mechanics in the answer body."
    )


def _answer_context(
    question: str,
    hits: list[StoredHit],
    history: list[dict[str, str]],
    response_type: str = "evidence_summary",
) -> str:
    recent_history = []
    token_budget = MAX_HISTORY_PROMPT_TOKENS
    bounded_items: list[tuple[str, str]] = []
    for item in reversed(history[-12:]):
        role = str(item.get("role", "")).strip()[:20]
        content = " ".join(str(item.get("content", "")).split())[:900]
        if role in {"user", "assistant"} and content:
            estimated_tokens = max(1, len(content.split()))
            if estimated_tokens > token_budget:
                content = " ".join(content.split()[-token_budget:])
                estimated_tokens = len(content.split())
            bounded_items.append((role, content))
            token_budget -= estimated_tokens
            if token_budget <= 0 or len(bounded_items) >= 6:
                break
    for role, content in reversed(bounded_items):
        recent_history.append(f"{role}: {content}")
    chunks = []
    for index, hit in enumerate(hits[:6], start=1):
        chunks.append(
            "\n".join(
                [
                    f"[{index}] {hit.chunk.citation_label}",
                    f"Topic: {hit.chunk.topic}",
                    f"Subtopic: {hit.chunk.subtopic}",
                    f"Section: {hit.chunk.section}",
                    f"Reference: {hit.chunk.reference}",
                    f"Validation Status: {hit.chunk.validation_status}",
                    f"Review Status: {hit.chunk.review_status}",
                    f"Original Text: {hit.chunk.original_text}",
                ]
            )
        )
    scope = _evidence_scope(question, hits, response_type=response_type)
    response_guidance = _response_length_guidance(question)
    response_guidance += " " + _response_structure_guidance(response_type)
    if scope["comparison"]:
        response_guidance += " Compare only dimensions supported for the named options; state asymmetry in the evidence."
    if scope["population_mismatch"]:
        response_guidance += (
            " The sources describe different populations or clinical settings. Disclose that mismatch and do not make a "
            "general head-to-head efficacy or safety conclusion."
        )
    if scope["general_explanation_limited"]:
        response_guidance += (
            " The evidence comes only from selected trial populations. Do not present it as a complete general disease "
            "description; state the limitation briefly."
        )
    if scope["date_aware"]:
        latest = scope["latest_reference_year"]
        response_guidance += (
            f" The newest matching reference year in this supplied collection is {latest}."
            if latest
            else " No publication year is verifiable from the matching references."
        )
        response_guidance += " Do not imply that a live or comprehensive literature search was performed."
    if _contains_unverified_breakthrough_claim(question):
        response_guidance += " Explicitly state that the supplied sources do not verify the user's claimed breakthrough."
    return "\n\n".join(
        [
            "Conversation history:",
            "\n".join(recent_history) if recent_history else "None.",
            f"Current user question: {question}",
            f"Response type: {response_type}",
            f"Response guidance: {response_guidance}",
            "Available source chunks:",
            "\n\n".join(chunks),
        ]
    )


def _response_length_guidance(question: str) -> str:
    normalized = question.casefold()
    if re.search(r"\b(brief|briefly|short|concise|one sentence|quick summary|tl;?dr)\b", normalized):
        return "Answer directly in one short paragraph, preferably under 90 words."
    if re.search(r"\b(detailed|detail|comprehensive|in depth|deep dive|thorough)\b", normalized):
        return "Give a focused, clinically useful explanation of up to about 350 words, without repetition."
    return "Answer directly in two to four short paragraphs, usually under 180 words."


def _response_structure_guidance(response_type: str) -> str:
    guidance = {
        "general_medical_explanation": (
            "Start with a plain-language definition. Use a short '### Key concepts' section only when multiple supported "
            "concepts are available, and finish with '### Takeaway'."
        ),
        "drug_comparison": (
            "If the sources support meaningful distinctions for both named treatments, use a compact Markdown table. "
            "Include only supported rows and omit unsupported efficacy, safety, dosing, or head-to-head conclusions. "
            "If the evidence is not comparable, state that directly and summarize each treatment separately."
        ),
        "clinical_trial": (
            "Use '### Study evidence', then distinguish '### Primary endpoint' from '### Secondary or exploratory findings', "
            "followed by '### Interpretation'. State the primary result first. Label nominal, secondary, subgroup, or "
            "exploratory findings accurately. A missed primary endpoint must not be reframed as a positive trial based on "
            "later or exploratory signals. Omit every field not established by the sources."
        ),
        "clinical_reasoning": (
            "Use '### Assessment considerations' followed by a short numbered list. Distinguish direct source evidence "
            "from inference and do not issue unsupported treatment directives."
        ),
        "recent_research": (
            "Lead with whether the requested date window can be verified. Summarize only reliably dated findings and keep "
            "the recency limitation brief."
        ),
        "follow_up": (
            "Answer the requested clarification directly in the context of the preceding topic. Prefer a short explanation "
            "or list and do not repeat the full prior report."
        ),
        "unsupported_claim": (
            "Correct the unsupported premise in the first sentence, then summarize only what the cited sources establish."
        ),
        "evidence_summary": (
            "Give a direct synthesis. Use one short heading or bullets only when they improve scanning."
        ),
    }
    return guidance.get(response_type, guidance["evidence_summary"])


def _build_extractive_answer(
    question: str,
    hits: list[StoredHit],
    evidence_scope: dict[str, Any] | None = None,
    response_type: str = "evidence_summary",
) -> str:
    scope = evidence_scope or _evidence_scope(question, hits, response_type=response_type)
    evidence_lines = [f"{hit.excerpt} [{index}]" for index, hit in enumerate(hits[:4], start=1)]
    if scope.get("requested_time_window") and not scope.get("window_verified"):
        days = int(scope["requested_time_window"].split("_")[1])
        answer = (
            f"I cannot verify a complete {days}-day research window because exact publication dates "
            "and an authorized current-literature feed are unavailable. "
        )
        if scope["latest_reference_year"]:
            answer += f"The newest matching publication I can date reliably is from {scope['latest_reference_year']}. "
        answer += "\n\n" + "\n".join(f"- {line}" for line in evidence_lines)
    elif response_type == "unsupported_claim" or _contains_unverified_breakthrough_claim(question):
        answer = "That claim is not verified here.\n\n" + "\n".join(
            f"- {line}" for line in evidence_lines
        )
    elif response_type == "drug_comparison":
        rows = []
        seen_rows: set[tuple[str, str]] = set()
        for index, hit in enumerate(hits[:6], start=1):
            label = _comparison_label(question, hit)
            row_key = (label.casefold(), hit.chunk.section.casefold())
            if row_key in seen_rows:
                continue
            seen_rows.add(row_key)
            finding = hit.excerpt.replace("|", "\\|")
            population = _population_scope_label(hit)
            rows.append(f"| {label} | {population} | {finding} [{index}] |")
        limitation = (
            "The available studies describe different populations and clinical settings, so they do not support a general "
            "head-to-head comparison."
            if scope.get("population_mismatch")
            else "The available studies do not establish a head-to-head conclusion."
        )
        answer = "\n".join(
            [
                "### Comparison",
                "",
                "| Treatment | Population/context | Supported evidence |",
                "| --- | --- | --- |",
                *rows,
                "",
                limitation,
            ]
        )
    elif response_type == "clinical_trial":
        sections: list[str] = ["### Study evidence"]
        categorized = [(_trial_hit_category(hit), line) for hit, line in zip(hits[:4], evidence_lines)]
        primary = [line for category, line in categorized if category == "primary"]
        secondary = [line for category, line in categorized if category == "secondary"]
        context = [line for category, line in categorized if category not in {"primary", "secondary"}]
        if primary:
            sections.extend(["", "### Primary endpoint", "", *(f"- {line}" for line in primary)])
        if secondary:
            sections.extend(
                ["", "### Secondary or exploratory findings", "", *(f"- {line}" for line in secondary)]
            )
        if context:
            sections.extend(["", "### Study context", "", *(f"- {line}" for line in context)])
        sections.extend(
            [
                "",
                "### Interpretation",
                "",
                "Primary endpoint findings remain the confirmatory result. Nominal, secondary, subgroup, or exploratory "
                "findings do not replace a missed primary endpoint; unsupported study fields are omitted.",
            ]
        )
        answer = "\n".join(sections)
    elif response_type == "clinical_reasoning":
        answer = "\n".join(
            [
                "### Assessment considerations",
                "",
                *(f"{index}. {line}" for index, line in enumerate(evidence_lines, start=1)),
                "",
                "These are clinical assessment considerations, not an individual treatment directive.",
            ]
        )
    elif response_type == "general_medical_explanation":
        answer = ""
        if scope.get("general_explanation_limited"):
            answer = (
                "Available research is limited to selected trial populations, so it cannot support a complete general "
                "explanation.\n\n"
            )
        answer += evidence_lines[0]
        if len(evidence_lines) > 1:
            answer += "\n\n### Key concepts\n\n" + "\n".join(
                f"- {line}" for line in evidence_lines[1:]
            )
        answer += "\n\n### Takeaway\n\nThis explanation applies only to the populations described above. [1]"
    elif response_type == "follow_up":
        answer = "In simpler terms, " + " ".join(evidence_lines[:2])
    elif scope["date_aware"] and scope["latest_reference_year"]:
        answer = (
            f"The newest matching publication is from {scope['latest_reference_year']}. "
            + " ".join(evidence_lines)
        )
    else:
        answer = "The findings indicate that " + " ".join(evidence_lines)
    if scope["review_required"]:
        answer += "\n\nClinical review is still required before applying these findings."
    return answer


def _comparison_label(question: str, hit: StoredHit) -> str:
    question_terms = set(tokenize(question))
    metadata_terms = set(tokenize(" ".join([hit.chunk.topic, hit.chunk.subtopic, hit.chunk.section])))
    named_terms = _required_named_entities(question_terms, [hit])
    matching = [term for term in named_terms if term in metadata_terms]
    if matching:
        return sorted(matching, key=lambda value: question.casefold().find(value))[0].title()
    if named_terms:
        return sorted(named_terms, key=lambda value: question.casefold().find(value))[0].title()
    return (hit.chunk.subtopic or hit.chunk.topic).strip()


def _validated_grounded_answer(answer: str, hits: list[StoredHit]) -> str | None:
    cited_answer = answer.strip()
    citation_numbers = [int(value) for value in re.findall(r"\[(\d+)\]", cited_answer)]
    if not citation_numbers or any(number < 1 or number > len(hits) for number in citation_numbers):
        return None
    start = 0
    for marker in re.finditer(r"(?:\[\d+\][ \t]*)+", cited_answer):
        numbers = {int(value) for value in re.findall(r"\[(\d+)\]", marker.group())}
        passage = cited_answer[start:marker.start()]
        # Ordered-list counters are formatting, not clinical statistics.
        passage = re.sub(r"(?m)^\s*\d+\.\s+", "", passage)
        source_text = "\n".join(hits[number - 1].chunk.original_text for number in sorted(numbers))
        if source_claim_issues(passage, source_text):
            return None
        start = marker.end()
    tail = cited_answer[start:].strip(" .\n|")
    if tail and source_claim_issues(
        tail, "\n".join(hits[number - 1].chunk.original_text for number in set(citation_numbers))
    ):
        return None
    return cited_answer


def _contains_unverified_breakthrough_claim(question: str) -> bool:
    normalized = question.casefold()
    absolute_claim = bool(re.search(r"\b(cure|cures|cured|proves?|proved|permanently|guaranteed)\b", normalized))
    unverifiable_recency = bool(re.search(r"\b(yesterday|today|last\s+\d+\s+days?)\b", normalized))
    return absolute_claim and unverifiable_recency


def _citation_label(index: int, hit: StoredHit) -> str:
    return f"[{index}] {hit.chunk.citation_label}"


def _citation_detail(index: int, hit: StoredHit) -> dict[str, Any]:
    return {
        "number": index,
        "label": _citation_label(index, hit),
        "source_file": hit.chunk.source_file,
        "chunk_number": hit.chunk.chunk_number,
        "section": hit.chunk.section,
        "reference": hit.chunk.reference,
        "validation_status": hit.chunk.validation_status,
    }

def _load_pipeline_chunks(chunks_path: Path, validation_status: str) -> list[StoredChunk]:
    data = json.loads(chunks_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("chunks"), list):
        raise ValueError("chunks.json must be a pipeline output object with a chunks list.")
    source_file = str(data.get("article", {}).get("source_file") or chunks_path.name)
    output_dir = chunks_path.parent.name
    identity = None
    report_path = chunks_path.parent/"validation_report.json"
    if report_path.exists():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        identity = (report.get("diagnostics") or {}).get("local_storage_identity")
    if identity is not None and (not isinstance(identity,dict) or identity.get("version")!="pdf-content-v1" or
            not isinstance(identity.get("pdf_sha256"),str) or not re.fullmatch(r"[a-f0-9]{64}",identity["pdf_sha256"])):
        raise ValueError("Invalid local storage source identity.")
    chunks: list[StoredChunk] = []
    seen_numbers: set[int] = set()
    for index, item in enumerate(data["chunks"], start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Chunk {index} must be an object.")
        for field in ("topic", "subtopic", "section", "reference", "original_text"):
            if not isinstance(item.get(field), str) or not item[field].strip():
                raise ValueError(f"Chunk {index} has an invalid {field} field.")
        chunk_number = item.get("chunk_number", index)
        if type(chunk_number) is not int or chunk_number < 1 or chunk_number in seen_numbers:
            raise ValueError(f"Chunk {index} has an invalid or duplicate chunk_number.")
        seen_numbers.add(chunk_number)
        chunk = StoredChunk(
            chunk_id="",
            source_file=source_file,
            output_dir=output_dir,
            chunk_number=chunk_number,
            topic=str(item.get("topic", "")).strip(),
            subtopic=str(item.get("subtopic", "")).strip(),
            section=str(item.get("section", "")).strip(),
            reference=str(item.get("reference", "")).strip(),
            original_text=str(item.get("original_text", "")).strip(),
            validation_status=validation_status,
            review_status="REVIEW_REQUIRED",
        )
        chunks.append(_with_chunk_id(chunk, identity["pdf_sha256"] if identity else None))
    return chunks


def _with_chunk_id(chunk: StoredChunk, pdf_sha256: str | None = None) -> StoredChunk:
    if pdf_sha256:
        identity = json.dumps(["pdf-content-v1",pdf_sha256,chunk.topic,chunk.subtopic,chunk.section,
                               chunk.reference,chunk.original_text],ensure_ascii=False)
        return StoredChunk(**{**chunk.__dict__, "chunk_id":hashlib.sha256(identity.encode("utf-8")).hexdigest()})
    identity = "\n".join([
        chunk.source_file,
        chunk.output_dir,
        str(chunk.chunk_number),
        chunk.topic,
        chunk.subtopic,
        chunk.section,
        chunk.reference,
        chunk.original_text,
    ])
    chunk_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return StoredChunk(**{**chunk.__dict__, "chunk_id": chunk_id})


def _content_hash(chunk: StoredChunk) -> str:
    content = "\n".join([chunk.topic, chunk.subtopic, chunk.section, chunk.reference, chunk.original_text])
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _chunk_embedding_text(chunk: StoredChunk) -> str:
    return " ".join([chunk.section, chunk.section, chunk.section, chunk.topic, chunk.topic, chunk.subtopic, chunk.reference, chunk.original_text])


def tokenize(text: str) -> list[str]:
    return [token.lower() for token in TOKEN_RE.findall(text) if token.lower() not in STOPWORDS]


def embed_text(text: str, dimensions: int = EMBEDDING_DIMENSIONS) -> list[float]:
    vector = [0.0] * dimensions
    for token in tokenize(text):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        vector[index] += 1.0
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return [value / norm for value in vector]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def extract_relevant_excerpt(text: str, query_terms: list[str], max_chars: int = 520) -> str:
    query_set = set(query_terms)
    sentences = re.split(r"(?<=[.!?])\s+", " ".join(text.split()))
    selected = [sentence for sentence in sentences if query_set & set(tokenize(sentence))] or sentences[:2]
    complete_sentences: list[str] = []
    for sentence in selected[:3]:
        candidate = " ".join([*complete_sentences, sentence])
        if complete_sentences and len(candidate) > max_chars:
            break
        complete_sentences.append(sentence)
    return " ".join(complete_sentences).strip()


def _audit_only(db_path: Path, event_type: str, output_dir: str, detail: dict[str, Any]) -> dict[str, Any]:
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "insert into ingestion_audit (event_type, output_dir, detail_json, created_at) values (?, ?, ?, ?)",
            (event_type, output_dir, json.dumps(detail), _utc_now()),
        )
    return {"status": event_type.upper(), "enabled": False, "output_dir": output_dir, **detail}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
















