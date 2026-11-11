---
name: mf-__COMPANY_SLUG__-accounting-router
description: __COMPANY_SLUG__ の独立した Money Forward 会計をChatGPTから実行するための単一受付。複数法人を混在させない。
---

## Single accounting entry per generated company

A generated company uses exactly **one** company-specific ChatGPT
accounting router and **one** company-bound accounting MCP.
If an authenticated full MCP is already present, reuse it and do not
start or publish `mcp_servers/company_accounting_server.py` as a second
connector. Template source is not an operational company endpoint.


# ChatGPT Money Forward — single-company router

This installed copy is for company slug `__COMPANY_SLUG__` only. Do not use another
company's MCP, runtime, DB, RAG, Keychain, OAuth, journal history, or Drive routes.
The actual legal name and authenticated office must be read from the copied,
ignored local profile and official provider response, not inferred from this
plugin's display name.

Read `docs/skills/chatgpt_project_shiwake.md` and
`docs/skills/company_resource_map.md` once when starting a task.
The first is the single business workflow authority for this company; the
second records locally verified, company-specific resource references.
Do not retrieve the entire repository for every item.

## Route the user's request

- 通常仕訳・新着 → DAILY: sales_invoice, payable_invoice,
  all_receipt_folders, bank_transfer, card. Create a separate run for
  each selected workflow, and verify exact scope before any write.
- 給与・賞与 → PAYROLL: payroll_bonus only, on explicit request.
- 月次締め・包括監査 → MONTHLY_CLOSE: read-only.

ChatGPT analyzes source evidence and makes accounting decisions. Existing
company-bound connectors/MCP/API handle authentic provider reads, approved
writes, idempotency records, official read-backs, and company-owned DB/RAG.
The local CLI is a maintenance/recovery tool, not a second ordinary business
orchestrator. A Mac may host Keychain/MCP/Tunnel; this does not relocate
ChatGPT decision-making to that Mac.

## Mandatory safety and completion

Fail closed on tenant/profile/office/credential/DB/RAG/Drive mismatch.
Missing mode-specific tools or authentication block only affected operations.
Never treat a plugin version, tool count, or optional metadata as a global
business gate. Do not bypass a denied write using another adapter.

One official Cross Audit and one durable final Google Chat receipt are
required for each independently completed DAILY workflow run (and for
each explicitly requested PAYROLL / MONTHLY_CLOSE run). A zero-item run must
not manufacture a journal or spam a completion Chat. Unknown external writes
must first be reconciled by official GET; completed operations must not be
reposted during resume. Use company-specific approved notification paths.

If a company already has a full, verified accounting MCP, reuse it rather
than installing a competing read-only MCP.

The bundled `mcp_servers/company_accounting_server.py` is read-only
foundation only. **It does not provide run_begin, journal writes, Cross Audit,
or notification delivery.** Those require the separately verified company
operation connector. Until those operations are demonstrably available, report
the affected workflow as not ready, never as completed. No deployment or
account registration occurs during bootstrap.
