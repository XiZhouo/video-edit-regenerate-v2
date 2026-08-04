"""Neutral exact internal seal for mutation-policy coverage bookkeeping.

This is a same-process misuse boundary, not a sandbox against arbitrary Python
code: an extension can import this module too.  Keeping the shared identity in
a dependency-free leaf prevents persistence and command modules from importing
the Document implementation merely to coordinate framework-owned calls.
"""


MUTATION_POLICY_COVERAGE_CAPABILITY = object()
MUTATION_POLICY_COVERAGE_VALIDATED = object()


__all__ = [
    "MUTATION_POLICY_COVERAGE_CAPABILITY",
    "MUTATION_POLICY_COVERAGE_VALIDATED",
]
