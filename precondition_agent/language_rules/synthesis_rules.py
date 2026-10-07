"""Normalization and over-inference filters for synthesis output."""

BANNED_DECISIVE_CONDITION_PATTERNS = [
    r"\bnon[- ]?empty\b",
    r"\bat least one\b",
    r"\bwithBackgroundProgress\b",
    r"\breadAction\b",
    r"\bwriteAction\b",
    r"\bwithContext\b",
    r"\bproject must be valid\b",
    r"\beditor must be valid\b",
]

NON_PRECONDITION_RESULT_TOKENS = {
    "at least one",
    "non-empty",
    "nonempty",
    "no extraction options found",
}

ENVIRONMENT_UI_TOKENS = {
    "editor must be valid",
    "project must be valid",
    "background progress",
    "readaction",
    "read action",
    "withbackgroundprogress",
    "with context",
    "show error hint",
    "showextracterrorhint",
    "ui feedback",
    "error feedback",
}

CORE_ANALYSIS_TOKENS = {
    "findalloptionstoextract",
    "extractexception",
    "canprocess",
    "isavailable",
    "check",
}

ERROR_MESSAGE_ONLY_TOKENS = {
    "valid error message",
    "meaningful error",
    "problem ranges",
    "complete information",
}

UNSUPPORTED_DEEP_DELEGATED_TOKENS = {
    "psiclass",
    "psifile",
    "ancestor chain",
    "targetclass",
    "class hierarchy",
    "class containers",
}

