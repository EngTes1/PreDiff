"""Shared lightweight rule tables.

These constants keep filtering and normalization policy out of the pipeline
code, so Java/Kotlin adaptation can evolve without scattering magic strings.
"""

ORDINARY_CONTAINER_TYPES = {
    "String", "Boolean", "Int", "Long", "Short", "Byte", "Float", "Double",
    "Char", "Unit", "Any", "List", "MutableList", "Set", "Map", "Collection",
    "Array", "Sequence", "Iterable", "MultiMap",
}

CONTROL_FLOW_CALL_NAMES = {
    "if", "for", "while", "switch", "catch", "return", "new",
    "when", "let", "run", "also", "apply", "use",
}

JAVA_KOTLIN_KEYWORDS = {
    "public", "private", "protected", "class", "interface", "enum",
    "static", "final", "void", "boolean", "int", "long", "short", "byte",
    "float", "double", "char", "return", "new", "if", "else", "for", "while",
    "do", "switch", "case", "break", "continue", "try", "catch", "finally",
    "throw", "throws", "this", "super", "instanceof", "null", "true", "false",
    "fun", "val", "var", "when", "is", "as", "object", "in",
}

SEMANTIC_TYPE_NAME_HINTS = {
    "option", "descriptor", "conflict", "usage", "exception", "error",
}

SEMANTIC_REASON_HINTS = {
    "exception", "error", "conflict", "option", "core", "determines",
}

NULL_GUARD_PATTERNS = [
    r"\b[A-Za-z_][A-Za-z0-9_.]*\s*(?:==|!=)\s*null\b",
    r"\b[A-Za-z_][A-Za-z0-9_.]*\s*\?:\s*return\b",
]

BOOLEAN_RETURN_PATTERNS = [
    r"\breturn\s+true\b",
    r"\breturn\s+false\b",
]

DIRECT_GUARD_TOKENS = {
    "instanceof", "is", "!is", "as?", "?: return", "return false", "return null",
    "== null", "!= null", "when",
}

HELPER_NAME_HINTS = {
    "check", "validate", "can", "should", "must", "resolve", "find", "collect",
    "build", "create", "extract", "rename", "conflict", "available", "applicable",
}
