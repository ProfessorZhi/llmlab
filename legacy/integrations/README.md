# Legacy integrations

Reference adapters from `integrations/` in the private `llm-lab` repository.

| File | Purpose |
|---|---|
| `strata.py` | Client for the Strata inference engine's OpenAI-compatible surface. |
| `openai_compatible.py` | Thin OpenAI-compatible chat client used by the console and scripts. |
| `feishu.py` | Feishu (Lark) message publishing helper. |
| `__init__.py` | Package marker. |

## Status

Reference only. The new backend owns the adapters it actually uses; these files
are kept because the request shapes and the error handling are still useful
documentation.

`feishu.py` contains no webhook URL or token — in the original repository those
were read from environment variables and configuration. Verify that before
lifting any code.
