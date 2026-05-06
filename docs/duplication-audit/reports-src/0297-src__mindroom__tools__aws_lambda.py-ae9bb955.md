# Duplication Audit: `src/mindroom/tools/aws_lambda.py`

## Summary

No meaningful duplication found for AWS Lambda-specific behavior.
The module follows the same registered Agno tool wrapper pattern used by many files in `src/mindroom/tools`, and it has AWS-adjacent metadata overlap with `aws_ses.py`, but there is no second implementation of Lambda listing or invocation behavior elsewhere under `./src`.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
aws_lambda_tools	function	lines 56-60	related-only	aws_lambda_tools, AWSLambdaTools, agno.tools.aws_lambda, LambdaTools, boto3, AWS, return *Tools	src/mindroom/tools/aws_lambda.py:56; src/mindroom/tools/aws_ses.py:63; src/mindroom/tools/redshift.py:145; src/mindroom/tools/calculator.py:27; src/mindroom/tools/webex.py:56; src/mindroom/tools/arxiv.py:56; src/mindroom/tools/__init__.py:31
```

## Findings

No real duplication requiring refactor.

`src/mindroom/tools/aws_lambda.py:56` is a small factory that lazily imports and returns `agno.tools.aws_lambda.AWSLambdaTools`.
This is related to the common MindRoom tool-registration wrapper pattern also present in `src/mindroom/tools/aws_ses.py:63`, `src/mindroom/tools/redshift.py:145`, `src/mindroom/tools/calculator.py:27`, `src/mindroom/tools/webex.py:56`, and `src/mindroom/tools/arxiv.py:56`.
The repeated behavior is generic adapter boilerplate: a metadata decorator around a function that returns the Agno toolkit class.
The metadata itself differs per tool, including category, setup type, dependencies, docs URL, and function names.

`src/mindroom/tools/aws_lambda.py:22` and `src/mindroom/tools/aws_ses.py:37` both define a `region_name` text config field with default `us-east-1`, and both use `dependencies=["boto3"]`.
This is AWS-adjacent metadata overlap, not duplicated Lambda behavior.
The two modules expose different Agno toolkit classes and different function surfaces: Lambda uses `invoke_function` and `list_functions`, while SES uses `send_email`.

## Proposed Generalization

No refactor recommended for this primary file.
The only duplicated behavior is repository-wide registration boilerplate for Agno toolkit wrappers.
A helper or decorator factory for these tiny modules could reduce lines, but it would also obscure per-tool metadata and is not justified by this single Lambda wrapper.

If the project later chooses to consolidate all Agno wrapper modules, the minimal target would be a shared helper in `src/mindroom/tool_system/metadata.py` or a small adjacent module that builds the standard lazy import factory from an import path and class name.
That broader change should be done across many tool modules at once and tested against tool registry loading.

## Risk/tests

Risk is low because no production code was changed.
If a future refactor consolidates the wrapper pattern, tests should cover tool metadata registration, lazy dependency imports, `src/mindroom/tools/__init__.py` exports, and runtime loading for configured tools with optional dependencies absent.
