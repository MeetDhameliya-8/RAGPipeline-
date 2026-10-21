# Clinical Assistant Reliability Upgrade

## Scope

This change improves the existing local Clinical Assistant without changing the PDF ingestion contract,
the five-field chunk schema, the local vector database schema, or the Azure OpenAI provider configuration.

## Confirmed root causes

- Greeting routing accepted only very short token sets, so capability questions entered clinical retrieval.
- Ambiguous follow-ups were expanded for retrieval, but relevance filtering still evaluated only the unresolved
  current sentence. This discarded the correctly retrieved prior topic.
- The phrase `current evidence` was treated as a publication-recency request, allowing a newer but less relevant
  article to outrank the requested clinical topic.
- Explicit windows such as `last 30 days` were not recognized or checked against exact publication dates.
- Off-topic routing covered only a narrow set of examples and missed consumer-device questions.
- Model output was accepted when it contained any numeric citation marker, even if that marker did not resolve to
  a retrieved source. Streaming could expose such text before final validation.
- The browser intro and transient status still exposed implementation-oriented language.

## Implementation

- Deterministic intent handling now covers greetings, capability questions, acknowledgements, common off-topic
  requests, evidence questions, follow-ups, comparisons, and explicit recency windows.
- Follow-up retrieval and relevance filtering share the same resolved query while the current question still
  controls requested evidence dimensions.
- Prompt history is bounded newest-first by a 600-token estimate and six messages.
- Exact recency windows are reported as unverified unless matching sources contain parseable publication dates.
  No external research search is performed.
- Unsupported breakthrough claims receive an explicit source-verification challenge.
- Every model citation number must resolve to the current retrieved-source map. Invalid non-streaming answers fall
  back to extractive evidence; streamed output is buffered until citation validation succeeds.
- New Chat and Clear remain client-local resets and send an empty history on the next request.

## Safety boundaries

- Clinical answers remain restricted to locally supplied chunks and retain human-review warnings.
- Conversation history helps interpret intent; it is not treated as verified medical evidence.
- No live Azure request, production database change, migration, external feed, or new paid dependency was added.
- A last-30-days request cannot be answered comprehensively without exact publication metadata and an authorized
  current-literature source.

## Verification

The acceptance suite covers the ten required conversations sequentially where context matters, irrelevant-source
rejection, missing evidence, invalid citation IDs in normal and streaming responses, and cross-session reset behavior.

## Milestone 2: Adaptive responses

The chat response keeps its existing `answer`, grounding, citation, retrieval, database, and evidence-scope fields.
It now adds two backward-compatible fields:

- `response_type`: deterministic semantic type such as `general_medical_explanation`, `drug_comparison`,
  `clinical_trial`, `clinical_reasoning`, `recent_research`, `follow_up`, or `unsupported_claim`.
- `presentation`: versioned metadata declaring the response as `safe_markdown` and naming its layout.

The response type controls prompt guidance and deterministic fallback formatting. It does not change retrieval,
weaken evidence gates, or authorize unsupported content. Comparison tables contain only retrieved findings; trial
sections omit unavailable study fields; reasoning lists remain source-bound; recent-research answers retain date
limitations.

The browser renderer constructs headings, lists, and tables with DOM APIs and `textContent`.
Raw HTML is never inserted from an answer. At the user's request, assistant answers omit visible numeric citation
markers and inline bold emphasis. Internal citation mappings remain intact; references are available in a collapsed
panel and appended to copied answers. User messages retain their literal text.

The 2026-10-15 stability audit also removed automatic citation insertion for uncited model answers. Model answers
must supply resolving citation IDs and pass the existing numeric and source-claim checks against their cited chunks
before release. Streamed output stays buffered until these checks pass; failures use deterministic excerpts.
