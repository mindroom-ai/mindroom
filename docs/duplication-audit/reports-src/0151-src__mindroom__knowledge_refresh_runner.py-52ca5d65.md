## Summary

No meaningful duplication found.
`src/mindroom/knowledge_refresh_runner.py` is an 8-line internal module entrypoint that delegates to `mindroom.knowledge.refresh_runner.main`.
The only related behavior is the same `raise SystemExit(main())` module-execution idiom in the implementation module, but the wrapper exists because refresh subprocesses are launched with `python -m mindroom.knowledge_refresh_runner`.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
MODULE_LEVEL	module	lines 1-8	related-only	knowledge_refresh_runner; from mindroom.knowledge.refresh_runner import main; raise SystemExit(main()); python -m mindroom.knowledge_refresh_runner	src/mindroom/knowledge/refresh_runner.py:230; src/mindroom/knowledge/refresh_runner.py:931; src/mindroom/knowledge/refresh_runner.py:950; tests/test_knowledge_manager.py:3991
```

## Findings

No real duplication requiring refactor.

Related-only:

- [src/mindroom/knowledge_refresh_runner.py](mindroom/src/mindroom/knowledge_refresh_runner.py:5) imports `main` from [src/mindroom/knowledge/refresh_runner.py](mindroom/src/mindroom/knowledge/refresh_runner.py:931) and exits through it when run as a module.
- [src/mindroom/knowledge/refresh_runner.py](mindroom/src/mindroom/knowledge/refresh_runner.py:950) has the same `if __name__ == "__main__": raise SystemExit(main())` idiom for direct execution of the implementation module.
- This is not meaningful duplicated functionality because [src/mindroom/knowledge/refresh_runner.py](mindroom/src/mindroom/knowledge/refresh_runner.py:230) explicitly launches subprocesses with `sys.executable -m mindroom.knowledge_refresh_runner`, and [tests/test_knowledge_manager.py](mindroom/tests/test_knowledge_manager.py:3991) asserts that module name.

Differences to preserve:

- The top-level wrapper module name is part of the subprocess contract.
- The implementation module owns argument parsing, stdin payload handling, async execution, logging, and exit status.

## Proposed Generalization

No refactor recommended.
Moving or removing this wrapper would make the subprocess module target less explicit and would require changing the tested subprocess launch contract without reducing active complexity.

## Risk/tests

Behavior risk is low if left unchanged.
Any future change to this file should run the knowledge refresh subprocess tests around `tests/test_knowledge_manager.py::test_refresh_knowledge_binding_in_subprocess_*` because they cover the `python -m mindroom.knowledge_refresh_runner` contract.
