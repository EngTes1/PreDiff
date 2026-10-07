package experiment.eclipse.runner;

import java.io.IOException;
import java.io.PrintWriter;
import java.io.StringWriter;
import java.lang.reflect.Method;
import java.lang.reflect.Field;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardCopyOption;
import java.nio.file.attribute.BasicFileAttributes;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

import org.eclipse.core.resources.IFile;
import org.eclipse.core.resources.IFolder;
import org.eclipse.core.resources.IProject;
import org.eclipse.core.resources.IProjectDescription;
import org.eclipse.core.resources.IWorkspace;
import org.eclipse.core.resources.ResourcesPlugin;
import org.eclipse.core.runtime.CoreException;
import org.eclipse.core.runtime.IProgressMonitor;
import org.eclipse.core.runtime.NullProgressMonitor;
import org.eclipse.core.runtime.Platform;
import org.eclipse.core.runtime.preferences.IEclipsePreferences;
import org.eclipse.core.runtime.preferences.InstanceScope;
import org.eclipse.equinox.app.IApplication;
import org.eclipse.equinox.app.IApplicationContext;
import org.eclipse.jface.text.Document;
import org.eclipse.jface.text.IDocument;
import org.eclipse.jdt.core.IClasspathEntry;
import org.eclipse.jdt.core.ICompilationUnit;
import org.eclipse.jdt.core.IField;
import org.eclipse.jdt.core.IJavaElement;
import org.eclipse.jdt.core.IMethod;
import org.eclipse.jdt.core.IJavaProject;
import org.eclipse.jdt.core.ILocalVariable;
import org.eclipse.jdt.core.IPackageFragment;
import org.eclipse.jdt.core.IPackageFragmentRoot;
import org.eclipse.jdt.core.IType;
import org.eclipse.jdt.core.JavaModelException;
import org.eclipse.jdt.core.JavaCore;
import org.eclipse.jdt.core.dom.AST;
import org.eclipse.jdt.core.dom.ASTNode;
import org.eclipse.jdt.core.dom.ASTParser;
import org.eclipse.jdt.core.dom.ASTVisitor;
import org.eclipse.jdt.core.dom.CompilationUnit;
import org.eclipse.jdt.core.dom.ClassInstanceCreation;
import org.eclipse.jdt.core.dom.ExpressionMethodReference;
import org.eclipse.jdt.core.dom.Expression;
import org.eclipse.jdt.core.dom.IBinding;
import org.eclipse.jdt.core.dom.ITypeBinding;
import org.eclipse.jdt.core.dom.IVariableBinding;
import org.eclipse.jdt.core.dom.MethodInvocation;
import org.eclipse.jdt.core.dom.SuperMethodReference;
import org.eclipse.jdt.core.dom.SimpleName;
import org.eclipse.jdt.core.dom.TypeMethodReference;
import org.eclipse.jdt.core.dom.VariableDeclarationFragment;
import org.eclipse.jdt.core.dom.SingleVariableDeclaration;
import org.eclipse.jdt.core.manipulation.CodeStyleConfiguration;
import org.eclipse.jdt.core.manipulation.JavaManipulation;
import org.eclipse.jdt.internal.core.manipulation.JavaManipulationPlugin;
import org.eclipse.jdt.internal.core.manipulation.MembersOrderPreferenceCacheCommon;
import org.eclipse.jdt.internal.corext.codemanipulation.CodeGenerationSettings;
import org.eclipse.jdt.internal.corext.refactoring.code.InlineMethodRefactoring;
import org.eclipse.jdt.internal.corext.refactoring.code.ExtractMethodRefactoring;
import org.eclipse.jdt.internal.corext.refactoring.code.ExtractTempRefactoring;
import org.eclipse.jdt.internal.corext.refactoring.rename.MethodChecks;
import org.eclipse.jdt.internal.corext.refactoring.rename.JavaRenameProcessor;
import org.eclipse.jdt.internal.corext.refactoring.rename.RenameFieldProcessor;
import org.eclipse.jdt.internal.corext.refactoring.rename.RenameLocalVariableProcessor;
import org.eclipse.jdt.internal.corext.refactoring.rename.RenameMethodProcessor;
import org.eclipse.jdt.internal.corext.refactoring.rename.RenameNonVirtualMethodProcessor;
import org.eclipse.jdt.internal.corext.refactoring.rename.RenameVirtualMethodProcessor;
import org.eclipse.jdt.internal.corext.refactoring.structure.MoveInstanceMethodProcessor;
import org.eclipse.ltk.core.refactoring.Change;
import org.eclipse.ltk.core.refactoring.CompositeChange;
import org.eclipse.ltk.core.refactoring.Refactoring;
import org.eclipse.ltk.core.refactoring.RefactoringStatus;
import org.eclipse.ltk.core.refactoring.RefactoringStatusEntry;
import org.eclipse.ltk.core.refactoring.TextChange;
import org.eclipse.ltk.core.refactoring.TextFileChange;
import org.eclipse.ltk.core.refactoring.participants.CheckConditionsContext;
import org.eclipse.ltk.core.refactoring.participants.MoveRefactoring;
import org.eclipse.ltk.core.refactoring.participants.RenameRefactoring;
import org.eclipse.ltk.core.refactoring.participants.ResourceChangeChecker;
import org.eclipse.ltk.core.refactoring.participants.ValidateEditChecker;
import org.eclipse.text.edits.TextEdit;
import org.osgi.framework.Bundle;

public class EclipseRefactoringApplication implements IApplication {
    @Override
    public Object start(IApplicationContext context) throws Exception {
        String[] args = (String[]) context.getArguments().get(IApplicationContext.APPLICATION_ARGS);
        Map<String, String> options = parseArgs(args);
        Path output = requirePath(options, "output");
        Map<String, Object> result;
        try {
            initializeJavaManipulationPreferences();
            result = run(options);
        } catch (Throwable throwable) {
            result = baseResult("failed", false, "ERROR");
            addMessage(result, throwable.getClass().getName() + ": " + String.valueOf(throwable.getMessage()));
            addMessage(result, stackTrace(throwable));
        }
        writeJson(output, result);
        return IApplication.EXIT_OK;
    }

    @Override
    public void stop() {
    }

    private void initializeJavaManipulationPreferences() throws Exception {
        Bundle jdtUi = Platform.getBundle("org.eclipse.jdt.ui");
        if (jdtUi != null && jdtUi.getState() != Bundle.ACTIVE) {
            jdtUi.start(Bundle.START_TRANSIENT);
        }
        if (JavaManipulation.getPreferenceNodeId() == null) {
            JavaManipulation.setPreferenceNodeId(JavaManipulation.ID_PLUGIN);
        }
        IEclipsePreferences preferences = InstanceScope.INSTANCE.getNode(JavaManipulation.getPreferenceNodeId());
        if (preferences.get(CodeStyleConfiguration.ORGIMPORTS_IMPORTORDER, null) == null) {
            preferences.put(CodeStyleConfiguration.ORGIMPORTS_IMPORTORDER, "java;javax;org;com;");
        }
        if (preferences.get(CodeStyleConfiguration.ORGIMPORTS_ONDEMANDTHRESHOLD, null) == null) {
            preferences.put(CodeStyleConfiguration.ORGIMPORTS_ONDEMANDTHRESHOLD, "99");
        }
        if (preferences.get(CodeStyleConfiguration.ORGIMPORTS_STATIC_ONDEMANDTHRESHOLD, null) == null) {
            preferences.put(CodeStyleConfiguration.ORGIMPORTS_STATIC_ONDEMANDTHRESHOLD, "99");
        }
        if (preferences.get(MembersOrderPreferenceCacheCommon.APPEARANCE_MEMBER_SORT_ORDER, null) == null) {
            preferences.put(MembersOrderPreferenceCacheCommon.APPEARANCE_MEMBER_SORT_ORDER, "T,C,I,M,F,SI,SM,SF");
        }
        if (preferences.get(MembersOrderPreferenceCacheCommon.APPEARANCE_VISIBILITY_SORT_ORDER, null) == null) {
            preferences.put(MembersOrderPreferenceCacheCommon.APPEARANCE_VISIBILITY_SORT_ORDER, "B,V,R,D");
        }
        if (preferences.get(MembersOrderPreferenceCacheCommon.APPEARANCE_ENABLE_VISIBILITY_SORT_ORDER, null) == null) {
            preferences.put(MembersOrderPreferenceCacheCommon.APPEARANCE_ENABLE_VISIBILITY_SORT_ORDER, "false");
        }
        if (JavaManipulationPlugin.getDefault() != null) {
            JavaManipulationPlugin.getDefault().getMembersOrderPreferenceCacheCommon().install();
        }
    }

    private Map<String, Object> run(Map<String, String> options) throws Exception {
        String refactoring = require(options, "refactoring");
        if (!"InlineMethod".equals(refactoring)
            && !"ExtractVariable".equals(refactoring)
            && !"ExtractMethod".equals(refactoring)
            && !"MoveInstanceMethod".equals(refactoring)
            && !"RenameMethod".equals(refactoring)
            && !"RenameField".equals(refactoring)
            && !"RenameVariable".equals(refactoring)) {
            Map<String, Object> result = baseResult("unsupported", false, "UNKNOWN");
            addMessage(result, "Unsupported refactoring: " + refactoring);
            return result;
        }

        Path caseDir = requirePath(options, "case-dir");
        Path javaFile = requirePath(options, "java-file");
        IProgressMonitor monitor = new NullProgressMonitor();
        IProject project = importCaseProject(caseDir, monitor);
        Path projectDir = project.getLocation().toFile().toPath().toAbsolutePath().normalize();
        Path projectJavaFile = projectDir.resolve(caseDir.toAbsolutePath().normalize().relativize(javaFile.toAbsolutePath().normalize()));
        ICompilationUnit cu = compilationUnit(project, projectDir, projectJavaFile);
        CompilationUnit astRoot = parse(cu, monitor);
        Map<String, Object> result;
        if ("ExtractVariable".equals(refactoring)) {
            result = runExtractVariable(options, cu, astRoot, monitor);
        } else if ("ExtractMethod".equals(refactoring)) {
            result = runExtractMethod(options, cu, monitor);
        } else if ("MoveInstanceMethod".equals(refactoring)) {
            result = runMoveInstanceMethod(options, cu, astRoot, monitor);
        } else if (refactoring.startsWith("Rename")) {
            result = runRename(options, refactoring, cu, astRoot, monitor);
        } else {
            result = runInlineMethod(options, cu, astRoot, monitor);
        }
        if (Boolean.TRUE.equals(result.get("applied"))) {
            copyJavaFiles(projectDir, caseDir.toAbsolutePath().normalize());
            project.refreshLocal(IProject.DEPTH_INFINITE, monitor);
        }
        return result;
    }

    private Map<String, Object> runExtractMethod(
        Map<String, String> options,
        ICompilationUnit cu,
        IProgressMonitor monitor
    ) throws Exception {
        int requestedStart = integerOption(options, "target-start", -1);
        int requestedLength = integerOption(options, "target-length", 0);
        if (requestedStart < 0 || requestedLength <= 0) {
            Map<String, Object> result = baseResult("rejected", false, "ERROR");
            addMessage(result, "ExtractMethod requires a non-empty source selection. selectionStart="
                + requestedStart + ", selectionLength=" + requestedLength + ".");
            return result;
        }

        ExtractMethodRefactoring extract = new ExtractMethodRefactoring(
            cu,
            requestedStart,
            requestedLength
        );
        String newName = options.getOrDefault("new-name", "extractedMethod").trim();
        if (newName.isBlank()) {
            newName = "extractedMethod";
        }
        extract.setMethodName(newName);
        extract.setReplaceDuplicates(booleanOption(options, "replace-duplicates", false));
        extract.setGenerateJavadoc(false);

        RefactoringStatus status = new RefactoringStatus();
        status.merge(extract.checkInitialConditions(monitor));
        status.merge(extract.checkMethodName());
        return runRefactoring(extract, status, monitor,
            "Eclipse extract method refactoring applied at [" + requestedStart + ", "
                + (requestedStart + requestedLength) + ") as " + newName + ".");
    }

    private Map<String, Object> runExtractVariable(
        Map<String, String> options,
        ICompilationUnit cu,
        CompilationUnit astRoot,
        IProgressMonitor monitor
    ) throws Exception {
        int requestedStart = integerOption(options, "target-start", -1);
        int requestedLength = integerOption(options, "target-length", 0);
        String targetHint = options.getOrDefault("target-hint", "");
        Expression expression = findExtractVariableTarget(astRoot, requestedStart, requestedLength, targetHint);
        if (expression == null) {
            Map<String, Object> result = baseResult("rejected", false, "ERROR");
            addMessage(result, "Cannot find ExtractVariable target expression at offset " + requestedStart
                + " with length " + requestedLength + ".");
            return result;
        }

        ExtractTempRefactoring extract = new ExtractTempRefactoring(
            cu,
            expression.getStartPosition(),
            expression.getLength()
        );
        extract.setDeclareFinal(false);
        boolean replaceAll = booleanOption(options, "replace-all", false);
        extract.setReplaceAllOccurrences(replaceAll);
        extract.setReplaceAllOccurrencesInThisFile(false);

        RefactoringStatus status = new RefactoringStatus();
        status.merge(extract.checkInitialConditions(monitor));
        if (status.hasFatalError() || status.hasError()) {
            Map<String, Object> result = baseResult("rejected", false, severityName(status.getSeverity()));
            addStatusMessages(result, status);
            return result;
        }

        String newName = options.getOrDefault("new-name", "").trim();
        if (newName.isBlank()) {
            newName = extract.guessTempNameWithContext();
        }
        if (newName == null || newName.isBlank()) {
            newName = "extractedValue";
        }
        status.merge(extract.checkTempName(newName));
        if (status.hasFatalError() || status.hasError()) {
            Map<String, Object> result = baseResult("rejected", false, severityName(status.getSeverity()));
            addStatusMessages(result, status);
            return result;
        }
        extract.setTempName(newName);
        return runRefactoring(extract, status, monitor, "Eclipse extract variable refactoring applied.");
    }

    private Expression findExtractVariableTarget(
        CompilationUnit astRoot,
        int requestedStart,
        int requestedLength,
        String targetHint
    ) {
        List<Expression> candidates = new ArrayList<>();
        astRoot.accept(new ASTVisitor() {
            @Override
            public void preVisit(ASTNode node) {
                if (node instanceof Expression expression) {
                    candidates.add(expression);
                }
            }
        });
        if (requestedLength > 0) {
            for (Expression candidate : candidates) {
                if (candidate.getStartPosition() == requestedStart && candidate.getLength() == requestedLength) {
                    return candidate;
                }
            }
            // Keep the editor selection exact. Silently shrinking an invalid selection
            // to a nested expression would disagree with the interactive action.
            return null;
        }

        String hint = targetHint == null ? "" : targetHint.toLowerCase();
        Expression best = null;
        long bestScore = Long.MAX_VALUE;
        for (Expression candidate : candidates) {
            int start = candidate.getStartPosition();
            if (requestedStart >= 0 && start < requestedStart) {
                continue;
            }
            long score = requestedStart < 0 ? start : start - requestedStart;
            String kind = candidate.getClass().getSimpleName().toLowerCase();
            if (hint.contains("null literal") && kind.equals("nullliteral")) {
                score -= 1_000_000L;
            } else if (hint.contains("array initializer") && kind.equals("arrayinitializer")) {
                score -= 1_000_000L;
            } else if (hint.contains("assignment") && kind.equals("assignment")) {
                score -= 1_000_000L;
            }
            score = score * 10_000L + candidate.getLength();
            if (best == null || score < bestScore) {
                best = candidate;
                bestScore = score;
            }
        }
        return best;
    }

    private int integerOption(Map<String, String> options, String key, int defaultValue) {
        try {
            return Integer.parseInt(options.getOrDefault(key, String.valueOf(defaultValue)));
        } catch (NumberFormatException ignored) {
            return defaultValue;
        }
    }

    private boolean booleanOption(Map<String, String> options, String key, boolean defaultValue) {
        String value = options.get(key);
        return value == null ? defaultValue : Boolean.parseBoolean(value);
    }

    private Map<String, Object> runInlineMethod(
        Map<String, String> options,
        ICompilationUnit cu,
        CompilationUnit astRoot,
        IProgressMonitor monitor
    ) throws Exception {
        String targetMethod = require(options, "target-method");
        int requestedStart = integerOption(options, "target-start", -1);
        int requestedLength = integerOption(options, "target-length", 0);
        ASTNode target = findInlineTarget(astRoot, targetMethod, requestedStart, requestedLength);
        if (target == null) {
            Map<String, Object> result = baseResult("rejected", false, "ERROR");
            addMessage(result, "Cannot find inline target: " + targetMethod);
            return result;
        }

        InlineMethodRefactoring inline = InlineMethodRefactoring.create(
            cu,
            astRoot,
            target.getStartPosition(),
            target.getLength()
        );
        if (inline == null) {
            Map<String, Object> result;
            if (isUnsupportedInlineSelection(target)) {
                result = baseResult("unsupported", false, "UNKNOWN");
                addMessage(result, "Eclipse InlineMethodRefactoring.create returned null for unsupported inline target node: " + inlineSelectionKind(target) + ".");
            } else {
                result = baseResult("rejected", false, "ERROR");
                addMessage(result, "Eclipse InlineMethodRefactoring.create returned null for inline target node: " + target.getClass().getSimpleName() + ".");
            }
            return result;
        }

        RefactoringStatus status = new RefactoringStatus();
        status.merge(inline.checkInitialConditions(monitor));
        return runRefactoring(inline, status, monitor, "Eclipse inline method refactoring applied.");
    }

    private Map<String, Object> runMoveInstanceMethod(
        Map<String, String> options,
        ICompilationUnit cu,
        CompilationUnit astRoot,
        IProgressMonitor monitor
    ) throws Exception {
        String targetMethod = require(options, "target-method");
        String targetSymbol = options.getOrDefault("target-symbol", "").trim();
        String targetName = options.getOrDefault("target-name", "").trim();
        String targetHint = options.getOrDefault("target-hint", "").trim();
        IMethod method = findMethod(cu, targetMethod);
        if (method == null) {
            Map<String, Object> result = baseResult("rejected", false, "ERROR");
            addMessage(result, "Cannot find target method declaration: " + targetMethod);
            return result;
        }
        IJavaProject methodProject = method.getJavaProject();
        if (methodProject == null || methodProject.getProject() == null) {
            Map<String, Object> result = baseResult("failed", false, "ERROR");
            addMessage(result, "MoveInstanceMethod target method is not attached to a workspace Java project.");
            addMessage(result, "method=" + method.getHandleIdentifier());
            addMessage(result, "methodPath=" + String.valueOf(method.getPath()));
            addMessage(result, "methodResource=" + String.valueOf(method.getResource()));
            addMessage(result, "cu=" + cu.getHandleIdentifier());
            addMessage(result, "cuPath=" + String.valueOf(cu.getPath()));
            addMessage(result, "cuResource=" + String.valueOf(cu.getResource()));
            addMessage(result, "javaProject=" + String.valueOf(methodProject));
            return result;
        }

        MoveInstanceMethodProcessor processor = new MoveInstanceMethodProcessor(
            method,
            defaultCodeGenerationSettings()
        );
        Refactoring move = new MoveRefactoring(processor);
        RefactoringStatus status = new RefactoringStatus();
        status.merge(move.checkInitialConditions(monitor));
        if (status.hasFatalError() || status.hasError()) {
            Map<String, Object> result = baseResult("rejected", false, severityName(status.getSeverity()));
            addStatusMessages(result, status);
            return result;
        }

        IVariableBinding target = chooseMoveTarget(processor, targetSymbol, targetHint);
        if (target == null) {
            Map<String, Object> result = baseResult("rejected", false, "ERROR");
            addMessage(result, "Cannot find move target: " + (targetSymbol.isEmpty() ? "<unspecified>" : targetSymbol));
            addMessage(result, "Available move targets: " + describeMoveTargets(processor));
            return result;
        }
        processor.setTarget(target);
        forceWorkspaceTargetType(processor, method, target);
        processor.setInlineDelegator(false);
        processor.setRemoveDelegator(false);
        processor.setDeprecateDelegates(false);
        if (!targetName.isEmpty()) {
            status.merge(processor.setTargetName(targetName));
        }

        return runRefactoring(move, status, monitor, "Eclipse move instance method refactoring applied.");
    }

    private Map<String, Object> runRename(
        Map<String, String> options,
        String refactoring,
        ICompilationUnit cu,
        CompilationUnit astRoot,
        IProgressMonitor monitor
    ) throws Exception {
        String targetSymbol = options.getOrDefault("target-symbol", "").trim();
        String targetMethod = options.getOrDefault("target-method", "").trim();
        String targetKind = options.getOrDefault("target-kind", "").trim();
        if (targetSymbol.isBlank()) {
            targetSymbol = targetMethod;
        }
        if (targetKind.isBlank()) {
            targetKind = renameKindFromRefactoring(refactoring);
        }
        String newName = require(options, "new-name");
        if (newName.isBlank()) {
            Map<String, Object> result = baseResult("rejected", false, "ERROR");
            addMessage(result, refactoring + " requires a non-empty new name.");
            return result;
        }

        if ("method".equals(targetKind)) {
            IMethod method = findMethod(cu, targetSymbol);
            if (method == null) {
                Map<String, Object> result = baseResult("rejected", false, "ERROR");
                addMessage(result, "Cannot find target method declaration: " + targetSymbol);
                return result;
            }
            RenameMethodProcessor processor = MethodChecks.isVirtual(method)
                ? new RenameVirtualMethodProcessor(method)
                : new RenameNonVirtualMethodProcessor(method);
            processor.setNewElementName(newName);
            processor.setUpdateReferences(true);
            RefactoringStatus status = new RefactoringStatus();
            status.merge(processor.checkInitialConditions(monitor));
            status.merge(checkRenameMethodFinalConditionsWithoutParticipants(processor, monitor));
            return runProcessorChange(processor, status, monitor, "Eclipse rename method refactoring applied.");
        }

        if ("field".equals(targetKind)) {
            IField field = findField(cu, astRoot, targetSymbol);
            if (field == null) {
                Map<String, Object> result = baseResult("rejected", false, "ERROR");
                addMessage(result, "Cannot find target field declaration: " + targetSymbol);
                return result;
            }
            RenameFieldProcessor processor = new RenameFieldProcessor(field);
            processor.setNewElementName(newName);
            processor.setUpdateReferences(true);
            processor.setUpdateTextualMatches(false);
            return runJavaRenameProcessor(processor, monitor, "Eclipse rename field refactoring applied.");
        }

        ILocalVariable variable = findLocalVariable(cu, astRoot, targetSymbol);
        if (variable == null) {
            Map<String, Object> result = baseResult("rejected", false, "ERROR");
            addMessage(result, "Cannot find target local variable or parameter declaration: " + targetSymbol);
            return result;
        }
        RenameLocalVariableProcessor processor = new RenameLocalVariableProcessor(variable);
        processor.setNewElementName(newName);
        processor.setUpdateReferences(true);
        return runJavaRenameProcessor(processor, monitor, "Eclipse rename local variable refactoring applied.");
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

    private RefactoringStatus checkRenameMethodFinalConditionsWithoutParticipants(
        RenameMethodProcessor processor,
        IProgressMonitor monitor
    ) throws Exception {
        Method method = RenameMethodProcessor.class.getDeclaredMethod(
            "doCheckFinalConditions",
            IProgressMonitor.class,
            CheckConditionsContext.class
        );
        method.setAccessible(true);
        CheckConditionsContext context = new CheckConditionsContext();
        RefactoringStatus status = (RefactoringStatus) method.invoke(processor, monitor, context);
        status.merge(context.check(monitor));
        return status;
    }

    private Map<String, Object> runProcessorChange(
        RenameMethodProcessor processor,
        RefactoringStatus status,
        IProgressMonitor monitor,
        String successMessage
    ) throws Exception {
        if (status.hasFatalError() || status.hasError()) {
            Map<String, Object> result = baseResult("rejected", false, severityName(status.getSeverity()));
            addStatusMessages(result, status);
            return result;
        }

        Change change = processor.createChange(monitor);
        return applyChange(change, status, monitor, successMessage);
    }

    private Map<String, Object> runJavaRenameProcessor(
        JavaRenameProcessor processor,
        IProgressMonitor monitor,
        String successMessage
    ) throws Exception {
        RefactoringStatus status = new RefactoringStatus();
        status.merge(processor.checkInitialConditions(monitor));
        CheckConditionsContext context = new CheckConditionsContext();
        context.add(new ResourceChangeChecker());
        context.add(new ValidateEditChecker(null));
        status.merge(processor.checkFinalConditions(monitor, context));
        status.merge(context.check(monitor));
        if (status.hasFatalError() || status.hasError()) {
            Map<String, Object> result = baseResult("rejected", false, severityName(status.getSeverity()));
            addStatusMessages(result, status);
            return result;
        }
        Change change = processor.createChange(monitor);
        return applyChange(change, status, monitor, successMessage);
    }

    private Map<String, Object> runRefactoring(
        Refactoring refactoring,
        RefactoringStatus status,
        IProgressMonitor monitor,
        String successMessage
    ) throws Exception {
        if (status.hasFatalError() || status.hasError()) {
            Map<String, Object> result = baseResult("rejected", false, severityName(status.getSeverity()));
            addStatusMessages(result, status);
            return result;
        }

        status.merge(refactoring.checkFinalConditions(monitor));
        if (status.hasFatalError() || status.hasError()) {
            Map<String, Object> result = baseResult("rejected", false, severityName(status.getSeverity()));
            addStatusMessages(result, status);
            return result;
        }

        Change change = refactoring.createChange(monitor);
        return applyChange(change, status, monitor, successMessage);
    }

    private Map<String, Object> applyChange(
        Change change,
        RefactoringStatus status,
        IProgressMonitor monitor,
        String successMessage
    ) throws Exception {
        if (status.hasWarning()) {
            Map<String, Object> result = baseResult("warning", false, "WARNING");
            addStatusMessages(result, status);
            return result;
        }
        if (change == null) {
            Map<String, Object> result = baseResult("rejected", false, severityName(status.getSeverity()));
            addMessage(result, "Eclipse refactoring createChange returned null.");
            addStatusMessages(result, status);
            return result;
        }
        change.initializeValidationData(monitor);
        RefactoringStatus validation = change.isValid(monitor);
        status.merge(validation);
        if (status.hasFatalError() || status.hasError()) {
            Map<String, Object> result = baseResult("rejected", false, severityName(status.getSeverity()));
            addStatusMessages(result, status);
            return result;
        }
        if (status.hasWarning()) {
            Map<String, Object> result = baseResult("warning", false, "WARNING");
            addStatusMessages(result, status);
            return result;
        }
        List<String> changedFiles = new ArrayList<>();
        applyTextEdits(change, changedFiles, monitor);

        String resultStatus = status.hasWarning() ? "warning" : "success";
        Map<String, Object> result = baseResult(resultStatus, true, severityName(status.getSeverity()));
        result.put("changed_files", changedFiles);
        addStatusMessages(result, status);
        if (((List<?>) result.get("messages")).isEmpty()) {
            addMessage(result, successMessage);
        }
        return result;
    }

    private void applyTextEdits(Change change, List<String> changedFiles, IProgressMonitor monitor) throws Exception {
        if (change instanceof CompositeChange composite) {
            for (Change child : composite.getChildren()) {
                applyTextEdits(child, changedFiles, monitor);
            }
            return;
        }
        if (!(change instanceof TextFileChange textFileChange)) {
            return;
        }
        TextEdit edit = ((TextChange) textFileChange).getEdit();
        if (edit == null) {
            return;
        }
        IFile file = textFileChange.getFile();
        Path path = file.getLocation().toFile().toPath();
        IDocument document = new Document(Files.readString(path, StandardCharsets.UTF_8));
        edit.apply(document);
        Files.writeString(path, document.get(), StandardCharsets.UTF_8);
        file.refreshLocal(IFile.DEPTH_ZERO, monitor);
        changedFiles.add(file.getProjectRelativePath().toString());
    }

    private IProject importCaseProject(Path caseDir, IProgressMonitor monitor) throws CoreException {
        IWorkspace workspace = ResourcesPlugin.getWorkspace();
        String projectName = "case_" + sanitize(caseDir.getFileName().toString());
        IProject project = workspace.getRoot().getProject(projectName);
        if (project.exists()) {
            project.delete(true, true, monitor);
        }
        IProjectDescription description = workspace.newProjectDescription(projectName);
        description.setNatureIds(new String[] { JavaCore.NATURE_ID });
        project.create(description, monitor);
        project.open(monitor);
        copyDirectory(caseDir, project.getLocation().toFile().toPath());
        project.refreshLocal(IProject.DEPTH_INFINITE, monitor);

        IJavaProject javaProject = JavaCore.create(project);
        IFolder src = sourceFolder(project);
        IFolder bin = project.getFolder("bin");
        if (!bin.exists()) {
            bin.create(true, true, monitor);
        }
        List<IClasspathEntry> entries = new ArrayList<>();
        if (src.exists()) {
            entries.add(JavaCore.newSourceEntry(src.getFullPath()));
        } else {
            entries.add(JavaCore.newSourceEntry(project.getFullPath()));
        }
        entries.add(JavaCore.newContainerEntry(PathBridge.runtimeClasspathContainer()));
        javaProject.setRawClasspath(entries.toArray(new IClasspathEntry[0]), bin.getFullPath(), monitor);
        javaProject.open(monitor);
        return project;
    }

    private void copyDirectory(Path source, Path target) throws CoreException {
        try {
            Files.createDirectories(target);
            Files.walkFileTree(source, new java.nio.file.SimpleFileVisitor<Path>() {
                @Override
                public java.nio.file.FileVisitResult preVisitDirectory(Path dir, BasicFileAttributes attrs) throws IOException {
                    Path relative = source.relativize(dir);
                    String name = dir.getFileName() == null ? "" : dir.getFileName().toString();
                    if ("out".equals(name) || "bin".equals(name) || ".metadata".equals(name)) {
                        return java.nio.file.FileVisitResult.SKIP_SUBTREE;
                    }
                    Files.createDirectories(target.resolve(relative));
                    return java.nio.file.FileVisitResult.CONTINUE;
                }

                @Override
                public java.nio.file.FileVisitResult visitFile(Path file, BasicFileAttributes attrs) throws IOException {
                    String name = file.getFileName().toString();
                    if (".eclipse_refactoring_result.json".equals(name) || name.endsWith(".class")) {
                        return java.nio.file.FileVisitResult.CONTINUE;
                    }
                    Path relative = source.relativize(file);
                    Path destination = target.resolve(relative);
                    Files.createDirectories(destination.getParent());
                    Files.copy(file, destination, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.COPY_ATTRIBUTES);
                    return java.nio.file.FileVisitResult.CONTINUE;
                }
            });
        } catch (IOException exception) {
            throw new CoreException(org.eclipse.core.runtime.Status.error("Failed to copy case project into Eclipse workspace.", exception));
        }
    }

    private void copyJavaFiles(Path source, Path target) throws CoreException {
        try {
            Set<Path> copied = new HashSet<>();
            Files.walkFileTree(source, new java.nio.file.SimpleFileVisitor<Path>() {
                @Override
                public java.nio.file.FileVisitResult preVisitDirectory(Path dir, BasicFileAttributes attrs) {
                    String name = dir.getFileName() == null ? "" : dir.getFileName().toString();
                    if ("bin".equals(name) || "out".equals(name) || ".metadata".equals(name)) {
                        return java.nio.file.FileVisitResult.SKIP_SUBTREE;
                    }
                    return java.nio.file.FileVisitResult.CONTINUE;
                }

                @Override
                public java.nio.file.FileVisitResult visitFile(Path file, BasicFileAttributes attrs) throws IOException {
                    if (!file.getFileName().toString().endsWith(".java")) {
                        return java.nio.file.FileVisitResult.CONTINUE;
                    }
                    Path relative = source.relativize(file);
                    copied.add(relative);
                    Path destination = target.resolve(relative);
                    Files.createDirectories(destination.getParent());
                    Files.copy(file, destination, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.COPY_ATTRIBUTES);
                    return java.nio.file.FileVisitResult.CONTINUE;
                }
            });
            Files.walkFileTree(target, new java.nio.file.SimpleFileVisitor<Path>() {
                @Override
                public java.nio.file.FileVisitResult preVisitDirectory(Path dir, BasicFileAttributes attrs) {
                    String name = dir.getFileName() == null ? "" : dir.getFileName().toString();
                    if ("bin".equals(name) || "out".equals(name) || ".metadata".equals(name)) {
                        return java.nio.file.FileVisitResult.SKIP_SUBTREE;
                    }
                    return java.nio.file.FileVisitResult.CONTINUE;
                }

                @Override
                public java.nio.file.FileVisitResult visitFile(Path file, BasicFileAttributes attrs) throws IOException {
                    if (file.getFileName().toString().endsWith(".java")) {
                        Path relative = target.relativize(file);
                        if (!copied.contains(relative)) {
                            Files.deleteIfExists(file);
                        }
                    }
                    return java.nio.file.FileVisitResult.CONTINUE;
                }
            });
        } catch (IOException exception) {
            throw new CoreException(org.eclipse.core.runtime.Status.error("Failed to copy refactored Java files from Eclipse workspace.", exception));
        }
    }

    private ICompilationUnit compilationUnit(IProject project, Path caseDir, Path javaFile) {
        Path relative = caseDir.toAbsolutePath().normalize().relativize(javaFile.toAbsolutePath().normalize());
        IFile file = project.getFile(relative.toString().replace('\\', '/'));
        IJavaElement element = JavaCore.create(file);
        if (element instanceof ICompilationUnit) {
            return (ICompilationUnit) element;
        }

        IJavaProject javaProject = JavaCore.create(project);
        IFolder sourceFolder = sourceFolder(project);
        Path sourceRelative = Path.of(sourceFolder.getProjectRelativePath().toString());
        if (relative.startsWith(sourceRelative)) {
            Path packageAndFile = sourceRelative.relativize(relative);
            Path packagePath = packageAndFile.getParent();
            String packageName = packagePath == null
                ? ""
                : packagePath.toString().replace('\\', '.').replace('/', '.');
            IPackageFragmentRoot root = javaProject.getPackageFragmentRoot(sourceFolder);
            return root.getPackageFragment(packageName).getCompilationUnit(javaFile.getFileName().toString());
        }
        return JavaCore.createCompilationUnitFrom(file);
    }

    private IFolder sourceFolder(IProject project) {
        IFolder mavenSource = project.getFolder("src/main/java");
        if (mavenSource.exists()) {
            return mavenSource;
        }
        return project.getFolder("src");
    }

    @SuppressWarnings("deprecation")
    private CompilationUnit parse(ICompilationUnit cu, IProgressMonitor monitor) {
        ASTParser parser = ASTParser.newParser(AST.JLS21);
        parser.setKind(ASTParser.K_COMPILATION_UNIT);
        parser.setSource(cu);
        parser.setResolveBindings(true);
        parser.setBindingsRecovery(true);
        parser.setStatementsRecovery(true);
        return (CompilationUnit) parser.createAST(monitor);
    }

    private ASTNode findInlineTarget(
        CompilationUnit astRoot,
        String targetMethod,
        int requestedStart,
        int requestedLength
    ) {
        List<ASTNode> matches = new ArrayList<>();
        astRoot.accept(new ASTVisitor() {
            @Override
            public boolean visit(MethodInvocation node) {
                if (node.getName() != null && targetMethod.equals(node.getName().getIdentifier())) {
                    matches.add(node.getName());
                }
                return true;
            }

            @Override
            public boolean visit(ClassInstanceCreation node) {
                if (node.getType() != null && typeNameMatches(node.getType().toString(), targetMethod)) {
                    matches.add(node.getType());
                }
                return true;
            }

            @Override
            public boolean visit(ExpressionMethodReference node) {
                if (node.getName() != null && targetMethod.equals(node.getName().getIdentifier())) {
                    matches.add(node.getName());
                }
                return true;
            }

            @Override
            public boolean visit(TypeMethodReference node) {
                if (node.getName() != null && targetMethod.equals(node.getName().getIdentifier())) {
                    matches.add(node.getName());
                }
                return true;
            }

            @Override
            public boolean visit(SuperMethodReference node) {
                if (node.getName() != null && targetMethod.equals(node.getName().getIdentifier())) {
                    matches.add(node.getName());
                }
                return true;
            }
        });
        if (requestedStart >= 0 && requestedLength > 0) {
            int requestedEnd = requestedStart + requestedLength;
            ASTNode best = null;
            int bestLength = Integer.MAX_VALUE;
            for (ASTNode candidate : matches) {
                ASTNode selection = inlineSelectionNode(candidate);
                int start = selection.getStartPosition();
                int end = start + selection.getLength();
                boolean intersectsSelection = start <= requestedStart && end >= requestedEnd;
                boolean selectedTextContainsTarget = requestedStart <= start && requestedEnd >= end;
                if ((intersectsSelection || selectedTextContainsTarget) && selection.getLength() < bestLength) {
                    best = candidate;
                    bestLength = selection.getLength();
                }
            }
            return best;
        }
        return matches.isEmpty() ? null : matches.get(0);
    }

    private ASTNode inlineSelectionNode(ASTNode candidate) {
        ASTNode parent = candidate == null ? null : candidate.getParent();
        if (parent instanceof MethodInvocation
            || parent instanceof ClassInstanceCreation
            || parent instanceof ExpressionMethodReference
            || parent instanceof TypeMethodReference
            || parent instanceof SuperMethodReference) {
            return parent;
        }
        return candidate;
    }

    private boolean isUnsupportedInlineSelection(ASTNode target) {
        ASTNode parent = target == null ? null : target.getParent();
        return parent instanceof ClassInstanceCreation
            || parent instanceof ExpressionMethodReference
            || parent instanceof TypeMethodReference
            || parent instanceof SuperMethodReference;
    }

    private String inlineSelectionKind(ASTNode target) {
        ASTNode parent = target == null ? null : target.getParent();
        if (parent instanceof ClassInstanceCreation
            || parent instanceof ExpressionMethodReference
            || parent instanceof TypeMethodReference
            || parent instanceof SuperMethodReference) {
            return parent.getClass().getSimpleName();
        }
        return target == null ? "null" : target.getClass().getSimpleName();
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

    private IMethod findMethod(ICompilationUnit cu, String targetMethod) throws JavaModelException {
        for (IType type : cu.getTypes()) {
            IMethod method = findMethod(type, targetMethod);
            if (method != null) {
                return method;
            }
        }
        return null;
    }

    private IMethod findMethod(IType type, String targetMethod) throws JavaModelException {
        for (IMethod method : type.getMethods()) {
            if (targetMethod.equals(method.getElementName())) {
                return method;
            }
        }
        for (IType nested : type.getTypes()) {
            IMethod method = findMethod(nested, targetMethod);
            if (method != null) {
                return method;
            }
        }
        return null;
    }

    private IField findField(ICompilationUnit cu, CompilationUnit astRoot, String targetField) throws JavaModelException {
        for (IType type : cu.getTypes()) {
            IField field = findField(type, targetField);
            if (field != null) {
                return field;
            }
        }
        final IField[] result = new IField[1];
        astRoot.accept(new ASTVisitor() {
            @Override
            public boolean visit(SimpleName node) {
                if (result[0] != null || !targetField.equals(node.getIdentifier())) {
                    return false;
                }
                if (!(node.getParent() instanceof VariableDeclarationFragment)) {
                    return true;
                }
                IBinding rawBinding = node.resolveBinding();
                if (rawBinding instanceof IVariableBinding binding && binding.isField() && binding.getJavaElement() instanceof IField) {
                    result[0] = (IField) binding.getJavaElement();
                    return false;
                }
                return true;
            }
        });
        return result[0];
    }

    private IField findField(IType type, String targetField) throws JavaModelException {
        for (IField field : type.getFields()) {
            if (targetField.equals(field.getElementName())) {
                return field;
            }
        }
        for (IType nested : type.getTypes()) {
            IField field = findField(nested, targetField);
            if (field != null) {
                return field;
            }
        }
        return null;
    }

    private ILocalVariable findLocalVariable(ICompilationUnit cu, CompilationUnit astRoot, String targetVariable) throws JavaModelException {
        final ILocalVariable[] result = new ILocalVariable[1];
        astRoot.accept(new ASTVisitor() {
            @Override
            public boolean visit(SimpleName node) {
                if (result[0] != null || !targetVariable.equals(node.getIdentifier())) {
                    return false;
                }
                ASTNode parent = node.getParent();
                if (!(parent instanceof VariableDeclarationFragment) && !(parent instanceof SingleVariableDeclaration)) {
                    return true;
                }
                IBinding rawBinding = node.resolveBinding();
                if (rawBinding instanceof IVariableBinding binding && !binding.isField() && binding.getJavaElement() instanceof ILocalVariable) {
                    result[0] = (ILocalVariable) binding.getJavaElement();
                    return false;
                }
                return true;
            }
        });
        return result[0];
    }

    private IVariableBinding chooseMoveTarget(MoveInstanceMethodProcessor processor, String targetSymbol, String targetHint) {
        IVariableBinding[] targets = processor.getPossibleTargets();
        if (targets.length == 0) {
            return null;
        }
        List<String> targetTokens = moveTargetTokens(targetSymbol, targetHint);
        if (targetTokens.isEmpty()) {
            return targets[0];
        }
        for (String token : targetTokens) {
            for (IVariableBinding target : targets) {
                if (token.equals(target.getName())) {
                    return target;
                }
            }
        }
        for (String token : targetTokens) {
            for (IVariableBinding target : targets) {
                ITypeBinding type = target.getType();
                if (type != null && typeMatches(type, token)) {
                    return target;
                }
            }
        }
        return null;
    }

    private List<String> moveTargetTokens(String targetSymbol, String targetHint) {
        List<String> tokens = new ArrayList<>();
        addMoveTargetToken(tokens, targetSymbol);
        if (targetHint != null && !targetHint.isBlank()) {
            String normalized = targetHint.replaceAll("[()]", " ");
            java.util.regex.Matcher targetClass = java.util.regex.Pattern
                .compile("(?i)target\\s+class\\s*:?\\s*([A-Za-z_$][\\w$]*(?:\\s*<[^>]+>)?)")
                .matcher(normalized);
            while (targetClass.find()) {
                addMoveTargetToken(tokens, targetClass.group(1));
            }
            java.util.regex.Matcher toMatcher = java.util.regex.Pattern
                .compile("(?i)\\bto\\s+(?:class\\s+)?([A-Za-z_$][\\w$]*(?:\\s*<[^>]+>)?)")
                .matcher(normalized);
            while (toMatcher.find()) {
                addMoveTargetToken(tokens, toMatcher.group(1));
            }
            java.util.regex.Matcher fieldMatcher = java.util.regex.Pattern
                .compile("(?i)target\\s+is\\s+field\\s+([A-Za-z_$][\\w$]*)")
                .matcher(normalized);
            while (fieldMatcher.find()) {
                addMoveTargetToken(tokens, fieldMatcher.group(1));
            }
        }
        return tokens;
    }

    private void addMoveTargetToken(List<String> tokens, String raw) {
        if (raw == null) {
            return;
        }
        String token = raw.trim();
        if (token.isEmpty()) {
            return;
        }
        token = token.replaceAll("<.*>", "").replaceAll("[^A-Za-z0-9_$.]", "");
        if (token.isEmpty() || tokens.contains(token)) {
            return;
        }
        tokens.add(token);
    }

    private boolean typeMatches(ITypeBinding type, String targetSymbol) {
        if (targetSymbol.equals(type.getName()) || targetSymbol.equals(type.getQualifiedName())) {
            return true;
        }
        ITypeBinding declaration = type.getTypeDeclaration();
        return declaration != null
            && (targetSymbol.equals(declaration.getName()) || targetSymbol.equals(declaration.getQualifiedName()));
    }

    private String describeMoveTargets(MoveInstanceMethodProcessor processor) {
        List<String> descriptions = new ArrayList<>();
        for (IVariableBinding target : processor.getPossibleTargets()) {
            ITypeBinding type = target.getType();
            String typeName = type == null ? "<unknown>" : type.getQualifiedName();
            descriptions.add(target.getName() + ":" + typeName);
        }
        return descriptions.toString();
    }

    private void forceWorkspaceTargetType(
        MoveInstanceMethodProcessor processor,
        IMethod method,
        IVariableBinding target
    ) throws ReflectiveOperationException, JavaModelException {
        IType targetType = findWorkspaceTargetType(method, target);
        if (targetType == null) {
            return;
        }
        Field field = MoveInstanceMethodProcessor.class.getDeclaredField("fTargetType");
        field.setAccessible(true);
        field.set(processor, targetType);
    }

    private IType findWorkspaceTargetType(IMethod method, IVariableBinding target) throws JavaModelException {
        ITypeBinding binding = target.getType();
        if (binding == null) {
            return null;
        }
        ITypeBinding declaration = binding.getTypeDeclaration();
        String simpleName = declaration == null ? binding.getName() : declaration.getName();
        String qualifiedName = declaration == null ? binding.getQualifiedName() : declaration.getQualifiedName();
        IJavaProject javaProject = method.getJavaProject();
        if (qualifiedName != null && !qualifiedName.isBlank()) {
            IType type = javaProject.findType(qualifiedName);
            if (type != null && type.exists()) {
                return type;
            }
        }
        if (simpleName == null || simpleName.isBlank()) {
            return null;
        }
        IType sameUnitType = findType(method.getCompilationUnit(), simpleName);
        if (sameUnitType != null && sameUnitType.exists()) {
            return sameUnitType;
        }
        IType type = javaProject.findType(simpleName);
        if (type != null && type.exists()) {
            return type;
        }
        for (IPackageFragmentRoot root : javaProject.getPackageFragmentRoots()) {
            if (root.getKind() != IPackageFragmentRoot.K_SOURCE) {
                continue;
            }
            for (IJavaElement child : root.getChildren()) {
                if (!(child instanceof IPackageFragment fragment)) {
                    continue;
                }
                for (ICompilationUnit unit : fragment.getCompilationUnits()) {
                    IType found = findType(unit, simpleName);
                    if (found != null && found.exists()) {
                        return found;
                    }
                }
            }
        }
        return null;
    }

    private IType findType(ICompilationUnit unit, String simpleName) throws JavaModelException {
        for (IType type : unit.getTypes()) {
            IType found = findType(type, simpleName);
            if (found != null) {
                return found;
            }
        }
        return null;
    }

    private IType findType(IType type, String simpleName) throws JavaModelException {
        if (simpleName.equals(type.getElementName())) {
            return type;
        }
        for (IType nested : type.getTypes()) {
            IType found = findType(nested, simpleName);
            if (found != null) {
                return found;
            }
        }
        return null;
    }

    private CodeGenerationSettings defaultCodeGenerationSettings() {
        CodeGenerationSettings settings = new CodeGenerationSettings();
        settings.createComments = false;
        settings.useKeywordThis = false;
        settings.importIgnoreLowercase = true;
        settings.overrideAnnotation = true;
        settings.tabWidth = 4;
        settings.indentWidth = 4;
        return settings;
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

    private void addStatusMessages(Map<String, Object> result, RefactoringStatus status) {
        for (RefactoringStatusEntry entry : status.getEntries()) {
            addMessage(result, severityName(entry.getSeverity()) + ": " + entry.getMessage());
        }
    }

    private String severityName(int severity) {
        if (severity == RefactoringStatus.FATAL) {
            return "FATAL";
        }
        if (severity == RefactoringStatus.ERROR) {
            return "ERROR";
        }
        if (severity == RefactoringStatus.WARNING) {
            return "WARNING";
        }
        if (severity == RefactoringStatus.INFO) {
            return "INFO";
        }
        return "OK";
    }

    private static Map<String, String> parseArgs(String[] args) {
        Map<String, String> options = new LinkedHashMap<>();
        for (int index = 0; index < args.length; index++) {
            String arg = args[index];
            if (!arg.startsWith("--")) {
                continue;
            }
            String key = arg.substring(2);
            String value = "true";
            if (index + 1 < args.length && !args[index + 1].startsWith("--")) {
                value = args[++index];
            }
            options.put(key, value);
        }
        return options;
    }

    private static String require(Map<String, String> options, String key) {
        String value = options.get(key);
        if (value == null || value.isBlank()) {
            throw new IllegalArgumentException("Missing required argument --" + key);
        }
        return value;
    }

    private static Path requirePath(Map<String, String> options, String key) {
        return Paths.get(require(options, key)).toAbsolutePath().normalize();
    }

    private static String sanitize(String value) {
        return value.replaceAll("[^A-Za-z0-9_.-]", "_");
    }

    private static String stackTrace(Throwable throwable) {
        StringWriter writer = new StringWriter();
        throwable.printStackTrace(new PrintWriter(writer));
        return writer.toString();
    }

    private static void writeJson(Path output, Map<String, Object> result) throws IOException {
        Files.createDirectories(output.getParent());
        Files.writeString(output, toJson(result), StandardCharsets.UTF_8);
    }

    private static String toJson(Object value) {
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

    private static String escapeJson(String text) {
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

    private static final class PathBridge {
        private static org.eclipse.core.runtime.IPath runtimeClasspathContainer() {
            return org.eclipse.core.runtime.Path.fromOSString("org.eclipse.jdt.launching.JRE_CONTAINER");
        }
    }
}
