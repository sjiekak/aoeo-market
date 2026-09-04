# CONTRIBUTING.md

## Coding Style

We aim to enforce coding style using a linter (`ruff`).

### Exceptions

- prefer raising an exception rather than returning a magic value
- prefer raising custom-defined exception types. Main modules should catch custom defined exceptions.
- raise built-in exceptions when they fully capture the situation

### Tests

- if a symbol is only used in tests, either define it in the same test module or define the symbol in a helper test module

### Misc

- do not mix importing module and importing module symbols of the same module (`import sys; from sys import argv`)
- a module should not leak internal details of another module

### Commits

- commits used [conventional commits convention](https://www.conventionalcommits.org/en/v1.0.0/) with a body
- commit body is a summary of changes and why they have been made
