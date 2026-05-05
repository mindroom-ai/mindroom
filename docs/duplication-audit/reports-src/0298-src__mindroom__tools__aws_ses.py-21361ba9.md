Summary: No meaningful duplication found in `src/mindroom/tools/aws_ses.py`.
The primary behavior is a metadata-registered lazy import wrapper for Agno's `AWSSESTool`; similar wrappers exist throughout `src/mindroom/tools`, but the AWS SES module has no SES-specific parsing, IO, validation, or API handling duplicated elsewhere.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
aws_ses_tools	function	lines 63-67	related-only	aws_ses_tools, AWSSESTool, agno.tools.aws_ses, boto3 AWS tool wrappers, send_email email tool wrappers	src/mindroom/tools/aws_ses.py:63, src/mindroom/tools/aws_lambda.py:56, src/mindroom/tools/email.py:70, src/mindroom/tools/resend.py:56, src/mindroom/tools/__init__.py:31, src/mindroom/tools/__init__.py:32, src/mindroom/tools/__init__.py:151, src/mindroom/tools/__init__.py:152
```

Findings:

No real duplication requiring refactor.

`aws_ses_tools` at `src/mindroom/tools/aws_ses.py:63` follows the repository's common tool-module pattern: a `register_tool_with_metadata(...)` decorator records UI/config/dependency metadata, and the function lazily imports and returns the Agno toolkit class.
`src/mindroom/tools/aws_lambda.py:56`, `src/mindroom/tools/email.py:70`, and `src/mindroom/tools/resend.py:56` use the same wrapper shape, but each wrapper points at a different Agno class and carries distinct metadata, config fields, dependencies, and function names.
This is related boilerplate, not duplicated domain behavior inside the primary file.

The closest related candidate is `src/mindroom/tools/aws_lambda.py:13`, which shares AWS branding metadata (`FaAws`, `text-orange-500`), a default `region_name` config field, and the `boto3` dependency.
The behavior still differs: Lambda is a development/serverless toolkit with `invoke_function` and `list_functions`; SES is an email toolkit with `send_email`, sender fields, and `REQUIRES_CONFIG` status.
A shared AWS metadata helper would save only a few lines and would make these small declarative modules less explicit.

Email-delivery modules are also only related.
`src/mindroom/tools/email.py:13` and `src/mindroom/tools/resend.py:13` share the email category and send-email intent, but their config fields and credential models differ from SES.
No repeated email sending implementation exists in MindRoom source; the behavior is delegated to separate Agno toolkits.

Proposed generalization: No refactor recommended.
The existing repetition is simple declarative registration metadata.
Introducing a helper for lazy-import wrappers or AWS/email metadata would add indirection without consolidating active duplicated behavior.

Risk/tests:

No code changes are recommended, so no tests are required for this audit.
If a future refactor centralizes tool registration boilerplate, tests should cover metadata export and toolkit loading for `aws_ses`, `aws_lambda`, `email`, and `resend` to ensure config fields, dependencies, and function names remain unchanged.
