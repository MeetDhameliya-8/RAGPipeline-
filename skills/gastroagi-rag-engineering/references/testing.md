# Testing Reference

Tests should protect against silent clinical corruption and accidental workflow bypasses.

## Minimum Coverage Areas

Add focused tests for:

- extraction quality warnings and text cleaning
- citation metadata detection
- generator configuration failures
- structured JSON normalization
- validation hard failures
- output writing and no-move generation behavior
- duplicate/reference checks
- vector indexing gates, when vector storage exists
- API request/response validation, when FastAPI exists

## Test Style

Prefer small deterministic unit tests. Mock LLM providers and external services. Do not require real API keys in automated tests. Use temporary directories for file outputs. Keep medical fixture text short but clinically realistic enough to exercise the rule being tested.

## Verification

Run the narrowest useful test command after edits, then the broader suite when shared contracts changed. Report skipped or unavailable checks clearly.
