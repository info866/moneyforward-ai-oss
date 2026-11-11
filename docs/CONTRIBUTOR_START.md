# Contributor entrypoint

Start with the documented source checkout and local test command. The codebase is intentionally Money Forward-specific.

Useful small changes: reproduce a wrong source-to-journal decision using synthetic fixtures; correct workflow resume without duplicating already verified operations; improve readable exception handling while retaining provider read-back and tenant guards. Include a failing test before the fix and its passing result afterward.

Do not add cosmetic security tooling as a substitute for accounting completion. Do not weaken assertions or remove a live-readback requirement to make tests green. Record the exact baseline, input set, environment and distinction between MOCK / TESTED / LIVE / REPRODUCIBLE.

Current modules and their boundaries are described in `docs/SPECIFICATION.md`. Full cloud accounting integration remains an explicit open task; the read-only MCP sample does not implement it.
