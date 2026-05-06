## Summary

The strongest duplication candidate is provider-specific email sending registration across `src/mindroom/tools/email.py`, `src/mindroom/tools/aws_ses.py`, and `src/mindroom/tools/resend.py`.
Each module declares nearly the same metadata shape for an outbound email toolkit, including `ToolCategory.EMAIL`, `ToolStatus.REQUIRES_CONFIG`, `SetupType.API_KEY`, sender/from configuration, an enable-send boolean, an `all` boolean, docs URL, and a one-method send function list.
The assigned `email_tools` symbol itself is only the standard lazy Agno class-returning provider function used throughout `src/mindroom/tools`; that function shape is related boilerplate, not a behavior-specific duplication worth extracting on its own.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
email_tools	function	lines 70-74	related-only	email_tools, EmailTools, ToolCategory.EMAIL, send_email, email_user, enable_send_email, sender_email	src/mindroom/tools/email.py:13, src/mindroom/tools/email.py:70, src/mindroom/tools/aws_ses.py:13, src/mindroom/tools/aws_ses.py:63, src/mindroom/tools/resend.py:13, src/mindroom/tools/resend.py:56, src/mindroom/tools/gmail.py:20, src/mindroom/tools/gmail.py:139, src/mindroom/tools/twilio.py:13, src/mindroom/tools/twilio.py:105, src/mindroom/tools/__init__.py:60, src/mindroom/tools/__init__.py:180
```

## Findings

### 1. Repeated outbound email toolkit registration metadata

- `src/mindroom/tools/email.py:13` registers SMTP email with `ToolCategory.EMAIL`, `ToolStatus.REQUIRES_CONFIG`, `SetupType.API_KEY`, sender/receiver config, `enable_email_user`, `all`, and `function_names=("email_user",)`.
- `src/mindroom/tools/aws_ses.py:13` registers AWS SES with the same category/status/setup type, sender config, `enable_send_email`, `all`, and `function_names=("send_email",)`.
- `src/mindroom/tools/resend.py:13` registers Resend with the same category/status/setup type, from/API key config, `enable_send_email`, `all`, and `function_names=("send_email",)`.

These are functionally related because all three expose one outbound email provider through the same tool metadata system and repeat the same configuration conventions for enabling send behavior.
The differences to preserve are provider names, icons, dependency lists, docs URLs, toolkit classes, provider-specific credential fields, and the SMTP toolkit's `email_user` naming instead of `send_email`.

### 2. Standard lazy toolkit provider boilerplate is widespread but low-value to extract

- `src/mindroom/tools/email.py:70` imports and returns `EmailTools`.
- `src/mindroom/tools/aws_ses.py:63` imports and returns `AWSSESTool`.
- `src/mindroom/tools/resend.py:56` imports and returns `ResendTools`.
- `src/mindroom/tools/gmail.py:139` imports and returns MindRoom's custom `GmailTools`.
- `src/mindroom/tools/twilio.py:105` follows the same shape for a communication toolkit.

The behavior is the same lazy class-returning registry adapter, but it is only a tiny two-line convention with type-specific imports.
Extracting it would likely reduce readability and complicate type checking.

## Proposed Generalization

A minimal future refactor could add small metadata helpers in a focused tool registration helper module, for example `src/mindroom/tools/_metadata_fields.py`.
The useful helper would be limited to shared `ConfigField` builders such as `optional_text_field`, `optional_password_field`, `enable_field`, and `all_field`, or a narrowly scoped `email_send_common_fields(sender_name=False, sender_email=False, all=True)` helper if broader field builders are considered too generic.

No refactor is recommended for the `email_tools` function itself.
The lazy provider function pattern is concise and explicit, and extracting it would add indirection for little maintenance gain.

## Risk/tests

The main risk in consolidating email metadata is silently changing config field names or defaults consumed by saved tool configuration.
Tests should verify exported tool metadata for `email`, `aws_ses`, and `resend`, especially field names, defaults, labels, `function_names`, dependencies, and docs URLs.
No production code was edited.
