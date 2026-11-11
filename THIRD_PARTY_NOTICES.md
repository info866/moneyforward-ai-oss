# Source and third-party notices

## First-party source

The `mf_phase12` implementation, per-company bootstrap, schema and tests were selected from the repository owner's pre-existing independent company starter. No source Git history, credentials, business records or production configuration was copied. The source snapshot and selected-file hashes are retained in a separate private audit record. This project remains private; the owner must review the final source rights and MIT licensing decision before public distribution.

No code from `knorq-ai/moneyforward-connector` was incorporated in this preparation. There is no runtime import of a private upstream repository.

## Dependencies (not vendored)

The default accounting core uses Python's standard library. `setuptools` is a build dependency. `pytest` and `ruff` are development dependencies; `mcp` is optional for the provider-read-only server and opt-in local metadata dispatch. PostgreSQL support is optional and needs `psycopg`; SQLite remains the default. External dependencies are installed separately under their own licenses. No Money Forward SDK source, browser binary, model weights, customer dataset or provider documentation was vendored.

A release requires a dependency/license inventory for the exact dependency set used. This notice is not a legal opinion and does not certify that every upstream source right has been cleared.

## Money Forward

Money Forward product names identify compatibility only. This project is not endorsed by Money Forward, Inc. Each user must comply with the provider's product contracts, API terms, available scopes and usage limits. Publishing client code does not redistribute API credentials or grant API access.


## Tested optional cloud environment — 2026-11-11

The following metadata was read from the installed test environment, not inferred
from package names. These packages are **not vendored**. The pins are in
`constraints-cloud.txt`; use `python -m pip install -c constraints-cloud.txt -e '.[test,cloud]'`.
This identifies the tested dependency set, not a guarantee for all OS/Python versions
or a comprehensive vulnerability audit of every dependency.

| Package | Tested version | Declared license |
| --- | --- | --- |
| `annotated-types` | 0.8.0 | MIT |
| `anyio` | 4.16.0 | MIT |
| `attrs` | 26.1.0 | MIT |
| `cffi` | 2.1.1 | MIT-0 |
| `click` | 8.5.1 | BSD-3-Clause |
| `cryptography` | 50.0.2 | Apache-2.0 OR BSD-3-Clause |
| `h11` | 0.16.0 | MIT |
| `httpcore2` | 2.16.0 | BSD-3-Clause |
| `httpx2` | 2.16.0 | BSD-3-Clause |
| `idna` | 3.20 | BSD-3-Clause |
| `iniconfig` | 2.3.1 | MIT |
| `jsonschema` | 4.26.0 | MIT |
| `jsonschema-specifications` | 2025.9.1 | MIT |
| `mcp` | 2.3.0 | MIT |
| `mcp-types` | 2.3.0 | MIT |
| `opentelemetry-api` | 1.45.2 | Apache-2.0 |
| `packaging` | 26.3 | Apache-2.0 OR BSD-2-Clause |
| `pluggy` | 1.7.0 | MIT |
| `pycparser` | 3.12 | BSD-3-Clause |
| `pydantic` | 2.14.3 | MIT |
| `pydantic_core` | 2.50.4 | MIT |
| `Pygments` | 2.21.0 | BSD-2-Clause |
| `PyJWT` | 2.15.1 | MIT |
| `pytest` | 8.4.2 | MIT |
| `python-multipart` | 0.0.33 | Apache-2.0 |
| `referencing` | 0.37.0 | MIT |
| `rpds-py` | 2026.9.1 | MIT |
| `ruff` | 0.17.4 | MIT |
| `sse-starlette` | 3.7.0 | BSD-3-Clause |
| `starlette` | 1.10.0 | BSD-3-Clause |
| `truststore` | 0.10.5 | MIT |
| `typing-inspection` | 0.4.5 | MIT |
| `typing_extensions` | 4.16.0 | PSF-2.0 |
| `uvicorn` | 0.54.0 | BSD-3-Clause |
