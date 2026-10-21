import json
import unittest
from unittest.mock import patch

import test_azure_connection as diagnostic


class AzureConnectionDiagnosticTests(unittest.TestCase):
    def test_live_probe_sends_non_clinical_token_bounded_request(self) -> None:
        captured: dict[str, object] = {}

        class FakeConnection:
            def __init__(self, host: str, port: int | None, timeout: float) -> None:
                captured["host"] = host
                captured["port"] = port
                captured["timeout"] = timeout
                self.closed = False

            def request(
                self,
                method: str,
                path: str,
                body: str,
                headers: dict[str, str],
            ) -> None:
                captured["method"] = method
                captured["path"] = path
                captured["body"] = body
                captured["headers"] = headers

            def getresponse(self) -> "FakeResponse":
                return FakeResponse(
                    200,
                    {"x-ms-request-id": "req-123"},
                    {
                        "id": "chatcmpl-test",
                        "model": "gpt-test",
                        "choices": [{"message": {"content": "OK"}}],
                        "usage": {
                            "prompt_tokens": 8,
                            "completion_tokens": 1,
                            "total_tokens": 9,
                        },
                    },
                )

            def close(self) -> None:
                captured["closed"] = True

        config = diagnostic.AzureConfig(
            provider="azure",
            api_key="secret-key",
            endpoint="https://gastroagi.openai.azure.com/",
            deployment="clinical deployment",
            api_version="2024-12-01-preview",
        )

        with patch.object(diagnostic, "HTTPSConnection", FakeConnection):
            result = diagnostic._run_probe(config, timeout=2, max_output_tokens=4)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["probe"]["max_output_tokens"], 4)
        self.assertEqual(
            result["probe"]["token_limit_parameter"], "max_completion_tokens"
        )
        self.assertEqual(captured["host"], "gastroagi.openai.azure.com")
        self.assertEqual(captured["timeout"], 2)
        self.assertEqual(captured["method"], "POST")
        self.assertIn("clinical%20deployment", str(captured["path"]))
        self.assertIn("api-version=2024-12-01-preview", str(captured["path"]))
        request_body = json.loads(str(captured["body"]))
        self.assertEqual(request_body["max_completion_tokens"], 4)
        self.assertEqual(
            request_body["messages"],
            [{"role": "user", "content": "Reply with exactly OK."}],
        )
        self.assertEqual(captured["closed"], True)
        self.assertNotIn("secret-key", json.dumps(result))

    def test_live_probe_clamps_requested_token_cap(self) -> None:
        self.assertEqual(diagnostic._bounded_max_output_tokens(0), 1)
        self.assertEqual(diagnostic._bounded_max_output_tokens(-5), 1)
        self.assertEqual(
            diagnostic._bounded_max_output_tokens(999),
            diagnostic.MAX_LIVE_PROBE_OUTPUT_TOKENS,
        )

    def test_live_probe_can_use_legacy_max_tokens_parameter(self) -> None:
        captured: dict[str, object] = {}

        class FakeConnection:
            def __init__(self, *_: object, **__: object) -> None:
                pass

            def request(
                self,
                method: str,
                path: str,
                body: str,
                headers: dict[str, str],
            ) -> None:
                captured["body"] = body

            def getresponse(self) -> "FakeResponse":
                return FakeResponse(
                    200,
                    {},
                    {"choices": [{"message": {"content": "OK"}}]},
                )

            def close(self) -> None:
                pass

        config = diagnostic.AzureConfig(
            provider="azure",
            api_key="secret-key",
            endpoint="https://gastroagi.openai.azure.com/",
            deployment="legacy-deployment",
            api_version="2024-12-01-preview",
        )

        with patch.object(diagnostic, "HTTPSConnection", FakeConnection):
            result = diagnostic._run_probe(
                config,
                timeout=2,
                max_output_tokens=3,
                token_limit_parameter="max_tokens",
            )

        request_body = json.loads(str(captured["body"]))
        self.assertEqual(request_body["max_tokens"], 3)
        self.assertNotIn("max_completion_tokens", request_body)
        self.assertEqual(result["probe"]["token_limit_parameter"], "max_tokens")

    def test_live_probe_reports_api_compatibility_failure_without_retrying(self) -> None:
        class FakeConnection:
            def __init__(self, *_: object, **__: object) -> None:
                pass

            def request(self, *_: object, **__: object) -> None:
                pass

            def getresponse(self) -> "FakeResponse":
                return FakeResponse(
                    400,
                    {"apim-request-id": "bad-api-version"},
                    {
                        "error": {
                            "code": "BadRequest",
                            "type": "invalid_request_error",
                            "message": "Unsupported parameter: max_tokens",
                        }
                    },
                )

            def close(self) -> None:
                pass

        config = diagnostic.AzureConfig(
            provider="azure",
            api_key="secret-key",
            endpoint="https://gastroagi.openai.azure.com/",
            deployment="gpt-5.4",
            api_version="bad-version",
        )

        with patch.object(diagnostic, "HTTPSConnection", FakeConnection):
            result = diagnostic._run_probe(
                config,
                timeout=2,
                max_output_tokens=16,
                token_limit_parameter="max_tokens",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "invalid_request_or_api_compatibility")
        self.assertEqual(result["request_id"], "bad-api-version")
        self.assertFalse(result["retryable"])
        self.assertEqual(result["probe"]["prompt"], "non_clinical_connectivity_check")
        self.assertEqual(result["probe"]["token_limit_parameter"], "max_tokens")
        self.assertNotIn("secret-key", json.dumps(result))


class FakeResponse:
    def __init__(
        self,
        status: int,
        headers: dict[str, str],
        payload: dict[str, object],
    ) -> None:
        self.status = status
        self.headers = headers
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")
