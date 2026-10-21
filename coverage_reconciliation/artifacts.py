"""Bounded read-only artifact snapshots. No creation or checkpoint repair."""
from __future__ import annotations
from hashlib import sha256
from itertools import islice
import json
from pathlib import Path
from .models import ReconciliationLimits,VerificationError


class ArtifactReader:
    def __init__(self,root: Path | None,limits: ReconciliationLimits):
        self.root=Path(root).resolve() if root is not None else None
        self.limits=limits
        self.total=0
        self.watched: dict[Path,str | None]={}

    def path(self,*parts: str) -> Path:
        if self.root is None:
            raise VerificationError('ARTIFACTS_UNAVAILABLE')
        target=(self.root/Path(*parts)).resolve()
        if not target.is_relative_to(self.root):
            raise VerificationError('ARTIFACT_PATH_INVALID')
        return target

    def read(self,*parts: str, required=True) -> dict | None:
        path=self.path(*parts)
        try:
            if not path.is_file():
                if required:
                    raise VerificationError('ARTIFACT_MISSING')
                self.watched[path]=None
                return None
            if path.stat().st_size > self.limits.max_artifact_bytes:
                raise VerificationError('ARTIFACT_BUDGET_EXCEEDED')
            with path.open('rb') as stream:
                raw=stream.read(self.limits.max_artifact_bytes+1)
            self.total += len(raw)
            if len(raw)>self.limits.max_artifact_bytes or self.total>self.limits.max_total_artifact_bytes:
                raise VerificationError('ARTIFACT_BUDGET_EXCEEDED')
            value=json.loads(raw)
            if not isinstance(value,dict):
                raise ValueError()
            hashed=sha256(raw).hexdigest()
            if path in self.watched and self.watched[path] != hashed:
                raise VerificationError('ARTIFACT_CHANGED_DURING_READ')
            self.watched[path]=hashed
            return value
        except VerificationError:
            raise
        except (OSError,ValueError,TypeError,RecursionError):
            raise VerificationError('ARTIFACT_CORRUPT_OR_UNREADABLE') from None

    def names(self,parts: tuple[str,...],pattern: str,limit: int) -> list[str]:
        try:
            values=list(islice(self.path(*parts).glob(pattern),limit+1))
            if len(values)>limit:
                raise VerificationError('ARTIFACT_BUDGET_EXCEEDED')
            return sorted(p.name for p in values)
        except OSError:
            raise VerificationError('ARTIFACT_CORRUPT_OR_UNREADABLE') from None

    def stable(self) -> bool:
        try:
            return all((not p.exists()) if h is None else (p.is_file() and p.stat().st_size<=self.limits.max_artifact_bytes
                and sha256(p.read_bytes()).hexdigest()==h) for p,h in self.watched.items())
        except OSError:
            return False
