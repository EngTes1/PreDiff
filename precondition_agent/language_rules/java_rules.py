"""Java-specific syntactic and semantic rule tables.

The current Java path mostly relies on the JavaParser helper, but keeping these
rules separate gives us a clean place to tune Java behavior without touching
the pipeline orchestration.
"""

JAVA_DECISIVE_CONDITION_PATTERNS = [
    r"\b[A-Za-z_][A-Za-z0-9_.]*\s+instanceof\s+[A-Z][A-Za-z0-9_.<>?]*",
    r"!\s*\(\s*[A-Za-z_][A-Za-z0-9_.]*\s+instanceof\s+[A-Z][A-Za-z0-9_.<>?]*\s*\)",
    r"\b[A-Za-z_][A-Za-z0-9_.]*\s*(?:==|!=)\s*null\b",
    r"\breturn\s+(?:true|false)\b",
]

JAVA_LOW_VALUE_ANNOTATIONS = {
    "NotNull", "Nullable", "Override", "Nls", "DialogMessage",
}

JAVA_GUARD_METHOD_HINTS = {
    "check", "validate", "can", "should", "must", "isavailable", "isapplicable",
    "conflict", "verify",
}

JAVA_LOW_VALUE_METHODS = {
    "getName", "getText", "getContainingClass", "getContainingFile", "getProject",
    "hashCode", "equals", "toString",
}
