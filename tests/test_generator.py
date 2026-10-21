import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.chunk_generator import (
    AzureChunkGenerator,
    ChunkGenerationError,
    generate_chunks,
    normalize_generation_result,
    parse_model_json,
    _provider_from_environment,
)


class GeneratorTests(unittest.TestCase):
    def test_parse_model_json_accepts_fenced_json(self) -> None:
        parsed = parse_model_json('```json\n{"article": {}, "chunks": []}\n```')
        self.assertEqual(parsed["chunks"], [])

    def test_parse_model_json_rejects_malformed_response(self) -> None:
        with self.assertRaises(ChunkGenerationError):
            parse_model_json("not json at all")

    def test_normalize_generation_result_accepts_capitalized_fields(self) -> None:
        normalized = normalize_generation_result(
            {
                "article": {"source_file": "paper.pdf", "Topic": "Ulcerative Colitis",
                            "Subtopic": "Moderate Disease", "reference": "doi:10.1000/example",
                            "number_of_chunks": 1},
                "chunks": [
                    {
                        "Topic": "Ulcerative Colitis",
                        "Subtopic": "Moderate Disease",
                        "Section": "Does Treatment Improve Remission?",
                        "Reference": "doi:10.1000/example",
                        "Original Text": "word " * 100,
                    }
                ],
            },
            source_file="paper.pdf",
        )

        self.assertEqual(normalized["article"]["source_file"], "paper.pdf")
        self.assertEqual(normalized["article"]["number_of_chunks"], 1)
        self.assertEqual(normalized["chunks"][0]["topic"], "Ulcerative Colitis")

    def test_generate_chunks_requires_real_llm_configuration(self) -> None:
        extraction = {
            "file_name": "paper.pdf",
            "full_text": _valid_source_text(),
        }

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(
                ChunkGenerationError, "GENERATION_FAILED: LLM generation unavailable"
            ):
                generate_chunks(extraction)

    def test_provider_selection_uses_azure_only(self) -> None:
        with patch.dict(os.environ, _azure_env(), clear=True):
            provider = _provider_from_environment()

        self.assertIsInstance(provider, AzureChunkGenerator)
        self.assertEqual(provider.deployment, "gpt-5.4")
        self.assertEqual(provider.endpoint, "https://gastroagi.openai.azure.com/")
        self.assertEqual(provider.api_version, "2024-12-01-preview")

    def test_provider_selection_does_not_fallback_to_openai_when_azure_is_incomplete(self) -> None:
        with patch.dict(
            os.environ,
            {
                "GASTROAGI_LLM_PROVIDER": "azure",
                "AZURE_OPENAI_API_KEY": "",
                "AZURE_OPENAI_ENDPOINT": "https://gastroagi.openai.azure.com/",
                "AZURE_OPENAI_DEPLOYMENT": "gpt-5.4",
                "AZURE_OPENAI_API_VERSION": "2024-12-01-preview",
                "OPENAI_API_KEY": "sk-openai-test",
                "OPENAI_MODEL": "gpt-test",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ChunkGenerationError, "AZURE_OPENAI_API_KEY"):
                _provider_from_environment()
    def test_provider_selection_rejects_openai_environment_provider(self) -> None:
        with patch.dict(
            os.environ,
            {
                "GASTROAGI_LLM_PROVIDER": "openai",
                "OPENAI_API_KEY": "sk-openai-test",
                "OPENAI_MODEL": "gpt-test",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(
                ChunkGenerationError, "Unsupported LLM provider `openai`"
            ):
                _provider_from_environment()

    def test_provider_selection_rejects_unsupported_provider(self) -> None:
        with patch.dict(os.environ, {"GASTROAGI_LLM_PROVIDER": "legacy-provider"}, clear=True):
            with self.assertRaisesRegex(
                ChunkGenerationError, "Unsupported LLM provider `legacy-provider`"
            ):
                _provider_from_environment()

    def test_azure_requires_all_configuration_values(self) -> None:
        for missing_name in (
            "AZURE_OPENAI_API_KEY",
            "AZURE_OPENAI_ENDPOINT",
            "AZURE_OPENAI_DEPLOYMENT",
            "AZURE_OPENAI_API_VERSION",
        ):
            env = _azure_env()
            env[missing_name] = ""
            with self.subTest(missing=missing_name):
                with patch.dict(os.environ, env, clear=True):
                    with self.assertRaisesRegex(ChunkGenerationError, missing_name):
                        AzureChunkGenerator.from_environment()

    def test_azure_generation_success_uses_chat_completions(self) -> None:
        content = json.dumps(_valid_generated_document())
        completion = _FakeCompletion(result=_chat_response(content))
        provider = AzureChunkGenerator(
            deployment="gpt-5.4",
            api_key="azure-secret",
            endpoint="https://gastroagi.openai.azure.com/",
            api_version="2024-12-01-preview",
            client_factory=_factory_for_completion(completion),
        )
        extraction = {
            "file_name": "paper.pdf",
            "full_text": _valid_source_text(),
            "reference_metadata": {"doi": "10.1000/example", "reference": "doi:10.1000/example"},
        }

        generated = generate_chunks(extraction, provider=provider)

        self.assertEqual(generated["article"]["number_of_chunks"], 1)
        self.assertEqual(generated["chunks"][0]["topic"], "Ulcerative Colitis")
        self.assertEqual(completion.calls[0]["model"], "gpt-5.4")
        self.assertNotIn("temperature", completion.calls[0])
        self.assertNotIn("max_tokens", completion.calls[0])
        self.assertNotIn("response_format", completion.calls[0])

    def test_generate_chunks_appends_retry_feedback_to_prompt(self) -> None:
        provider = _CapturingProvider(json.dumps(_valid_generated_document()))

        generate_chunks(
            _valid_extraction(),
            provider=provider,
            retry_feedback="Retry Feedback: avoid unsupported 99.9%.",
        )

        self.assertIn("Retry Feedback: avoid unsupported 99.9%.", provider.prompt)

    def test_azure_generation_rejects_invalid_json(self) -> None:
        completion = _FakeCompletion(result=_chat_response("not json"))
        provider = AzureChunkGenerator(
            deployment="gpt-5.4",
            api_key="azure-secret",
            endpoint="https://gastroagi.openai.azure.com/",
            api_version="2024-12-01-preview",
            client_factory=_factory_for_completion(completion),
        )
        extraction = {
            "file_name": "paper.pdf",
            "full_text": _valid_source_text(),
            "reference_metadata": {"doi": "10.1000/example", "reference": "doi:10.1000/example"},
        }

        with self.assertRaisesRegex(ChunkGenerationError, "not parseable JSON"):
            generate_chunks(extraction, provider=provider)

    def test_azure_generation_binds_missing_chunk_reference_from_verified_metadata(self) -> None:
        document = _valid_generated_document()
        document["chunks"][0].pop("reference")
        completion = _FakeCompletion(result=_chat_response(json.dumps(document)))
        provider = AzureChunkGenerator(
            deployment="gpt-5.4",
            api_key="azure-secret",
            endpoint="https://gastroagi.openai.azure.com/",
            api_version="2024-12-01-preview",
            client_factory=_factory_for_completion(completion),
        )
        extraction = {
            "file_name": "paper.pdf",
            "full_text": _valid_source_text(),
            "reference_metadata": {"doi": "10.1000/example", "reference": "doi:10.1000/example"},
        }

        generated = generate_chunks(extraction, provider=provider)

        self.assertEqual(generated["chunks"][0]["reference"], "doi:10.1000/example")

    def test_azure_generation_rejects_empty_required_chunk_field(self) -> None:
        invalid = _valid_generated_document()
        invalid["chunks"][0]["original_text"] = ""
        completion = _FakeCompletion(result=_chat_response(json.dumps(invalid)))
        provider = AzureChunkGenerator(
            deployment="gpt-5.4",
            api_key="azure-secret",
            endpoint="https://gastroagi.openai.azure.com/",
            api_version="2024-12-01-preview",
            client_factory=_factory_for_completion(completion),
        )

        with self.assertRaisesRegex(ChunkGenerationError, "missing required field `original_text`"):
            generate_chunks(_valid_extraction(), provider=provider)

    def test_azure_generation_rejects_extra_chunk_fields(self) -> None:
        invalid = _valid_generated_document()
        invalid["chunks"][0]["page"] = 4
        completion = _FakeCompletion(result=_chat_response(json.dumps(invalid)))
        provider = AzureChunkGenerator(
            deployment="gpt-5.4",
            api_key="azure-secret",
            endpoint="https://gastroagi.openai.azure.com/",
            api_version="2024-12-01-preview",
            client_factory=_factory_for_completion(completion),
        )

        with self.assertRaisesRegex(ChunkGenerationError, "schema validation"):
            generate_chunks(_valid_extraction(), provider=provider)

    def test_azure_generation_defers_unsupported_numbers_to_validation(self) -> None:
        invalid = _valid_generated_document()
        invalid["chunks"][0]["original_text"] = "Ulcerative colitis treatment context. " * 35 + "Unsupported mortality was 99.9%."
        completion = _FakeCompletion(result=_chat_response(json.dumps(invalid)))
        provider = AzureChunkGenerator(
            deployment="gpt-5.4",
            api_key="azure-secret",
            endpoint="https://gastroagi.openai.azure.com/",
            api_version="2024-12-01-preview",
            client_factory=_factory_for_completion(completion),
        )

        generated = generate_chunks(_valid_extraction(), provider=provider)

        self.assertIn("99.9%", generated["chunks"][0]["original_text"])

    def test_azure_generation_rejects_fabricated_reference_doi(self) -> None:
        invalid = _valid_generated_document()
        invalid["chunks"][0]["reference"] = "doi:10.9999/fabricated"
        completion = _FakeCompletion(result=_chat_response(json.dumps(invalid)))
        provider = AzureChunkGenerator(
            deployment="gpt-5.4",
            api_key="azure-secret",
            endpoint="https://gastroagi.openai.azure.com/",
            api_version="2024-12-01-preview",
            client_factory=_factory_for_completion(completion),
        )

        with self.assertRaisesRegex(ChunkGenerationError, "DOI"):
            generate_chunks(_valid_extraction(), provider=provider)

    def test_generation_accepts_equivalent_verified_nejm_citation_format(self) -> None:
        document = _valid_generated_document()
        document["article"]["reference"] = "N Engl J Med 2011;364(6):501-513"
        document["chunks"][0]["reference"] = "N Engl J Med 2011;364(6):501-513"
        completion = _FakeCompletion(result=_chat_response(json.dumps(document)))
        provider = AzureChunkGenerator(
            deployment="gpt-5.4",
            api_key="azure-secret",
            endpoint="https://gastroagi.openai.azure.com/",
            api_version="2024-12-01-preview",
            client_factory=_factory_for_completion(completion),
        )
        extraction = {
            "file_name": "NEJMoa1003825.pdf",
            "full_text": "Ulcerative colitis treatment context. " * 40 + "N Engl J Med 2011;36 4(6):501-13.",
            "reference_metadata": {
                "citation": "N Engl J Med. 2011;364(6):501-13",
                "reference": "N Engl J Med. 2011;364(6):501-13",
            },
        }

        generated = generate_chunks(extraction, provider=provider)

        self.assertEqual(generated["chunks"][0]["reference"], "N Engl J Med 2011;364(6):501-513")
    def test_azure_stream_generate_yields_chat_completion_deltas(self) -> None:
        completion = _FakeCompletion(
            result=[
                _chat_stream_chunk("Hello"),
                _chat_stream_chunk(" from stream"),
                _chat_stream_chunk(None),
            ]
        )
        provider = AzureChunkGenerator(
            deployment="gpt-5.4",
            api_key="azure-secret",
            endpoint="https://gastroagi.openai.azure.com/",
            api_version="2024-12-01-preview",
            client_factory=_factory_for_completion(completion),
        )

        parts = list(provider.stream_generate("article text", "paper.pdf", "prompt"))

        self.assertEqual(parts, ["Hello", " from stream"])
        self.assertEqual(completion.calls[0]["stream"], True)
        self.assertEqual(completion.calls[0]["model"], "gpt-5.4")
    def test_azure_error_handling_is_sanitized(self) -> None:
        completion = _FakeCompletion(
            exc=_FakeAPIError(
                401,
                {"message": "Invalid API key provided", "type": "authentication_error", "code": "invalid_api_key"},
            )
        )
        provider = AzureChunkGenerator(
            deployment="gpt-5.4",
            api_key="azure-secret",
            endpoint="https://gastroagi.openai.azure.com/",
            api_version="2024-12-01-preview",
            client_factory=_factory_for_completion(completion),
        )

        with self.assertRaises(ChunkGenerationError) as raised:
            provider.generate("article text", "paper.pdf", "prompt")

        message = str(raised.exception)
        self.assertIn("Azure OpenAI rejected the API credentials", message)
        self.assertNotIn("azure-secret", message)
        self.assertNotIn("article text", message)

    def test_azure_generation_does_not_retry_authorization_failure(self) -> None:
        completion = _FakeCompletion(
            exc=_FakeAPIError(
                403,
                {"message": "Forbidden", "type": "authentication_error", "code": "permission_denied"},
                headers={"X-Request-ID": "azure-authz-1"},
            )
        )
        provider = AzureChunkGenerator(
            deployment="gpt-5.4",
            api_key="azure-secret",
            endpoint="https://gastroagi.openai.azure.com/",
            api_version="2024-12-01-preview",
            client_factory=_factory_for_completion(completion),
            sleep=lambda _: self.fail("Permanent authorization errors must not be retried."),
        )

        with self.assertRaises(ChunkGenerationError) as raised:
            provider.generate("article text", "paper.pdf", "prompt")

        self.assertIn("rejected the API credentials", str(raised.exception))
        self.assertIn("request_id=azure-authz-1", str(raised.exception))
        self.assertEqual(len(completion.calls), 1)

    def test_azure_connectivity_check_uses_token_bounded_non_clinical_request(self) -> None:
        completion = _FakeCompletion(result=_chat_response("OK"))
        provider = AzureChunkGenerator(
            deployment="gpt-5.4",
            api_key="azure-secret",
            endpoint="https://gastroagi.openai.azure.com/",
            api_version="2024-12-01-preview",
            client_factory=_factory_for_completion(completion),
        )

        result = provider.check_connectivity()

        self.assertTrue(result["ok"])
        self.assertEqual(result["probe"]["max_output_tokens"], 16)
        self.assertEqual(result["probe"]["token_limit_parameter"], "max_completion_tokens")
        self.assertEqual(completion.calls[0]["max_completion_tokens"], 16)
        self.assertEqual(
            completion.calls[0]["messages"],
            [{"role": "user", "content": "Reply OK."}],
        )
        self.assertNotIn("article text", json.dumps(completion.calls[0]))
        self.assertNotIn("azure-secret", json.dumps(result))

    def test_azure_connectivity_check_supports_legacy_token_parameter(self) -> None:
        completion = _FakeCompletion(result=_chat_response("OK"))
        provider = AzureChunkGenerator(
            deployment="gpt-5.4",
            api_key="azure-secret",
            endpoint="https://gastroagi.openai.azure.com/",
            api_version="2024-12-01-preview",
            client_factory=_factory_for_completion(completion),
            connectivity_max_output_tokens=3,
            connectivity_token_parameter="max_tokens",
        )

        result = provider.check_connectivity()

        self.assertTrue(result["ok"])
        self.assertEqual(result["probe"]["max_output_tokens"], 3)
        self.assertEqual(result["probe"]["token_limit_parameter"], "max_tokens")
        self.assertEqual(completion.calls[0]["max_tokens"], 3)
        self.assertNotIn("max_completion_tokens", completion.calls[0])

    def test_azure_from_environment_rejects_non_azure_openai_endpoint(self) -> None:
        env = _azure_env()
        env["AZURE_OPENAI_ENDPOINT"] = "https://gastroagi.services.ai.azure.com/models"

        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ChunkGenerationError, "Azure AI Foundry Models endpoint"):
                AzureChunkGenerator.from_environment()
    def test_azure_connectivity_check_reports_sanitized_rate_limit(self) -> None:
        completion = _FakeCompletion(
            exc=_FakeAPIError(
                429,
                {"message": "Slow down", "type": "rate_limit_error", "code": "rate_limit_exceeded"},
                headers={"Retry-After": "7", "X-Request-ID": "azure-req-1"},
            )
        )
        provider = AzureChunkGenerator(
            deployment="gpt-5.4",
            api_key="azure-secret",
            endpoint="https://gastroagi.openai.azure.com/",
            api_version="2024-12-01-preview",
            client_factory=_factory_for_completion(completion),
        )

        result = provider.check_connectivity()

        self.assertFalse(result["ok"])
        self.assertEqual(result["http_status"], 429)
        self.assertEqual(result["retry_after"], "7")
        self.assertEqual(result["request_id"], "azure-req-1")
        self.assertNotIn("azure-secret", json.dumps(result))


    def test_azure_connectivity_check_reports_network_timeout(self) -> None:
        completion = _FakeCompletion(exc=TimeoutError("request timed out"))
        provider = AzureChunkGenerator(
            deployment="gpt-5.4",
            api_key="azure-secret",
            endpoint="https://gastroagi.openai.azure.com/",
            api_version="2024-12-01-preview",
            client_factory=_factory_for_completion(completion),
        )

        result = provider.check_connectivity()

        self.assertFalse(result["ok"])
        self.assertEqual(result["exception_class"], "TimeoutError")
        self.assertIn("timed out", result["message"])
        self.assertTrue(result["retryable"])
        self.assertNotIn("azure-secret", json.dumps(result))

    def test_azure_generation_retries_transient_error_then_succeeds(self) -> None:
        content = json.dumps(_valid_generated_document())
        completion = _FakeCompletion(
            sequence=[
                _FakeAPIError(
                    500,
                    {"message": "Server error", "type": "server_error", "code": "internal_error"},
                    headers={"X-Request-ID": "azure-req-500"},
                ),
                _chat_response(content),
            ]
        )
        sleeps: list[float] = []
        provider = AzureChunkGenerator(
            deployment="gpt-5.4",
            api_key="azure-secret",
            endpoint="https://gastroagi.openai.azure.com/",
            api_version="2024-12-01-preview",
            client_factory=_factory_for_completion(completion),
            sleep=sleeps.append,
        )
        extraction = {
            "file_name": "paper.pdf",
            "full_text": _valid_source_text(),
            "reference_metadata": {"doi": "10.1000/example", "reference": "doi:10.1000/example"},
        }

        generated = generate_chunks(extraction, provider=provider)

        self.assertEqual(generated["article"]["number_of_chunks"], 1)
        self.assertEqual(len(completion.calls), 2)
        self.assertEqual(sleeps, [1.0])

    def test_azure_generation_reports_service_failure_after_retries(self) -> None:
        completion = _FakeCompletion(
            exc=_FakeAPIError(
                503,
                {"message": "Service unavailable", "type": "server_error", "code": "service_unavailable"},
                headers={"X-Request-ID": "azure-req-503"},
            )
        )
        sleeps: list[float] = []
        provider = AzureChunkGenerator(
            deployment="gpt-5.4",
            api_key="azure-secret",
            endpoint="https://gastroagi.openai.azure.com/",
            api_version="2024-12-01-preview",
            client_factory=_factory_for_completion(completion),
            max_retries=1,
            sleep=sleeps.append,
        )

        with self.assertRaises(ChunkGenerationError) as raised:
            provider.generate("article text", "paper.pdf", "prompt")

        self.assertIn("transient status 503", str(raised.exception))
        self.assertIn("request_id=azure-req-503", str(raised.exception))
        self.assertEqual(len(completion.calls), 2)
        self.assertEqual(sleeps, [1.0])
        self.assertNotIn("azure-secret", str(raised.exception))
    def test_azure_generation_respects_retry_after_for_rate_limit(self) -> None:
        content = json.dumps(_valid_generated_document())
        completion = _FakeCompletion(
            sequence=[
                _FakeAPIError(
                    429,
                    {"message": "Slow down", "type": "rate_limit_error", "code": "rate_limit_exceeded"},
                    headers={"Retry-After": "0.25", "X-Request-ID": "azure-req-429"},
                ),
                _chat_response(content),
            ]
        )
        sleeps: list[float] = []
        provider = AzureChunkGenerator(
            deployment="gpt-5.4",
            api_key="azure-secret",
            endpoint="https://gastroagi.openai.azure.com/",
            api_version="2024-12-01-preview",
            client_factory=_factory_for_completion(completion),
            sleep=sleeps.append,
        )
        extraction = {
            "file_name": "paper.pdf",
            "full_text": _valid_source_text(),
            "reference_metadata": {"doi": "10.1000/example", "reference": "doi:10.1000/example"},
        }

        generate_chunks(extraction, provider=provider)

        self.assertEqual(len(completion.calls), 2)
        self.assertEqual(sleeps, [0.25])

    def test_azure_generation_does_not_retry_authentication_failure(self) -> None:
        completion = _FakeCompletion(
            exc=_FakeAPIError(
                401,
                {"message": "Invalid API key", "type": "authentication_error", "code": "invalid_api_key"},
                headers={"X-Request-ID": "azure-auth-1"},
            )
        )
        provider = AzureChunkGenerator(
            deployment="gpt-5.4",
            api_key="azure-secret",
            endpoint="https://gastroagi.openai.azure.com/",
            api_version="2024-12-01-preview",
            client_factory=_factory_for_completion(completion),
            sleep=lambda _: self.fail("Permanent authentication errors must not be retried."),
        )

        with self.assertRaises(ChunkGenerationError) as raised:
            provider.generate("article text", "paper.pdf", "prompt")

        self.assertIn("rejected the API credentials", str(raised.exception))
        self.assertIn("request_id=azure-auth-1", str(raised.exception))
        self.assertEqual(len(completion.calls), 1)

    def test_azure_generation_reports_deployment_not_found_without_retry(self) -> None:
        completion = _FakeCompletion(
            exc=_FakeAPIError(
                404,
                {"message": "Deployment not found", "type": "invalid_request_error", "code": "DeploymentNotFound"},
            )
        )
        provider = AzureChunkGenerator(
            deployment="missing-deployment",
            api_key="azure-secret",
            endpoint="https://gastroagi.openai.azure.com/",
            api_version="2024-12-01-preview",
            client_factory=_factory_for_completion(completion),
        )

        with self.assertRaises(ChunkGenerationError) as raised:
            provider.generate("article text", "paper.pdf", "prompt")

        self.assertIn("deployment or model was not found", str(raised.exception))
        self.assertEqual(len(completion.calls), 1)

    def test_azure_generation_rejects_malformed_chat_response(self) -> None:
        completion = _FakeCompletion(result=SimpleNamespace(id="bad-response", choices=[]))
        provider = AzureChunkGenerator(
            deployment="gpt-5.4",
            api_key="azure-secret",
            endpoint="https://gastroagi.openai.azure.com/",
            api_version="2024-12-01-preview",
            client_factory=_factory_for_completion(completion),
        )

        with self.assertRaisesRegex(ChunkGenerationError, "malformed response"):
            provider.generate("article text", "paper.pdf", "prompt")

    def test_azure_generation_logs_token_usage_when_available(self) -> None:
        content = json.dumps(_valid_generated_document())
        completion = _FakeCompletion(
            result=_chat_response(
                content,
                usage=SimpleNamespace(prompt_tokens=12, completion_tokens=4, total_tokens=16),
            )
        )
        provider = AzureChunkGenerator(
            deployment="gpt-5.4",
            api_key="azure-secret",
            endpoint="https://gastroagi.openai.azure.com/",
            api_version="2024-12-01-preview",
            client_factory=_factory_for_completion(completion),
        )

        with self.assertLogs("src.chunk_generator", level="INFO") as logs:
            provider.generate("article text", "paper.pdf", "prompt")

        joined = "\n".join(logs.output)
        self.assertIn("prompt_tokens=12", joined)
        self.assertIn("completion_tokens=4", joined)
        self.assertIn("total_tokens=16", joined)
        self.assertNotIn("azure-secret", joined)

def _azure_env() -> dict[str, str]:
    return {
        "GASTROAGI_LLM_PROVIDER": "azure",
        "AZURE_OPENAI_API_KEY": "azure-test-key",
        "AZURE_OPENAI_ENDPOINT": "https://gastroagi.openai.azure.com/",
        "AZURE_OPENAI_DEPLOYMENT": "gpt-5.4",
        "AZURE_OPENAI_API_VERSION": "2024-12-01-preview",
    }




def _valid_extraction() -> dict[str, object]:
    return {
        "file_name": "paper.pdf",
        "full_text": _valid_source_text(),
        "reference_metadata": {"doi": "10.1000/example", "reference": "doi:10.1000/example"},
    }

def _valid_source_text() -> str:
    return "Ulcerative colitis treatment context. " * 40 + " doi:10.1000/example"

def _valid_generated_document() -> dict[str, object]:
    return {
        "article": {
            "source_file": "paper.pdf",
            "topic": "Ulcerative Colitis",
            "subtopic": "Adults With Active Disease",
            "reference": "doi:10.1000/example",
            "number_of_chunks": 1,
        },
        "chunks": [
            {
                "chunk_number": 1,
                "topic": "Ulcerative Colitis",
                "subtopic": "Adults With Active Disease",
                "section": "Does Treatment Improve Clinical Remission?",
                "reference": "doi:10.1000/example",
                "original_text": "Ulcerative colitis treatment context. " * 40,
            }
        ],
    }


def _chat_response(content: str, usage: SimpleNamespace | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id="chatcmpl-test",
        choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=content))],
        usage=usage,
    )



def _chat_stream_chunk(content: str | None) -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=content))])
class _FakeCompletion:
    def __init__(
        self,
        result: SimpleNamespace | None = None,
        exc: Exception | None = None,
        sequence: list[SimpleNamespace | Exception] | None = None,
    ) -> None:
        self.result = result
        self.exc = exc
        self.sequence = list(sequence or [])
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(kwargs)
        if self.sequence:
            outcome = self.sequence.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        if self.exc is not None:
            raise self.exc
        if self.result is None:
            raise AssertionError("Fake completion has no result.")
        return self.result


class _CapturingProvider:
    def __init__(self, output: str) -> None:
        self.output = output
        self.prompt = ""

    def generate(self, article_text: str, source_file: str, prompt: str) -> str:
        self.prompt = prompt
        return self.output


def _factory_for_completion(completion: _FakeCompletion):
    def factory(**_: object) -> SimpleNamespace:
        return SimpleNamespace(chat=SimpleNamespace(completions=completion))

    return factory


class _FakeResponse:
    def __init__(self, payload: dict[str, object], headers: dict[str, str] | None = None) -> None:
        self._payload = payload
        self.headers = headers or {}

    def json(self) -> dict[str, object]:
        return self._payload


class _FakeAPIError(Exception):
    def __init__(
        self,
        status_code: int,
        error: dict[str, object],
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(str(error.get("message", "")))
        self.status_code = status_code
        self.code = error.get("code")
        self.response = _FakeResponse({"error": error}, headers=headers)






