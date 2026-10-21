import json
import sys
import types
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch

import pytest

from src.adaptive_ingestion import AdaptiveConfig, assess_page_text, build_ingestion_plan
from src.chunk_generator import generate_chunks
from src.providers.base import ChunkGenerationError
from src.pdf_extractor import extract_document_reference_metadata, _should_use_pymupdf_page, _extract_page_text, _extract_pymupdf_pages


def test_plan_preserves_every_character_and_bounds_contexts():
    text = ("A complete source sentence. " * 20 + "\n\n") * 15
    plan = build_ingestion_plan({"full_text": text}, AdaptiveConfig(max_batch_chars=1000))
    assert not plan.blockers
    assert len(plan.batches) > 1
    assert ''.join(b.text for b in plan.batches) == text
    for previous, current in zip(plan.batches, plan.batches[1:]):
        assert previous.char_end == current.char_start
    assert all(len(b.text) <= 1000 for b in plan.batches)


def test_long_prose_uses_sentence_boundaries():
    text = "A complete sentence about a study. " * 200
    plan = build_ingestion_plan({"full_text": text}, AdaptiveConfig(max_batch_chars=1000))
    assert not plan.blockers
    assert ''.join(b.text for b in plan.batches) == text
    assert all(b.text.rstrip().endswith('.') for b in plan.batches)


def test_oversized_table_is_never_cut_into_cells():
    text = 'Table 1. Results\n' + 'Group 12 34 56 78\n' * 300
    plan = build_ingestion_plan({"full_text": text}, AdaptiveConfig(max_batch_chars=1000))
    assert plan.blockers
    assert ''.join(b.text for b in plan.batches) == text


def test_unsplittable_text_blocks_before_provider_call():
    provider = Mock()
    with pytest.raises(ChunkGenerationError, match='ADAPTIVE_INGESTION_BLOCKED'):
        generate_chunks({'full_text': 'x' * 25000}, provider=provider)
    provider.generate.assert_not_called()


def test_route_uses_content_instead_of_filename():
    plan = build_ingestion_plan({'file_name': 'random.pdf', 'full_text': 'A systematic review of evidence.'})
    assert plan.document_type == 'systematic_review'
    guideline = build_ingestion_plan({'full_text': 'Clinical practice guidelines. Evidence includes a meta-analysis.'})
    assert guideline.document_type == 'guideline_or_consensus'


def test_sparse_visual_page_needs_ocr_or_review():
    assert assess_page_text('small footer', 7, True)
    assert assess_page_text('small' + ' ' * 2000 + 'footer', 7, True)
    assert assess_page_text('', 7, False) == []


def test_encoding_damage_requires_review():
    assert assess_page_text('Broken \ufffd\ufffd\ufffd\ufffd text', 2)
    assert assess_page_text('Values /C01 /C01 /C01 /C01', 2)


def test_batch_count_limit_does_not_truncate_source():
    text = ('A complete source sentence. ' * 20 + '\n\n') * 15
    plan = build_ingestion_plan({'full_text': text}, AdaptiveConfig(max_batch_chars=1000, max_batches=1))
    assert any('requests exceeds' in b for b in plan.blockers)
    assert ''.join(b.text for b in plan.batches) == text


def response(number):
    return json.dumps({'article': {'source_file':'paper.pdf', 'topic':'Study', 'subtopic':'Population',
        'reference':'doi:10.1000/example123','number_of_chunks':1}, 'chunks':[
        {'topic':'Study','subtopic':'Population','section':f'Finding {number}',
         'reference':'doi:10.1000/example123', 'original_text':f'The source describes finding {number}.'}]})


def extraction():
    return {'file_name':'paper.pdf', 'full_text': 'doi:10.1000/example123\n\n' + ('A complete source sentence. '*250+'\n\n')*8,
            'reference_metadata': {'reference':'doi:10.1000/example123','doi':'10.1000/example123'}}


def test_generation_processes_every_context_and_keeps_schema():
    source = extraction()
    plan = build_ingestion_plan(source)
    provider = Mock()
    provider.generate.side_effect = [response(i) for i in range(len(plan.batches))]
    result = generate_chunks(source, provider=provider)
    assert provider.generate.call_count == len(plan.batches)
    assert len(result['chunks']) == len(plan.batches)
    assert result['article']['number_of_chunks'] == len(result['chunks'])
    assert [c['chunk_number'] for c in result['chunks']] == list(range(1,len(result['chunks'])+1))
    assert all(set(c)=={'chunk_number','topic','subtopic','section','reference','original_text'} for c in result['chunks'])
    for batch, call in zip(plan.batches, provider.generate.call_args_list):
        assert call.args[0].endswith(batch.text.strip())


def test_failed_context_does_not_return_partial_success():
    provider = Mock()
    provider.generate.side_effect = [response(1), ChunkGenerationError('provider failed')]
    with pytest.raises(ChunkGenerationError, match='no complete package'):
        generate_chunks(extraction(), provider=provider)


def test_repeated_exact_chunks_are_deduplicated():
    provider = Mock()
    provider.generate.return_value = response(1)
    result = generate_chunks(extraction(), provider=provider)
    assert len(result['chunks']) == 1


def test_page_routes_block_generation_before_network():
    provider = Mock()
    with pytest.raises(ChunkGenerationError, match='Page 7'):
        generate_chunks(dict(extraction(), adaptive_blockers=['Page 7 requires OCR']), provider=provider)
    provider.generate.assert_not_called()


def test_reference_list_doi_is_not_assigned_to_source_article():
    pages = [{'raw_text': 'Article title\n\nBody without DOI.'},
             {'raw_text': 'More body.'},
             {'raw_text': 'References\n\nCited paper doi:10.1000/cited123'}]
    assert extract_document_reference_metadata(pages)['doi'] == ''


def test_front_matter_doi_remains_available():
    pages = [{'raw_text': 'Article title\ndoi:10.1000/source123'},
             {'raw_text': '\nReferences\n\nCited doi:10.1000/cited123'}]
    assert extract_document_reference_metadata(pages)['doi'] == '10.1000/source123'


def test_whitespace_explosion_does_not_win_extractor_selection():
    content = 'A complete scientific source sentence. ' * 100
    inflated = content + ' ' * 150000
    assert _should_use_pymupdf_page(inflated, content)


def test_layout_failure_and_pathological_spaces_use_plain_extraction():
    page = Mock()
    page.extract_text.side_effect = [ValueError('layout unavailable'), 'Verified plain text.']
    assert _extract_page_text(page) == 'Verified plain text.'
    page.extract_text.side_effect = ['Evidence' + ' ' * 300000, 'Evidence']
    assert _extract_page_text(page) == 'Evidence'


def test_visual_route_ignores_layout_padding_in_pymupdf_text():
    page = Mock()
    page.get_text.return_value = 'footer' + ' ' * 2000 + 'page'
    page.get_images.return_value = [object()]
    document = Mock()
    document.__iter__ = Mock(return_value=iter([page]))
    with patch.dict(sys.modules, {'fitz': types.SimpleNamespace(open=lambda _: document)}):
        pages = _extract_pymupdf_pages(Path('example.pdf'), [])
    assert pages[0]['has_visual_content'] is True
    document.close.assert_called_once()
