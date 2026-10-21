import json

from src import local_chunk_store
from src.local_chunk_store import (
    build_db_rag_response,
    db_status,
    ingest_chunks_file,
    retrieve,
    stream_db_rag_response,
    verify_chunks_file_ingested,
)


def write_chunks_file(tmp_path):
    output_dir = tmp_path / "article-output"
    output_dir.mkdir()
    chunks_path = output_dir / "chunks.json"
    chunks_path.write_text(
        json.dumps(
            {
                "article": {"source_file": "paper.pdf"},
                "chunks": [
                    {
                        "chunk_number": 1,
                        "topic": "Crohn disease",
                        "subtopic": "Biologic therapy",
                        "section": "Biologic Therapy in Adults",
                        "reference": "Example Journal. 2026;1:1-5.",
                        "original_text": "Biologic therapy is discussed for adults with moderate Crohn disease and response outcomes.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return chunks_path


def test_ingest_chunks_file_upserts_and_retrieves(tmp_path):
    db_path = tmp_path / "chunks.sqlite3"
    chunks_path = write_chunks_file(tmp_path)

    result = ingest_chunks_file(chunks_path, validation_status="PASS", db_path=db_path)
    hits = retrieve("Crohn biologic adults", db_path=db_path)
    status = db_status(db_path)

    assert result["status"] == "LOCAL_REVIEW_DB_STORED"
    assert result["inserted"] == 1
    assert status["chunk_count"] == 1
    assert hits[0].chunk.section == "Biologic Therapy in Adults"


def test_ingest_is_idempotent_for_same_chunks(tmp_path):
    db_path = tmp_path / "chunks.sqlite3"
    chunks_path = write_chunks_file(tmp_path)

    first = ingest_chunks_file(chunks_path, validation_status="PASS", db_path=db_path)
    second = ingest_chunks_file(chunks_path, validation_status="PASS", db_path=db_path)

    assert first["inserted"] == 1
    assert second["unchanged"] == 1
    assert db_status(db_path)["chunk_count"] == 1


def test_source_content_identity_deduplicates_retry_folders_and_chunk_renumbering(tmp_path):
    first=write_chunks_file(tmp_path)
    report={"diagnostics":{"local_storage_identity":{"version":"pdf-content-v1","pdf_sha256":"a"*64}}}
    (first.parent/'validation_report.json').write_text(json.dumps(report))
    retry=tmp_path/'retry-output';retry.mkdir()
    data=json.loads(first.read_text());data['chunks'][0]['chunk_number']=7
    second=retry/'chunks.json';second.write_text(json.dumps(data))
    (retry/'validation_report.json').write_text(json.dumps(report))
    db=tmp_path/'review.sqlite3'
    assert ingest_chunks_file(first,'REVIEW_REQUIRED',db)['inserted']==1
    assert ingest_chunks_file(second,'REVIEW_REQUIRED',db)['unchanged']==1
    assert db_status(db)['chunk_count']==1
    assert verify_chunks_file_ingested(second,'REVIEW_REQUIRED',db)['verified']
    assert verify_chunks_file_ingested(first,'REVIEW_REQUIRED',db)['verified']


def test_storage_identity_distinguishes_different_pdf_bytes(tmp_path):
    path=write_chunks_file(tmp_path);db=tmp_path/'review.sqlite3'
    report=path.parent/'validation_report.json'
    for digest in ('a'*64,'b'*64):
        # Different sources may legitimately contain the same prose.
        other=tmp_path/digest[:1];other.mkdir();copy=other/'chunks.json';copy.write_bytes(path.read_bytes())
        (other/'validation_report.json').write_text(json.dumps({'diagnostics':{
            'local_storage_identity':{'version':'pdf-content-v1','pdf_sha256':digest}}}))
        ingest_chunks_file(copy,'REVIEW_REQUIRED',db)
    assert db_status(db)['chunk_count']==2


def test_ingest_readback_verifies_chunks_and_embeddings(tmp_path):
    db_path = tmp_path / "chunks.sqlite3"
    chunks_path = write_chunks_file(tmp_path)

    ingest = ingest_chunks_file(chunks_path, validation_status="PASS", db_path=db_path)
    readback = verify_chunks_file_ingested(chunks_path, validation_status="PASS", db_path=db_path)

    assert ingest["status"] == "LOCAL_REVIEW_DB_STORED"
    assert readback["status"] == "READBACK_VERIFIED"
    assert readback["verified"] is True
    assert readback["expected_chunks"] == 1
    assert readback["stored_chunks"] == 1
    assert readback["embedding_rows"] == 1


def test_failed_validation_is_not_indexed(tmp_path):
    db_path = tmp_path / "chunks.sqlite3"
    chunks_path = write_chunks_file(tmp_path)

    result = ingest_chunks_file(chunks_path, validation_status="FAILED", db_path=db_path)

    assert result["status"] == "BLOCKED_FAILED_VALIDATION"
    assert db_status(db_path)["chunk_count"] == 0



def test_db_rag_response_handles_greeting_without_retrieval(tmp_path):
    db_path = tmp_path / "chunks.sqlite3"
    chunks_path = write_chunks_file(tmp_path)
    ingest_chunks_file(chunks_path, validation_status="PASS", db_path=db_path)

    response = build_db_rag_response("Hi GastroAGI", db_path=db_path)

    assert response["grounding_status"] == "CONVERSATIONAL_GREETING"
    assert response["answer"] == "Hello! I'm GastroAGI. How can I help you with clinical research today?"
    assert response["retrieved_chunks"] == []
    assert "PEPTIC" not in response["answer"]


def test_retrieve_requires_lexical_overlap_not_hash_collision(tmp_path):
    db_path = tmp_path / "chunks.sqlite3"
    chunks_path = write_chunks_file(tmp_path)
    ingest_chunks_file(chunks_path, validation_status="PASS", db_path=db_path)

    assert retrieve("hii", db_path=db_path) == []
def test_db_rag_response_is_source_bound(tmp_path):
    db_path = tmp_path / "chunks.sqlite3"
    chunks_path = write_chunks_file(tmp_path)
    ingest_chunks_file(chunks_path, validation_status="PASS", db_path=db_path)

    response = build_db_rag_response("What adults are discussed for Crohn biologic therapy?", db_path=db_path)

    assert response["grounding_status"] == "ANSWERED_FROM_LOCAL_REVIEW_DB"
    assert "paper.pdf | chunk 1" not in response["answer"]
    assert response["citations"][0].startswith("[1] paper.pdf | chunk 1")
    assert "REVIEW_REQUIRED" not in response["answer"]





def test_llm_answer_is_source_grounded_and_preserves_citations(monkeypatch, tmp_path):
    db_path = tmp_path / "chunks.sqlite3"
    chunks_path = write_chunks_file(tmp_path)
    ingest_chunks_file(chunks_path, validation_status="PASS", db_path=db_path)

    class FakeProvider:
        def generate(self, article_text, source_file, prompt):
            assert "Original Text:" in article_text
            assert "Cite every clinical sentence" in prompt
            return "Biologic therapy is discussed for adults with moderate Crohn disease. [1]"

    monkeypatch.setattr(
        local_chunk_store.AzureChunkGenerator,
        "from_environment",
        staticmethod(lambda: FakeProvider()),
    )

    response = build_db_rag_response(
        "What adults are discussed for Crohn biologic therapy?",
        db_path=db_path,
        use_llm=True,
    )

    assert response["answer_mode"] == "azure_grounded_synthesis"
    assert response["answer"] == "Biologic therapy is discussed for adults with moderate Crohn disease. [1]"
    assert response["citation_details"][0]["chunk_number"] == response["retrieved_chunks"][0]["chunk_number"]
    assert response["citation_details"][0]["section"] == response["retrieved_chunks"][0]["section"]


def test_review_required_chunks_are_not_presented_as_validated_guidance(tmp_path):
    db_path = tmp_path / "chunks.sqlite3"
    chunks_path = write_chunks_file(tmp_path)
    ingest_chunks_file(chunks_path, validation_status="REVIEW_REQUIRED", db_path=db_path)

    response = build_db_rag_response("What adults are discussed for Crohn biologic therapy?", db_path=db_path)

    assert response["grounding_status"] == "ANSWERED_FROM_LOCAL_REVIEW_DB"
    assert response["retrieved_chunks"][0]["validation_status"] == "REVIEW_REQUIRED"
    assert "Clinical review is still required" in response["answer"]
    assert "before applying these findings" in response["answer"]
def test_db_rag_response_rejects_generic_only_matches(tmp_path):
    db_path = tmp_path / "chunks.sqlite3"
    chunks_path = write_chunks_file(tmp_path)
    ingest_chunks_file(chunks_path, validation_status="PASS", db_path=db_path)

    response = build_db_rag_response("What were the main findings?", db_path=db_path)

    assert response["grounding_status"] == "INSUFFICIENT_REVIEWED_EVIDENCE"
    assert response["retrieved_chunks"] == []
def write_peptic_and_other_chunks_file(tmp_path):
    output_dir = tmp_path / "trial-output"
    output_dir.mkdir()
    chunks_path = output_dir / "chunks.json"
    chunks_path.write_text(
        json.dumps(
            {
                "article": {"source_file": "trials.pdf"},
                "chunks": [
                    {
                        "chunk_number": 1,
                        "topic": "Stress ulcer prophylaxis",
                        "subtopic": "PEPTIC trial",
                        "section": "PEPTIC Trial Safety Outcomes",
                        "reference": "PEPTIC Investigators. Example Journal. 2026.",
                        "original_text": "In the PEPTIC trial, adverse events and safety outcomes were described for patients receiving proton pump inhibitors or histamine-2 receptor blockers.",
                    },
                    {
                        "chunk_number": 2,
                        "topic": "Inflammatory bowel disease",
                        "subtopic": "OTHER trial",
                        "section": "OTHER Trial Safety Outcomes",
                        "reference": "Other Investigators. Example Journal. 2026.",
                        "original_text": "In the OTHER trial, adverse events and safety outcomes were described for a separate inflammatory bowel disease population.",
                    },
                    {
                        "chunk_number": 3,
                        "topic": "Stress ulcer prophylaxis",
                        "subtopic": "PEPTIC trial",
                        "section": "PEPTIC Trial Mortality and Limitations",
                        "reference": "PEPTIC Investigators. Example Journal. 2026.",
                        "original_text": "The PEPTIC trial mortality outcome and limitations were described for mechanically ventilated intensive care patients receiving proton pump inhibitor or histamine-2 receptor blocker default strategies.",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return chunks_path




def test_conversation_acknowledgement_bypasses_retrieval(tmp_path):
    db_path = tmp_path / "chunks.sqlite3"
    chunks_path = write_peptic_and_other_chunks_file(tmp_path)
    ingest_chunks_file(chunks_path, validation_status="PASS", db_path=db_path)

    response = build_db_rag_response("Thanks", db_path=db_path)

    assert response["grounding_status"] == "CONVERSATIONAL_ACKNOWLEDGEMENT"
    assert response["answer"] == "You're welcome. Send another clinical question whenever you're ready."
    assert response["retrieved_chunks"] == []


def test_good_morning_bypasses_retrieval(tmp_path):
    db_path = tmp_path / "chunks.sqlite3"
    chunks_path = write_peptic_and_other_chunks_file(tmp_path)
    ingest_chunks_file(chunks_path, validation_status="PASS", db_path=db_path)

    response = build_db_rag_response("Good morning", db_path=db_path)

    assert response["grounding_status"] == "CONVERSATIONAL_GREETING"
    assert response["retrieved_chunks"] == []


def test_clinical_evidence_question_uses_rag(tmp_path):
    db_path = tmp_path / "chunks.sqlite3"
    chunks_path = write_peptic_and_other_chunks_file(tmp_path)
    ingest_chunks_file(chunks_path, validation_status="PASS", db_path=db_path)

    response = build_db_rag_response("Explain the PEPTIC trial.", db_path=db_path)

    assert response["grounding_status"] == "ANSWERED_FROM_LOCAL_REVIEW_DB"
    assert response["retrieved_chunks"]
    assert any("PEPTIC" in chunk["section"] for chunk in response["retrieved_chunks"])
def test_follow_up_retrieval_uses_bounded_history_to_keep_study_context(tmp_path):
    db_path = tmp_path / "chunks.sqlite3"
    chunks_path = write_peptic_and_other_chunks_file(tmp_path)
    ingest_chunks_file(chunks_path, validation_status="PASS", db_path=db_path)

    response = build_db_rag_response(
        "What about adverse events?",
        db_path=db_path,
        history=[
            {"role": "user", "content": "What were the primary findings of the PEPTIC trial?"},
            {"role": "assistant", "content": "The PEPTIC trial reported mortality findings from locally stored chunks [1]."},
        ],
    )

    assert response["grounding_status"] == "ANSWERED_FROM_LOCAL_REVIEW_DB"
    assert response["retrieved_chunks"][0]["section"] == "PEPTIC Trial Safety Outcomes"
    assert "peptic" in response["retrieved_chunks"][0]["matched_terms"]




def test_contextual_follow_up_resolves_recent_trial_context(tmp_path):
    db_path = tmp_path / "chunks.sqlite3"
    chunks_path = write_peptic_and_other_chunks_file(tmp_path)
    ingest_chunks_file(chunks_path, validation_status="PASS", db_path=db_path)

    response = build_db_rag_response(
        "What about mortality?",
        db_path=db_path,
        history=[
            {"role": "user", "content": "Explain the PEPTIC trial."},
            {"role": "assistant", "content": "The PEPTIC trial compared PPI and H2 blocker default strategies [1]."},
        ],
    )

    assert response["grounding_status"] == "ANSWERED_FROM_LOCAL_REVIEW_DB"
    assert response["retrieved_chunks"][0]["section"] == "PEPTIC Trial Mortality and Limitations"
    assert "mortality" in response["retrieved_chunks"][0]["matched_terms"]
def test_db_rag_response_refuses_unsupported_general_question(tmp_path):
    db_path = tmp_path / "chunks.sqlite3"
    chunks_path = write_peptic_and_other_chunks_file(tmp_path)
    ingest_chunks_file(chunks_path, validation_status="PASS", db_path=db_path)

    response = build_db_rag_response("What is the capital of France?", db_path=db_path)

    assert response["grounding_status"] == "OUT_OF_SCOPE"
    assert response["retrieved_chunks"] == []
    assert "gastroenterology" in response["answer"].lower()
    assert "database" not in response["answer"].lower()

def test_stream_db_rag_response_forwards_provider_deltas(monkeypatch, tmp_path):
    db_path = tmp_path / "chunks.sqlite3"
    chunks_path = write_chunks_file(tmp_path)
    ingest_chunks_file(chunks_path, validation_status="PASS", db_path=db_path)

    class FakeProvider:
        def stream_generate(self, article_text, source_file, prompt):
            yield "Streamed "
            yield "clinical answer [1]"

    monkeypatch.setattr(
        local_chunk_store.AzureChunkGenerator,
        "from_environment",
        staticmethod(lambda: FakeProvider()),
    )

    events = list(stream_db_rag_response("What adults are discussed for Crohn biologic therapy?", db_path=db_path))

    assert events[0]["type"] == "meta"
    assert events[0]["answer_mode"] == "azure_grounded_synthesis_stream"
    assert [event["text"] for event in events if event["type"] == "delta"] == ["Streamed ", "clinical answer [1]"]
    assert events[-1]["type"] == "done"
    assert events[-1]["result"]["streaming"] == "provider_token_stream"


def test_off_topic_question_gets_natural_scope_reply_without_database_language(tmp_path):
    db_path = tmp_path / "chunks.sqlite3"
    ingest_chunks_file(write_chunks_file(tmp_path), validation_status="PASS", db_path=db_path)

    response = build_db_rag_response("What is the capital of France?", db_path=db_path)

    assert response["grounding_status"] == "OUT_OF_SCOPE"
    assert response["retrieved_chunks"] == []
    assert "gastroenterology" in response["answer"].lower()
    assert "database" not in response["answer"].lower()
    assert "chunk" not in response["answer"].lower()


def test_unsupported_clinical_question_explains_evidence_limit_naturally(tmp_path):
    db_path = tmp_path / "chunks.sqlite3"
    ingest_chunks_file(write_chunks_file(tmp_path), validation_status="PASS", db_path=db_path)

    response = build_db_rag_response("How should cirrhosis ascites be treated?", db_path=db_path)

    assert response["grounding_status"] == "INSUFFICIENT_REVIEWED_EVIDENCE"
    assert response["retrieved_chunks"] == []
    assert "verified information" in response["answer"].lower()
    assert "database" not in response["answer"].lower()


def test_latest_follow_up_context_wins_over_verbose_stale_history(tmp_path):
    db_path = tmp_path / "chunks.sqlite3"
    ingest_chunks_file(write_peptic_and_other_chunks_file(tmp_path), validation_status="PASS", db_path=db_path)
    stale = " ".join(["OTHER inflammatory bowel disease safety outcomes"] * 20)

    response = build_db_rag_response(
        "What about adverse events?",
        db_path=db_path,
        history=[
            {"role": "user", "content": stale},
            {"role": "assistant", "content": stale},
            {"role": "user", "content": "Now let's discuss the PEPTIC trial."},
            {"role": "assistant", "content": "Sure, the PEPTIC trial is our current topic."},
        ],
    )

    assert response["retrieved_chunks"][0]["section"] == "PEPTIC Trial Safety Outcomes"


def write_dated_comparison_chunks_file(tmp_path):
    output_dir = tmp_path / "dated-output"
    output_dir.mkdir()
    chunks_path = output_dir / "chunks.json"
    chunks_path.write_text(
        json.dumps(
            {
                "article": {"source_file": "dated-evidence.pdf"},
                "chunks": [
                    {
                        "chunk_number": 1,
                        "topic": "Ulcerative colitis",
                        "subtopic": "Therapy A",
                        "section": "Therapy A induction findings",
                        "reference": "Example Journal. 2019;1:1-8.",
                        "original_text": "Therapy A was evaluated for induction treatment in adults with ulcerative colitis.",
                    },
                    {
                        "chunk_number": 2,
                        "topic": "Ulcerative colitis",
                        "subtopic": "Therapy B",
                        "section": "Therapy B induction findings",
                        "reference": "Example Journal. 2024;8:20-29.",
                        "original_text": "Therapy B was evaluated for induction treatment in adults with ulcerative colitis.",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return chunks_path


def test_recent_research_query_ranks_newest_reviewed_reference_first(tmp_path):
    db_path = tmp_path / "chunks.sqlite3"
    ingest_chunks_file(write_dated_comparison_chunks_file(tmp_path), validation_status="PASS", db_path=db_path)

    response = build_db_rag_response("What is the latest research on ulcerative colitis induction?", db_path=db_path)

    assert response["grounding_status"] == "ANSWERED_FROM_LOCAL_REVIEW_DB"
    assert response["retrieved_chunks"][0]["reference"] == "Example Journal. 2024;8:20-29."
    assert response["evidence_scope"]["date_aware"] is True
    assert response["evidence_scope"]["latest_reference_year"] == 2024


def test_comparison_query_keeps_evidence_for_both_named_therapies(tmp_path):
    db_path = tmp_path / "chunks.sqlite3"
    ingest_chunks_file(write_dated_comparison_chunks_file(tmp_path), validation_status="PASS", db_path=db_path)

    response = build_db_rag_response("Compare Therapy A versus Therapy B for ulcerative colitis induction.", db_path=db_path)

    subtopics = {chunk["subtopic"] for chunk in response["retrieved_chunks"]}
    assert subtopics == {"Therapy A", "Therapy B"}


def test_comparison_query_covers_named_drugs_even_with_population_mismatch(tmp_path):
    db_path = tmp_path / "chunks.sqlite3"
    output_dir = tmp_path / "comparison-output"
    output_dir.mkdir()
    chunks_path = output_dir / "chunks.json"
    chunks_path.write_text(
        json.dumps(
            {
                "article": {"source_file": "comparison.pdf"},
                "chunks": [
                    {
                        "chunk_number": 1,
                        "topic": "Crohn disease",
                        "subtopic": "Adalimumab",
                        "section": "Adalimumab in pediatric Crohn disease",
                        "reference": "Example Journal. 2012;1:1-8.",
                        "original_text": "Adalimumab was evaluated in pediatric patients with moderate to severe Crohn disease.",
                    },
                    {
                        "chunk_number": 2,
                        "topic": "Ulcerative colitis",
                        "subtopic": "Golimumab",
                        "section": "Golimumab induction",
                        "reference": "Example Journal. 2013;2:10-18.",
                        "original_text": "Golimumab was evaluated for induction treatment in adults with ulcerative colitis.",
                    },
                    {
                        "chunk_number": 3,
                        "topic": "Ulcerative colitis",
                        "subtopic": "General management",
                        "section": "Ulcerative colitis background",
                        "reference": "Example Journal. 2014;3:20-28.",
                        "original_text": "Ulcerative colitis management and clinical outcomes were reviewed.",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    ingest_chunks_file(chunks_path, validation_status="PASS", db_path=db_path)

    response = build_db_rag_response(
        "Compare adalimumab versus golimumab for ulcerative colitis.",
        db_path=db_path,
    )

    subtopics = {chunk["subtopic"] for chunk in response["retrieved_chunks"]}
    assert "Adalimumab" in subtopics
    assert "Golimumab" in subtopics


def test_grounded_prompt_requests_natural_concise_answer_without_rigid_headings():
    prompt = local_chunk_store._grounded_answer_prompt()

    assert "Cite every clinical sentence" in prompt
    assert "concise" in prompt.lower()
    assert "Do not force rigid headings" in prompt
    assert "Clearly separate study evidence from interpretation and uncertainty" not in prompt


def test_response_length_guidance_adapts_to_user_request():
    assert "under 90 words" in local_chunk_store._response_length_guidance("Briefly summarize the findings")
    assert "under 180 words" in local_chunk_store._response_length_guidance("What did the study find?")
    assert "350 words" in local_chunk_store._response_length_guidance("Give me a detailed clinical explanation")






