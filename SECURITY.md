# Security policy (pre-release)

This repository is a **private staging area**, with no production deployment and no production credential access.

## Reporting

For a suspected vulnerability or sensitive data exposure, use an appropriate **private GitHub security reporting channel** to the repository owner where available. Do not open a public issue containing exploit details, credentials, actual Money Forward records, or personal information.

If a secret or sensitive record is found in any commit, **stop release and distribution**. Remove access, assess rotation/revocation where appropriate, and recreate the public candidate with a clean history; deleting a file in a later commit does not erase earlier objects or copies.

## Mandatory boundaries

- The owner and each eventual user hold their own Money Forward API/OAuth credentials outside Git.
- Do not log access/refresh tokens, authorization codes, raw financial documents, or full private HTTP responses.
- No public issue, automated test, external PR, or local fixture may access the owner's production Money Forward environment.
- All company/office permissions, irreversible writes, approvals, payroll changes, and financial reporting require explicit, correctly scoped authorization.
- Network timeout or an uncertain write result never authorizes an unverified retry.
- Publication is a separate owner-authorized step after the release checklist passes.

This policy does not certify that the source is safe to publish. It defines how the release should be reviewed.
