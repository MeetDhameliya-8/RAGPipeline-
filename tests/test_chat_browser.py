import json
import re
import threading
import time

import pytest
from playwright.sync_api import sync_playwright, expect

from src.local_rag_chat import LocalRagChatHandler, ThreadingHTTPServer


@pytest.fixture()
def live_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), LocalRagChatHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _ndjson(events):
    return "\n".join(json.dumps(event) for event in events) + "\n"


def test_browser_chat_controls_streaming_errors_duplicates_and_history(live_server):
    requests = []
    delayed_request_seen = {"value": False}

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()

        def route_chat(route):
            body = json.loads(route.request.post_data or "{}")
            requests.append(body)
            question = body.get("question", "")
            if question == "slow duplicate":
                delayed_request_seen["value"] = True
                time.sleep(0.8)
                route.fulfill(
                    status=200,
                    content_type="application/x-ndjson",
                    body=_ndjson([
                        {"type": "meta", "grounding_status": "ANSWERED_FROM_LOCAL_REVIEW_DB", "answer_mode": "test_stream"},
                        {"type": "delta", "text": "Slow"},
                        {"type": "delta", "text": " answer [1]"},
                        {"type": "done", "result": {"citation_details": []}},
                    ]),
                )
                return
            if question == "network fail":
                route.fulfill(status=500, content_type="application/json", body='{"error":"boom"}')
                return
            if question == "stop me":
                time.sleep(2.0)
                route.fulfill(
                    status=200,
                    content_type="application/x-ndjson",
                    body=_ndjson([
                        {"type": "meta", "grounding_status": "ANSWERED_FROM_LOCAL_REVIEW_DB", "answer_mode": "test_stream"},
                        {"type": "delta", "text": "Too late"},
                        {"type": "done", "result": {"citation_details": []}},
                    ]),
                )
                return
            route.fulfill(
                status=200,
                content_type="application/x-ndjson",
                body=_ndjson([
                    {"type": "meta", "grounding_status": "ANSWERED_FROM_LOCAL_REVIEW_DB", "answer_mode": "test_stream"},
                    {"type": "delta", "text": "Structured "},
                    {"type": "delta", "text": "answer [1]"},
                    {"type": "done", "result": {"citation_details": [{"number": 1, "reference": "Ref", "section": "Section", "source_file": "paper.pdf", "chunk_number": 1, "validation_status": "REVIEW_REQUIRED"}]}},
                ]),
            )

        page.route("**/chat-stream", route_chat)
        page.goto(f"{live_server}/bot")

        page.locator("#question").fill("Explain the PEPTIC trial.")
        page.locator("#askButton").click()
        expect(page.get_by_text("Structured answer")).to_be_visible(timeout=5000)
        expect(page.get_by_text("References (1)")).to_be_visible()
        assert requests[-1]["history"] == []

        page.evaluate("""
            document.querySelector('#question').value = 'slow duplicate';
            document.querySelector('#askButton').click();
            document.querySelector('#askButton').click();
        """)
        expect(page.get_by_text("Slow answer")).to_be_visible(timeout=5000)
        assert [request["question"] for request in requests].count("slow duplicate") == 1
        assert delayed_request_seen["value"] is True

        page.locator("#question").fill("network fail")
        page.locator("#askButton").click()
        expect(page.locator(".error-note").filter(has_text="Network request failed with status 500.")).to_be_visible(timeout=5000)
        expect(page.locator("#question")).to_have_value("network fail")

        page.locator("#newChat").click()
        page.locator("#question").fill("After reset")
        page.locator("#askButton").click()
        expect(page.get_by_text("Structured answer")).to_be_visible(timeout=5000)
        assert requests[-1]["question"] == "After reset"
        assert requests[-1]["history"] == []

        page.once("dialog", lambda dialog: dialog.accept())
        page.locator("#clearChat").click()
        page.locator("#question").fill("After clear")
        page.locator("#askButton").click()
        expect(page.get_by_text("Structured answer")).to_be_visible(timeout=5000)
        assert requests[-1]["question"] == "After clear"
        assert requests[-1]["history"] == []

        page.evaluate("""() => {
            window.fetch = (_url, options = {}) => new Promise((_resolve, reject) => {
                if (options.signal) options.signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
            });
        }""")
        page.locator("#question").fill("stop me")
        page.locator("#askButton").click()
        expect(page.locator("#stopButton")).to_be_visible()
        page.locator("#stopButton").click()
        expect(page.get_by_text("Generation stopped.")).to_be_visible(timeout=5000)
        expect(page.locator("#stopButton")).to_be_hidden()

        browser.close()


def test_browser_renders_safe_adaptive_markdown_without_inline_citations_or_emphasis(live_server):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()

        def route_chat(route):
            route.fulfill(
                status=200,
                content_type="application/x-ndjson",
                body=_ndjson([
                    {
                        "type": "meta",
                        "grounding_status": "ANSWERED_FROM_LOCAL_REVIEW_DB",
                        "answer_mode": "test_stream",
                        "response_type": "drug_comparison",
                    },
                    {
                        "type": "delta",
                        "text": (
                            "### Comparison\n\n"
                            "| Treatment | Supported evidence |\n"
                            "| --- | --- |\n"
                            "| Infliximab | Reduced endoscopic recurrence [1] |\n"
                            "| Adalimumab | Maintenance outcomes were reported [2] |\n\n"
                            "1. Do **not** infer a head-to-head result. [1][2]\n\n"
                            "Legacy malformed marker [99] remains readable.\n\n"
                            "<script>window.__unsafe = true</script>"
                        ),
                    },
                    {
                        "type": "done",
                        "result": {
                            "response_type": "drug_comparison",
                            "citation_details": [
                                {"number": 1, "reference": "Ref A", "section": "Results A", "source_file": "a.pdf", "chunk_number": 1, "validation_status": "PASS"},
                                {"number": 1, "reference": "Duplicate Ref A", "section": "Duplicate", "source_file": "duplicate.pdf", "chunk_number": 99, "validation_status": "PASS"},
                                {"number": 2, "reference": "Ref B", "section": "Results B", "source_file": "b.pdf", "chunk_number": 2, "validation_status": "PASS"},
                            ],
                        },
                    },
                ]),
            )

        page.route("**/chat-stream", route_chat)
        page.goto(f"{live_server}/bot")
        page.evaluate("""() => {
            window.__copiedText = '';
            Object.defineProperty(navigator, 'clipboard', {
                configurable: true,
                value: { writeText: async value => { window.__copiedText = value; } }
            });
        }""")
        page.locator("#question").fill("Compare treatments [1]")
        page.locator("#askButton").click()

        expect(page.locator(".turn.user .msg").last).to_have_text("Compare treatments [1]")
        expect(page.locator(".msg h3").filter(has_text="Comparison")).to_be_visible()
        expect(page.locator(".msg table")).to_be_visible()
        expect(page.locator(".msg ol li")).to_have_count(1)
        expect(page.locator(".citation-marker")).to_have_count(0)
        expect(page.locator(".msg strong")).to_have_count(0)
        expect(page.locator(".source")).to_have_count(2)
        expect(page.get_by_text("References (2)")).to_be_visible()
        assert "[1]" not in page.locator(".turn.assistant").last.locator(".msg").inner_text()
        assert "[2]" not in page.locator(".turn.assistant").last.locator(".msg").inner_text()
        assert "Do not infer" in page.locator(".turn.assistant").last.locator(".msg").inner_text()
        expect(page.get_by_text("Legacy malformed marker remains readable.")).to_be_visible()
        assert page.locator(".msg script").count() == 0
        assert page.evaluate("window.__unsafe") is None

        page.locator(".turn.assistant").last.locator("button", has_text="Copy").click()
        copied = page.evaluate("window.__copiedText")
        assert "References:" in copied
        assert "[99]" not in copied
        assert "[1] Ref A" in copied
        assert "Results A" in copied
        assert "a.pdf; chunk 1" in copied
        assert "Duplicate Ref A" not in copied

        page.set_viewport_size({"width": 375, "height": 800})
        table_wrap = page.locator(".table-wrap")
        expect(table_wrap).to_be_visible()
        assert table_wrap.evaluate("node => node.scrollWidth > node.clientWidth") is True
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth") is True
        browser.close()


def test_browser_keeps_legacy_plain_text_readable_while_removing_numeric_citations(live_server):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()

        page.route(
            "**/chat-stream",
            lambda route: route.fulfill(
                status=200,
                content_type="application/x-ndjson",
                body=_ndjson([
                    {"type": "meta", "grounding_status": "LEGACY", "answer_mode": "legacy"},
                    {"type": "delta", "text": "Legacy answer with [1], malformed [x], and missing [7]."},
                    {"type": "done", "result": {"citation_details": []}},
                ]),
            ),
        )
        page.goto(f"{live_server}/bot")
        page.locator("#question").fill("Legacy response")
        page.locator("#askButton").click()

        expect(page.get_by_text("Legacy answer with, malformed [x], and missing.")).to_be_visible()
        expect(page.locator(".citation-marker")).to_have_count(0)
        expect(page.locator(".sources")).to_have_count(0)
        browser.close()








