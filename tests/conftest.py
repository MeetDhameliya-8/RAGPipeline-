from pathlib import Path
import sys
import inspect
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(autouse=True)
def isolated_local_runtime(monkeypatch, tmp_path):
    """Unit tests must never reset a user's queue or ingest into their live DB.

    Legacy helpers capture Path defaults at definition time. Redirect those
    defaults as well as module globals, including aliases imported by the UI.
    Actual PDF regression tests still read their explicitly selected fixtures.
    """
    from src import local_rag_chat, local_chunk_store, local_admin_panel, pipeline
    modules = (local_rag_chat, local_chunk_store, local_admin_panel, pipeline)
    replacements = {
        PROJECT_ROOT / "local_db" / "gastroagi_chunks.sqlite3": tmp_path/"runtime"/"review.sqlite3",
        PROJECT_ROOT / "input_pdfs": tmp_path/"runtime"/"inputs",
        PROJECT_ROOT / "output": tmp_path/"runtime"/"outputs",
        PROJECT_ROOT / "processed": tmp_path/"runtime"/"processed",
        PROJECT_ROOT / "logs": tmp_path/"runtime"/"logs",
        PROJECT_ROOT / "logs" / "pipeline.log": tmp_path/"runtime"/"logs"/"pipeline.log",
    }
    for path in replacements.values():
        (path.parent if path.suffix else path).mkdir(parents=True,exist_ok=True)
    seen = set()
    for module in modules:
        for name, value in list(vars(module).items()):
            if isinstance(value,Path) and value in replacements:
                monkeypatch.setattr(module,name,replacements[value])
            elif inspect.isfunction(value) and id(value) not in seen:
                seen.add(id(value))
                if value.__defaults__:
                    defaults = tuple(replacements.get(x,x) if isinstance(x,Path) else x for x in value.__defaults__)
                    if defaults != value.__defaults__:
                        monkeypatch.setattr(value,"__defaults__",defaults)
    yield
    thread = local_rag_chat._queue_worker_thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def offline_provider_guard(monkeypatch):
    """The local suite may exercise transport only through an explicit fake client."""
    from src.providers.azure import AzureChunkGenerator
    original = AzureChunkGenerator._client

    def guarded(self, max_retries):
        if self.client_factory is None:
            raise AssertionError("Live Azure clients are prohibited in offline tests.")
        return original(self, max_retries)

    monkeypatch.setattr(AzureChunkGenerator, "_client", guarded)

