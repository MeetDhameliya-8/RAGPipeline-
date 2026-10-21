"""Raw provider responses must not be repaired by coercing clinical content."""
import copy
import json
from unittest.mock import Mock

import pytest

from src.chunk_generator import ChunkGenerationError, generate_chunks, normalize_generation_result
from src.chunk_generator import parse_model_json


def document():
    return {
        "article": {"source_file": "paper.pdf", "topic": "Condition", "subtopic": "Adults",
                    "reference": "doi:10.1000/source123", "number_of_chunks": 1},
        "chunks": [{"chunk_number": 1, "topic": "Condition", "subtopic": "Adults",
                    "section": "Treatment outcome", "reference": "doi:10.1000/source123",
                    "original_text": "The source describes the treatment outcome in adults."}],
    }


def generate(data):
    provider = Mock()
    provider.generate.return_value = json.dumps(data)
    return generate_chunks({"file_name": "paper.pdf", "full_text": "doi:10.1000/source123"}, provider=provider)


@pytest.mark.parametrize("value", [None, 12, True, ["text"], {"text": "value"}])
@pytest.mark.parametrize("field", ["topic", "subtopic", "section", "reference", "original_text"])
def test_raw_content_types_reject_before_normalization(field, value):
    data = document()
    data["chunks"][0][field] = value
    with pytest.raises(ChunkGenerationError, match="schema"):
        generate(data)


@pytest.mark.parametrize("value", [None, [], "article", 12])
def test_malformed_article_yields_typed_error(value):
    data = document()
    data["article"] = value
    with pytest.raises(ChunkGenerationError):
        generate(data)


@pytest.mark.parametrize("value", [None, True, 0, -1, "1", 1.2, {}, []])
def test_explicit_invalid_chunk_number_is_not_inferred(value):
    data = document()
    data["chunks"][0]["chunk_number"] = value
    with pytest.raises(ChunkGenerationError):
        generate(data)


@pytest.mark.parametrize("value", [9, "1", True, None])
def test_declared_count_cannot_be_overwritten(value):
    data = document()
    data["article"]["number_of_chunks"] = value
    with pytest.raises(ChunkGenerationError):
        generate(data)


def test_unexpected_envelope_is_rejected():
    data = document()
    data["explanation"] = "Do not silently discard this field."
    with pytest.raises(ChunkGenerationError):
        generate(data)


def test_alias_collisions_are_rejected():
    data = document()
    data["chunks"][0]["Topic"] = "Another condition"
    with pytest.raises(ChunkGenerationError):
        generate(data)


def test_duplicate_chunk_numbers_are_rejected():
    data = document()
    data["chunks"].append(copy.deepcopy(data["chunks"][0]))
    data["article"]["number_of_chunks"] = 2
    with pytest.raises(ChunkGenerationError):
        generate(data)


def test_aliases_and_absent_operational_number_preserve_content():
    data = document()
    data["chunks"][0].pop("chunk_number")
    data["chunks"][0] = {k.replace("_", " ").title(): v for k, v in data["chunks"][0].items()}
    normalized = normalize_generation_result(data)
    assert normalized["chunks"][0]["chunk_number"] == 1
    assert normalized["chunks"][0]["original_text"] == document()["chunks"][0]["original_text"]


def test_missing_nonreference_content_is_rejected():
    data = document()
    data["chunks"][0].pop("original_text")
    with pytest.raises(ChunkGenerationError, match="original_text"):
        normalize_generation_result(data)


def test_duplicate_json_keys_are_not_silently_overwritten():
    with pytest.raises(ChunkGenerationError):
        parse_model_json('{"article": {}, "article": {}, "chunks": []}')
