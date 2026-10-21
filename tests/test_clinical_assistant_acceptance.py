import json
import re
from datetime import datetime, timedelta, timezone

from src import local_chunk_store
from src.local_chunk_store import build_db_rag_response, ingest_chunks_file


def _write_clinical_fixture(tmp_path):
    output_dir = tmp_path / "clinical-assistant-fixture"
    output_dir.mkdir()
    chunks_path = output_dir / "chunks.json"
    chunks_path.write_text(
        json.dumps(
            {
                "article": {"source_file": "reviewed-ibd-evidence.pdf"},
                "chunks": [
                    {
                        "chunk_number": 1,
                        "topic": "Crohn disease",
                        "subtopic": "Disease overview",
                        "section": "Clinical description",
                        "reference": "Reviewed clinical source. 2022.",
                        "original_text": (
                            "Crohn disease is a chronic inflammatory bowel disease that can affect the "
                            "gastrointestinal tract and may follow a relapsing course."
                        ),
                    },
                    {
                        "chunk_number": 2,
                        "topic": "Crohn disease",
                        "subtopic": "Postoperative recurrence",
                        "section": "PREVENT trial findings and limitations",
                        "reference": "PREVENT trial report. 2016;150:1568-1578.",
                        "original_text": (
                            "In adults after ileocolonic resection for Crohn disease, infliximab reduced endoscopic "
                            "recurrence, but the trial did not meet its primary clinical recurrence endpoint."
                        ),
                    },
                    {
                        "chunk_number": 3,
                        "topic": "Crohn disease",
                        "subtopic": "Postoperative recurrence",
                        "section": "Assessment before treatment change",
                        "reference": "Reviewed postoperative Crohn source. 2020.",
                        "original_text": (
                            "Assessment of suspected postoperative recurrence included objective confirmation "
                            "by endoscopy, recurrence severity, adherence, infliximab drug concentration, "
                            "anti-drug antibodies, prior biologic exposure, and alternative diagnoses."
                        ),
                    },
                    {
                        "chunk_number": 4,
                        "topic": "Crohn disease",
                        "subtopic": "Adalimumab",
                        "section": "Maintenance evidence",
                        "reference": "Adalimumab Crohn study. 2018.",
                        "original_text": (
                            "Adalimumab maintenance outcomes were reported in children aged 6 to 17 years with Crohn disease; "
                            "this source did not provide a head-to-head comparison with infliximab."
                        ),
                    },
                    {
                        "chunk_number": 8,
                        "topic": "Crohn disease",
                        "subtopic": "Vedolizumab after anti-TNF failure",
                        "section": "Exploratory week 10 outcomes",
                        "reference": "GEMINI 3 trial report. 2014.",
                        "original_text": (
                            "Week 10 remission and response outcomes favored vedolizumab in nominal analyses, but formal "
                            "ranked secondary testing was not performed after the week 6 primary endpoint was not met."
                        ),
                    },
                    {
                        "chunk_number": 5,
                        "topic": "Crohn disease",
                        "subtopic": "Vedolizumab after anti-TNF failure",
                        "section": "Week 6 induction endpoint",
                        "reference": "GEMINI 3 trial report. 2014.",
                        "original_text": (
                            "In patients with Crohn disease and prior anti-TNF failure, vedolizumab did not "
                            "achieve a statistically significant clinical remission difference at the week 6 "
                            "primary endpoint; later outcomes were exploratory."
                        ),
                    },
                    {
                        "chunk_number": 6,
                        "topic": "Inflammatory bowel disease",
                        "subtopic": "Corpus update",
                        "section": "Dated IBD findings",
                        "reference": "Reviewed IBD source. 2025.",
                        "original_text": "The reviewed source reported dated findings in inflammatory bowel disease.",
                    },
                    {
                        "chunk_number": 7,
                        "topic": "Ulcerative colitis",
                        "subtopic": "Fecal microbiota transplantation",
                        "section": "Unrelated UC evidence",
                        "reference": "UC FMT study. 2023.",
                        "original_text": "Fecal microbiota transplantation was studied in ulcerative colitis.",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return chunks_path


def _clinical_db(tmp_path):
    db_path = tmp_path / "clinical-assistant.sqlite3"
    ingest_chunks_file(_write_clinical_fixture(tmp_path), validation_status="PASS", db_path=db_path)
    return db_path


def test_required_interactions_1_and_2_greeting_and_plain_language(tmp_path):
    db_path = _clinical_db(tmp_path)

    greeting = build_db_rag_response("Hi, what can you help me with?", db_path=db_path)
    explanation = build_db_rag_response("Can you explain Crohn's disease in simple terms?", db_path=db_path)

    assert greeting["grounding_status"] == "CONVERSATIONAL_GREETING"
    assert greeting["response_type"] == "greeting"
    assert greeting["retrieved_chunks"] == []
    assert "###" not in greeting["answer"]
    assert not {"database", "chunk", "retrieval"} & set(greeting["answer"].lower().split())
    assert explanation["grounding_status"] == "ANSWERED_FROM_LOCAL_REVIEW_DB"
    assert explanation["response_type"] == "general_medical_explanation"
    assert explanation["presentation"] == {
        "version": 1,
        "format": "safe_markdown",
        "layout": "general_medical_explanation",
    }
    assert "### Takeaway" in explanation["answer"]
    assert explanation["retrieved_chunks"][0]["section"] == "Clinical description"
    assert explanation["citation_details"][0]["reference"] == "Reviewed clinical source. 2022."


def test_required_interactions_3_and_4_share_context_without_topic_drift(tmp_path):
    db_path = _clinical_db(tmp_path)
    question = "What does current evidence say about preventing postoperative recurrence in Crohn's disease?"
    evidence = build_db_rag_response(question, db_path=db_path)
    history = [
        {"role": "user", "content": question},
        {"role": "assistant", "content": evidence["answer"]},
    ]

    follow_up = build_db_rag_response(
        "Can you explain that in simpler terms and give me a clinical example?",
        db_path=db_path,
        history=history,
    )

    assert evidence["grounding_status"] == "ANSWERED_FROM_LOCAL_REVIEW_DB"
    assert "PREVENT trial findings and limitations" in {
        chunk["section"] for chunk in evidence["retrieved_chunks"]
    }
    assert follow_up["grounding_status"] == "ANSWERED_FROM_LOCAL_REVIEW_DB"
    assert follow_up["response_type"] == "follow_up"
    assert all(chunk["topic"] == "Crohn disease" for chunk in follow_up["retrieved_chunks"])
    assert not any("FMT" in chunk["reference"] for chunk in follow_up["retrieved_chunks"])


def test_required_interactions_5_and_6_comparison_and_reasoning(tmp_path):
    db_path = _clinical_db(tmp_path)

    comparison = build_db_rag_response(
        "How do infliximab and adalimumab compare in the management of Crohn's disease?",
        db_path=db_path,
    )
    reasoning = build_db_rag_response(
        "If a patient develops postoperative Crohn's recurrence despite infliximab, what factors should be assessed before changing treatment?",
        db_path=db_path,
    )

    comparison_subtopics = {chunk["subtopic"] for chunk in comparison["retrieved_chunks"]}
    assert comparison["response_type"] == "drug_comparison"
    assert "| Treatment | Population/context | Supported evidence |" in comparison["answer"]
    assert "| Infliximab |" in comparison["answer"]
    assert "| Adalimumab |" in comparison["answer"]
    assert comparison["evidence_scope"]["population_mismatch"] is True
    assert "pediatric" in comparison["answer"].lower() or "children" in comparison["answer"].lower()
    assert "head-to-head" in comparison["answer"].lower()
    assert {"Postoperative recurrence", "Adalimumab"} <= comparison_subtopics
    assert reasoning["response_type"] == "clinical_reasoning"
    assert "### Assessment considerations" in reasoning["answer"]
    assert "1." in reasoning["answer"]
    assert reasoning["retrieved_chunks"][0]["section"] == "Assessment before treatment change"
    assert reasoning["citation_details"][0]["number"] == 1


def test_required_interaction_7_last_30_days_is_explicitly_date_aware(tmp_path):
    response = build_db_rag_response(
        "What are the most important new findings in IBD research from the last 30 days?",
        db_path=_clinical_db(tmp_path),
    )

    assert response["evidence_scope"]["date_aware"] is True
    assert response["response_type"] == "recent_research"
    assert response["evidence_scope"]["requested_time_window"] == "last_30_days"
    assert response["evidence_scope"]["window_verified"] is False
    assert response["grounding_status"] == "RECENT_RESEARCH_UNAVAILABLE"
    assert response["retrieved_chunks"] == []
    assert response["citations"] == []
    assert "30-day" in response["answer"] or "30 days" in response["answer"]
    assert "live" not in response["answer"].lower() or "not" in response["answer"].lower()


def test_recent_window_accepts_only_exactly_dated_in_window_ibd_evidence(tmp_path):
    today = datetime.now(timezone.utc).date()
    current_date = (today - timedelta(days=2)).isoformat()
    old_date = (today - timedelta(days=90)).isoformat()
    output_dir = tmp_path / "dated-window"
    output_dir.mkdir()
    chunks_path = output_dir / "chunks.json"
    chunks_path.write_text(
        json.dumps(
            {
                "article": {"source_file": "dated-ibd.pdf"},
                "chunks": [
                    {
                        "chunk_number": 1,
                        "topic": "Inflammatory bowel disease",
                        "subtopic": "Recent IBD evidence",
                        "section": "Verified publication",
                        "reference": f"IBD Journal. {current_date};10:1-8.",
                        "original_text": "The dated source reported an inflammatory bowel disease study finding.",
                    },
                    {
                        "chunk_number": 2,
                        "topic": "Inflammatory bowel disease",
                        "subtopic": "Older IBD evidence",
                        "section": "Older publication",
                        "reference": f"IBD Journal. {old_date};9:1-8.",
                        "original_text": "The older source reported an inflammatory bowel disease study finding.",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "dated-window.sqlite3"
    ingest_chunks_file(chunks_path, validation_status="PASS", db_path=db_path)

    response = build_db_rag_response(
        "What are the new findings in IBD research from the last 30 days?",
        db_path=db_path,
    )

    assert response["grounding_status"] == "ANSWERED_FROM_LOCAL_REVIEW_DB"
    assert response["evidence_scope"]["window_verified"] is True
    assert [chunk["reference"] for chunk in response["retrieved_chunks"]] == [
        f"IBD Journal. {current_date};10:1-8."
    ]


def test_postoperative_reasoning_retains_pharmacokinetic_and_immunogenicity_evidence(tmp_path):
    output_dir = tmp_path / "postoperative-reasoning"
    output_dir.mkdir()
    chunks_path = output_dir / "chunks.json"
    chunks_path.write_text(
        json.dumps(
            {
                "article": {"source_file": "postoperative.pdf"},
                "chunks": [
                    {
                        "chunk_number": 1,
                        "topic": "Crohn disease",
                        "subtopic": "Postoperative recurrence",
                        "section": "Endoscopic recurrence",
                        "reference": "Postoperative trial. 2016.",
                        "original_text": "Postoperative Crohn recurrence was assessed by ileocolonoscopy.",
                    },
                    {
                        "chunk_number": 2,
                        "topic": "Crohn disease",
                        "subtopic": "Postoperative infliximab",
                        "section": "Pharmacokinetics and immunogenicity",
                        "reference": "Postoperative trial. 2016.",
                        "original_text": (
                            "Infliximab trough concentration and anti-drug antibodies were assessed in adults after "
                            "ileocolonic resection for Crohn disease."
                        ),
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "postoperative-reasoning.sqlite3"
    ingest_chunks_file(chunks_path, validation_status="PASS", db_path=db_path)

    response = build_db_rag_response(
        "What should be assessed when postoperative Crohn recurrence occurs despite infliximab?",
        db_path=db_path,
    )

    sections = {chunk["section"] for chunk in response["retrieved_chunks"]}
    assert "Endoscopic recurrence" in sections
    assert "Pharmacokinetics and immunogenicity" in sections


def test_required_interactions_8_to_10_interpretation_scope_and_claim_challenge(tmp_path):
    db_path = _clinical_db(tmp_path)

    interpretation = build_db_rag_response(
        "What do the available studies tell us about vedolizumab induction after anti-TNF failure in Crohn's disease?",
        db_path=db_path,
    )
    off_topic = build_db_rag_response(
        "Can you recommend a good smartphone for photography?",
        db_path=db_path,
    )
    unsupported_claim = build_db_rag_response(
        "A major randomized trial published yesterday proved vedolizumab cures Crohn's disease permanently. Can you summarize the results?",
        db_path=db_path,
    )

    assert interpretation["retrieved_chunks"][0]["section"] == "Week 6 induction endpoint"
    assert interpretation["response_type"] == "clinical_trial"
    assert "### Study evidence" in interpretation["answer"]
    assert "### Primary endpoint" in interpretation["answer"]
    assert "### Secondary or exploratory findings" in interpretation["answer"]
    sections = [chunk["section"] for chunk in interpretation["retrieved_chunks"]]
    assert sections.index("Week 6 induction endpoint") < sections.index("Exploratory week 10 outcomes")
    assert all("Vedolizumab" in chunk["subtopic"] for chunk in interpretation["retrieved_chunks"])
    assert off_topic["grounding_status"] == "OUT_OF_SCOPE"
    assert off_topic["response_type"] == "off_topic"
    assert not {"database", "chunk", "retrieval"} & set(off_topic["answer"].lower().split())
    assert unsupported_claim["grounding_status"] == "ANSWERED_FROM_LOCAL_REVIEW_DB"
    assert unsupported_claim["response_type"] == "unsupported_claim"
    assert "not verified" in unsupported_claim["answer"].lower()
    assert all(detail["reference"] != "" for detail in unsupported_claim["citation_details"])
    assert len(unsupported_claim["answer"].split()) <= 120
    assert all("Vedolizumab" in chunk["subtopic"] for chunk in unsupported_claim["retrieved_chunks"])


def test_prevent_trial_uses_trial_structure_without_empty_invented_fields(tmp_path):
    response = build_db_rag_response(
        "Summarize the PREVENT trial in postoperative Crohn disease.",
        db_path=_clinical_db(tmp_path),
    )

    assert response["response_type"] == "clinical_trial"
    assert "### Study evidence" in response["answer"]
    assert "### Interpretation" in response["answer"]
    assert "Participants: unknown" not in response["answer"]
    assert "Comparator: unknown" not in response["answer"]


def test_severe_population_only_sources_are_disclosed_as_incomplete_general_evidence(tmp_path):
    output_dir = tmp_path / "population-limited"
    output_dir.mkdir()
    chunks_path = output_dir / "chunks.json"
    chunks_path.write_text(
        json.dumps(
            {
                "article": {"source_file": "limited.pdf"},
                "chunks": [
                    {
                        "chunk_number": 1,
                        "topic": "Crohn disease",
                        "subtopic": "Pediatric severe disease",
                        "section": "Pediatric trial population",
                        "reference": "Pediatric Crohn trial. 2012.",
                        "original_text": "Children aged 6 to 17 years with moderate to severe Crohn disease entered a biologic trial.",
                    },
                    {
                        "chunk_number": 2,
                        "topic": "Crohn disease",
                        "subtopic": "Refractory disease",
                        "section": "Anti-TNF failure population",
                        "reference": "Refractory Crohn trial. 2014.",
                        "original_text": "Adults with moderately to severely active Crohn disease after anti-TNF failure were enrolled.",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "limited.sqlite3"
    ingest_chunks_file(chunks_path, validation_status="PASS", db_path=db_path)

    response = build_db_rag_response("Explain Crohn disease in simple terms.", db_path=db_path)

    assert response["evidence_scope"]["general_explanation_limited"] is True
    assert "selected trial populations" in response["answer"].lower()
    assert len(response["retrieved_chunks"]) == 1


def test_adaptive_prompt_keeps_structure_subordinate_to_source_safety():
    prompt = local_chunk_store._grounded_answer_prompt()
    guidance = local_chunk_store._response_structure_guidance("drug_comparison")

    assert "formatting instruction never permits" in prompt
    assert "Markdown table" in guidance
    assert "omit" in guidance.lower()


def test_invalid_model_citation_id_falls_back_to_verified_extractive_answer(monkeypatch, tmp_path):
    db_path = _clinical_db(tmp_path)

    class FakeProvider:
        def generate(self, article_text, source_file, prompt):
            return "Vedolizumab permanently cures Crohn disease [99]."

    monkeypatch.setattr(
        local_chunk_store.AzureChunkGenerator,
        "from_environment",
        staticmethod(lambda: FakeProvider()),
    )

    response = build_db_rag_response(
        "What does the vedolizumab study report in Crohn disease?",
        db_path=db_path,
        use_llm=True,
    )

    assert response["answer_mode"] == "extractive_synthesis"
    assert "[99]" not in response["answer"]
    cited_numbers = {int(value) for value in re.findall(r"\[(\d+)\]", response["answer"])}
    assert cited_numbers <= {detail["number"] for detail in response["citation_details"]}


def test_invalid_streamed_citation_is_never_released(monkeypatch, tmp_path):
    db_path = _clinical_db(tmp_path)

    class FakeProvider:
        def stream_generate(self, article_text, source_file, prompt):
            yield "Unsupported permanent cure "
            yield "claim [42]."

    monkeypatch.setattr(
        local_chunk_store.AzureChunkGenerator,
        "from_environment",
        staticmethod(lambda: FakeProvider()),
    )

    events = list(
        local_chunk_store.stream_db_rag_response(
            "What does the vedolizumab study report in Crohn disease?",
            db_path=db_path,
        )
    )
    released_text = "".join(event.get("text", "") for event in events if event["type"] == "delta")

    assert "[42]" not in released_text
    assert "permanent cure" not in released_text
    assert events[-1]["result"]["answer_mode"] == "extractive_synthesis_after_stream_error"


def test_context_is_request_scoped_and_does_not_leak_between_sessions(tmp_path):
    db_path = _clinical_db(tmp_path)
    prior_session = [
        {"role": "user", "content": "Explain postoperative recurrence in Crohn disease."},
        {"role": "assistant", "content": "Infliximab evidence was discussed [1]."},
    ]

    with_context = build_db_rag_response("Can you explain that more simply?", db_path=db_path, history=prior_session)
    new_session = build_db_rag_response("Can you explain that more simply?", db_path=db_path, history=[])

    assert with_context["grounding_status"] == "ANSWERED_FROM_LOCAL_REVIEW_DB"
    assert new_session["grounding_status"] == "INSUFFICIENT_REVIEWED_EVIDENCE"
    assert new_session["retrieved_chunks"] == []
