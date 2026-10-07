"""Kotlin-specific syntactic and semantic filtering rules."""

KOTLIN_FEATURE_PATTERNS = [
    ("function_declaration", r"\b(?:override\s+|suspend\s+|private\s+|public\s+|internal\s+|protected\s+)*fun\s+"),
    ("property_declaration", r"\b(?:val|var)\s+[A-Za-z_][A-Za-z0-9_]*"),
    ("type_check", r"(?:\bis\b|!is\s+)\s+[A-Z][A-Za-z0-9_]*"),
    ("safe_cast", r"\bas\?\s+[A-Z][A-Za-z0-9_]*"),
    ("unsafe_cast", r"\bas\s+[A-Z][A-Za-z0-9_]*"),
    ("elvis", r"\?:"),
    ("elvis_return", r"\?:\s*return\b"),
    ("safe_call", r"\?\."),
    ("when", r"\bwhen\s*(?:\(|\{)"),
    ("lambda", r"\b[A-Za-z_][A-Za-z0-9_.]*\s*\{[^{}]*(?:->|\bit\b)"),
]

KOTLIN_DECISIVE_CONDITION_PATTERNS = [
    r"\b[A-Za-z_][A-Za-z0-9_.]*\s+!is\s+[A-Z][A-Za-z0-9_.<>?]*",
    r"\b[A-Za-z_][A-Za-z0-9_.]*\s+is\s+[A-Z][A-Za-z0-9_.<>?]*",
    r"\b[A-Za-z_][A-Za-z0-9_.]*\s+as\?\s+[A-Z][A-Za-z0-9_.<>?]*(?:\s*\?:\s*return\s+[^\n;}]*)?",
    r"\b[A-Za-z_][A-Za-z0-9_.]*\s*\?:\s*return\s+[^\n;}]*",
    r"\b[A-Za-z_][A-Za-z0-9_.]*\?\.[A-Za-z_][A-Za-z0-9_]*(?:\([^)]*\))?\s*(?:==|!=)\s*(?:true|false|null)",
    r"!\s*[A-Za-z_][A-Za-z0-9_.]*(?:\(\))?",
    r"\b[A-Za-z_][A-Za-z0-9_.]*\.(?:isEmpty|isNotEmpty)\s*(?:\(\))?",
    r"\bwhen\s*\([^)]*\)",
    r"(?:^|[{\s])(?:is|!is)\s+[A-Z][A-Za-z0-9_.<>?]*\s*->",
]

LOW_VALUE_KOTLIN_METHODS = {
    "emptyList", "listOf", "mutableListOf", "setOf", "mapOf", "sequenceOf",
    "orEmpty", "orNull", "toList", "toSet", "toMutableList",
    "isEmpty", "isNotEmpty",
    "message", "getMessage",
    "withBackgroundProgress", "readAction", "writeAction",
    "runReadAction", "runWriteAction", "withContext",
    "showExtractErrorHint", "showErrorHint", "showHint",
    "let", "run", "also", "apply",
}

LOW_VALUE_KOTLIN_TYPES = {
    "Editor", "Project", "InplaceExtractUtils", "JavaRefactoringBundle",
}

LOW_VALUE_KOTLIN_RECEIVERS = {
    "InplaceExtractUtils", "JavaRefactoringBundle",
}

KOTLIN_AST_FEATURE_PATTERNS = [
    ("type_check", r"(?:\bis\b|!is\s+)\s+[A-Z]"),
    ("safe_cast", r"\bas\?\s+[A-Z]"),
    ("unsafe_cast", r"\bas\s+[A-Z]"),
    ("elvis", r"\?:"),
    ("elvis_return", r"\?:\s*return\b"),
    ("safe_call", r"\?\."),
    ("when", r"\bwhen\s*(?:\(|\{)"),
    ("lambda", r"\b[A-Za-z_][A-Za-z0-9_.]*\s*\{[^{}]*(?:->|\bit\b)"),
]

KOTLIN_GUARD_METHOD_HINTS = {
    "check", "validate", "can", "should", "must", "resolve", "find",
    "extract", "available", "applicable", "conflict",
}
