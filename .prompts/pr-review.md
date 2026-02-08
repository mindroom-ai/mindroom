Review the pull request for:

- **Code cleanliness**: Is the implementation clean and well-structured?
- **DRY principle**: Does it avoid duplication?
- **Code reuse**: Are there parts that should be reused from other places?
- **Organization**: Is everything in the right place?
- **Consistency**: Is it in the same style as other parts of the codebase?
- **Simplicity**: Is it not over-engineered? Remember KISS and YAGNI. No dead code paths and NO defensive programming. No unnecessary try-excepts.
- **No pointless wrappers**: Identify functions/methods that just call another function and return its result. Callers should call the underlying function directly instead of going through unnecessary indirection.
- **Functional style**: Does it prefer functions over classes where appropriate? Are dataclasses used instead of raw dicts?
- **Imports**: Are all imports at the top of the file (not inside functions, unless avoiding circular imports)?
- **User experience**: Does it provide a good user experience?
- **PR**: Is the PR description and title clear and informative?
- **Tests**: Are there tests, and do they cover the changes adequately? Are they testing something meaningful or are they just trivial? Run `just test-backend` to verify.
- **Live tests**: If feasible, test the changes with a local Matrix stack (`just local-matrix-up`) and the Matty CLI to verify agent behavior end-to-end.
- **Rules**: Does the code follow the project's coding standards and guidelines as laid out in @CLAUDE.md?

Look at `git diff origin/main..HEAD` for the changes made in this pull request.
