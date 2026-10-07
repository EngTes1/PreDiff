# RenameVariable generation constraints

Generate only Java local-variable rename tests that can be consumed by the existing IDE runners.

1. Use `operation.type = "RenameVariable"` and `operation.target_kind = "variable"`.
2. Set `target_variable`, `target_symbol`, and `old_name` to the exact old local-variable identifier.
3. Set `new_name` to the intended legal Java identifier. It may intentionally collide only when required by the selected scenario.
4. Mark the declaration inline as `Type /* rename target variable */ oldName = ...;`.
5. The target must be a local variable, including a for-loop initializer variable when required. Do not target fields, parameters, pattern variables, labels, or methods.
6. Keep the marked declaration unambiguous and include at least one observable use after it.
7. Every source program must compile and run before refactoring and print deterministic output.
8. Preserve the selected scenario's scope, field-shadowing, naming-warning, or for-loop mechanism instead of substituting a superficial rename.
