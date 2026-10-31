"""Semantic tagger: a second axis next to the triage bucket.

A mutant is tagged with a fault class (tenant, auth, time, transaction, pagination, ...) by explicit tokens
in the source text of its node, plus the classes its producing fact recorded on the mutant at emission (the
fact owns the site's meaning — no string parsing). Conservative: no token and no recorded class, no tag
(untagged mutants count only in the overall score). The token table is configurable per project through
``[tool.balamut.operators.semantic]``; the result is a ``class -> killed/total`` matrix over a campaign.
"""

from __future__ import annotations

from balamut.config import DEFAULT_SEMANTIC, SemanticPatterns
from balamut.core.mutants import Mutant


def semantic_classes(mutant: Mutant, patterns: SemanticPatterns = DEFAULT_SEMANTIC) -> list[str]:
    """Semantic classes of a mutant: by tokens of its node source plus the classes recorded at
    emission. Sorted, deduplicated."""
    classes = [cls for cls, tokens in patterns if any(token in mutant.original for token in tokens)]
    classes.extend(mutant.semantic)
    return sorted(set(classes))
