import unittest

from src.chunk_validator import validate_chunks


def _chunk(**overrides: str) -> dict[str, str | int]:
    text = (
        "Ulcerative colitis treatment was evaluated in adults with active disease. "
        "The study reported clinical remission in 45% of patients after 8 weeks, "
        "and the source text describes the patient population, intervention, "
        "outcomes, and safety considerations in clinician-facing language. "
        "The chunk remains independent by naming ulcerative colitis and the "
        "treatment context directly. It preserves uncertainty and does not "
        "add unverified recommendations beyond the cited source passage. "
    )
    base: dict[str, str | int] = {
        "chunk_number": 1,
        "topic": "Ulcerative Colitis",
        "subtopic": "Adults With Active Disease",
        "section": "Does Treatment Improve Clinical Remission?",
        "reference": "doi:10.1000/example",
        "original_text": text * 3,
    }
    base.update(overrides)
    return base


def _doc(*chunks: dict[str, str | int]) -> dict[str, object]:
    return {
        "article": {
            "source_file": "paper.pdf",
            "topic": "Ulcerative Colitis",
            "subtopic": "Adults With Active Disease",
            "reference": "doi:10.1000/example",
            "number_of_chunks": len(chunks),
        },
        "chunks": list(chunks),
    }


class ValidatorTests(unittest.TestCase):
    def test_valid_chunk_passes_schema_and_rules(self) -> None:
        document = _doc(_chunk())
        report = validate_chunks(document, source_text=document["chunks"][0]["original_text"])
        self.assertEqual(report["status"], "PASS")

    def test_missing_required_field_is_error(self) -> None:
        report = validate_chunks(_doc(_chunk(reference="")))
        self.assertEqual(report["status"], "FAILED")
        self.assertTrue(
            any(issue["rule"] == "REQUIRED_FIELD" for issue in report["issues"])
        )

    def test_word_count_under_target_is_warning(self) -> None:
        report = validate_chunks(_doc(_chunk(original_text="Too short for clinical review.")))
        self.assertEqual(report["status"], "FAILED")
        self.assertTrue(any(issue["rule"] == "WORD_COUNT" for issue in report["issues"]))

    def test_word_count_outside_typical_target_requires_review(self) -> None:
        short_but_reviewable = "Ulcerative colitis source-supported clinical content. " * 20

        report = validate_chunks(_doc(_chunk(original_text=short_but_reviewable)))

        self.assertEqual(report["status"], "REVIEW_REQUIRED")
        self.assertTrue(
            any(
                issue["rule"] == "WORD_COUNT" and issue["severity"] == "WARNING"
                for issue in report["issues"]
            )
        )


    def test_invalid_field_type_fails_technical_schema(self) -> None:
        document = _doc(_chunk())
        document["chunks"][0]["topic"] = ["Ulcerative Colitis"]

        report = validate_chunks(document)

        self.assertEqual(report["status"], "FAILED")
        self.assertTrue(any(issue["rule"] == "SCHEMA" for issue in report["issues"]))

    def test_empty_chunk_list_fails_output_completeness(self) -> None:
        report = validate_chunks(_doc())

        self.assertEqual(report["status"], "FAILED")
        self.assertTrue(
            any(issue["rule"] == "OUTPUT_COMPLETENESS" for issue in report["issues"])
        )

    def test_article_chunk_count_mismatch_fails_output_completeness(self) -> None:
        document = _doc(_chunk())
        document["article"]["number_of_chunks"] = 3

        report = validate_chunks(document)

        self.assertEqual(report["status"], "FAILED")
        self.assertTrue(
            any(issue["rule"] == "OUTPUT_COMPLETENESS" for issue in report["issues"])
        )

    def test_placeholder_reference_is_invalid(self) -> None:
        report = validate_chunks(_doc(_chunk(reference="N/A")))

        self.assertEqual(report["status"], "FAILED")
        self.assertTrue(
            any(issue["rule"] == "INVALID_REFERENCE" for issue in report["issues"])
        )


    def test_source_grounding_records_exact_page_provenance(self) -> None:
        chunk = _chunk()
        document = _doc(chunk)
        source_text = str(chunk["original_text"])

        report = validate_chunks(
            document,
            source_text=source_text,
            source_pages=[{"page_number": 7, "text": source_text}],
        )

        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["source_evidence"][0]["match_type"], "exact")
        self.assertEqual(report["source_evidence"][0]["page_numbers"], [7])
        self.assertIsNotNone(report["source_evidence"][0]["text_span"])


    def test_source_grounding_records_section_and_document_provenance(self) -> None:
        chunk = _chunk()
        document = _doc(chunk)
        source_text = str(chunk["original_text"])

        report = validate_chunks(
            document,
            source_text=source_text,
            source_pages=[{"page_number": 3, "text": source_text}],
            source_sections=[
                {
                    "document_id": "paper-abc123",
                    "source_file": "paper.pdf",
                    "section_id": "paper-abc123:s002",
                    "title": "Clinical Outcomes",
                    "kind": "body",
                    "page_start": 3,
                    "page_end": 4,
                    "text": source_text,
                }
            ],
        )

        evidence = report["source_evidence"][0]
        self.assertEqual(evidence["source_document_id"], "paper-abc123")
        self.assertEqual(evidence["source_file"], "paper.pdf")
        self.assertEqual(evidence["source_section_ids"], ["paper-abc123:s002"])
        self.assertEqual(evidence["source_sections"][0]["title"], "Clinical Outcomes")
        self.assertEqual(evidence["page_range"], [3, 3])

    def test_source_grounding_flags_untraceable_recommendation(self) -> None:
        recommendation_text = (
            "Adults with ulcerative colitis should receive investigational maintenance therapy "
            "after induction response. The chunk describes the disease, treatment context, "
            "patient population, safety review, and follow-up considerations in independent "
            "clinician-facing language without relying on another section. "
        ) * 3
        source_text = (
            "Adults with ulcerative colitis were enrolled in an observational treatment study. "
            "The source describes follow-up and safety review but does not make a treatment recommendation. "
        ) * 3

        report = validate_chunks(_doc(_chunk(original_text=recommendation_text)), source_text=source_text)

        self.assertEqual(report["status"], "FAILED")
        self.assertTrue(
            any(issue["rule"] == "SOURCE_GROUNDING" for issue in report["issues"])
        )

    def test_source_grounding_treats_evidence_suggested_as_claim_not_recommendation(self) -> None:
        background_text = (
            "Evidence cited in the article suggested that intestinal microbiota may contribute "
            "to irritable bowel syndrome pathophysiology and treatment rationale. The chunk "
            "describes the disease, treatment context, patient population, safety review, "
            "and follow-up considerations in independent clinician-facing language. "
        ) * 3
        source_text = (
            "Evidence suggests that gut flora may play an important role in the pathophysiology "
            "of irritable bowel syndrome. The source describes treatment rationale and study follow-up. "
        ) * 3

        report = validate_chunks(_doc(_chunk(original_text=background_text)), source_text=source_text)

        self.assertFalse(
            any(
                issue["rule"] == "SOURCE_GROUNDING" and issue["severity"] == "ERROR"
                for issue in report["issues"]
            )
        )

    def test_source_cleaning_allows_clinical_division_of_anatomic_ligament(self) -> None:
        procedure_text = (
            "Laparoscopic Heller myotomy included division of the phrenoesophageal ligament "
            "and mobilization of the distal esophagus before myotomy. The chunk describes "
            "the disease, treatment context, patient population, safety review, and follow-up "
            "considerations in independent clinician-facing language. "
        ) * 3

        report = validate_chunks(_doc(_chunk(original_text=procedure_text)), source_text=procedure_text)

        self.assertFalse(
            any(issue["rule"] == "SOURCE_CLEANING" for issue in report["issues"])
        )

    def test_source_grounding_flags_source_contradiction(self) -> None:
        contradiction_text = (
            "Adults with ulcerative colitis had a significant improvement in clinical remission "
            "with treatment compared with placebo. The chunk describes the disease, treatment "
            "context, patient population, safety review, and follow-up considerations in "
            "independent clinician-facing language. "
        ) * 3
        source_text = (
            "Adults with ulcerative colitis were treated in a placebo-controlled study. "
            "Clinical remission was not statistically significant and did not differ between groups. "
        ) * 3

        report = validate_chunks(_doc(_chunk(original_text=contradiction_text)), source_text=source_text)

        self.assertEqual(report["status"], "FAILED")
        self.assertTrue(
            any(
                issue["rule"] == "SOURCE_GROUNDING" and issue["severity"] == "ERROR"
                for issue in report["issues"]
            )
        )

    def test_source_grounding_does_not_use_unrelated_negative_result_as_contradiction(self) -> None:
        supported_positive_text = (
            "Adults with Clostridium difficile infection had significantly fewer recurrences with fidaxomicin compared with vancomycin. "
            "The chunk describes the disease, treatment context, patient population, safety review, and follow-up considerations in independent clinician-facing language. "
        ) * 3
        source_text = (
            "Adults with Clostridium difficile infection had significantly fewer recurrences with fidaxomicin compared with vancomycin. "
            "Median time to resolution of diarrhea was not statistically significant between treatment groups. "
        ) * 3

        report = validate_chunks(_doc(_chunk(original_text=supported_positive_text)), source_text=source_text)

        self.assertFalse(
            any(issue.get("category") == "CLINICAL_CONTRADICTION" for issue in report["issues"])
        )

    def test_source_evidence_records_fuzzy_sentence_overlap_for_supported_paraphrase(self) -> None:
        paraphrase = (
            "Adults with active ulcerative colitis were evaluated for clinical remission after 8 weeks of treatment. "
            "The chunk describes the patient population, intervention, outcomes, and safety considerations in independent clinician-facing language. "
        ) * 3
        source_text = (
            "The trial evaluated adults with active ulcerative colitis and reported clinical remission in 45% of patients after 8 weeks of treatment. "
            "Safety considerations and the treatment context were described for clinical review. "
        ) * 3

        report = validate_chunks(
            _doc(_chunk(original_text=paraphrase)),
            source_text=source_text,
            source_pages=[{"page_number": 4, "text": source_text}],
            source_sections=[
                {
                    "document_id": "paper-source",
                    "source_file": "paper.pdf",
                    "section_id": "paper-source:s001",
                    "title": "Results",
                    "kind": "body",
                    "page_start": 4,
                    "page_end": 4,
                    "text": source_text,
                }
            ],
        )

        self.assertEqual(report["source_evidence"][0]["match_type"], "sentence_overlap")
        self.assertEqual(report["source_evidence"][0]["page_numbers"], [4])
        self.assertEqual(report["source_evidence"][0]["source_section_ids"], ["paper-source:s001"])
    def test_source_grounding_flags_missing_source_qualification(self) -> None:
        strong_text = (
            "Adults with ulcerative colitis demonstrated effective maintenance response with "
            "treatment in a clinician-facing study summary. The chunk describes the disease, "
            "treatment context, patient population, safety review, and follow-up considerations "
            "without relying on another section. "
        ) * 3
        source_text = strong_text + " The source states that evidence was exploratory and limitations included small sample size."

        report = validate_chunks(_doc(_chunk(original_text=strong_text)), source_text=source_text)

        self.assertEqual(report["status"], "REVIEW_REQUIRED")
        self.assertTrue(
            any(
                issue["rule"] == "SOURCE_GROUNDING" and issue["severity"] == "WARNING"
                for issue in report["issues"]
            )
        )

    def test_inconsistent_topic_and_subtopic_are_warnings(self) -> None:
        first = _chunk(chunk_number=1)
        second = _chunk(
            chunk_number=2,
            topic="Crohn Disease",
            subtopic="Postoperative Recurrence",
            section="Does Monitoring Identify Recurrence?",
        )
        report = validate_chunks(_doc(first, second))
        rules = [issue["rule"] for issue in report["issues"]]
        self.assertIn("CONSISTENCY", rules)

    def test_duplicate_chunks_are_detected(self) -> None:
        first = _chunk(chunk_number=1)
        second = _chunk(chunk_number=2)
        report = validate_chunks(_doc(first, second))
        self.assertEqual(report["status"], "FAILED")
        self.assertTrue(any(issue["rule"] == "DUPLICATE" for issue in report["issues"]))

    def test_vague_section_is_warning(self) -> None:
        report = validate_chunks(_doc(_chunk(section="Results")))
        self.assertEqual(report["status"], "FAILED")
        self.assertTrue(
            any(issue["rule"] == "SECTION_QUALITY" for issue in report["issues"])
        )

    def test_numbers_are_checked_against_source_text(self) -> None:
        report = validate_chunks(_doc(_chunk()), source_text="The study reported 45%.")
        self.assertTrue(
            any(issue["rule"] == "SOURCE_FIDELITY" for issue in report["issues"])
        )

    def test_generic_article_labels_are_errors(self) -> None:
        report = validate_chunks(
            _doc(_chunk(topic="ORIGINAL ARTICLE", subtopic="ORIGINAL ARTICLE"))
        )
        self.assertEqual(report["status"], "FAILED")
        self.assertTrue(
            any(issue["rule"] == "CLASSIFICATION" for issue in report["issues"])
        )

    def test_reference_list_leakage_is_error(self) -> None:
        reference_text = (
            "Collagenous colitis maintenance therapy was evaluated in adults with "
            "clinical remission. The study text describes intervention dosing, "
            "outcomes, and safety in a way that should remain independent for review. "
            "Hjortswang H, Tysk C, Bohr J, et al. Health-related quality of life "
            "is impaired in collagenous colitis. Miehlke S, Madisch A, et al. "
            "Mattson ME, Furberg CF, Elinson J. eds. Assessment of quality of life "
            "in clinical trials. "
        ) * 2
        report = validate_chunks(_doc(_chunk(original_text=reference_text)))
        self.assertEqual(report["status"], "FAILED")
        self.assertTrue(
            any(issue["rule"] == "REFERENCE_LEAKAGE" for issue in report["issues"])
        )





