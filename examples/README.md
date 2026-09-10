# Synthetic contract fixtures

These JSON files contain synthetic contract data, not verified model outputs. `create-case.json` can now be posted to the case API. `server-config.json` is a local setup template with no usable token; follow docs/backend.md to generate credentials outside Git. Document and reply assessments remain independent examples against snapshot version 1; do not apply them sequentially without refreshing the version. The referenced document and reply are fictional and are not uploaded assets.

Student 1 uses these for deterministic contract tests and as a starting point for fresh live analysis cases. Students 2 and 3 implement matching assessment outputs. Student 4 can use the case/run/review API and this snapshot fixture for isolated UI work. See ../docs/contracts.md for v0.5 states and validation rules, and ../docs/llm-runtime.md for mandatory live integration. These fixtures are not a substitute for provider calls or deployment tests.
