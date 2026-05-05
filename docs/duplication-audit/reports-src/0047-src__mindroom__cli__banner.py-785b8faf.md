## Summary

No meaningful duplication found.
`src/mindroom/cli/banner.py` owns a single CLI banner renderer that combines MindRoom ASCII art, a red-pill-to-green character gradient, Matrix-themed tagline selection, a March 31 easter egg, and Rich `Panel` output.
Other source files use Rich panels or print MindRoom CLI text, but none duplicate this banner construction behavior.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
make_banner	function	lines 32-74	related-only	make_banner, _LOGO, _TAGLINES, Happy birthday Matrix, Color.from_rgb gradient, Text justify center, Panel border_style green, banner	src/mindroom/cli/main.py:156; src/mindroom/cli/main.py:414; src/mindroom/avatar_generation.py:274; src/mindroom/avatar_generation.py:277
```

## Findings

No real duplication found.

`src/mindroom/cli/main.py:156` and `src/mindroom/cli/main.py:414` call `make_banner`, so they are consumers of the canonical banner helper rather than duplicate implementations.

`src/mindroom/avatar_generation.py:274` prints generated avatar prompts inside a Rich `Panel` with `border_style="green"` at `src/mindroom/avatar_generation.py:277`.
That overlaps only in presentation primitives.
It does not duplicate the CLI banner's ASCII logo rendering, per-character RGB gradient, tagline override/random selection, or March 31 special-case behavior.

Searches for `_LOGO`, `_TAGLINES`, `Happy birthday, Matrix`, `Color.from_rgb`, `Text(justify="center")`, `Panel(... border_style="green")`, `random.choice`, and `make_banner` did not identify another implementation of the same behavior under `src`.

## Proposed Generalization

No refactor recommended.
Extracting a generic Rich panel helper would couple unrelated UI call sites and would not remove meaningful duplicated behavior.

## Risk/Tests

No production change is recommended, so behavior risk is none.
If `make_banner` changes later, focused tests should cover deterministic tagline overrides, March 31 tagline behavior, and returned Rich `Panel` content shape.
