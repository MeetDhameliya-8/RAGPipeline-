"""Read-only, indexed SQLite snapshot. Never initialize/repair/upsert a DB."""
from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3

from clinical_proposals.source_adapter import digest
from src import local_chunk_store as store
from .artifacts import ArtifactReader
from .models import VerificationError,ReconciliationLimits


class StorageSnapshot(AbstractContextManager):
    def __init__(self,path: Path | None,limits: ReconciliationLimits):
        self.path=Path(path).resolve() if path is not None else None
        self.conn=None
        self.limits=limits
        self.bytes_read=0
        self.unavailable=path is None
        self.invalid=self.path is not None and self.path.exists() and not self.path.is_file()
        self.evidence: dict[str,object]={}
        self.cache={}

    def __enter__(self):
        if self.path is None or not self.path.is_file():
            return self
        try:
            self.conn=sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True,timeout=1)
            self.conn.row_factory=sqlite3.Row
            self.conn.execute('PRAGMA query_only=ON')
            self.conn.execute('BEGIN')
            for table,columns in (
                ('chunks',{'chunk_id','source_file','output_dir','chunk_number','topic','subtopic','section',
                    'reference','original_text','content_hash','validation_status','review_status'}),
                ('embeddings',{'chunk_id','algorithm','dimensions','vector_json'}),
                ('ingestion_audit',{'id','event_type','chunk_id','detail_json'})):
                if not columns<={r[1] for r in self.conn.execute('PRAGMA table_info('+table+')')}:
                    raise ValueError()
        except (sqlite3.Error,ValueError):
            self.invalid=True
        return self

    def __exit__(self,*args):
        if self.conn is not None:
            self.conn.close()

    def lookup(self,chunk_id: str):
        if self.unavailable or self.invalid:
            raise VerificationError('STORAGE_UNAVAILABLE')
        if chunk_id in self.cache:
            return self.cache[chunk_id]
        try:
            row=embedding=None
            events=[]
            if self.conn is not None:
                columns=('chunk_id','source_file','output_dir','chunk_number','topic','subtopic','section',
                    'reference','original_text','content_hash','validation_status','review_status')
                size=self.conn.execute('SELECT '+ '+'.join('length(CAST('+c+' AS BLOB))' for c in columns)+
                    ' FROM chunks WHERE chunk_id=?',(chunk_id,)).fetchone()
                vector_size=self.conn.execute('SELECT length(CAST(vector_json AS BLOB)) FROM embeddings WHERE chunk_id=?',
                    (chunk_id,)).fetchone()
                amount=(size[0] if size else 0)+(vector_size[0] if vector_size else 0)
                self.bytes_read+=amount
                if amount>self.limits.max_artifact_bytes or self.bytes_read>self.limits.max_total_artifact_bytes:
                    raise VerificationError('STORAGE_BUDGET_EXCEEDED')
                found=self.conn.execute('SELECT '+','.join(columns)+' FROM chunks WHERE chunk_id=?',(chunk_id,)).fetchone()
                row=dict(found) if found else None
                found=self.conn.execute('SELECT algorithm,dimensions,vector_json FROM embeddings WHERE chunk_id=?',(chunk_id,)).fetchone()
                embedding=dict(found) if found else None
                # Only a revoked draft needs audit corroboration. Successful
                # readback never repeatedly scans the unindexed audit table.
                if row and row['validation_status']=='FAILED':
                    event=self.conn.execute(
                        "SELECT length(CAST(detail_json AS BLOB)),detail_json FROM ingestion_audit WHERE chunk_id=? AND event_type='proposal_editorial_quarantine' ORDER BY id DESC LIMIT 1",
                        (chunk_id,)).fetchone()
                    if event:
                        if event[0]>self.limits.max_artifact_bytes:
                            raise VerificationError('STORAGE_BUDGET_EXCEEDED')
                        events=[event[1]]
            self.evidence[chunk_id]=digest([row,embedding,events])
            self.cache[chunk_id]=(row,embedding,events)
            return self.cache[chunk_id]
        except sqlite3.Error:
            raise VerificationError('STORAGE_READBACK_INCOMPLETE') from None


def expected_chunk(document: dict,key: str):
    c=document['chunks'][0]
    return store._with_chunk_id(store.StoredChunk(chunk_id='',source_file=document['article']['source_file'],
        output_dir='a3_'+key,chunk_number=1,**{k:c[k] for k in ('topic','subtopic','section','reference','original_text')},
        validation_status='REVIEW_REQUIRED',review_status='REVIEW_REQUIRED'))


def verify_storage(reader: ArtifactReader,snapshot: StorageSnapshot,key: str,document: dict,
                   linkage: dict,*,eligible: bool,policy: str) -> tuple[str,str | None,tuple[str,...]]:
    expected=expected_chunk(document,key)
    row,embedded,events=snapshot.lookup(expected.chunk_id)
    folder=('local','a3_'+key)
    saved=reader.read(*folder,'chunks.json',required=False)
    source_link=reader.read(*folder,'source_link.json',required=False)
    if row is None:
        # An explicitly inspected absent row is different from no DB evidence.
        return 'not_stored',None,()
    if saved!=document or source_link!=linkage:
        return 'reconciliation_required',None,('STORAGE_SOURCE_LINK_INVALID',)
    fields=asdict(expected)
    exact=all(row[k]==v for k,v in fields.items() if k not in {'validation_status','review_status'})
    exact=exact and row['content_hash']==store._content_hash(expected)
    if not exact:
        return 'readback_incomplete',None,('STORED_CONTENT_OR_IDENTITY_MISMATCH',)
    if not eligible:
        revoked=(row['validation_status']=='FAILED' and any(
            json.loads(e).get('validation_policy')==policy for e in events))
        return ('eligibility_revoked',expected.chunk_id,()) if revoked else (
            'reconciliation_required',None,('REJECTED_DRAFT_STORAGE_RECONCILIATION',))
    vector=store.embed_text(store._chunk_embedding_text(expected))
    vector_ok=(embedded is not None and embedded['algorithm']==store.EMBEDDING_ALGORITHM
        and embedded['dimensions']==store.EMBEDDING_DIMENSIONS and json.loads(embedded['vector_json'])==vector)
    if row['review_status']!='REVIEW_REQUIRED':
        return 'reconciliation_required',None,('UNAUTHENTICATED_REVIEW_MARKER',)
    if row['validation_status']!='REVIEW_REQUIRED' or not vector_ok:
        return 'readback_incomplete',None,('STORAGE_READBACK_INCOMPLETE',)
    return 'verified_local_review',expected.chunk_id,()
