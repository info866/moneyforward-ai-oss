# Publication operator checklist

**Historical procedure:** Public visibility was approved and completed on 2026-11-11. The repository remains an unofficial developer preview; it has not been deployed for production accounting or released as a certified product. Re-run these steps for future scope/visibility changes.

This document does not authorize a Public change or production deployment.

1. Obtain the owner's explicit authorization for the exact source commit and
   developer-preview scope. Confirm repo identity and no concurrent changes.
2. Re-run `make test test-cloud test-accounting audit`, history/private-denylist
   screening and Gitleaks on all refs. Review all GitHub Issues/PRs/comments,
   assets and commit metadata, not only the default branch.
3. From a clean committed checkout, run `make source-release`, verify its manifest
   with `python scripts/build_source_release.py --verify dist/source.tar.gz`,
   and compare the archive SHA-256 against the recorded receipt. Never package
   a local company copy with real configuration or its operational data.
4. Inspect the release notice: developer preview, own credentials only, no
   production certification, MIT notices and known gaps. No company names,
   private URLs or real-API results may be pasted into the announcement.
5. Only after the separate approval, change the existing repo's visibility.
   Do not rename/recreate the repo or import private historical objects.
6. For the public repo, enable GitHub Private Vulnerability Reporting and verify
   the Security → Report a vulnerability path. While Private this public-repo
   feature is not claimed active. GitHub Actions must remain disabled.
7. Read back public visibility, default branch SHA, security reporting and the
   published contents. A source release is not a production activation.

No automatic publication script is supplied. Package building and audit commands
have no GitHub write or provider effect. If the contents change, repeat the
checks against the new commit rather than reuse an old readiness result.

Official feature description:
https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository
