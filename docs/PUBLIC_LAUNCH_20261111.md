# GitHub Public source launch — 2026-11-11

**Status: Public developer preview (0.1.0a2), not production-certified.**

- Owner explicitly requested Public transition in this conversation on 2026-11-11.
- Generated company templates still default to **Private / not authorized**. Public visibility of this source is recorded separately in `docs/release-readiness.json` and is never inherited as permission to publish a copied company repository.
- Initial reviewed commit: `a771c6c5d14170235c2c4d861aa5ec5c8e817087`.
- Before switching: synthetic tests **412 PASS**, Ruff PASS, MFA core audit PASS, Gitleaks PASS, full reachable source history and private identity denylist PASS with scope limits.
- The same initial source commit generated a verified deterministic archive (161 files) with SHA-256 `ee750d70e115f353b42ab42d6759ea1da44bea93698b41ea6f3ea8c9130e7a7a` (audit artifact, not public release attachment).
- GitHub API reported visibility **public** after switch, Actions **disabled**, default branch `main` matching the reviewed SHA.
- GitHub private vulnerability reporting enabled and read back `enabled=true`.
- Topics: `moneyforward`, `accounting`, `python`, `mcp`, `ai-agent`, `fintech`, `bookkeeping`, `idempotency`, `open-source`.
- Expert contribution tasks are Issues #4–#8, with `help wanted` labels, and one `good first issue`.
- Public profile/owner handle remains visible. The project does **not** claim full anonymity, provider endorsement or membership in any formal partner program.
- Official Money Forward's [AI-agent partner application](https://developers.biz.moneyforward.com/ai-agent/) requires truthful name/company/email/consents. No fabricated information or personally identifying contact was sent.
- Original private production repositories, credentials, source receipts, accounting/employee data and Git history were not incorporated.

## What is explicitly unverified

- Independent users' actual Money Forward account canary and authenticated API E2E.
- Full receipt/voucher read-back, all five DAILY workflows and complete Cross Audit + final notification.
- Payroll, attendance and monthly/annual-close unattended operation.

The original full production release gates stay in [PRODUCTION_GATES](PRODUCTION_GATES.md).
Public source is intended to attract community review and improvements, not to claim live financial software certification.
