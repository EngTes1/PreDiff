"""AST context block taxonomy used for retrieval and explanation."""

SEMANTIC_CONTEXT_BLOCK_KINDS = {
    "related_method",
    "extension_function",
    "top_level_function",
    "kotlin_guard_function",
    "property_declaration",
    "property_getter",
    "guard_condition_block",
    "delegation_block",
    "exception_handling_block",
    "return_effect_block",
    "call_container",
    "type_reference_method",
}

CONSTRUCTION_REASON_HINTS = {
    "light", "synthetic", "builder", "build", "prototype", "construct",
}

CONSTRUCTOR_REASON_HINTS = {
    "construct", "builder", "build", "synthetic", "light", "create",
}

METHOD_NAME_SCORE_HINTS = [
    ("context", 8),
    ("resolve", 8),
    ("prototype", 8),
    ("build", 7),
    ("light", 7),
    ("synthetic", 7),
    ("origin", 6),
    ("delegate", 6),
    ("accept", 5),
    ("extract", 8),
    ("available", 6),
    ("applicable", 6),
]

SUB_BLOCK_PATTERNS = {
    "guard_condition_block": [
        r"\bif\s*\(",
        r"\bwhen\s*\(",
        r"\binstanceof\b",
        r"\bis\s+[A-Z]",
        r"\b!is\s+[A-Z]",
        r"\bas\?\s+[A-Z]",
        r"\b==\s*null\b",
        r"\b!=\s*null\b",
        r"\?\.?[A-Za-z_][A-Za-z0-9_]*\s*(?:==|!=)\s*(?:true|false|null)",
    ],
    "delegation_block": [
        r"\bcheck[A-Za-z0-9_]*\s*\(",
        r"\bvalidate[A-Za-z0-9_]*\s*\(",
        r"\bfind[A-Za-z0-9_]*\s*\(",
        r"\bresolve[A-Za-z0-9_]*\s*\(",
        r"\bcollect[A-Za-z0-9_]*\s*\(",
        r"\bbuild[A-Za-z0-9_]*\s*\(",
    ],
    "exception_handling_block": [
        r"\bcatch\s*\(",
        r"\bthrow\s+",
    ],
    "return_effect_block": [
        r"\breturn\s+false\b",
        r"\breturn\s+null\b",
        r"\breturn\s+emptyList\s*\(",
        r"\?:\s*return\b",
    ],
}
