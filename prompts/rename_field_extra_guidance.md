# RenameField generation constraints

Generate only source-backed Java field rename tests that can be consumed by the existing IDE runners.

1. Use `operation.type = "RenameField"` and `operation.target_kind = "field"`.
2. Set `target_field`, `target_symbol`, and `old_name` to the exact old field identifier.
3. Set `new_name` to a legal Java identifier. It may intentionally conflict only when required by the selected scenario.
4. Mark the declaration inline as `Type /* rename target field */ oldName = ...;`.
5. Keep the target field declaration unique and source-backed. Do not use a local variable, parameter, method, or class as the target.
6. Exercise at least one read or write of the target field from `main()` and print deterministic output.
7. Preserve the selected scenario's conflict mechanism; do not replace it with an unrelated name collision.
8. Use multiple source files and packages when a static-import or visibility scenario requires them.
9. Do not automatically generate S003 or S004. They require manual project-state fixtures for broken compilation units or binary/library declarations.
10. If an unsupported external setup is unavoidable, set `needs_human_review` to true and describe the exact setup in `notes`.
