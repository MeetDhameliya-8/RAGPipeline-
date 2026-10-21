"""Compatible adapter to the existing local review DB, never production RAG."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from src import local_chunk_store as store
from src import diagnostics as telemetry
from .checkpoints import atomic_json
from .models import WorkflowError


def check_target(db_path: Path) -> None:
    if not db_path.exists():
        return
    try:
        with sqlite3.connect(db_path.resolve().as_uri()+'?mode=ro',uri=True) as conn:
            required = {'chunk_id','source_file','output_dir','chunk_number','topic','subtopic','section',
                'reference','original_text','validation_status','review_status','content_hash'}
            if not required <= {r[1] for r in conn.execute('pragma table_info(chunks)')}:
                raise WorkflowError('CONFIGURATION_ERROR')
            for table, fields in (
                ('embeddings',{'chunk_id','algorithm','dimensions','vector_json'}),
                ('ingestion_audit',{'event_type','chunk_id','output_dir','detail_json','created_at'})):
                if not fields <= {r[1] for r in conn.execute('pragma table_info('+table+')')}:
                    raise WorkflowError('CONFIGURATION_ERROR')
    except sqlite3.Error:
        raise WorkflowError('CONFIGURATION_ERROR') from None


def _row(db_path, chunk_id):
    if not db_path.exists():
        return None
    with sqlite3.connect(db_path.resolve().as_uri()+'?mode=ro',uri=True) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute('select * from chunks where chunk_id = ?', (chunk_id,)).fetchone()
        return dict(row) if row else None


def _package(root, key):
    return root/'local'/('a3_'+key)


def save_review(root: Path, key: str, document: dict, report: dict, linkage: dict, db_path: Path) -> dict:
    package = _package(root,key)
    with telemetry.stage('output_persistence'):
        atomic_json(package/'chunks.json',document,immutable=True)
        atomic_json(package/'source_link.json',linkage,immutable=True)
        # Reuse the existing package-scoped ID mode. A new A3 namespace must
        # never upsert a historical global PDF-content row. The full PDF and
        # evidence identities remain in this report and immutable source_link.
        local_report = dict(report,diagnostics={'proposal_storage_identity':{
            'version':'proposal-editorial-v1','pdf_sha256':linkage['pdf_sha256'],
            'candidate_key':key}})
        atomic_json(package/'validation_report.json',local_report)
    expected = store._load_pipeline_chunks(package/'chunks.json','REVIEW_REQUIRED')[0]
    existing = _row(db_path,expected.chunk_id)
    historical = _row(db_path,store._with_chunk_id(expected,linkage['pdf_sha256']).chunk_id)
    if historical and historical['output_dir'] != package.name:
        return {'status':'EXISTING_ROW_PRESERVED','persisted':False,'readback_verified':False,
            'duplicate_existing_row':True,'local_package_id':package.name}
    if existing and (existing['output_dir'] != package.name or existing['review_status'] != expected.review_status):
        # Content identities can match older packages. Never rewrite their row,
        # source metadata, review decision, embedding or package membership.
        return {'status':'EXISTING_ROW_PRESERVED','persisted':False,'readback_verified':False,
            'duplicate_existing_row':True,'local_package_id':package.name}
    if existing:
        for field in ('topic','subtopic','section','reference','original_text','source_file','output_dir','chunk_number'):
            if existing[field] != getattr(expected,field):
                raise WorkflowError('SOURCE_VALIDATION_FAILED')
    try:
        stored,verified = _store_and_verify(package,expected,db_path)
        if not verified:
            raise WorkflowError('STORAGE_READBACK_INCOMPLETE')
    except Exception as exc:
        telemetry.failure(exc,'GENERATION_FAILED')
        try:
            quarantine_owned(root,key,report.get('validation_policy','readback_failed'),db_path,check_content=False)
        except Exception as quarantine_error:
            telemetry.failure(quarantine_error,'GENERATION_FAILED')
            raise WorkflowError('STORAGE_QUARANTINE_FAILED') from None
        raise WorkflowError('STORAGE_READBACK_INCOMPLETE') from None
    return {'status':stored['status'],'persisted':True,'readback_verified':True,
        'inserted':stored['inserted'],'unchanged':stored['unchanged'],'updated':stored['updated'],
        'local_package_id':package.name,'chunk_id':expected.chunk_id,'production_backend':False}


def _store_and_verify(package, expected, db_path):
    stored = store.ingest_chunks_file(package/'chunks.json','REVIEW_REQUIRED',db_path=db_path)
    readback = store.verify_chunks_file_ingested(package/'chunks.json','REVIEW_REQUIRED',db_path=db_path)
    with telemetry.stage('sqlite_readback'):
        row = _row(db_path,expected.chunk_id)
        exact = row is not None and all(row[k] == getattr(expected,k) for k in (
            'source_file','output_dir','chunk_number','topic','subtopic','section','reference','original_text',
            'validation_status','review_status')) and row['content_hash'] == store._content_hash(expected)
        with sqlite3.connect(db_path.resolve().as_uri()+'?mode=ro',uri=True) as conn:
            embedded = conn.execute('select algorithm, dimensions, vector_json from embeddings where chunk_id=?',
                (expected.chunk_id,)).fetchone()
        vector_ok = (embedded is not None and embedded[:2] == (store.EMBEDDING_ALGORITHM,store.EMBEDDING_DIMENSIONS)
            and json.loads(embedded[2]) == store.embed_text(store._chunk_embedding_text(expected)))
    return stored,bool(readback['verified'] and exact and vector_ok)


def quarantine_owned(root: Path, key: str, policy: str, db_path: Path, *, check_content: bool = True) -> int:
    """Exclude only this workflow's own previously stored draft after rejection.

    Five content fields are unchanged. Legacy packages/foreign content IDs are
    never altered. Existing retrieval already excludes validation_status FAILED.
    """
    package = _package(root,key)
    if not db_path.exists() or not (package/'source_link.json').exists():
        return 0
    linkage = json.loads((package/'source_link.json').read_text(encoding='utf-8'))
    if linkage.get('candidate_key') != key:
        raise WorkflowError('EXTRACTION_BLOCKED')
    expected = store._load_pipeline_chunks(package/'chunks.json','REVIEW_REQUIRED')[0]
    row = _row(db_path,expected.chunk_id)
    if not row or row['output_dir'] != package.name:
        return 0
    if check_content and any(row[k] != getattr(expected,k) for k in ('topic','subtopic','section','reference','original_text','source_file','chunk_number')):
        raise WorkflowError('SOURCE_VALIDATION_FAILED')
    if row['validation_status'] != 'FAILED':
        with telemetry.stage('sqlite_storage'):
            with sqlite3.connect(db_path) as conn:
                conn.execute("update chunks set validation_status='FAILED' where chunk_id=? and output_dir=?",
                    (expected.chunk_id,package.name))
                conn.execute('insert into ingestion_audit (event_type,chunk_id,output_dir,detail_json,created_at) values (?,?,?,?,?)',
                    ('proposal_editorial_quarantine',expected.chunk_id,package.name,json.dumps({'validation_policy':policy}),store._utc_now()))
    return 1
