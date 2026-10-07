# Extra Guidance For Refactoring Test Generation

The generated tests must be compatible with the existing automatic refactoring runners.

General constraints:

1. Every Java source file must compile before refactoring.
2. Every test must contain exactly one public class whose name matches the file name.
3. Every test must declare `package generated.refactoring.tests;`.
4. Every test must have a deterministic `public static void main(String[] args)` method.
5. Every test must print deterministic output before refactoring.
6. Avoid random numbers, current time, networking, file I/O, threads with nondeterministic output, and external libraries.
7. Include `// Refactoring operation:` and clear target comments.
8. Keep `operation.target_method`, `operation.target_symbol`, and `operation.target_location_hint` consistent with the Java code.
9. Do not generate tests that require user interaction.
10. Do not generate uncompilable boundary cases unless the scenario explicitly requires uncompilable input.

Inline Method specific constraints:

1. Mark the method or invocation that should be inlined.
2. If the target is a constructor, the constructor name must exactly match its declaring class name.
3. If using a helper class such as `Foo`, declare `class Foo { Foo() { ... } }`; do not put `Foo()` directly inside a differently named public class.
4. If using method references, keep the referenced method name and receiver obvious in comments.
5. For private member access scenarios, the original program must compile before refactoring.
