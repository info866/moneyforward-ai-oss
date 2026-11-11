# Security policy

This is a developer preview, not a production-certified accounting service.
The maintainer has no shared service holding contributors' credentials.

## Reporting

Once this repository is public and private vulnerability reporting is enabled,
use GitHub **Security → Report a vulnerability** (the repository's private
advisory channel). The publication operator must enable and verify this channel
as described in docs/PUBLICATION_CHECKLIST.md. While the repository is Private,
access is limited to approved collaborators; the public-report feature is not
claimed enabled. Do not open an issue with exploit details or real data. If the
private channel is unavailable, request that it be enabled with a non-sensitive
message only; do not publish the vulnerability itself.

Never submit API keys, OAuth/Keychain material, real invoices, payroll, bank
records, office IDs, raw API responses or personal information. Reproduce with
synthetic data. If sensitive material is found, stop publication, contain the
exposure and assess revocation/rotation. Deleting a later file does not erase
old commits or downloaded copies.

## Operation boundaries

Each user owns their tenant, locally stored credentials and exact approvals.
Unknown-effect writes require provider reconciliation; do not retry blindly.
Source tests, source publication and MCP tool listing do not authorize business
operations or certify live readiness. Production gates remain in
`docs/PRODUCTION_GATES.md`. No SLA or audit certification is offered.
