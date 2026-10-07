package experiment.netbeans.runner;

import com.sun.source.tree.ClassTree;
import com.sun.source.tree.ExpressionTree;
import com.sun.source.tree.MethodTree;
import com.sun.source.tree.MethodInvocationTree;
import com.sun.source.tree.MemberReferenceTree;
import com.sun.source.tree.NewClassTree;
import com.sun.source.tree.VariableTree;
import com.sun.source.tree.Tree;
import com.sun.source.util.TreePath;
import com.sun.source.util.TreePathScanner;
import javax.lang.model.element.Element;
import javax.lang.model.element.ElementKind;
import javax.lang.model.element.Modifier;
import java.io.IOException;
import java.lang.reflect.Constructor;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.Collection;
import java.util.Collections;
import java.util.EnumMap;
import java.util.EnumSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicBoolean;
import javax.lang.model.SourceVersion;
import org.netbeans.api.java.source.CompilationController;
import org.netbeans.api.java.source.JavaSource;
import org.netbeans.api.java.source.ModificationResult;
import org.netbeans.api.java.source.SourceUtils;
import org.netbeans.api.java.source.Task;
import org.netbeans.api.java.source.TreePathHandle;
import org.netbeans.api.project.Project;
import org.netbeans.api.project.FileOwnerQuery;
import org.netbeans.api.project.ProjectManager;
import org.netbeans.api.project.ui.OpenProjects;
import org.netbeans.modules.refactoring.api.AbstractRefactoring;
import org.netbeans.modules.refactoring.api.MoveRefactoring;
import org.netbeans.modules.refactoring.api.Problem;
import org.netbeans.modules.refactoring.api.RefactoringSession;
import org.netbeans.modules.refactoring.java.api.InlineRefactoring;
import org.netbeans.modules.refactoring.java.api.JavaMoveMembersProperties;
import org.netbeans.modules.refactoring.api.RenameRefactoring;
import org.netbeans.modules.java.hints.introduce.IntroduceFixBase;
import org.netbeans.modules.java.hints.introduce.IntroduceHint;
import org.netbeans.modules.java.hints.introduce.IntroduceKind;
import org.netbeans.modules.java.hints.introduce.IntroduceMethodFix;
import org.netbeans.modules.parsing.api.Source;
import org.netbeans.modules.parsing.api.UserTask;
import org.netbeans.spi.editor.hints.ErrorDescription;
import org.netbeans.spi.editor.hints.Fix;
import org.openide.filesystems.FileObject;
import org.openide.filesystems.FileUtil;
import org.openide.cookies.SaveCookie;
import org.openide.loaders.DataObject;
import org.openide.modules.ModuleInstall;
import org.openide.util.NbBundle;
import org.openide.util.lookup.Lookups;

public class NetBeansRefactoringModule extends ModuleInstall {
    @Override
    public void restored() {
        Map<String, Object> result;
        Path output = Paths.get(System.getProperty("nb.refactor.output", "netbeans_refactoring_result.json")).toAbsolutePath();
        try {
            result = run();
        } catch (Throwable throwable) {
            result = baseResult("failed", false, "ERROR");
            addMessage(result, throwable.getClass().getName() + ": " + String.valueOf(throwable.getMessage()));
        }
        try {
            writeJson(output, result);
        } catch (IOException ignored) {
        } finally {
            System.exit(0);
        }
    }

    private Map<String, Object> run() throws Exception {
        String refactoring = requireProperty("nb.refactor.type");
        if (!"InlineMethod".equals(refactoring)
                && !"ExtractVariable".equals(refactoring)
                && !"ExtractMethod".equals(refactoring)
                && !"RenameMethod".equals(refactoring)
                && !"RenameField".equals(refactoring)
                && !"RenameVariable".equals(refactoring)
                && !"MoveInstanceMethod".equals(refactoring)) {
            Map<String, Object> result = baseResult("unsupported", false, "UNKNOWN");
            addMessage(result, "Unsupported refactoring: " + refactoring);
            return result;
        }
        Path javaFile = Paths.get(requireProperty("nb.refactor.javaFile")).toAbsolutePath().normalize();
        Path caseDir = Paths.get(requireProperty("nb.refactor.caseDir")).toAbsolutePath().normalize();
        String targetMethod = System.getProperty("nb.refactor.targetMethod", "");
        openProject(caseDir);
        FileObject fileObject = FileUtil.toFileObject(javaFile.toFile());
        if (fileObject == null) {
            Map<String, Object> result = baseResult("failed", false, "ERROR");
            addMessage(result, "Cannot resolve FileObject for " + javaFile);
            return result;
        }
        Project owner = FileOwnerQuery.getOwner(fileObject);
        if (owner == null) {
            Map<String, Object> result = baseResult("failed", false, "ERROR");
            addMessage(result, "NetBeans FileOwnerQuery does not associate the Java file with an open project: " + javaFile);
            return result;
        }

        JavaSource javaSource = JavaSource.forFileObject(fileObject);
        if (javaSource == null) {
            Map<String, Object> result = baseResult("failed", false, "ERROR");
            addMessage(result, "NetBeans JavaSource is not available for " + javaFile);
            return result;
        }

        if (refactoring.startsWith("Rename")) {
            String targetKind = System.getProperty("nb.refactor.targetKind", renameKindFromRefactoring(refactoring));
            String targetSymbol = System.getProperty("nb.refactor.targetSymbol", "");
            if (targetSymbol.isBlank()) {
                targetSymbol = targetMethod;
            }
            return runRename(javaSource, refactoring, targetKind, targetSymbol);
        }
        if ("ExtractVariable".equals(refactoring)) {
            return runExtractVariable(javaSource);
        }
        if ("ExtractMethod".equals(refactoring)) {
            return runExtractMethod(javaSource);
        }
        if ("MoveInstanceMethod".equals(refactoring)) {
            return runMoveInstanceMethod(javaSource, targetMethod);
        }

        int targetStart = integerProperty("nb.refactor.targetStart", -1);
        int targetLength = integerProperty("nb.refactor.targetLength", 0);
        TreePathHandle handle = findTarget(javaSource, targetMethod, targetStart, targetLength);
        if (handle == null) {
            Map<String, Object> result = baseResult("rejected", false, "ERROR");
            addMessage(result, "Cannot find inline target: " + targetMethod);
            return result;
        }

        InlineRefactoring inline = new InlineRefactoring(handle, InlineRefactoring.Type.METHOD);
        List<Problem> problems = new ArrayList<>();
        problems.add(inline.preCheck());
        if (firstFatal(problems) != null) {
            return rejectedFromProblems(problems);
        }
        problems.add(inline.fastCheckParameters());
        if (firstFatal(problems) != null) {
            return rejectedFromProblems(problems);
        }
        problems.add(inline.checkParameters());
        if (firstFatal(problems) != null) {
            return rejectedFromProblems(problems);
        }
        RefactoringSession session = RefactoringSession.create("NetBeans inline method");
        problems.add(inline.prepare(session));

        Problem fatal = firstFatal(problems);
        if (fatal != null) {
            return rejectedFromProblems(problems);
        }
        if (hasProblem(problems)) {
            return warningFromProblems(problems);
        }

        Problem applyProblem = session.doRefactoring(true);
        problems.add(applyProblem);
        fatal = firstFatal(problems);
        if (fatal != null) {
            Map<String, Object> result = baseResult("rejected", false, "FATAL");
            addProblems(result, problems);
            return result;
        }

        boolean hasWarning = hasProblem(problems);
        Map<String, Object> result = baseResult(hasWarning ? "warning" : "success", !hasWarning, hasWarning ? "WARNING" : "OK");
        addProblems(result, problems);
        if (((List<?>) result.get("messages")).isEmpty()) {
            addMessage(result, "NetBeans inline method refactoring applied.");
        }
        return result;
    }

    private Map<String, Object> runExtractVariable(JavaSource javaSource) throws Exception {
        int requestedStart = integerProperty("nb.refactor.targetStart", -1);
        int requestedLength = integerProperty("nb.refactor.targetLength", 0);
        String targetHint = System.getProperty("nb.refactor.targetHint", "");
        String newName = System.getProperty("nb.refactor.newName", "extractedValue").trim();
        boolean replaceAll = Boolean.parseBoolean(System.getProperty("nb.refactor.replaceAll", "false"));

        if (newName.isBlank()) {
            newName = "extractedValue";
        }
        if (!SourceVersion.isIdentifier(newName) || SourceVersion.isKeyword(newName)) {
            Map<String, Object> result = baseResult("rejected", false, "ERROR");
            addMessage(result, "Invalid extracted variable name: " + newName);
            return result;
        }

        final Fix[] selectedFix = new Fix[1];
        final String[] rejection = new String[1];
        final int[] selectedRange = new int[] {-1, -1};
        javaSource.runUserActionTask(new Task<CompilationController>() {
            @Override
            public void run(CompilationController controller) throws Exception {
                controller.toPhase(JavaSource.Phase.RESOLVED);
                TreePath target = findExtractVariableTarget(
                        controller, requestedStart, requestedLength, targetHint);
                if (target == null) {
                    rejection[0] = "ExtractVariable target expression not found at offset "
                            + requestedStart + " with length " + requestedLength + ".";
                    return;
                }
                long start = controller.getTrees().getSourcePositions().getStartPosition(
                        controller.getCompilationUnit(), target.getLeaf());
                long end = controller.getTrees().getSourcePositions().getEndPosition(
                        controller.getCompilationUnit(), target.getLeaf());
                if (start < 0 || end < start || end > Integer.MAX_VALUE) {
                    rejection[0] = "NetBeans could not resolve the source range for the selected expression.";
                    return;
                }
                selectedRange[0] = (int) start;
                selectedRange[1] = (int) end;

                Map<IntroduceKind, Fix> fixes = new EnumMap<>(IntroduceKind.class);
                Map<IntroduceKind, String> errors = new EnumMap<>(IntroduceKind.class);
                List<ErrorDescription> descriptions = IntroduceHint.computeError(
                        controller,
                        selectedRange[0],
                        selectedRange[1],
                        fixes,
                        errors,
                        new AtomicBoolean(false)
                );
                selectedFix[0] = fixes.get(IntroduceKind.CREATE_VARIABLE);
                if (selectedFix[0] == null) {
                    String message = errors.get(IntroduceKind.CREATE_VARIABLE);
                    if (message == null || message.isBlank()) {
                        List<String> details = new ArrayList<>();
                        for (ErrorDescription description : descriptions) {
                            if (description.getDescription() != null && !description.getDescription().isBlank()) {
                                details.add(description.getDescription());
                            }
                        }
                        message = details.isEmpty()
                                ? "NetBeans did not offer Introduce Variable for the selected expression."
                                : String.join("\n", details);
                    }
                    rejection[0] = localizeIntroduceMessage(message);
                }
            }
        }, true);

        if (selectedFix[0] == null) {
            Map<String, Object> result = baseResult("rejected", false, "ERROR");
            addMessage(result, rejection[0] == null
                    ? "NetBeans did not offer Introduce Variable for the selected expression."
                    : rejection[0]);
            return result;
        }
        if (!(selectedFix[0] instanceof IntroduceFixBase introduceFix)) {
            Map<String, Object> result = baseResult("failed", false, "ERROR");
            addMessage(result, "NetBeans returned an unexpected ExtractVariable fix type: "
                    + selectedFix[0].getClass().getName());
            return result;
        }

        ModificationResult modification = namedExtractVariableModification(
                introduceFix, replaceAll, newName);
        if (modification == null) {
            Map<String, Object> result = baseResult("rejected", false, "ERROR");
            addMessage(result, "NetBeans ExtractVariable produced no source modification.");
            return result;
        }
        commitAndSave(modification);
        Map<String, Object> result = baseResult("success", true, "OK");
        addMessage(result, "NetBeans extract variable refactoring applied at ["
                + selectedRange[0] + ", " + selectedRange[1] + ") as " + newName + ".");
        return result;
    }

    private Map<String, Object> runExtractMethod(JavaSource javaSource) throws Exception {
        int requestedStart = integerProperty("nb.refactor.targetStart", -1);
        int requestedLength = integerProperty("nb.refactor.targetLength", 0);
        String newName = System.getProperty("nb.refactor.newName", "extractedMethod").trim();
        boolean replaceDuplicates = Boolean.parseBoolean(
                System.getProperty("nb.refactor.replaceDuplicates", "false"));
        if (newName.isBlank()) {
            newName = "extractedMethod";
        }
        if (!SourceVersion.isIdentifier(newName) || SourceVersion.isKeyword(newName)) {
            Map<String, Object> result = baseResult("rejected", false, "ERROR");
            addMessage(result, "Invalid extracted method name: " + newName);
            return result;
        }
        if (requestedStart < 0 || requestedLength <= 0) {
            Map<String, Object> result = baseResult("rejected", false, "ERROR");
            addMessage(result, "ExtractMethod requires a non-empty source selection. selectionStart="
                    + requestedStart + ", selectionLength=" + requestedLength + ".");
            return result;
        }

        final Fix[] selectedFix = new Fix[1];
        final String[] rejection = new String[1];
        final int selectionEnd = requestedStart + requestedLength;
        javaSource.runUserActionTask(new Task<CompilationController>() {
            @Override
            public void run(CompilationController controller) throws Exception {
                controller.toPhase(JavaSource.Phase.RESOLVED);
                Map<IntroduceKind, Fix> fixes = new EnumMap<>(IntroduceKind.class);
                Map<IntroduceKind, String> errors = new EnumMap<>(IntroduceKind.class);
                List<ErrorDescription> descriptions = IntroduceHint.computeError(
                        controller,
                        requestedStart,
                        selectionEnd,
                        fixes,
                        errors,
                        new AtomicBoolean(false)
                );
                selectedFix[0] = fixes.get(IntroduceKind.CREATE_METHOD);
                String methodError = errors.get(IntroduceKind.CREATE_METHOD);
                // The statement-based path can report an error even when the expression-based path supplied a fix.
                if (selectedFix[0] == null) {
                    if (methodError == null || methodError.isBlank()) {
                        List<String> details = new ArrayList<>();
                        for (ErrorDescription description : descriptions) {
                            if (description.getDescription() != null && !description.getDescription().isBlank()) {
                                details.add(description.getDescription());
                            }
                        }
                        methodError = details.isEmpty()
                                ? "NetBeans did not offer Introduce Method for the selected code."
                                : String.join("\n", details);
                    }
                    rejection[0] = localizeIntroduceMessage(methodError);
                    selectedFix[0] = null;
                }
            }
        }, true);

        if (selectedFix[0] == null) {
            Map<String, Object> result = baseResult("rejected", false, "ERROR");
            addMessage(result, rejection[0] == null
                    ? "NetBeans did not offer Introduce Method for the selected code."
                    : rejection[0]);
            return result;
        }

        ModificationResult modification = namedExtractMethodModification(
                selectedFix[0], newName, replaceDuplicates);
        boolean usedDefaultNameFallback = false;
        if (modification != null && modification.getModifiedFileObjects().isEmpty()
                && selectedFix[0] instanceof IntroduceMethodFix introduceMethodFix) {
            // NetBeans exposes a public non-interactive path as well. Keep it as a
            // compatibility fallback when the dialog-equivalent task produces no edits.
            modification = introduceMethodFix.getModificationResult();
            usedDefaultNameFallback = modification != null
                    && !modification.getModifiedFileObjects().isEmpty();
        }
        if (modification == null) {
            Map<String, Object> result = baseResult("rejected", false, "ERROR");
            addMessage(result, "NetBeans ExtractMethod produced no source modification.");
            return result;
        }
        if (modification.getModifiedFileObjects().isEmpty()) {
            Map<String, Object> result = baseResult("failed", false, "ERROR");
            addMessage(result, "NetBeans ExtractMethod returned an empty ModificationResult.");
            return result;
        }
        commitAndSave(modification);
        if (usedDefaultNameFallback && !"method".equals(newName)) {
            Map<String, Object> renameResult = runRename(
                    javaSource, "RenameMethod", "method", "method");
            if (!Boolean.TRUE.equals(renameResult.get("applied"))) {
                addMessage(renameResult, "ExtractMethod created the default method name, but the follow-up "
                        + "NetBeans RenameMethod operation could not apply the requested name " + newName + ".");
                return renameResult;
            }
        }
        Map<String, Object> result = baseResult("success", true, "OK");
        addMessage(result, "NetBeans extract method refactoring applied at ["
                + requestedStart + ", " + selectionEnd + ") as " + newName + ".");
        return result;
    }

    private void commitAndSave(ModificationResult modification) throws Exception {
        Collection<? extends FileObject> modifiedFiles = modification.getModifiedFileObjects();
        modification.commit();
        for (FileObject modifiedFile : modifiedFiles) {
            DataObject dataObject = DataObject.find(modifiedFile);
            SaveCookie saveCookie = dataObject.getLookup().lookup(SaveCookie.class);
            if (saveCookie != null) {
                saveCookie.save();
            }
            modifiedFile.refresh();
        }
    }

    private ModificationResult namedExtractMethodModification(
            Fix fix,
            String newName,
            boolean replaceDuplicates
    ) throws Exception {
        if (fix instanceof IntroduceMethodFix introduceMethodFix) {
            Field targetsField = IntroduceMethodFix.class.getDeclaredField("targets");
            targetsField.setAccessible(true);
            Collection<?> targets = (Collection<?>) targetsField.get(introduceMethodFix);
            if (targets == null || targets.isEmpty()) {
                return null;
            }
            Object target = targets.iterator().next();
            Field sourceField = IntroduceFixBase.class.getDeclaredField("source");
            sourceField.setAccessible(true);
            Source source = (Source) sourceField.get(introduceMethodFix);
            Class<?> taskClass = Class.forName(
                    "org.netbeans.modules.java.hints.introduce.IntroduceMethodFix$TaskImpl");
            for (Constructor<?> constructor : taskClass.getDeclaredConstructors()) {
                if (constructor.getParameterCount() != 7) {
                    continue;
                }
                constructor.setAccessible(true);
                UserTask task = (UserTask) constructor.newInstance(
                        introduceMethodFix,
                        EnumSet.of(Modifier.PRIVATE),
                        newName,
                        target,
                        replaceDuplicates,
                        null,
                        false
                );
                return ModificationResult.runModificationTask(
                        Collections.singleton(source), task);
            }
            return null;
        }

        Field targetsField = fix.getClass().getDeclaredField("targets");
        targetsField.setAccessible(true);
        Collection<?> targets = (Collection<?>) targetsField.get(fix);
        if (targets == null || targets.isEmpty()) {
            return null;
        }
        Object target = targets.iterator().next();
        for (Method method : fix.getClass().getDeclaredMethods()) {
            if (!method.getName().equals("getModificationResult") || method.getParameterCount() != 6) {
                continue;
            }
            method.setAccessible(true);
            return (ModificationResult) method.invoke(
                    fix,
                    newName,
                    target,
                    replaceDuplicates,
                    EnumSet.of(Modifier.PRIVATE),
                    false,
                    null
            );
        }
        return fix instanceof IntroduceFixBase introduceFix
                ? introduceFix.getModificationResult()
                : null;
    }

    private TreePath findExtractVariableTarget(
            CompilationController controller,
            int requestedStart,
            int requestedLength,
            String targetHint
    ) {
        List<TreePath> expressions = new ArrayList<>();
        new TreePathScanner<Void, Void>() {
            @Override
            public Void scan(Tree tree, Void unused) {
                if (tree instanceof ExpressionTree) {
                    expressions.add(new TreePath(getCurrentPath(), tree));
                }
                return super.scan(tree, unused);
            }
        }.scan(controller.getCompilationUnit(), null);

        if (requestedLength > 0) {
            TreePath containing = null;
            long containingLength = Long.MAX_VALUE;
            long requestedEnd = (long) requestedStart + requestedLength;
            for (TreePath expression : expressions) {
                long start = controller.getTrees().getSourcePositions().getStartPosition(
                        controller.getCompilationUnit(), expression.getLeaf());
                long end = controller.getTrees().getSourcePositions().getEndPosition(
                        controller.getCompilationUnit(), expression.getLeaf());
                if (start == requestedStart && end - start == requestedLength) {
                    return expression;
                }
                if (start >= 0 && start <= requestedStart && requestedEnd <= end
                        && end - start < containingLength) {
                    containing = expression;
                    containingLength = end - start;
                }
            }
            return containing;
        }

        String hint = targetHint == null ? "" : targetHint.toLowerCase();
        TreePath best = null;
        long bestScore = Long.MAX_VALUE;
        for (TreePath expression : expressions) {
            long start = controller.getTrees().getSourcePositions().getStartPosition(
                    controller.getCompilationUnit(), expression.getLeaf());
            long end = controller.getTrees().getSourcePositions().getEndPosition(
                    controller.getCompilationUnit(), expression.getLeaf());
            if (start < 0 || end < start || (requestedStart >= 0 && start < requestedStart)) {
                continue;
            }
            long score = requestedStart < 0 ? start : start - requestedStart;
            Tree.Kind kind = expression.getLeaf().getKind();
            if (hint.contains("null literal") && kind == Tree.Kind.NULL_LITERAL) {
                score -= 1_000_000L;
            } else if (hint.contains("array initializer") && kind == Tree.Kind.NEW_ARRAY) {
                score -= 1_000_000L;
            } else if (hint.contains("assignment") && isAssignmentKind(kind)) {
                score -= 1_000_000L;
            }
            score = score * 10_000L + (end - start);
            if (best == null || score < bestScore) {
                best = expression;
                bestScore = score;
            }
        }
        return best;
    }

    private boolean isAssignmentKind(Tree.Kind kind) {
        return kind == Tree.Kind.ASSIGNMENT
                || kind == Tree.Kind.AND_ASSIGNMENT
                || kind == Tree.Kind.DIVIDE_ASSIGNMENT
                || kind == Tree.Kind.LEFT_SHIFT_ASSIGNMENT
                || kind == Tree.Kind.MINUS_ASSIGNMENT
                || kind == Tree.Kind.MULTIPLY_ASSIGNMENT
                || kind == Tree.Kind.OR_ASSIGNMENT
                || kind == Tree.Kind.PLUS_ASSIGNMENT
                || kind == Tree.Kind.REMAINDER_ASSIGNMENT
                || kind == Tree.Kind.RIGHT_SHIFT_ASSIGNMENT
                || kind == Tree.Kind.UNSIGNED_RIGHT_SHIFT_ASSIGNMENT
                || kind == Tree.Kind.XOR_ASSIGNMENT;
    }

    private ModificationResult namedExtractVariableModification(
            IntroduceFixBase fix,
            boolean replaceAll,
            String newName
    ) throws Exception {
        for (Method method : fix.getClass().getDeclaredMethods()) {
            if (!method.getName().equals("getModificationResult") || method.getParameterCount() != 5) {
                continue;
            }
            method.setAccessible(true);
            return (ModificationResult) method.invoke(fix, replaceAll, newName, false, false, null);
        }
        return fix.getModificationResult();
    }

    private String localizeIntroduceMessage(String message) {
        if (message == null || message.isBlank() || !message.matches("[A-Za-z][A-Za-z0-9_]+")) {
            return message;
        }
        try {
            return NbBundle.getMessage(IntroduceHint.class, message);
        } catch (RuntimeException ignored) {
            return message;
        }
    }

    private int integerProperty(String key, int defaultValue) {
        try {
            return Integer.parseInt(System.getProperty(key, String.valueOf(defaultValue)));
        } catch (NumberFormatException ignored) {
            return defaultValue;
        }
    }

    private Map<String, Object> runMoveInstanceMethod(JavaSource javaSource, String targetMethod) throws Exception {
        String targetSymbol = System.getProperty("nb.refactor.targetSymbol", "");
        String targetHint = System.getProperty("nb.refactor.targetHint", "");
        TreePathHandle methodHandle = findMethodDeclarationTarget(javaSource, targetMethod);
        if (methodHandle == null) {
            Map<String, Object> result = baseResult("rejected", false, "ERROR");
            addMessage(result, "Cannot find move instance method target method: " + targetMethod);
            return result;
        }
        TreePathHandle targetHandle = findTypeTarget(javaSource, targetSymbol, targetHint);
        if (targetHandle == null) {
            Map<String, Object> result = baseResult("rejected", false, "ERROR");
            addMessage(result, "Cannot find move instance method target class. targetSymbol=" + targetSymbol + " targetHint=" + targetHint);
            return result;
        }

        MoveRefactoring move = new MoveRefactoring(Lookups.fixed(methodHandle));
        move.setTarget(Lookups.fixed(targetHandle));
        JavaMoveMembersProperties properties = new JavaMoveMembersProperties(methodHandle);
        properties.setDelegate(false);
        properties.setAddDeprecated(false);
        properties.setUpdateJavaDoc(false);
        move.getContext().add(properties);

        List<Problem> problems = new ArrayList<>();
        problems.add(move.preCheck());
        if (firstFatal(problems) != null) {
            return rejectedFromProblems(problems);
        }
        problems.add(move.fastCheckParameters());
        if (firstFatal(problems) != null) {
            return rejectedFromProblems(problems);
        }
        problems.add(move.checkParameters());
        if (firstFatal(problems) != null) {
            return rejectedFromProblems(problems);
        }
        RefactoringSession session = RefactoringSession.create("NetBeans move members method");
        problems.add(move.prepare(session));

        Problem fatal = firstFatal(problems);
        if (fatal != null) {
            return rejectedFromProblems(problems);
        }
        if (hasProblem(problems)) {
            return warningFromProblems(problems);
        }

        Problem applyProblem = session.doRefactoring(true);
        problems.add(applyProblem);
        fatal = firstFatal(problems);
        if (fatal != null) {
            Map<String, Object> result = baseResult("rejected", false, "FATAL");
            addProblems(result, problems);
            return result;
        }

        boolean hasWarning = hasProblem(problems);
        Map<String, Object> result = baseResult(hasWarning ? "warning" : "success", !hasWarning, hasWarning ? "WARNING" : "OK");
        addProblems(result, problems);
        if (((List<?>) result.get("messages")).isEmpty()) {
            addMessage(result, "NetBeans move members method refactoring applied.");
        }
        return result;
    }

    private Map<String, Object> runRename(JavaSource javaSource, String refactoring, String targetKind, String targetSymbol) throws Exception {
        String newName = requireProperty("nb.refactor.newName");
        if (newName.isBlank()) {
            Map<String, Object> result = baseResult("rejected", false, "ERROR");
            addMessage(result, refactoring + " requires a non-empty new name.");
            return result;
        }
        TreePathHandle handle = findRenameTarget(javaSource, targetKind, targetSymbol);
        if (handle == null) {
            Map<String, Object> result = baseResult("rejected", false, "ERROR");
            addMessage(result, "Cannot find rename target: kind=" + targetKind + " symbol=" + targetSymbol);
            return result;
        }

        RenameRefactoring rename = new RenameRefactoring(Lookups.singleton(handle));
        rename.setNewName(newName);
        rename.setSearchInComments(false);
        List<Problem> problems = new ArrayList<>();
        problems.add(rename.preCheck());
        if (firstFatal(problems) != null) {
            return rejectedFromProblems(problems);
        }
        problems.add(rename.fastCheckParameters());
        if (firstFatal(problems) != null) {
            return rejectedFromProblems(problems);
        }
        problems.add(rename.checkParameters());
        if (firstFatal(problems) != null) {
            return rejectedFromProblems(problems);
        }
        RefactoringSession session = RefactoringSession.create("NetBeans rename");
        problems.add(rename.prepare(session));

        Problem fatal = firstFatal(problems);
        if (fatal != null) {
            return rejectedFromProblems(problems);
        }
        if (hasProblem(problems)) {
            return warningFromProblems(problems);
        }

        Problem applyProblem = session.doRefactoring(true);
        problems.add(applyProblem);
        fatal = firstFatal(problems);
        if (fatal != null) {
            Map<String, Object> result = baseResult("rejected", false, "FATAL");
            addProblems(result, problems);
            return result;
        }

        boolean hasWarning = hasProblem(problems);
        Map<String, Object> result = baseResult(hasWarning ? "warning" : "success", !hasWarning, hasWarning ? "WARNING" : "OK");
        addProblems(result, problems);
        if (((List<?>) result.get("messages")).isEmpty()) {
            addMessage(result, "NetBeans rename refactoring applied.");
        }
        return result;
    }

    private String renameKindFromRefactoring(String refactoring) {
        String value = refactoring == null ? "" : refactoring.toLowerCase();
        if (value.contains("method")) {
            return "method";
        }
        if (value.contains("field")) {
            return "field";
        }
        return "variable";
    }

    private Map<String, Object> rejectedFromProblems(List<Problem> problems) {
        Map<String, Object> result = baseResult("rejected", false, "FATAL");
        addProblems(result, problems);
        return result;
    }

    private Map<String, Object> warningFromProblems(List<Problem> problems) {
        Map<String, Object> result = baseResult("warning", false, "WARNING");
        addProblems(result, problems);
        return result;
    }

    private TreePathHandle findTarget(
            JavaSource javaSource,
            String targetMethod,
            int targetStart,
            int targetLength
    ) throws IOException {
        final TreePathHandle[] handle = new TreePathHandle[1];
        javaSource.runUserActionTask(new Task<CompilationController>() {
            @Override
            public void run(CompilationController controller) throws Exception {
                controller.toPhase(JavaSource.Phase.RESOLVED);
                new TreePathScanner<Void, Void>() {
                    @Override
                    public Void visitMethodInvocation(MethodInvocationTree node, Void unused) {
                        if (handle[0] == null
                                && node.getMethodSelect().toString().endsWith(targetMethod)
                                && selectionMatches(controller, node, targetStart, targetLength)) {
                            TreePath path = getCurrentPath();
                            handle[0] = TreePathHandle.create(path, controller);
                        }
                        return super.visitMethodInvocation(node, unused);
                    }

                    @Override
                    public Void visitNewClass(NewClassTree node, Void unused) {
                        if (handle[0] == null
                                && typeNameMatches(String.valueOf(node.getIdentifier()), targetMethod)
                                && selectionMatches(controller, node, targetStart, targetLength)) {
                            TreePath path = getCurrentPath();
                            handle[0] = TreePathHandle.create(path, controller);
                        }
                        return super.visitNewClass(node, unused);
                    }

                    @Override
                    public Void visitMemberReference(MemberReferenceTree node, Void unused) {
                        if (handle[0] == null
                                && targetMethod.equals(String.valueOf(node.getName()))
                                && selectionMatches(controller, node, targetStart, targetLength)) {
                            TreePath path = getCurrentPath();
                            handle[0] = TreePathHandle.create(path, controller);
                        }
                        return super.visitMemberReference(node, unused);
                    }
                }.scan(controller.getCompilationUnit(), null);
            }
        }, true);
        return handle[0];
    }

    private boolean selectionMatches(
            CompilationController controller,
            Tree node,
            int targetStart,
            int targetLength
    ) {
        if (targetStart < 0 || targetLength <= 0) {
            return true;
        }
        long start = controller.getTrees().getSourcePositions().getStartPosition(
                controller.getCompilationUnit(), node);
        long end = controller.getTrees().getSourcePositions().getEndPosition(
                controller.getCompilationUnit(), node);
        long targetEnd = (long) targetStart + targetLength;
        return (start <= targetStart && end >= targetEnd)
                || (targetStart <= start && targetEnd >= end);
    }

    private TreePathHandle findMethodDeclarationTarget(JavaSource javaSource, String targetMethod) throws IOException {
        final TreePathHandle[] handle = new TreePathHandle[1];
        javaSource.runUserActionTask(new Task<CompilationController>() {
            @Override
            public void run(CompilationController controller) throws Exception {
                controller.toPhase(JavaSource.Phase.RESOLVED);
                new TreePathScanner<Void, Void>() {
                    @Override
                    public Void visitMethod(MethodTree node, Void unused) {
                        if (handle[0] == null && targetMethod.equals(String.valueOf(node.getName()))) {
                            TreePath path = getCurrentPath();
                            handle[0] = TreePathHandle.create(path, controller);
                        }
                        return super.visitMethod(node, unused);
                    }
                }.scan(controller.getCompilationUnit(), null);
            }
        }, true);
        return handle[0];
    }

    private TreePathHandle findRenameTarget(JavaSource javaSource, String targetKind, String targetSymbol) throws IOException {
        final TreePathHandle[] handle = new TreePathHandle[1];
        String kind = targetKind == null ? "" : targetKind.toLowerCase();
        javaSource.runUserActionTask(new Task<CompilationController>() {
            @Override
            public void run(CompilationController controller) throws Exception {
                controller.toPhase(JavaSource.Phase.RESOLVED);
                new TreePathScanner<Void, Void>() {
                    @Override
                    public Void visitMethod(MethodTree node, Void unused) {
                        if (handle[0] == null && kind.equals("method") && targetSymbol.equals(String.valueOf(node.getName()))) {
                            handle[0] = TreePathHandle.create(getCurrentPath(), controller);
                        }
                        return super.visitMethod(node, unused);
                    }

                    @Override
                    public Void visitVariable(VariableTree node, Void unused) {
                        if (handle[0] != null || !targetSymbol.equals(String.valueOf(node.getName()))) {
                            return super.visitVariable(node, unused);
                        }
                        TreePath path = getCurrentPath();
                        Element element = controller.getTrees().getElement(path);
                        if (element == null) {
                            return super.visitVariable(node, unused);
                        }
                        ElementKind elementKind = element.getKind();
                        if (kind.equals("field") && elementKind == ElementKind.FIELD) {
                            handle[0] = TreePathHandle.create(path, controller);
                        } else if (kind.equals("variable") && elementKind != ElementKind.FIELD) {
                            handle[0] = TreePathHandle.create(path, controller);
                        }
                        return super.visitVariable(node, unused);
                    }
                }.scan(controller.getCompilationUnit(), null);
            }
        }, true);
        return handle[0];
    }

    private TreePathHandle findTypeTarget(JavaSource javaSource, String targetSymbol, String targetHint) throws IOException {
        List<String> tokens = moveTargetTokens(targetSymbol, targetHint);
        if (tokens.isEmpty()) {
            return null;
        }
        final TreePathHandle[] handle = new TreePathHandle[1];
        javaSource.runUserActionTask(new Task<CompilationController>() {
            @Override
            public void run(CompilationController controller) throws Exception {
                controller.toPhase(JavaSource.Phase.RESOLVED);
                new TreePathScanner<Void, Void>() {
                    @Override
                    public Void visitClass(ClassTree node, Void unused) {
                        if (handle[0] == null && classNameMatches(String.valueOf(node.getSimpleName()), tokens)) {
                            TreePath path = getCurrentPath();
                            handle[0] = TreePathHandle.create(path, controller);
                        }
                        return super.visitClass(node, unused);
                    }
                }.scan(controller.getCompilationUnit(), null);
            }
        }, true);
        return handle[0];
    }

    private boolean classNameMatches(String simpleName, List<String> tokens) {
        for (String token : tokens) {
            if (token.equals(simpleName)) {
                return true;
            }
            int dot = token.lastIndexOf('.');
            if (dot >= 0 && token.substring(dot + 1).equals(simpleName)) {
                return true;
            }
        }
        return false;
    }

    private List<String> moveTargetTokens(String targetSymbol, String targetHint) {
        List<String> tokens = new ArrayList<>();
        addMoveTargetToken(tokens, targetSymbol);
        if (targetHint != null && !targetHint.isBlank()) {
            java.util.regex.Matcher targetClass = java.util.regex.Pattern
                    .compile("(?i)target\\s+class\\s*:?\\s*([A-Za-z_$][\\w$]*(?:\\s*<[^>]+>)?)")
                    .matcher(targetHint);
            while (targetClass.find()) {
                addMoveTargetToken(tokens, targetClass.group(1));
            }
            java.util.regex.Matcher toMatcher = java.util.regex.Pattern
                    .compile("(?i)\\bto\\s+(?:class\\s+)?([A-Za-z_$][\\w$]*(?:\\s*<[^>]+>)?)")
                    .matcher(targetHint);
            while (toMatcher.find()) {
                addMoveTargetToken(tokens, toMatcher.group(1));
            }
        }
        return tokens;
    }

    private void addMoveTargetToken(List<String> tokens, String raw) {
        if (raw == null) {
            return;
        }
        String token = raw.trim().replaceAll("<.*>", "").replaceAll("[^A-Za-z0-9_$.]", "");
        if (!token.isBlank() && !tokens.contains(token)) {
            tokens.add(token);
        }
    }

    private boolean typeNameMatches(String typeName, String targetMethod) {
        if (typeName == null || targetMethod == null) {
            return false;
        }
        if (targetMethod.equals(typeName)) {
            return true;
        }
        int dot = typeName.lastIndexOf('.');
        return dot >= 0 && targetMethod.equals(typeName.substring(dot + 1));
    }

    private void openProject(Path caseDir) throws Exception {
        FileObject projectDirectory = FileUtil.toFileObject(caseDir.toFile());
        if (projectDirectory == null) {
            throw new IOException("Cannot resolve NetBeans project directory: " + caseDir);
        }
        Project project = ProjectManager.getDefault().findProject(projectDirectory);
        if (project == null) {
            throw new IOException("Cannot open generated NetBeans project: " + caseDir);
        }
        OpenProjects openProjects = OpenProjects.getDefault();
        openProjects.open(new Project[] { project }, false, true);
        openProjects.openProjects().get();
        openProjects.setMainProject(project);
        SourceUtils.waitScanFinished();
    }

    private Problem firstFatal(List<Problem> problems) {
        for (Problem problem : flatten(problems)) {
            if (problem.isFatal()) {
                return problem;
            }
        }
        return null;
    }

    private boolean hasProblem(List<Problem> problems) {
        return !flatten(problems).isEmpty();
    }

    private List<Problem> flatten(List<Problem> problems) {
        List<Problem> result = new ArrayList<>();
        for (Problem problem : problems) {
            Problem current = problem;
            while (current != null) {
                result.add(current);
                current = current.getNext();
            }
        }
        return result;
    }

    private Map<String, Object> baseResult(String status, boolean applied, String severity) {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("status", status);
        result.put("applied", applied);
        result.put("severity", severity);
        result.put("messages", new ArrayList<String>());
        result.put("changed_files", new ArrayList<String>());
        result.put("diff", "");
        return result;
    }

    @SuppressWarnings("unchecked")
    private void addMessage(Map<String, Object> result, String message) {
        ((List<String>) result.get("messages")).add(message);
    }

    private void addProblems(Map<String, Object> result, List<Problem> problems) {
        for (Problem problem : flatten(problems)) {
            addMessage(result, (problem.isFatal() ? "FATAL: " : "WARNING: ") + problem.getMessage());
        }
    }

    private String requireProperty(String key) {
        String value = System.getProperty(key);
        if (value == null || value.isBlank()) {
            throw new IllegalArgumentException("Missing required system property: " + key);
        }
        return value;
    }

    private void writeJson(Path output, Map<String, Object> result) throws IOException {
        Files.createDirectories(output.getParent());
        Files.writeString(output, toJson(result), StandardCharsets.UTF_8);
    }

    private String toJson(Object value) {
        if (value == null) {
            return "null";
        }
        if (value instanceof Boolean || value instanceof Number) {
            return String.valueOf(value);
        }
        if (value instanceof Map<?, ?> map) {
            StringBuilder builder = new StringBuilder();
            builder.append("{");
            boolean first = true;
            for (Map.Entry<?, ?> entry : map.entrySet()) {
                if (!first) {
                    builder.append(",");
                }
                first = false;
                builder.append(toJson(String.valueOf(entry.getKey())));
                builder.append(":");
                builder.append(toJson(entry.getValue()));
            }
            builder.append("}");
            return builder.toString();
        }
        if (value instanceof Iterable<?> iterable) {
            StringBuilder builder = new StringBuilder();
            builder.append("[");
            boolean first = true;
            for (Object item : iterable) {
                if (!first) {
                    builder.append(",");
                }
                first = false;
                builder.append(toJson(item));
            }
            builder.append("]");
            return builder.toString();
        }
        return "\"" + escapeJson(String.valueOf(value)) + "\"";
    }

    private String escapeJson(String text) {
        StringBuilder builder = new StringBuilder();
        for (int index = 0; index < text.length(); index++) {
            char ch = text.charAt(index);
            switch (ch) {
                case '\\':
                    builder.append("\\\\");
                    break;
                case '"':
                    builder.append("\\\"");
                    break;
                case '\n':
                    builder.append("\\n");
                    break;
                case '\r':
                    builder.append("\\r");
                    break;
                case '\t':
                    builder.append("\\t");
                    break;
                default:
                    builder.append(ch);
                    break;
            }
        }
        return builder.toString();
    }
}
