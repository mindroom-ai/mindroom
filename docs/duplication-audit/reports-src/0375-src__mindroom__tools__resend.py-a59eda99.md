Summary: `resend_tools` duplicates the repository-wide registered toolkit factory pattern used by many `src/mindroom/tools/*.py` modules. The closest domain duplication is `aws_ses_tools`, which exposes another transactional email sender with nearly the same `send_email` capability metadata and a matching lazy import/return factory. No production refactor is recommended from this module alone because `resend.py` is a small declarative adapter and the duplicated shape appears to be the standard registry convention.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
resend_tools	function	lines 56-60	duplicate-found	resend_tools ResendTools send_email enable_send_email from_email register_tool_with_metadata aws_ses_tools email_tools	function factory pattern checked at src/mindroom/tools/aws_ses.py:63, src/mindroom/tools/email.py:70, src/mindroom/tools/gmail.py:139, src/mindroom/tools/cartesia.py:77; email send metadata checked at src/mindroom/tools/aws_ses.py:13, src/mindroom/tools/email.py:13, src/mindroom/tools/gmail.py:20
```

Findings:

1. Registered toolkit factory pattern is repeated across tool modules.
   `src/mindroom/tools/resend.py:56` lazily imports `agno.tools.resend.ResendTools` inside `resend_tools` and returns the toolkit class.
   The same behavior appears in `src/mindroom/tools/aws_ses.py:63`, `src/mindroom/tools/email.py:70`, `src/mindroom/tools/gmail.py:139`, and many other tool modules: a metadata-decorated no-argument function returns a toolkit class while keeping the runtime import out of module import time.
   The functional intent is the same: register metadata at import time while deferring optional dependency imports until tool construction or lookup.
   Differences to preserve are the concrete toolkit class, import path, type annotation, docstring, and per-tool metadata.

2. Transactional email send-tool metadata overlaps most closely with AWS SES.
   `src/mindroom/tools/resend.py:13` and `src/mindroom/tools/aws_ses.py:13` both register email-category API-key tools with `ToolStatus.REQUIRES_CONFIG`, `SetupType.API_KEY`, an `enable_send_email` boolean defaulting to `True`, an `all` boolean defaulting to `False`, and `function_names=("send_email",)`.
   Both wrap provider-specific Agno email delivery toolkits.
   Differences to preserve are Resend's `api_key` and `from_email` fields, AWS SES's `sender_email`, `sender_name`, and `region_name` fields, dependencies, icons, descriptions, and docs URLs.
   `src/mindroom/tools/email.py:13` and `src/mindroom/tools/gmail.py:20` are related email tools, but their behavior differs enough to avoid treating them as the same duplication: SMTP exposes `email_user`, and Gmail is an OAuth read/write mailbox toolkit with many functions.

Proposed generalization:

No refactor recommended for `resend.py` alone.
If this duplication is addressed across the whole tools registry, the smallest useful helper would be a private metadata helper in `src/mindroom/tools/_metadata_helpers.py` or `src/mindroom/tool_system/metadata.py` that builds common `ConfigField` instances such as `enable_send_email` and `all`.
A broader factory abstraction for lazy-importing toolkit classes is not recommended unless many modules are changed at once, because the current explicit functions keep imports, annotations, docs, and registry metadata easy to inspect.

Risk/tests:

Any shared `ConfigField` helper would need tests that generated metadata remains byte-for-byte compatible for affected tools, especially `tools_metadata.json` generation and tool configuration UI field order.
For Resend and AWS SES, tests should verify `function_names`, default values for `enable_send_email` and `all`, dependencies, and docs URLs remain unchanged.
No production code was edited for this audit.
