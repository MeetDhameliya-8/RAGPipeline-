import pytest

from src.pdf_extractor import extract_reference_metadata, extract_document_reference_metadata, _select_reference_metadata
from src.chunk_generator import ChunkGenerationError, _validate_generated_reference_against_source, bind_verified_references


def test_doi_does_not_absorb_next_line_page_number():
    metadata = extract_reference_metadata("doi:10.1234/study.2020.014\n205\nJournal 2020;12:200-210")
    assert metadata["doi"] == "10.1234/study.2020.014"


def test_doi_does_not_absorb_same_line_layout_padding_page_number():
    metadata = extract_reference_metadata("doi:10.1234/study.2020.014    205")
    assert metadata["doi"] == "10.1234/study.2020.014"


def test_incomplete_doi_prefix_not_recognized_as_identifier():
    assert extract_reference_metadata("doi:10.1234/   Clinical results")['doi'] == ''


def test_source_identity_has_raw_page_spans():
    text = "Article title\nJournal 2020;12:200-210\ndoi:10.1234/study014"
    metadata = extract_document_reference_metadata([{"page_number": 1, "raw_text": text}])
    assert metadata["identity_status"] == "source_supported"
    for item in metadata["source_spans"]:
        assert item["buffer_id"] == "page:1:raw_text"
        assert text[item["char_start"]:item["char_end"]] == item["text"]


def test_competing_front_matter_dois_are_unresolved():
    metadata = extract_document_reference_metadata([{"raw_text": "Article\ndoi:10.1234/first01\nPrior study doi:10.1234/other02"}])
    assert metadata["identity_status"] == "conflict"
    assert metadata["reference"] == ""
    assert len(metadata["citation_candidates"]) == 2


def test_cited_inline_identifier_is_not_focal_identity():
    metadata = extract_document_reference_metadata([{"raw_text": "Article title\nEarlier research reported this (doi:10.1234/cited01)."}])
    assert metadata["identity_status"] == "unresolved"
    assert metadata["reference"] == ""


def test_labeled_doi_in_separate_layout_column_preserves_identity():
    raw = "Article title\nJournal 2020;12:200-210\nArticle body continues here.      https://doi.org/10.1234/source01\n"
    metadata = extract_document_reference_metadata([{"page_number": 1, "raw_text": raw}])
    assert metadata["doi"] == "10.1234/source01"
    assert metadata["identity_status"] == "source_supported"


def test_extractor_conflict_blocks_reference_binding():
    warnings = []
    metadata = _select_reference_metadata([
        {"reference": "doi:10.1234/first01", "doi": "10.1234/first01"},
        {"reference": "doi:10.1234/other02", "doi": "10.1234/other02"}], warnings)
    with pytest.raises(ChunkGenerationError):
        bind_verified_references({"article": {"reference": ""}, "chunks": [{"reference": ""}]},
                                 {"full_text": "doi:10.1234/first01", "reference_metadata": metadata})


def test_metadata_cannot_validate_itself_without_source_evidence():
    with pytest.raises(ChunkGenerationError):
        _validate_generated_reference_against_source(None, "doi:10.1234/invented01", "Unrelated source text.",
            {"reference": "doi:10.1234/invented01", "doi": "10.1234/invented01"})


@pytest.mark.parametrize("reference", [
    "Wrong Journal. 2099;99:1-8. doi:10.1234/source01",
    "doi:10.1234/source01. Invented article title",
    "doi:10.1234/cited02",
])
def test_doi_presence_does_not_validate_other_components_or_ownership(reference):
    with pytest.raises(ChunkGenerationError):
        _validate_generated_reference_against_source(None, reference,
            "Journal 2020;12:200-210. doi:10.1234/source01. Cited paper doi:10.1234/cited02",
            {"reference": "Journal 2020;12:200-210. doi:10.1234/source01", "doi": "10.1234/source01",
             "citation": "Journal 2020;12:200-210"})


def test_legacy_metadata_does_not_disambiguate_multiple_source_dois():
    with pytest.raises(ChunkGenerationError):
        _validate_generated_reference_against_source(None, "doi:10.1234/cited02",
            "doi:10.1234/source01. A cited paper doi:10.1234/cited02",
            {"reference": "doi:10.1234/cited02"})


def test_doi_only_identity_does_not_own_an_unassociated_bibliography():
    with pytest.raises(ChunkGenerationError):
        _validate_generated_reference_against_source(None, "Other Journal 2020;12:200-210. doi:10.1234/source01",
            "doi:10.1234/source01. A cited paper: Other Journal 2020;12:200-210.",
            {"reference": "doi:10.1234/source01"})
