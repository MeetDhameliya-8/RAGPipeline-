import pytest

from src.chunk_validator import source_claim_issues, _build_source_evidence


def errors(claim, source):
    return [i for i in source_claim_issues(claim, source) if i["severity"] == "ERROR"]


@pytest.mark.parametrize("source, claim", [
    ("Treatment A enrolled w75% of adults.", "Treatment A enrolled 75% of adults."),
    ("Treatment A changed glucose by \u00fe0.20%.", "Treatment A changed glucose by 0.20%."),
    ("The annual rate was 57 cases per 100,000.1 Reported rates varied.", "The annual rate was 57 cases per 100000."),
])
def test_extraction_artifacts_require_review_instead_of_false_unsupported_error(source, claim):
    issues = source_claim_issues(claim, source)
    assert not [i for i in issues if i["severity"] == "ERROR"]
    assert any(i["rule"] == "EVIDENCE_QUALITY" and i["severity"] == "WARNING" for i in issues)


def test_number_present_in_another_endpoint_does_not_certify_claim():
    issues = source_claim_issues("Treatment A remission was 45% at week 8.",
                               "Treatment A remission was 20% at week 8. The hospitalization rate was 45%.")
    assert issues
    assert any(i["rule"] in {"NUMERIC_ASSOCIATION", "SOURCE_FIDELITY"} for i in issues)


def test_reversed_arms_cannot_pass_numeric_presence():
    issues = source_claim_issues("Treatment B remission was 45% versus 20% with Treatment A.",
                               "Treatment A remission was 45% versus 20% with Treatment B.")
    assert any(i["rule"] == "NUMERIC_ASSOCIATION" for i in issues)


@pytest.mark.parametrize("claim, source", [
    ("Treatment A changed the score by +5.", "Treatment A changed the score by -5."),
    ("Treatment A was administered at 5 mg daily.", "Treatment A was administered at 5 micrograms daily."),
    ("Eligibility required a count >50.", "Eligibility required a count <50."),
])
def test_explicit_sign_unit_or_operator_conflicts_fail(claim, source):
    assert any(i["rule"] == "NUMERIC_RELATION" for i in errors(claim, source))


def test_truly_absent_number_fails():
    assert any(i["rule"] == "SOURCE_FIDELITY" for i in errors(
        "Treatment A remission was 99.9%.", "Treatment A remission was 15.2%."))


@pytest.mark.parametrize("claim, source", [
    ("Treatment A improved the primary outcome.", "Treatment A improved the primary outcome. Other outcomes did not differ."),
    ("Although the primary endpoint was not met, Treatment A improved secondary symptoms.",
     "The primary endpoint was not met. Treatment A improved secondary symptoms."),
    ("Treatment A reduced bleeding.", "Treatment A reduced bleeding. Mortality was not significantly different."),
    ("Treatment A showed a nonsignificant trend toward lower exposure.", "Treatment A exposure was not significantly different."),
    ("Treatment A showed a numerical benefit without significant difference.", "Treatment A showed no significant difference."),
])
def test_other_endpoint_or_negated_scope_is_not_false_contradiction(claim, source):
    assert not [i for i in errors(claim, source) if i["rule"] == "SOURCE_GROUNDING"]


def test_true_same_endpoint_contradiction_still_fails():
    assert any(i["rule"] == "SOURCE_GROUNDING" for i in errors(
        "Treatment A significantly improved clinical remission.",
        "Clinical remission did not differ with Treatment A."))


def test_unresolved_paraphrase_association_cannot_pass_silently():
    assert source_claim_issues("Treatment B improved clinical remission at week 6.",
                              "Treatment A improved clinical remission at week 6.")


def test_late_unsupported_recommendation_is_not_skipped_after_early_warning():
    issues = source_claim_issues("A different intervention improved several unrecorded symptoms. Clinicians must prescribe a new therapy.",
                                "The study assessed clinical outcomes without a recommendation.")
    assert any(i["severity"] == "ERROR" for i in issues)


def test_evidence_offsets_name_source_buffer():
    text = "Treatment A remission was 45% after follow-up."
    evidence = _build_source_evidence([{"original_text": text}], text, [], [])[0]
    assert evidence["text_span"]["buffer_id"].startswith("full_text:sha256:")


def test_leading_decimal_is_not_a_different_numeric_value():
    assert not [i for i in errors("Treatment A had a P value of 0.052.",
                                "Treatment A had a P value of .052.") if i["rule"] == "SOURCE_FIDELITY"]


def test_different_named_arm_negative_result_is_not_a_contradiction():
    assert not [i for i in errors("Treatment A significantly improved remission.",
                                "Treatment B remission did not differ.") if i["rule"] == "SOURCE_GROUNDING"]


def test_missing_source_evidence_requires_review():
    assert source_claim_issues("Treatment A improved remission.", "")


def test_different_quantity_context_is_not_proven_unit_contradiction():
    issues = source_claim_issues("Treatment A included adults weighing 40 kg.",
                                "Treatment A dose was 40 mg in adults.")
    assert issues
    assert not [i for i in issues if i["severity"] == "ERROR" and i["rule"] == "NUMERIC_RELATION"]


def test_recommendation_negation_cannot_be_dropped():
    assert any(i["severity"] == "ERROR" for i in source_claim_issues(
        "Clinicians should prescribe Treatment A for this condition.",
        "Clinicians should not prescribe Treatment A for this condition."))


def test_paraphrased_recommendation_requires_association_review():
    assert source_claim_issues("Clinicians should use Treatment A in these adults.",
                              "Clinicians should use Treatment B in these adults.")
