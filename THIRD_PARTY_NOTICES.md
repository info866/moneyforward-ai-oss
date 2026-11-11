# Source and third-party notices

## First-party source

The `mf_phase12` implementation, per-company bootstrap, schema and tests were selected from the repository owner's pre-existing independent company starter. No source Git history, credentials, business records or production configuration was copied. The source snapshot and selected-file hashes are retained in a separate private audit record. This project remains private; the owner must review the final source rights and MIT licensing decision before public distribution.

No code from `knorq-ai/moneyforward-connector` was incorporated in this preparation. There is no runtime import of a private upstream repository.

## Dependencies (not vendored)

The default accounting core uses Python's standard library. `setuptools` is a build dependency. `pytest` and `ruff` are development dependencies; `mcp` is optional for the read-only sample server. PostgreSQL support is optional and needs `psycopg`; SQLite remains the default. External dependencies are installed separately under their own licenses. No Money Forward SDK source, browser binary, model weights, customer dataset or provider documentation was vendored.

A release requires a dependency/license inventory for the exact dependency set used. This notice is not a legal opinion and does not certify that every upstream source right has been cleared.

## Money Forward

Money Forward product names identify compatibility only. This project is not endorsed by Money Forward, Inc. Each user must comply with the provider's product contracts, API terms, available scopes and usage limits. Publishing client code does not redistribute API credentials or grant API access.
