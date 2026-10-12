# Security Review: Input Validation & Injection Prevention

September 2025 review of input validation and injection risks in the SaaS platform backend and its operator scripts.
Core MindRoom runtime injection (Matrix messages, tools and skills, sandboxed execution) was not covered; its trust model is [security-posture.md](../../architecture/security-posture.md).
Findings that the current code fixes are omitted.

## Open

- The admin list endpoint interpolates the search parameter `q` into a PostgREST `or` filter, so filter syntax in `q`, such as commas or parentheses, can change or break the filter.
  This is filter injection, not SQL injection, because PostgREST still sends values as parameters; the exposure is limited to admins.
- Admin create and update endpoints write the raw JSON body to the table, so an admin request can set any column of the allowed resources; typed request models were recommended instead.
- `cluster/scripts/mindroom-cli.sh` interpolates its unquoted arguments into kubectl selectors, deployment names, and the provision JSON body, so it must only run with operator-typed input and is unsafe to reuse behind any untrusted input.
