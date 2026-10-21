# Official Documentation References

Last verified: 2026-10-09.

These links are persistent reference pointers for GastroAGI RAG engineering. A URL in this file is not proof that the page has been read for a specific implementation task. Before changing code that depends on one of these libraries, open the relevant official documentation page, verify the installed package version locally, and inspect the exact API surface when practical.

## Core Python

- Python 3.13 documentation: https://docs.python.org/3.13/
- Python latest documentation landing page: https://docs.python.org/3/

Use the versioned documentation matching the interpreter used to run tests. This checkout was inspected with Python 3.13.7.

## Current Pipeline Dependencies

- pypdf text extraction: https://pypdf.readthedocs.io/en/stable/user/extract-text.html
- OpenAI Python SDK: https://github.com/openai/openai-python
- Azure OpenAI REST/reference docs: https://learn.microsoft.com/en-us/azure/ai-services/openai/reference
- pytest documentation: https://docs.pytest.org/en/stable/

The current implementation uses `pypdf.PdfReader`, `page.extract_text(extraction_mode="layout")`, Azure OpenAI chat completions through the OpenAI Python SDK, Pydantic schemas, and pytest for the deterministic test suite.

## Planned Or Optional RAG Stack

- LangChain Python overview: https://docs.langchain.com/oss/python/langchain/overview
- LangGraph Python overview: https://docs.langchain.com/oss/python/langgraph/overview
- FastAPI documentation: https://fastapi.tiangolo.com/
- Chroma documentation: https://docs.trychroma.com/docs/overview/introduction
- PyMuPDF documentation: https://pymupdf.readthedocs.io/en/latest/
- Pydantic documentation: https://docs.pydantic.dev/latest/

As of the 2026-10-08 review, LangChain, LangGraph, FastAPI, and ChromaDB are not declared project dependencies and were not installed in the inspected environment. PyMuPDF and Pydantic were installed in the broader Python environment, but they are not declared project dependencies and should not be used in production code unless intentionally added with tests and documentation.

## Verification Checklist

Before implementing a library feature:

1. Read the relevant official documentation page for the behavior being used.
2. Record the local package version with `python -m pip show <package>` or `importlib.metadata.version`.
3. Inspect the local callable signature or object behavior when the code depends on a specific method, parameter, or return shape.
4. Add or update focused tests around the boundary.
5. Document any version-specific behavior, unavailable dependency, or API uncertainty in `docs/engineering/`.



