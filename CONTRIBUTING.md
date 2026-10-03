# Contributing

Issues and pull requests are welcome. A few rules make them quick to accept.

## Before you start

For anything larger than a fix, open an issue first and say what you want to
change and why. The roadmap is in the [README](README.md#roadmap); the
[sandbox specification](docs/sandbox-tools.md) is the largest piece of it.

## The rules the code keeps

- **The API shape is not ours.** Both dialects follow the originals: Anthropic's
  official SDK types and OpenAI's published OpenAPI document. A change that makes
  a response differ from them is a defect, however convenient.
- **Refuse by name, never ignore.** A field or tool this service does not support
  fails the request with an error that names it.
- **Nothing of a caller outlives its invocation** — a provider key, a token, a
  vault credential. `tests/test_worker.py` checks it; keep it passing.
- **No secret in the repository, in a log line or in an event.**
- **Documents describe what is built.** A change of behaviour changes the document
  that describes it, in the same pull request.

## Running the tests

Python 3.12. Everything runs locally, without AWS and without a model key: the
model backend is `echo`, deterministic and free.

```bash
python3 -m venv .venv && .venv/bin/pip install -r src/requirements.txt -r tests/requirements.txt
```

Each suite is a script, as the release runs them:

```bash
for t in test_api test_fold test_store test_agents test_sessions test_worker test_model test_vault test_quality conformance_anthropic conformance_openai behaviour; do .venv/bin/python tests/$t.py || break; done
```

[docs/testing.md](docs/testing.md) explains the layers, and how to run the
suites against a deployment and the quality evals against real models — those
need your own provider keys and cost money.

## Pull requests

- One change per pull request, with the tests that show it works.
- Commit messages say what changed and why, in the present tense.
- By submitting a contribution you agree that it is licensed under the
  [Apache License 2.0](LICENSE), as the rest of the repository.
