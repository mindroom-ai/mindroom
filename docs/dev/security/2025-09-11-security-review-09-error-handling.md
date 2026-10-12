# MindRoom Security Review: Error Handling and Information Disclosure

Review of how the SaaS platform backend's error responses and logs can disclose internal details, first written in September 2025.
It keeps the findings still open when re-checked against the code on 2026-10-12.

## Raw exception text in error responses

Several instance-lifecycle failures put the caught exception's text in the HTTP 500 detail, so Kubernetes, database, and provider error text, which can name internal resources, constraints, or identifiers, reaches the client, including customers using the instance controls.
FastAPI keeps Python tracebacks out of production responses, so these exception strings are the remaining disclosure path; the fix is a stable public message with the detail kept in server logs.

## Log redaction is opt-in

Production log redaction is not enforced globally: modules that log through standard-library loggers bypass the sanitizing wrapper, so production logs can still contain the account IDs, emails, and tokens the wrapper would redact.
Server logs also keep full tracebacks from `logger.exception`, so log access control and retention, not redaction alone, limit what logs disclose.
