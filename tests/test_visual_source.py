"""Visual transcription remains source-review evidence, never medical approval."""
import json
from pathlib import Path
from unittest.mock import Mock
import pytest
import fitz

from src.pdf_extractor import extract_pdf,PdfExtractionError
from src.source_map import build_source_map
from src.source_packages import build_source_package_plan,validate_source_package_plan
from src.visual_source import recover_visual_source
from test_source_packages import TEXT


def scan(tmp_path):
    value=TEXT.replace('8 weeks','the protocol treatment period')
    d=fitz.open();p=d.new_page(width=600,height=800)
    p.insert_textbox(fitz.Rect(40,50,550,650),value,fontsize=10)
    png=p.get_pixmap().tobytes('png');d.close()
    d=fitz.open();p=d.new_page(width=600,height=800);p.insert_image(p.rect,stream=png)
    path=tmp_path/'scan.pdf';d.save(path);d.close()
    with pytest.raises(PdfExtractionError) as exc:extract_pdf(path)
    return path,exc.value.extraction,value


def test_two_pass_visual_source_is_additive_cached_and_recomputable(tmp_path):
    path,e,text=scan(tmp_path);before=e['evidence']
    provider=Mock();provider.transcribe_image.return_value=json.dumps({'paragraphs':[text],'uncertainties':[]})
    recovered=recover_visual_source(path,e,provider=provider,cache_directory=tmp_path/'cache')
    assert provider.transcribe_image.call_count==2
    assert recovered['full_text']==e['full_text']=='' and e['evidence']==before
    mapped=build_source_map(recovered);p=build_source_package_plan(recovered,mapped)
    assert len(p.packages)==1 and p.packages[0].visual_source_ids
    assert p.packages[0].witnesses==[] and not p.document_complete
    validate_source_package_plan(p,mapped.evidence)
    provider.transcribe_image.reset_mock()
    recover_visual_source(path,e,provider=provider,cache_directory=tmp_path/'cache')
    provider.transcribe_image.assert_not_called()
    image_path=next((tmp_path/'cache').glob('*.png'))
    image_path.write_bytes(b'corrupted source rendering')
    with pytest.raises(ValueError,match='rendering integrity'):
        recover_visual_source(path,e,provider=provider,cache_directory=tmp_path/'cache')
    provider.transcribe_image.assert_not_called()
    p.visual_sources[0].image_sha256='a'*64
    with pytest.raises(ValueError):validate_source_package_plan(p,mapped.evidence)


def test_disagreeing_visual_transcriptions_do_not_admit_a_paragraph(tmp_path):
    path,e,text=scan(tmp_path);provider=Mock()
    provider.transcribe_image.side_effect=[json.dumps({'paragraphs':[text],'uncertainties':[]}),
        json.dumps({'paragraphs':[text.replace('colitis','hepatitis')],'uncertainties':['Source wording differs']})]
    recovered=recover_visual_source(path,e,provider=provider,cache_directory=tmp_path/'cache')
    mapped=build_source_map(recovered);p=build_source_package_plan(recovered,mapped)
    assert not p.packages and p.visual_sources


def test_visual_transcription_does_not_validate_numbers_missing_from_native_source(tmp_path):
    from src.chunk_validator import validate_chunks
    from test_source_packages import response
    # The real validator must not treat OCR consensus as independent numerical proof.
    e={'file_name':'scan.pdf','reference_metadata':{'reference':'doi:10.1234/fixture123'}}
    generated=json.loads(response(e,TEXT+' There were 999999999 participants.'))
    report=validate_chunks(generated,chunk_sources={1:[{'text':TEXT+' There were 999999999 participants.',
        'review_required':True,'visual_transcription':True,'native_numeric_source':''}]})
    assert report['status']=='FAILED'
    assert any(i['rule']=='SOURCE_FIDELITY' for i in report['issues'])
    assert any(i['rule']=='VISUAL_SOURCE_REVIEW' for i in report['issues'])


def multi_scan(tmp_path, pages=5):
    path,e,text=scan(tmp_path)
    with fitz.open(path) as source:
        result=fitz.open()
        for _ in range(pages):result.insert_pdf(source)
        other=tmp_path/'long-scan.pdf';result.save(other);result.close()
    with pytest.raises(PdfExtractionError) as exc:extract_pdf(other)
    return other,exc.value.extraction,text


def test_recovery_reads_beyond_page_three_and_does_not_stop_after_first_success(tmp_path):
    path,e,text=multi_scan(tmp_path)
    provider=Mock();provider.transcribe_image.return_value=json.dumps({'paragraphs':[text],'uncertainties':[]})
    recovered=recover_visual_source(path,e,provider=provider,cache_directory=tmp_path/'cache')
    assert provider.transcribe_image.call_count==10
    assert [p['page_number'] for p in recovered['visual_source_pages']]==[1,2,3,4,5]
    mapped=build_source_map(recovered);plan=build_source_package_plan(recovered,mapped)
    assert {p for unit in plan.packages for p in unit.page_numbers}=={1,2,3,4,5}
    validate_source_package_plan(plan,mapped.evidence)


def test_one_page_outage_keeps_other_pages_and_retries_only_missing_transcription(tmp_path):
    from src.providers.base import ChunkGenerationError
    path,e,text=multi_scan(tmp_path,3)
    reply=json.dumps({'paragraphs':[text],'uncertainties':[]})
    provider=Mock();provider.transcribe_image.side_effect=[reply,reply,
        ChunkGenerationError('temporary',code='TRANSPORT_TIMEOUT'),reply,reply,reply]
    first=recover_visual_source(path,e,provider=provider,cache_directory=tmp_path/'cache')
    assert [p['page_number'] for p in first['visual_source_pages']]==[1,3]
    assert first['visual_recovery_outcome']['failures'][0]['code']=='TRANSPORT_TIMEOUT'
    provider.transcribe_image.reset_mock();provider.transcribe_image.side_effect=None
    provider.transcribe_image.return_value=reply
    second=recover_visual_source(path,e,provider=provider,cache_directory=tmp_path/'cache')
    assert provider.transcribe_image.call_count==1  # The other pass was saved concurrently.
    assert len(second['visual_source_pages'])==3


def test_only_requested_pages_are_rendered_and_invalid_selection_rejects(tmp_path):
    path,e,text=multi_scan(tmp_path)
    provider=Mock();provider.transcribe_image.return_value=json.dumps({'paragraphs':[text],'uncertainties':[]})
    r=recover_visual_source(path,e,provider=provider,cache_directory=tmp_path/'cache',page_numbers=[5,4,5])
    assert [p['page_number'] for p in r['visual_source_pages']]==[4,5]
    assert provider.transcribe_image.call_count==4
    with pytest.raises(ValueError,match='outside'):
        recover_visual_source(path,e,provider=provider,cache_directory=tmp_path/'cache',page_numbers=[6])
