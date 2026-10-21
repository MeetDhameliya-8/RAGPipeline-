"""Dedicated opt-in workspace, OS locking and immutable response publication."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
from uuid import uuid4

from clinical_proposals.source_adapter import digest
from src.evidence import text_hash
from .models import WorkflowError

MARKER = {'version':'proposal-editorial-workspace-v1','production_ingestion':False}


def check_path_budget(root: Path) -> None:
    # Fail before a possibly paid request. Do not depend on a machine's optional
    # Windows long-path registry policy or silently shorten identity hashes.
    longest = root/'units'/('0'*64)/('validation-'+('0'*64)+'.json')
    if os.name == 'nt' and len(str(longest.resolve())) >= 250:
        raise WorkflowError('CONFIGURATION_ERROR')


def atomic_json(path: Path, value: dict, *, immutable: bool = False) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    data = json.dumps(value,sort_keys=True,ensure_ascii=False).encode('utf-8')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as output:
            temporary = Path(output.name)
            output.write(data); output.flush(); os.fsync(output.fileno())
        if immutable:
            try:
                os.link(temporary,path)
            except FileExistsError:
                if path.read_bytes() != data:
                    raise WorkflowError('EXTRACTION_BLOCKED') from None
        else:
            os.replace(temporary,path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


@contextmanager
def workspace_lock(root: Path):
    root = root.resolve()
    check_path_budget(root)
    if root.exists() and not (root/'workspace.json').is_file() and any(root.iterdir()):
        raise WorkflowError('CONFIGURATION_ERROR')
    root.mkdir(parents=True,exist_ok=True)
    lock = (root/'workflow.lock').open('a+b')
    acquired = False
    try:
        if lock.seek(0,2) == 0:
            lock.write(b'0');lock.flush()
        lock.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            acquired = True
        except OSError:
            raise WorkflowError('REQUEST_BUDGET_EXCEEDED') from None
        marker = root/'workspace.json'
        if marker.exists() and json.loads(marker.read_text(encoding='utf-8')) != MARKER:
            raise WorkflowError('CONFIGURATION_ERROR')
        atomic_json(marker,MARKER,immutable=True)
        yield root
    finally:
        if acquired:
            lock.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(),fcntl.LOCK_UN)
        lock.close()


class Checkpoints:
    def __init__(self, root: Path):
        self.root = root

    def path(self, key: str, kind: str) -> Path:
        return self.root/'units'/key/(kind+'.json')

    def load(self, key: str) -> dict | None:
        path = self.path(key,'response')
        if not path.exists():
            return None
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
            if record['candidate_key'] != key or record['raw_sha256'] != text_hash(record['raw']):
                raise ValueError()
            return record
        except (ValueError,KeyError,TypeError,AttributeError):
            # A damaged response is not permission to repeat a possibly paid call.
            raise WorkflowError('EXTRACTION_BLOCKED') from None

    def publish(self, key: str, raw: str, transport: dict) -> dict:
        record = {'candidate_key':key, 'raw':raw,'raw_sha256':text_hash(raw),'transport':transport}
        atomic_json(self.path(key,'response'),record,immutable=True)
        return record

    def intent(self, key: str) -> None:
        record = {'candidate_key':key,'phase':'request_committed','attempt':uuid4().hex}
        atomic_json(self.path(key,'request-'+record['attempt']),record,immutable=True)
        atomic_json(self.path(key,'request-intent'),record)

    def uncertain(self, key: str) -> bool:
        intent, failure = self.path(key,'request-intent'), self.path(key,'last-failure')
        if not intent.exists():
            return False
        if not failure.exists():
            return True
        try:
            return json.loads(intent.read_text(encoding='utf-8'))['attempt'] != json.loads(failure.read_text(encoding='utf-8'))['attempt']
        except (ValueError,KeyError):
            return True

    def failure(self, key: str, value: dict) -> None:
        attempt = json.loads(self.path(key,'request-intent').read_text(encoding='utf-8'))['attempt']
        atomic_json(self.path(key,'last-failure'),dict(value,attempt=attempt))

    def decision(self, key: str, policy: str, record: dict, decision: dict) -> str:
        payload = {'candidate_key':key,'validation_policy':policy,'raw_sha256':record['raw_sha256'],'validation':decision}
        path = self.path(key,'validation-'+policy)
        previous = 'none'
        if path.exists():
            previous = 'same_policy'
        elif any(path.parent.glob('validation-*.json')):
            previous = 'changed_policy'
        atomic_json(path,payload)
        return previous

    def run_report(self, diagnostics: dict) -> None:
        atomic_json(self.root/'runs'/(uuid4().hex+'.json'),diagnostics)
