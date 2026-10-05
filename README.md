# atc-ia

Personal AI ATC for MSFS 2024. Skeleton stage: the text loop, airport generator, prompt builder and fake sim work; the real sim link and voice are unverified drafts.

## Try it now (no sim, no key, no audio)
```
pip install -e .[dev]
pytest
python -m atc --airport KTST --airports-dir tests/fixtures/airports
```
Type calls like `ready for taxi`. Commands: `/freq 118.1` (tower), `/wind 270 10 1013`, `/air`, `/ground`, `/state`, `/quit`.
`KTST` is a synthetic test airport. Without an LLM key the replies come from a stub.

## Use a real model
```
set ATC_LLM_BASE_URL=https://openrouter.ai/api/v1
set ATC_LLM_API_KEY=...
set ATC_LLM_MODEL=<model name>
```
(any OpenAI-compatible endpoint works; use `export` instead of `set` outside Windows cmd)

## Make an airport
```
atc-gen --download          # once
atc-gen KXXX                # writes airports/KXXX.yaml, skips files you already edited
```

Scenario checks (works with the stub, or with a real model through the env vars above):
```
python -m atc.scenarios
```

Start with docs/PRE_IDE_CHECKLIST.md (what to verify on your PC yourself), then CLAUDE.md and docs/ROADMAP.md.
