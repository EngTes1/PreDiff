import com.sun.source.tree.BinaryTree;
import com.sun.source.tree.ClassTree;
import com.sun.source.tree.CompilationUnitTree;
import com.sun.source.tree.ConditionalExpressionTree;
import com.sun.source.tree.DoWhileLoopTree;
import com.sun.source.tree.EnhancedForLoopTree;
import com.sun.source.tree.ForLoopTree;
import com.sun.source.tree.IfTree;
import com.sun.source.tree.InstanceOfTree;
import com.sun.source.tree.MemberReferenceTree;
import com.sun.source.tree.MethodInvocationTree;
import com.sun.source.tree.MethodTree;
import com.sun.source.tree.NewClassTree;
import com.sun.source.tree.ReturnTree;
import com.sun.source.tree.Tree;
import com.sun.source.tree.TypeCastTree;
import com.sun.source.tree.VariableTree;
import com.sun.source.tree.WhileLoopTree;
import com.sun.source.util.JavacTask;
import com.sun.source.util.SourcePositions;
import com.sun.source.util.TreeScanner;
import com.sun.source.util.Trees;

import javax.tools.JavaCompiler;
import javax.tools.JavaFileObject;
import javax.tools.StandardJavaFileManager;
import javax.tools.ToolProvider;
import javax.lang.model.element.Modifier;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

public class JavaAstParserCli {
    public static void main(String[] args) throws Exception {
        if (args.length != 1) {
            System.err.println("Usage: JavaAstParserCli <java-file>");
            System.exit(2);
        }

        Path sourceFile = Path.of(args[0]).toAbsolutePath().normalize();
        JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
        if (compiler == null) {
            System.err.println("System Java compiler is not available.");
            System.exit(2);
        }

        try (StandardJavaFileManager fileManager = compiler.getStandardFileManager(null, null, StandardCharsets.UTF_8)) {
            Iterable<? extends JavaFileObject> fileObjects = fileManager.getJavaFileObjects(sourceFile.toFile());
            JavacTask task = (JavacTask) compiler.getTask(
                null,
                fileManager,
                null,
                List.of("-proc:none"),
                null,
                fileObjects
            );

            Iterable<? extends CompilationUnitTree> parsedUnits = task.parse();
            Trees trees = Trees.instance(task);
            CompilationUnitTree unit = parsedUnits.iterator().next();
            String sourceText = Files.readString(sourceFile, StandardCharsets.UTF_8);

            AstCollector collector = new AstCollector(sourceFile.toString(), sourceText, unit, trees);
            collector.scan(unit, null);
            System.out.println(collector.toJson());
        }
    }

    private static final class AstCollector extends TreeScanner<Void, Void> {
        private final String path;
        private final String sourceText;
        private final CompilationUnitTree unit;
        private final Trees trees;
        private final SourcePositions sourcePositions;
        private final List<TypeRecord> types = new ArrayList<>();
        private final List<MethodRecord> methods = new ArrayList<>();
        private final Deque<String> ownerStack = new ArrayDeque<>();

        private AstCollector(String path, String sourceText, CompilationUnitTree unit, Trees trees) {
            this.path = path;
            this.sourceText = sourceText;
            this.unit = unit;
            this.trees = trees;
            this.sourcePositions = trees.getSourcePositions();
        }

        @Override
        public Void visitClass(ClassTree node, Void unused) {
            String name = node.getSimpleName() == null ? "" : node.getSimpleName().toString();
            TypeRecord record = new TypeRecord();
            record.name = name;
            record.kind = node.getKind().name();
            record.startLine = lineOf(node, true);
            record.endLine = lineOf(node, false);
            record.headerEndLine = lineOfOffset(findHeaderEndOffset(node));
            record.modifiers.addAll(normalizeModifiers(node.getModifiers() == null ? Set.of() : node.getModifiers().getFlags()));
            if (node.getExtendsClause() != null) {
                String extendsType = normalizeTypeName(node.getExtendsClause().toString());
                if (!extendsType.isBlank()) {
                    record.extendsTypes.add(extendsType);
                }
            }
            for (Tree implementClause : node.getImplementsClause()) {
                String implementType = normalizeTypeName(implementClause == null ? "" : implementClause.toString());
                if (!implementType.isBlank()) {
                    record.implementsTypes.add(implementType);
                }
            }
            types.add(record);

            ownerStack.push(name);
            super.visitClass(node, unused);
            ownerStack.pop();
            return null;
        }

        @Override
        public Void visitMethod(MethodTree node, Void unused) {
            MethodRecord record = new MethodRecord();
            record.ownerType = ownerStack.isEmpty() ? "" : ownerStack.peek();
            record.name = node.getName() == null ? "" : node.getName().toString();
            record.isConstructor = node.getReturnType() == null;
            record.startLine = lineOf(node, true);
            record.endLine = lineOf(node, false);
            record.modifiers.addAll(normalizeModifiers(node.getModifiers() == null ? Set.of() : node.getModifiers().getFlags()));

            if (node.getReturnType() != null) {
                record.returnType = normalizeTypeName(node.getReturnType().toString());
                record.typeRefs.add(record.returnType);
            }
            for (VariableTree parameter : node.getParameters()) {
                if (parameter.getType() != null) {
                    String parameterType = normalizeTypeName(parameter.getType().toString());
                    record.parameterTypes.add(parameterType);
                    record.typeRefs.add(parameterType);
                }
            }

            MethodBodyCollector bodyCollector = new MethodBodyCollector(record);
            bodyCollector.scan(node, null);
            methods.add(record);
            return null;
        }

        private int lineOf(Tree tree, boolean start) {
            long position = start
                ? sourcePositions.getStartPosition(unit, tree)
                : sourcePositions.getEndPosition(unit, tree);
            return lineOfOffset(position);
        }

        private int lineOfOffset(long position) {
            if (position < 0 || unit.getLineMap() == null) {
                return 1;
            }
            long line = unit.getLineMap().getLineNumber(position);
            return line > 0 ? (int) line : 1;
        }

        private long findHeaderEndOffset(ClassTree node) {
            long start = sourcePositions.getStartPosition(unit, node);
            long end = sourcePositions.getEndPosition(unit, node);
            if (start < 0 || end < 0) {
                return start;
            }
            int from = (int) Math.max(0, start);
            int to = (int) Math.min(sourceText.length(), end);
            int brace = sourceText.indexOf('{', from);
            if (brace >= 0 && brace < to) {
                return brace;
            }
            return start;
        }

        private String toJson() {
            StringBuilder sb = new StringBuilder();
            sb.append("{");
            sb.append("\"path\":").append(json(path)).append(",");
            sb.append("\"package\":").append(json(unit.getPackageName() == null ? "" : unit.getPackageName().toString())).append(",");
            sb.append("\"types\":[");
            for (int i = 0; i < types.size(); i++) {
                if (i > 0) {
                    sb.append(",");
                }
                types.get(i).appendJson(sb);
            }
            sb.append("],");
            sb.append("\"methods\":[");
            for (int i = 0; i < methods.size(); i++) {
                if (i > 0) {
                    sb.append(",");
                }
                methods.get(i).appendJson(sb);
            }
            sb.append("]}");
            return sb.toString();
        }
    }

    private static final class MethodBodyCollector extends TreeScanner<Void, Void> {
        private final MethodRecord record;

        private MethodBodyCollector(MethodRecord record) {
            this.record = record;
        }

        @Override
        public Void visitMethodInvocation(MethodInvocationTree node, Void unused) {
            record.calledMethods.add(extractMethodName(node.getMethodSelect().toString()));
            return super.visitMethodInvocation(node, unused);
        }

        @Override
        public Void visitMemberReference(MemberReferenceTree node, Void unused) {
            record.calledMethods.add(extractMethodName(node.getName().toString()));
            return super.visitMemberReference(node, unused);
        }

        @Override
        public Void visitInstanceOf(InstanceOfTree node, Void unused) {
            if (node.getType() != null) {
                String typeName = normalizeTypeName(node.getType().toString());
                record.instanceofTypes.add(typeName);
                record.typeRefs.add(typeName);
            }
            return super.visitInstanceOf(node, unused);
        }

        @Override
        public Void visitVariable(VariableTree node, Void unused) {
            if (node.getType() != null) {
                record.typeRefs.add(normalizeTypeName(node.getType().toString()));
            }
            return super.visitVariable(node, unused);
        }

        @Override
        public Void visitNewClass(NewClassTree node, Void unused) {
            if (node.getIdentifier() != null) {
                record.typeRefs.add(normalizeTypeName(node.getIdentifier().toString()));
            }
            return super.visitNewClass(node, unused);
        }

        @Override
        public Void visitTypeCast(TypeCastTree node, Void unused) {
            if (node.getType() != null) {
                String typeName = normalizeTypeName(node.getType().toString());
                record.castTypes.add(typeName);
                record.typeRefs.add(typeName);
            }
            return super.visitTypeCast(node, unused);
        }

        @Override
        public Void visitIf(IfTree node, Void unused) {
            if (node.getCondition() != null) {
                record.conditions.add(node.getCondition().toString());
            }
            return super.visitIf(node, unused);
        }

        @Override
        public Void visitConditionalExpression(ConditionalExpressionTree node, Void unused) {
            if (node.getCondition() != null) {
                record.conditions.add(node.getCondition().toString());
            }
            return super.visitConditionalExpression(node, unused);
        }

        @Override
        public Void visitWhileLoop(WhileLoopTree node, Void unused) {
            if (node.getCondition() != null) {
                record.conditions.add(node.getCondition().toString());
            }
            return super.visitWhileLoop(node, unused);
        }

        @Override
        public Void visitDoWhileLoop(DoWhileLoopTree node, Void unused) {
            if (node.getCondition() != null) {
                record.conditions.add(node.getCondition().toString());
            }
            return super.visitDoWhileLoop(node, unused);
        }

        @Override
        public Void visitForLoop(ForLoopTree node, Void unused) {
            if (node.getCondition() != null) {
                record.conditions.add(node.getCondition().toString());
            }
            return super.visitForLoop(node, unused);
        }

        @Override
        public Void visitEnhancedForLoop(EnhancedForLoopTree node, Void unused) {
            if (node.getExpression() != null) {
                record.conditions.add(node.getExpression().toString());
            }
            return super.visitEnhancedForLoop(node, unused);
        }

        @Override
        public Void visitReturn(ReturnTree node, Void unused) {
            if (node.getExpression() != null && looksLikeBooleanExpression(node.getExpression().toString())) {
                record.conditions.add(node.getExpression().toString());
            }
            return super.visitReturn(node, unused);
        }

        @Override
        public Void visitBinary(BinaryTree node, Void unused) {
            if (looksLikeBooleanExpression(node.toString())) {
                record.conditions.add(node.toString());
            }
            return super.visitBinary(node, unused);
        }
    }

    private static final class TypeRecord {
        private String name = "";
        private String kind = "";
        private int startLine = 1;
        private int endLine = 1;
        private int headerEndLine = 1;
        private final Set<String> modifiers = new LinkedHashSet<>();
        private final Set<String> extendsTypes = new LinkedHashSet<>();
        private final Set<String> implementsTypes = new LinkedHashSet<>();

        private void appendJson(StringBuilder sb) {
            sb.append("{");
            sb.append("\"name\":").append(json(name)).append(",");
            sb.append("\"kind\":").append(json(kind)).append(",");
            sb.append("\"start_line\":").append(startLine).append(",");
            sb.append("\"end_line\":").append(endLine).append(",");
            sb.append("\"header_end_line\":").append(headerEndLine).append(",");
            appendStringSet(sb, "modifiers", modifiers);
            sb.append(",");
            appendStringSet(sb, "extends_types", extendsTypes);
            sb.append(",");
            appendStringSet(sb, "implements_types", implementsTypes);
            sb.append("}");
        }
    }

    private static final class MethodRecord {
        private String ownerType = "";
        private String name = "";
        private boolean isConstructor = false;
        private int startLine = 1;
        private int endLine = 1;
        private String returnType = "";
        private final List<String> parameterTypes = new ArrayList<>();
        private final Set<String> modifiers = new LinkedHashSet<>();
        private final Set<String> calledMethods = new LinkedHashSet<>();
        private final Set<String> typeRefs = new LinkedHashSet<>();
        private final Set<String> instanceofTypes = new LinkedHashSet<>();
        private final Set<String> castTypes = new LinkedHashSet<>();
        private final Set<String> conditions = new LinkedHashSet<>();

        private void appendJson(StringBuilder sb) {
            sb.append("{");
            sb.append("\"owner_type\":").append(json(ownerType)).append(",");
            sb.append("\"name\":").append(json(name)).append(",");
            sb.append("\"is_constructor\":").append(isConstructor).append(",");
            sb.append("\"start_line\":").append(startLine).append(",");
            sb.append("\"end_line\":").append(endLine).append(",");
            sb.append("\"return_type\":").append(json(returnType)).append(",");
            appendStringList(sb, "parameter_types", parameterTypes);
            sb.append(",");
            appendStringSet(sb, "modifiers", modifiers);
            sb.append(",");
            appendStringSet(sb, "called_methods", calledMethods);
            sb.append(",");
            appendStringSet(sb, "type_refs", typeRefs);
            sb.append(",");
            appendStringSet(sb, "instanceof_types", instanceofTypes);
            sb.append(",");
            appendStringSet(sb, "cast_types", castTypes);
            sb.append(",");
            appendStringSet(sb, "conditions", conditions);
            sb.append("}");
        }
    }

    private static void appendStringSet(StringBuilder sb, String key, Set<String> values) {
        sb.append("\"").append(key).append("\":[");
        int index = 0;
        for (String value : values) {
            if (index > 0) {
                sb.append(",");
            }
            sb.append(json(value));
            index++;
        }
        sb.append("]");
    }

    private static void appendStringList(StringBuilder sb, String key, List<String> values) {
        sb.append("\"").append(key).append("\":[");
        for (int i = 0; i < values.size(); i++) {
            if (i > 0) {
                sb.append(",");
            }
            sb.append(json(values.get(i)));
        }
        sb.append("]");
    }

    private static String extractMethodName(String raw) {
        if (raw == null || raw.isBlank()) {
            return "";
        }
        int parenIndex = raw.indexOf('(');
        if (parenIndex >= 0) {
            raw = raw.substring(0, parenIndex);
        }
        int dotIndex = raw.lastIndexOf('.');
        if (dotIndex >= 0 && dotIndex + 1 < raw.length()) {
            return raw.substring(dotIndex + 1).trim();
        }
        return raw.trim();
    }

    private static String normalizeTypeName(String raw) {
        if (raw == null || raw.isBlank()) {
            return "";
        }
        String cleaned = raw.trim();
        int genericIndex = cleaned.indexOf('<');
        if (genericIndex >= 0) {
            cleaned = cleaned.substring(0, genericIndex);
        }
        int dotIndex = cleaned.lastIndexOf('.');
        if (dotIndex >= 0 && dotIndex + 1 < cleaned.length()) {
            cleaned = cleaned.substring(dotIndex + 1);
        }
        return cleaned.replace("...", "").trim();
    }

    private static List<String> normalizeModifiers(Set<Modifier> raw) {
        List<String> modifiers = new ArrayList<>();
        if (raw == null || raw.isEmpty()) {
            return modifiers;
        }
        for (Modifier modifier : raw) {
            if (modifier != null) {
                String normalized = modifier.toString().trim();
                if (!normalized.isBlank()) {
                    modifiers.add(normalized);
                }
            }
        }
        return modifiers;
    }

    private static boolean looksLikeBooleanExpression(String expression) {
        if (expression == null || expression.isBlank()) {
            return false;
        }
        return expression.contains("&&")
            || expression.contains("||")
            || expression.contains("instanceof")
            || expression.contains("==")
            || expression.contains("!=")
            || expression.startsWith("!")
            || "true".equals(expression)
            || "false".equals(expression);
    }

    private static String json(String value) {
        StringBuilder sb = new StringBuilder();
        sb.append('"');
        if (value != null) {
            for (int i = 0; i < value.length(); i++) {
                char ch = value.charAt(i);
                switch (ch) {
                    case '\\' -> sb.append("\\\\");
                    case '"' -> sb.append("\\\"");
                    case '\n' -> sb.append("\\n");
                    case '\r' -> sb.append("\\r");
                    case '\t' -> sb.append("\\t");
                    default -> {
                        if (ch < 0x20) {
                            sb.append(String.format("\\u%04x", (int) ch));
                        } else {
                            sb.append(ch);
                        }
                    }
                }
            }
        }
        sb.append('"');
        return sb.toString();
    }
}
