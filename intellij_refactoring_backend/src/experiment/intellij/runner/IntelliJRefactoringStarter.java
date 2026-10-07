package experiment.intellij.runner;

import com.intellij.ide.impl.OpenProjectTask;
import com.intellij.openapi.application.ApplicationManager;
import com.intellij.openapi.application.ApplicationStarter;
import com.intellij.openapi.command.WriteCommandAction;
import com.intellij.openapi.fileEditor.FileDocumentManager;
import com.intellij.openapi.editor.Document;
import com.intellij.openapi.editor.Editor;
import com.intellij.openapi.editor.EditorFactory;
import com.intellij.openapi.project.DumbService;
import com.intellij.openapi.project.Project;
import com.intellij.openapi.project.ex.ProjectManagerEx;
import com.intellij.openapi.util.Computable;
import com.intellij.openapi.util.Ref;
import com.intellij.openapi.vfs.LocalFileSystem;
import com.intellij.openapi.vfs.VirtualFile;
import com.intellij.psi.PsiFile;
import com.intellij.psi.PsiClass;
import com.intellij.psi.PsiClassType;
import com.intellij.psi.PsiField;
import com.intellij.psi.PsiJavaFile;
import com.intellij.psi.PsiJavaCodeReferenceElement;
import com.intellij.psi.PsiElement;
import com.intellij.psi.PsiExpression;
import com.intellij.psi.PsiMethodReferenceExpression;
import com.intellij.psi.PsiManager;
import com.intellij.psi.PsiMethod;
import com.intellij.psi.PsiModifier;
import com.intellij.psi.PsiMethodCallExpression;
import com.intellij.psi.PsiNewExpression;
import com.intellij.psi.PsiParameter;
import com.intellij.psi.PsiDocumentManager;
import com.intellij.psi.PsiReference;
import com.intellij.psi.PsiType;
import com.intellij.psi.PsiVariable;
import com.intellij.psi.util.PsiTreeUtil;
import com.intellij.refactoring.BaseRefactoringProcessor;
import com.intellij.refactoring.IntroduceVariableUtil;
import com.intellij.refactoring.RefactoringBundle;
import com.intellij.refactoring.extractMethod.newImpl.ExtractMethodPipeline;
import com.intellij.refactoring.extractMethod.newImpl.ExtractSelector;
import com.intellij.refactoring.extractMethod.newImpl.MethodExtractor;
import com.intellij.refactoring.extractMethod.newImpl.structures.ExtractOptions;
import com.intellij.openapi.util.TextRange;
import com.intellij.psi.PsiAnonymousClass;
import com.intellij.refactoring.inline.InlineMethodProcessor;
import com.intellij.refactoring.introduceVariable.InputValidator;
import com.intellij.refactoring.introduceVariable.IntroduceVariableBase;
import com.intellij.refactoring.introduceVariable.IntroduceVariableHandler;
import com.intellij.refactoring.introduceVariable.IntroduceVariableSettings;
import com.intellij.refactoring.move.moveInstanceMethod.MoveInstanceMethodHandler;
import com.intellij.refactoring.move.moveInstanceMethod.MoveInstanceMethodProcessor;
import com.intellij.refactoring.rename.RenamePsiElementProcessor;
import com.intellij.refactoring.rename.RenameProcessor;
import com.intellij.refactoring.rename.RenameUtil;
import com.intellij.refactoring.ui.TypeSelectorManagerImpl;
import com.intellij.usageView.UsageInfo;
import com.intellij.util.containers.MultiMap;

import java.io.IOException;
import java.io.PrintWriter;
import java.io.StringWriter;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Collection;
import java.util.LinkedHashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

public final class IntelliJRefactoringStarter implements ApplicationStarter {
    private static final Pattern STANDALONE_LOOP_EXIT = Pattern.compile(
            "\\A\\s*(?:break|continue)(?:\\s+\\p{javaJavaIdentifierStart}\\p{javaJavaIdentifierPart}*)?\\s*;\\s*\\z");

    @Override
    public int getRequiredModality() {
        return ApplicationStarter.NOT_IN_EDT;
    }

    @Override
    public boolean isHeadless() {
        return true;
    }

    @Override
    public void main(List<String> args) {
        Map<String, String> options = parseOptions(args);
        Path output = Path.of(required(options, "output"));
        Result result;
        try {
            safeWriteJson(output, Result.failed("IntelliJ backend started."));
            result = runRefactoring(options, output);
        } catch (Throwable throwable) {
            result = Result.failed("IntelliJ backend failed: " + stackTrace(throwable));
        }
        try {
            writeJson(output, result);
        } catch (IOException ioException) {
            System.err.println("Failed to write IntelliJ refactoring result: " + ioException.getMessage());
            ioException.printStackTrace(System.err);
        }
        ApplicationManager.getApplication().exit(true, true, false, 0);
        System.exit(0);
    }

    private static Result runRefactoring(Map<String, String> options, Path output) {
        String refactoring = required(options, "refactoring");
        if (!"InlineMethod".equalsIgnoreCase(refactoring)
                && !"ExtractVariable".equalsIgnoreCase(refactoring)
                && !"ExtractMethod".equalsIgnoreCase(refactoring)
                && !"RenameMethod".equalsIgnoreCase(refactoring)
                && !"RenameField".equalsIgnoreCase(refactoring)
                && !"RenameVariable".equalsIgnoreCase(refactoring)
                && !"MoveInstanceMethod".equalsIgnoreCase(refactoring)) {
            return Result.unsupported("Only InlineMethod, ExtractVariable, ExtractMethod, RenameMethod, RenameField, RenameVariable and MoveInstanceMethod are currently implemented for the IntelliJ backend.");
        }

        Path caseDir = Path.of(required(options, "case-dir"));
        Path javaFile = Path.of(required(options, "java-file"));
        String targetMethod = options.getOrDefault("target-method", "");

        trace("opening project: " + caseDir);
        safeWriteJson(output, Result.failed("IntelliJ backend stage: opening project."));
        Project project = openProjectOnEdt(caseDir);
        if (project == null) {
            return Result.failed("IntelliJ could not open project: " + caseDir);
        }

        try {
            trace("waiting for smart mode");
            safeWriteJson(output, Result.failed("IntelliJ backend stage: waiting for indexes."));
            DumbService.getInstance(project).waitForSmartMode();
            trace("resolving java file: " + javaFile);
            VirtualFile virtualFile = LocalFileSystem.getInstance().refreshAndFindFileByNioFile(javaFile);
            if (virtualFile == null) {
                return Result.failed("Java file not found by VFS: " + javaFile);
            }
            PsiFile psiFile = ApplicationManager.getApplication().runReadAction(
                    (Computable<PsiFile>) () -> PsiManager.getInstance(project).findFile(virtualFile)
            );
            if (!(psiFile instanceof PsiJavaFile)) {
                return Result.failed("Input file is not a Java PSI file: " + javaFile);
            }

            if ("ExtractVariable".equalsIgnoreCase(refactoring)) {
                int targetStart = integerOption(options, "target-start", -1);
                int targetLength = integerOption(options, "target-length", 0);
                String targetHint = options.getOrDefault("target-hint", "");
                String newName = options.getOrDefault("new-name", "extractedValue");
                boolean replaceAll = booleanOption(options, "replace-all", false);
                PsiExpression expression = ApplicationManager.getApplication().runReadAction(
                        (Computable<PsiExpression>) () -> findExtractVariableExpression(
                                project, psiFile, targetStart, targetLength, targetHint)
                );
                if (expression == null) {
                    return Result.rejected("IntelliJ rejected ExtractVariable: Selected block should represent an expression. "
                            + "selectionStart=" + targetStart + ", selectionLength=" + targetLength + ".");
                }
                String expressionText = ApplicationManager.getApplication().runReadAction(
                        (Computable<String>) expression::getText
                );
                safeWriteJson(output, Result.failed("IntelliJ backend stage: running ExtractVariable for "
                        + expressionText + "."));
                return runExtractVariable(
                        project, expression, expressionText, javaFile, virtualFile, newName, replaceAll);
            }

            if ("ExtractMethod".equalsIgnoreCase(refactoring)) {
                int targetStart = integerOption(options, "target-start", -1);
                int targetLength = integerOption(options, "target-length", 0);
                String newName = options.getOrDefault("new-name", "extractedMethod").trim();
                boolean replaceDuplicates = booleanOption(options, "replace-duplicates", false);
                if (targetStart < 0 || targetLength <= 0) {
                    return Result.rejected(cannotRefactorMessage(
                            "selected.block.should.represent.a.set.of.statements.or.an.expression")
                            + " selectionStart=" + targetStart + ", selectionLength=" + targetLength + ".");
                }
                safeWriteJson(output, Result.failed("IntelliJ backend stage: running ExtractMethod at ["
                        + targetStart + ", " + (targetStart + targetLength) + ")."));
                return runExtractMethod(project, psiFile, javaFile, virtualFile, targetStart,
                        targetLength, newName, replaceDuplicates);
            }

            if (isRenameRefactoring(refactoring)) {
                String newName = required(options, "new-name");
                String targetSymbol = options.getOrDefault("target-symbol", targetMethod);
                String targetKind = options.getOrDefault("target-kind", renameKindFromRefactoring(refactoring));
                trace("finding rename target: kind=" + targetKind + " symbol=" + targetSymbol);
                safeWriteJson(output, Result.failed("IntelliJ backend stage: finding " + refactoring + " target " + targetSymbol + "."));
                PsiElement element = findRenameTargetWithRetries(project, psiFile, targetKind, targetSymbol);
                if (element == null) {
                    return Result.rejected(refactoring + " target not found: kind=" + targetKind + " symbol=" + targetSymbol);
                }
                trace("running rename: " + targetSymbol + " -> " + newName);
                safeWriteJson(output, Result.failed("IntelliJ backend stage: running " + refactoring + " for " + targetSymbol + "."));
                return runRenameElement(project, element, javaFile, refactoring, targetSymbol, newName);
            }

            if ("MoveInstanceMethod".equalsIgnoreCase(refactoring)) {
                String targetSymbol = options.getOrDefault("target-symbol", "");
                String targetHint = options.getOrDefault("target-hint", "");
                trace("finding move instance method target: " + targetMethod + " target=" + targetSymbol);
                safeWriteJson(output, Result.failed("IntelliJ backend stage: finding MoveInstanceMethod target " + targetMethod + "."));
                PsiMethod method = findMethodWithRetries(project, psiFile, targetMethod);
                if (method == null) {
                    return Result.rejected("MoveInstanceMethod target method not found: " + targetMethod);
                }
                PsiVariable targetVariable = findMoveTargetVariable(project, method, targetSymbol, targetHint);
                if (targetVariable == null) {
                    return Result.rejected("MoveInstanceMethod target variable not found for " + targetMethod
                            + ". targetSymbol=" + targetSymbol + " targetHint=" + targetHint);
                }
                trace("running move instance method: " + describeMethod(method) + " target=" + targetVariable.getName());
                safeWriteJson(output, Result.failed("IntelliJ backend stage: running MoveInstanceMethod for " + targetMethod + "."));
                return runMoveInstanceMethod(project, method, targetVariable, javaFile, targetMethod);
            }

            trace("finding inline target: " + targetMethod);
            safeWriteJson(output, Result.failed("IntelliJ backend stage: finding inline target " + targetMethod + "."));
            int targetStart = integerOption(options, "target-start", -1);
            int targetLength = integerOption(options, "target-length", 0);
            Target target = findTargetWithRetries(project, psiFile, targetMethod, targetStart, targetLength);
            if (target == null) {
                return Result.rejected("Inline target not found or could not be resolved: " + targetMethod);
            }

            trace("running inline method: " + targetMethod);
            safeWriteJson(output, Result.failed("IntelliJ backend stage: running InlineMethod for " + targetMethod + "."));
            return runInlineMethod(project, target, javaFile, targetMethod);
        } finally {
            try {
                trace("closing project");
                closeProjectOnEdt(project);
            } catch (Throwable ignored) {
                // The application exits immediately after writing the result. Cleanup failures must
                // not overwrite the actual refactoring outcome.
            }
        }
    }

    private static Result runExtractVariable(
            Project project,
            PsiExpression expression,
            String expressionText,
            Path javaFile,
            VirtualFile virtualFile,
            String newName,
            boolean replaceAll
    ) {
        if (newName == null || newName.isBlank()) {
            newName = "extractedValue";
        }
        String requestedName = newName;
        String sourceBefore;
        try {
            sourceBefore = Files.readString(javaFile, StandardCharsets.UTF_8);
        } catch (IOException exception) {
            return Result.failed("Cannot read source before ExtractVariable: " + exception.getMessage());
        }

        Throwable lastIndexError = null;
        for (int attempt = 1; attempt <= 4; attempt++) {
            try {
                DumbService.getInstance(project).waitForSmartMode();
                CapturingIntroduceVariableHandler handler = new CapturingIntroduceVariableHandler(requestedName, replaceAll);
                ApplicationManager.getApplication().invokeAndWait(() -> {
                    PsiDocumentManager documentManager = PsiDocumentManager.getInstance(project);
                    documentManager.commitAllDocuments();
                    Document document = documentManager.getDocument(expression.getContainingFile());
                    if (document == null) {
                        throw new RefactoringAbortedException("Cannot obtain an editor document for ExtractVariable.");
                    }
                    Editor editor = EditorFactory.getInstance().createEditor(document, project, virtualFile, false);
                    try {
                        editor.getSettings().setVariableInplaceRenameEnabled(false);
                        boolean applied = handler.invokeImpl(project, expression, null, null, editor);
                        if (!applied) {
                            throw new RefactoringAbortedException(handler.failureMessage());
                        }
                        documentManager.commitAllDocuments();
                        FileDocumentManager.getInstance().saveAllDocuments();
                        LocalFileSystem.getInstance().refreshIoFiles(List.of(javaFile.toFile()), false, true, null);
                    } finally {
                        EditorFactory.getInstance().releaseEditor(editor);
                    }
                });
                waitForSourceChange(project, javaFile, sourceBefore, 5000);
                String sourceAfter = Files.readString(javaFile, StandardCharsets.UTF_8);
                if (sourceBefore.equals(sourceAfter)) {
                    return Result.failed("ExtractVariable backend completed without changing the source file. target="
                            + expressionText + ", newName=" + requestedName + ".");
                }
                return Result.success("ExtractVariable completed for " + expressionText
                        + " as " + requestedName + ".");
            } catch (Throwable throwable) {
                RefactoringAbortedException aborted = findCause(throwable, RefactoringAbortedException.class);
                if (aborted != null) {
                    return Result.rejected("IntelliJ rejected ExtractVariable: " + aborted.getMessage());
                }
                if (isIndexNotReady(throwable)) {
                    lastIndexError = throwable;
                    sleepQuietly(1000L * attempt);
                    continue;
                }
                String message = throwable.getMessage();
                if (message == null || message.isBlank()) {
                    message = throwable.getClass().getName();
                }
                return Result.rejected("IntelliJ rejected or aborted ExtractVariable: " + message
                        + "\n" + stackTrace(throwable));
            }
        }
        return Result.failed("IntelliJ indexing was not ready after retries:\n" + stackTrace(lastIndexError));
    }

    private static Result runExtractMethod(
            Project project,
            PsiFile psiFile,
            Path javaFile,
            VirtualFile virtualFile,
            int targetStart,
            int targetLength,
            String newName,
            boolean replaceDuplicates
    ) {
        if (newName == null || newName.isBlank()) {
            newName = "extractedMethod";
        }
        final String requestedName = newName;
        if (replaceDuplicates) {
            return Result.rejected("IntelliJ Extract Method duplicate replacement requires the interactive duplicate chooser; "
                    + "the headless runner does not silently choose on the user's behalf.");
        }
        String sourceBefore;
        try {
            sourceBefore = Files.readString(javaFile, StandardCharsets.UTF_8);
        } catch (IOException exception) {
            return Result.failed("Cannot read source before ExtractMethod: " + exception.getMessage());
        }

        try {
            DumbService.getInstance(project).waitForSmartMode();
            ApplicationManager.getApplication().invokeAndWait(() -> {
                PsiDocumentManager documentManager = PsiDocumentManager.getInstance(project);
                documentManager.commitAllDocuments();
                Document document = documentManager.getDocument(psiFile);
                if (document == null) {
                    throw new RefactoringAbortedException("Cannot obtain an editor document for ExtractMethod.");
                }
                if (targetStart + targetLength > document.getTextLength()) {
                    throw new RefactoringAbortedException("ExtractMethod selection lies outside the editor document.");
                }
                String selectedText = document.getText(new TextRange(targetStart, targetStart + targetLength));
                if (STANDALONE_LOOP_EXIT.matcher(selectedText).matches()) {
                    throw new RefactoringUnsupportedException(
                            "The headless Extract Method adapter cannot faithfully extract a standalone break or continue statement.");
                }
                Editor editor = EditorFactory.getInstance().createEditor(document, project, virtualFile, false);
                try {
                    editor.getSelectionModel().setSelection(targetStart, targetStart + targetLength);
                    List<PsiElement> elements = new ExtractSelector().suggestElementsToExtract(
                            psiFile, new TextRange(targetStart, targetStart + targetLength));
                    if (elements.isEmpty()) {
                        throw new RefactoringAbortedException(cannotRefactorMessage(
                                "selected.block.should.represent.a.set.of.statements.or.an.expression"));
                    }
                    List<ExtractOptions> options = ExtractMethodPipeline.INSTANCE.findAllOptionsToExtract(elements);
                    if (options.isEmpty()) {
                        throw new RefactoringAbortedException(cannotRefactorMessage(
                                "selected.block.should.represent.a.set.of.statements.or.an.expression"));
                    }
                    ExtractOptions selected = options.stream()
                            .filter(option -> !(option.getTargetClass() instanceof PsiAnonymousClass))
                            .findFirst()
                            .orElse(options.get(0));
                    ExtractOptions configured = withExtractedMethodName(selected, requestedName);
                    WriteCommandAction.runWriteCommandAction(project, () -> {
                        new MethodExtractor().extractMethod(configured);
                    });
                    documentManager.commitAllDocuments();
                    FileDocumentManager.getInstance().saveAllDocuments();
                    LocalFileSystem.getInstance().refreshIoFiles(List.of(javaFile.toFile()), false, true, null);
                } catch (RefactoringAbortedException | RefactoringUnsupportedException exception) {
                    throw exception;
                } catch (Throwable throwable) {
                    String message = throwable.getMessage();
                    throw new RefactoringAbortedException(message == null || message.isBlank()
                            ? throwable.getClass().getName() : message, throwable);
                } finally {
                    EditorFactory.getInstance().releaseEditor(editor);
                }
            });
            waitForSourceChange(project, javaFile, sourceBefore, 5000);
            String sourceAfter = Files.readString(javaFile, StandardCharsets.UTF_8);
            if (sourceBefore.equals(sourceAfter)) {
                return Result.failed("ExtractMethod processor completed without changing the source file. selectionStart="
                        + targetStart + ", selectionLength=" + targetLength + ".");
            }
            return Result.success("ExtractMethod completed at [" + targetStart + ", "
                    + (targetStart + targetLength) + ") as " + requestedName + ".");
        } catch (Throwable throwable) {
            RefactoringUnsupportedException unsupported = findCause(throwable, RefactoringUnsupportedException.class);
            if (unsupported != null) {
                return Result.unsupported(unsupported.getMessage());
            }
            RefactoringAbortedException aborted = findCause(throwable, RefactoringAbortedException.class);
            if (aborted != null) {
                String detail = aborted.getCause() == null ? "" : "\n" + stackTrace(aborted.getCause());
                return Result.rejected("IntelliJ rejected ExtractMethod: " + aborted.getMessage() + detail);
            }
            String message = throwable.getMessage();
            return Result.rejected("IntelliJ rejected or aborted ExtractMethod: "
                    + (message == null || message.isBlank() ? throwable.getClass().getName() : message)
                    + "\n" + stackTrace(throwable));
        }
    }

    private static ExtractOptions withExtractedMethodName(ExtractOptions options, String name) {
        return options.copy(
                options.getTargetClass(),
                options.getElements(),
                options.getFlowOutput(),
                options.getDataOutput(),
                options.getThrownExceptions(),
                options.getRequiredVariablesInside(),
                options.getInputParameters(),
                options.getTypeParameters(),
                name,
                options.isStatic(),
                options.getVisibility(),
                options.getExposedLocalVariables(),
                options.getDisabledParameters(),
                options.isConstructor(),
                options.getProject());
    }

    private static String cannotRefactorMessage(String key) {
        return RefactoringBundle.getCannotRefactorMessage(RefactoringBundle.message(key));
    }

    private static PsiExpression findExtractVariableExpression(
            Project project,
            PsiFile psiFile,
            int requestedStart,
            int requestedLength,
            String targetHint
    ) {
        if (requestedLength > 0) {
            if (requestedStart < 0) {
                return null;
            }
            // Resolve the editor selection with the API provided by this IDEA build.
            return IntroduceVariableUtil.getSelectedExpression(
                    project, psiFile, requestedStart, requestedStart + requestedLength);
        }

        Collection<PsiExpression> expressions = PsiTreeUtil.findChildrenOfType(psiFile, PsiExpression.class);
        String hint = targetHint == null ? "" : targetHint.toLowerCase();
        PsiExpression best = null;
        long bestScore = Long.MAX_VALUE;
        for (PsiExpression expression : expressions) {
            int start = expression.getTextRange().getStartOffset();
            if (requestedStart >= 0 && start < requestedStart) {
                continue;
            }
            long score = requestedStart < 0 ? start : start - requestedStart;
            String kind = expression.getClass().getSimpleName().toLowerCase();
            if (hint.contains("null literal") && kind.contains("nullliteral")) {
                score -= 1_000_000L;
            } else if (hint.contains("array initializer") && kind.contains("arrayinitializer")) {
                score -= 1_000_000L;
            } else if (hint.contains("assignment") && kind.contains("assignment")) {
                score -= 1_000_000L;
            }
            score = score * 10_000L + expression.getTextLength();
            if (best == null || score < bestScore) {
                best = expression;
                bestScore = score;
            }
        }
        return best;
    }

    private static int integerOption(Map<String, String> options, String key, int defaultValue) {
        try {
            return Integer.parseInt(options.getOrDefault(key, String.valueOf(defaultValue)));
        } catch (NumberFormatException ignored) {
            return defaultValue;
        }
    }

    private static boolean booleanOption(Map<String, String> options, String key, boolean defaultValue) {
        String value = options.get(key);
        return value == null ? defaultValue : Boolean.parseBoolean(value);
    }

    private static Target findTargetWithRetries(
            Project project,
            PsiFile psiFile,
            String targetMethod,
            int targetStart,
            int targetLength
    ) {
        Throwable lastIndexError = null;
        for (int attempt = 1; attempt <= 4; attempt++) {
            try {
                DumbService.getInstance(project).waitForSmartMode();
                return ApplicationManager.getApplication().runReadAction(
                        (Computable<Target>) () -> findTargetCall(
                                psiFile, targetMethod, targetStart, targetLength)
                );
            } catch (Throwable throwable) {
                if (!isIndexNotReady(throwable)) {
                    throw throwable;
                }
                lastIndexError = throwable;
                sleepQuietly(1000L * attempt);
            }
        }
        throw new RuntimeException("IntelliJ indexing was not ready while resolving inline target.", lastIndexError);
    }

    private static PsiMethod findMethodWithRetries(Project project, PsiFile psiFile, String targetMethod) {
        Throwable lastIndexError = null;
        for (int attempt = 1; attempt <= 4; attempt++) {
            try {
                DumbService.getInstance(project).waitForSmartMode();
                return ApplicationManager.getApplication().runReadAction(
                        (Computable<PsiMethod>) () -> findMarkedTargetMethod(psiFile, targetMethod)
                );
            } catch (Throwable throwable) {
                if (!isIndexNotReady(throwable)) {
                    throw throwable;
                }
                lastIndexError = throwable;
                sleepQuietly(1000L * attempt);
            }
        }
        throw new RuntimeException("IntelliJ indexing was not ready while resolving rename target.", lastIndexError);
    }

    private static PsiElement findRenameTargetWithRetries(Project project, PsiFile psiFile, String targetKind, String targetSymbol) {
        Throwable lastIndexError = null;
        for (int attempt = 1; attempt <= 4; attempt++) {
            try {
                DumbService.getInstance(project).waitForSmartMode();
                return ApplicationManager.getApplication().runReadAction(
                        (Computable<PsiElement>) () -> findRenameTarget(psiFile, targetKind, targetSymbol)
                );
            } catch (Throwable throwable) {
                if (!isIndexNotReady(throwable)) {
                    throw throwable;
                }
                lastIndexError = throwable;
                sleepQuietly(1000L * attempt);
            }
        }
        throw new RuntimeException("IntelliJ indexing was not ready while resolving rename target.", lastIndexError);
    }

    private static Result runInlineMethod(Project project, Target target, Path javaFile, String targetMethod) {
        Throwable lastIndexError = null;
        for (int attempt = 1; attempt <= 4; attempt++) {
            try {
                DumbService.getInstance(project).waitForSmartMode();
                ApplicationManager.getApplication().invokeAndWait(() -> {
                    CapturingInlineMethodProcessor processor = new CapturingInlineMethodProcessor(
                            project,
                            target.method,
                            target.reference,
                            true
                    );
                    processor.setPreviewUsages(false);
                    processor.run();
                    processor.throwIfConflicts();
                    FileDocumentManager.getInstance().saveAllDocuments();
                    LocalFileSystem.getInstance().refreshIoFiles(List.of(javaFile.toFile()), false, true, null);
                });
                return Result.success("InlineMethod completed for " + targetMethod + ".");
            } catch (Throwable throwable) {
                RefactoringConflictException conflict = findCause(throwable, RefactoringConflictException.class);
                if (conflict != null) {
                    return Result.rejected("IntelliJ reported conflicts and the runner did not force-apply the refactoring:\n" + conflict.getMessage());
                }
                BaseRefactoringProcessor.ConflictsInTestsException testConflict =
                        findCause(throwable, BaseRefactoringProcessor.ConflictsInTestsException.class);
                if (testConflict != null) {
                    return Result.rejected("IntelliJ reported conflicts and the runner did not force-apply the refactoring:\n" + testConflict.getMessage());
                }
                if (isIndexNotReady(throwable)) {
                    lastIndexError = throwable;
                    sleepQuietly(1000L * attempt);
                    continue;
                }
                String message = throwable.getMessage();
                if (message == null || message.isBlank()) {
                    message = throwable.getClass().getName();
                }
                return Result.rejected("IntelliJ rejected or aborted InlineMethod: " + message + "\n" + stackTrace(throwable));
            }
        }
        return Result.failed("IntelliJ indexing was not ready after retries:\n" + stackTrace(lastIndexError));
    }

    private static Result runRenameMethod(Project project, PsiMethod method, Path javaFile, String oldName, String newName) {
        return runRenameElement(project, method, javaFile, "RenameMethod", oldName, newName);
    }

    private static Result runRenameElement(Project project, PsiElement element, Path javaFile, String refactoring, String oldName, String newName) {
        if (newName == null || newName.isBlank()) {
            return Result.rejected(refactoring + " requires a non-empty new name.");
        }
        String sourceBefore;
        try {
            sourceBefore = Files.readString(javaFile, StandardCharsets.UTF_8);
        } catch (IOException ioException) {
            return Result.failed("Cannot read source file before " + refactoring + ": " + ioException.getMessage());
        }
        Throwable lastIndexError = null;
        for (int attempt = 1; attempt <= 4; attempt++) {
            try {
                DumbService.getInstance(project).waitForSmartMode();
                ApplicationManager.getApplication().invokeAndWait(() -> {
                    CapturingRenameProcessor processor = new CapturingRenameProcessor(project, element, newName, false, false);
                    processor.setPreviewUsages(false);
                    UsageInfo[] usages = processor.findUsages();
                    Ref<UsageInfo[]> usageRef = new Ref<>(usages);
                    if (!processor.preprocessUsages(usageRef)) {
                        throw new RefactoringAbortedException("RenameProcessor.preprocessUsages returned false.");
                    }
                    processor.executeEx(usageRef.get());
                    PsiDocumentManager.getInstance(project).commitAllDocuments();
                    FileDocumentManager.getInstance().saveAllDocuments();
                    LocalFileSystem.getInstance().refreshIoFiles(List.of(javaFile.toFile()), false, true, null);
                });
                waitForSourceChange(project, javaFile, sourceBefore, 5000);
                String sourceAfter = Files.readString(javaFile, StandardCharsets.UTF_8);
                if (sourceBefore.equals(sourceAfter)) {
                    return Result.failed(refactoring + " backend completed without changing the source file. "
                            + "Target=" + describePsiElement(element) + ", oldName=" + oldName + ", newName=" + newName + ".");
                }
                return Result.success(refactoring + " completed for " + oldName + " -> " + newName + ".");
            } catch (Throwable throwable) {
                RefactoringConflictException conflict = findCause(throwable, RefactoringConflictException.class);
                if (conflict != null) {
                    return Result.rejected("IntelliJ reported conflicts and the runner did not force-apply the refactoring:\n" + conflict.getMessage());
                }
                RefactoringAbortedException aborted = findCause(throwable, RefactoringAbortedException.class);
                if (aborted != null) {
                    return Result.rejected("IntelliJ aborted " + refactoring + " before applying changes:\n" + aborted.getMessage());
                }
                BaseRefactoringProcessor.ConflictsInTestsException testConflict =
                        findCause(throwable, BaseRefactoringProcessor.ConflictsInTestsException.class);
                if (testConflict != null) {
                    return Result.rejected("IntelliJ reported conflicts and the runner did not force-apply the refactoring:\n" + testConflict.getMessage());
                }
                if (isIndexNotReady(throwable)) {
                    lastIndexError = throwable;
                    sleepQuietly(1000L * attempt);
                    continue;
                }
                String message = throwable.getMessage();
                if (message == null || message.isBlank()) {
                    message = throwable.getClass().getName();
                }
                return Result.rejected("IntelliJ rejected or aborted " + refactoring + ": " + message + "\n" + stackTrace(throwable));
            }
        }
        return Result.failed("IntelliJ indexing was not ready after retries:\n" + stackTrace(lastIndexError));
    }

    private static void waitForSourceChange(Project project, Path javaFile, String sourceBefore, long timeoutMillis) {
        long deadline = System.currentTimeMillis() + timeoutMillis;
        while (System.currentTimeMillis() < deadline) {
            try {
                ApplicationManager.getApplication().invokeAndWait(() -> {
                    PsiDocumentManager.getInstance(project).commitAllDocuments();
                    FileDocumentManager.getInstance().saveAllDocuments();
                    LocalFileSystem.getInstance().refreshIoFiles(List.of(javaFile.toFile()), false, true, null);
                });
                String current = Files.readString(javaFile, StandardCharsets.UTF_8);
                if (!sourceBefore.equals(current)) {
                    return;
                }
                Thread.sleep(200);
            } catch (Throwable ignored) {
                return;
            }
        }
    }

    private static String describePsiElement(PsiElement element) {
        if (element instanceof PsiMethod method) {
            return "method " + describeMethod(method);
        }
        if (element instanceof PsiField field) {
            PsiClass containingClass = field.getContainingClass();
            return "field " + (containingClass == null ? "" : containingClass.getQualifiedName() + "#") + field.getName();
        }
        if (element instanceof PsiVariable variable) {
            return "variable " + variable.getName();
        }
        return element == null ? "<null>" : element.getClass().getName();
    }

    private static boolean isRenameRefactoring(String refactoring) {
        return refactoring != null && refactoring.toLowerCase().startsWith("rename");
    }

    private static String renameKindFromRefactoring(String refactoring) {
        String value = refactoring == null ? "" : refactoring.toLowerCase();
        if (value.contains("method")) {
            return "method";
        }
        if (value.contains("field")) {
            return "field";
        }
        return "variable";
    }

    private static Result runMoveInstanceMethod(Project project, PsiMethod method, PsiVariable targetVariable, Path javaFile, String targetMethod) {
        Throwable lastIndexError = null;
        for (int attempt = 1; attempt <= 4; attempt++) {
            try {
                DumbService.getInstance(project).waitForSmartMode();
                ApplicationManager.getApplication().invokeAndWait(() -> {
                    CapturingMoveInstanceMethodProcessor processor = new CapturingMoveInstanceMethodProcessor(
                            project,
                            method,
                            targetVariable
                    );
                    processor.setPreviewUsages(false);
                    processor.run();
                    processor.throwIfConflicts();
                    FileDocumentManager.getInstance().saveAllDocuments();
                    LocalFileSystem.getInstance().refreshIoFiles(List.of(javaFile.toFile()), false, true, null);
                });
                return Result.success("MoveInstanceMethod completed for " + targetMethod + ".");
            } catch (Throwable throwable) {
                RefactoringConflictException conflict = findCause(throwable, RefactoringConflictException.class);
                if (conflict != null) {
                    return Result.rejected("IntelliJ reported conflicts and the runner did not force-apply the refactoring:\n" + conflict.getMessage());
                }
                BaseRefactoringProcessor.ConflictsInTestsException testConflict =
                        findCause(throwable, BaseRefactoringProcessor.ConflictsInTestsException.class);
                if (testConflict != null) {
                    return Result.rejected("IntelliJ reported conflicts and the runner did not force-apply the refactoring:\n" + testConflict.getMessage());
                }
                if (isIndexNotReady(throwable)) {
                    lastIndexError = throwable;
                    sleepQuietly(1000L * attempt);
                    continue;
                }
                String message = throwable.getMessage();
                if (message == null || message.isBlank()) {
                    message = throwable.getClass().getName();
                }
                return Result.rejected("IntelliJ rejected or aborted MoveInstanceMethod: " + message + "\n" + stackTrace(throwable));
            }
        }
        return Result.failed("IntelliJ indexing was not ready after retries:\n" + stackTrace(lastIndexError));
    }

    private static Project openProjectOnEdt(Path caseDir) {
        final Project[] holder = new Project[1];
        ApplicationManager.getApplication().invokeAndWait(() -> holder[0] = ProjectManagerEx.getInstanceEx().openProject(
                caseDir,
                OpenProjectTask.build().withForceOpenInNewFrame(true)
        ));
        return holder[0];
    }

    private static void closeProjectOnEdt(Project project) {
        if (project == null || project.isDisposed()) {
            return;
        }
        ApplicationManager.getApplication().invokeAndWait(() -> ProjectManagerEx.getInstanceEx().forceCloseProject(project, true));
    }

    private static Target findTargetCall(
            PsiFile psiFile,
            String targetMethod,
            int targetStart,
            int targetLength
    ) {
        PsiMethod markedTarget = findMarkedTargetMethod(psiFile, targetMethod);
        Collection<PsiMethodCallExpression> calls = PsiTreeUtil.findChildrenOfType(psiFile, PsiMethodCallExpression.class);
        trace("method call candidates: " + calls.size());
        for (PsiMethodCallExpression call : calls) {
            String referenceName = call.getMethodExpression().getReferenceName();
            trace("method call candidate name=" + referenceName + " text=" + compact(call.getText()));
            if (!targetMethod.equals(referenceName)) {
                continue;
            }
            if (!selectionMatches(call, targetStart, targetLength)) {
                continue;
            }
            PsiMethod method = call.resolveMethod();
            PsiReference reference = call.getMethodExpression().getReference();
            if (method == null && reference != null && reference.resolve() instanceof PsiMethod resolvedMethod) {
                method = resolvedMethod;
            }
            if (method == null && markedTarget != null && reference != null) {
                trace("using marked target method fallback for call: " + compact(call.getText()));
                method = markedTarget;
            }
            trace("matched method call name=" + referenceName
                    + " method=" + describeMethod(method)
                    + " reference=" + (reference == null ? "null" : reference.getClass().getName()));
            if (method != null && reference != null) {
                return new Target(method, reference);
            }
        }
        Collection<PsiNewExpression> newExpressions = PsiTreeUtil.findChildrenOfType(psiFile, PsiNewExpression.class);
        for (PsiNewExpression expression : newExpressions) {
            PsiJavaCodeReferenceElement classReference = expression.getClassReference();
            String referenceName = classReference == null ? null : classReference.getReferenceName();
            if (!targetMethod.equals(referenceName)) {
                continue;
            }
            if (!selectionMatches(expression, targetStart, targetLength)) {
                continue;
            }
            PsiMethod method = expression.resolveConstructor();
            if (method != null && classReference != null) {
                return new Target(method, classReference);
            }
        }
        Collection<PsiMethodReferenceExpression> references = PsiTreeUtil.findChildrenOfType(psiFile, PsiMethodReferenceExpression.class);
        for (PsiMethodReferenceExpression referenceExpression : references) {
            String referenceName = referenceExpression.getReferenceName();
            if (!targetMethod.equals(referenceName)) {
                continue;
            }
            if (!selectionMatches(referenceExpression, targetStart, targetLength)) {
                continue;
            }
            if (referenceExpression.resolve() instanceof PsiMethod method) {
                return new Target(method, referenceExpression);
            }
        }
        return null;
    }

    private static boolean selectionMatches(PsiElement element, int targetStart, int targetLength) {
        if (targetStart < 0 || targetLength <= 0) {
            return true;
        }
        int targetEnd = targetStart + targetLength;
        int start = element.getTextRange().getStartOffset();
        int end = element.getTextRange().getEndOffset();
        return (start <= targetStart && end >= targetEnd)
                || (targetStart <= start && targetEnd >= end);
    }

    private static PsiMethod findMarkedTargetMethod(PsiFile psiFile, String targetMethod) {
        PsiMethod uniqueSameName = null;
        int sameNameCount = 0;
        Collection<PsiMethod> methods = PsiTreeUtil.findChildrenOfType(psiFile, PsiMethod.class);
        for (PsiMethod method : methods) {
            if (!targetMethod.equals(method.getName())) {
                continue;
            }
            sameNameCount++;
            if (method.getText().contains("target method") || method.getText().contains("rename target")) {
                trace("found marked target method: " + describeMethod(method));
                return method;
            }
            uniqueSameName = method;
        }
        if (sameNameCount == 1) {
            trace("using unique same-name method fallback: " + describeMethod(uniqueSameName));
            return uniqueSameName;
        }
        return null;
    }

    private static PsiElement findRenameTarget(PsiFile psiFile, String targetKind, String targetSymbol) {
        String kind = targetKind == null ? "" : targetKind.toLowerCase();
        if (kind.equals("method")) {
            return findMarkedTargetMethod(psiFile, targetSymbol);
        }
        if (kind.equals("field")) {
            PsiField uniqueSameName = null;
            int sameNameCount = 0;
            Collection<PsiField> fields = PsiTreeUtil.findChildrenOfType(psiFile, PsiField.class);
            for (PsiField field : fields) {
                if (!targetSymbol.equals(field.getName())) {
                    continue;
                }
                sameNameCount++;
                if (field.getText().contains("target field") || field.getText().contains("rename target")) {
                    trace("found marked target field: " + field.getName());
                    return field;
                }
                uniqueSameName = field;
            }
            if (sameNameCount == 1) {
                trace("using unique same-name field fallback: " + uniqueSameName.getName());
                return uniqueSameName;
            }
            return null;
        }

        PsiVariable uniqueSameName = null;
        int sameNameCount = 0;
        Collection<PsiVariable> variables = PsiTreeUtil.findChildrenOfType(psiFile, PsiVariable.class);
        for (PsiVariable variable : variables) {
            if (variable instanceof PsiField) {
                continue;
            }
            if (!targetSymbol.equals(variable.getName())) {
                continue;
            }
            sameNameCount++;
            if (variable.getText().contains("target variable")
                    || variable.getText().contains("target parameter")
                    || variable.getText().contains("rename target")) {
                trace("found marked target variable: " + variable.getName());
                return variable;
            }
            uniqueSameName = variable;
        }
        if (sameNameCount == 1) {
            trace("using unique same-name variable fallback: " + uniqueSameName.getName());
            return uniqueSameName;
        }
        return null;
    }

    private static PsiVariable findMoveTargetVariable(Project project, PsiMethod method, String targetSymbol, String targetHint) {
        return ApplicationManager.getApplication().runReadAction((Computable<PsiVariable>) () -> {
            List<PsiVariable> variables = new ArrayList<>();
            for (PsiParameter parameter : method.getParameterList().getParameters()) {
                variables.add(parameter);
            }
            PsiClass containingClass = method.getContainingClass();
            if (containingClass != null) {
                for (PsiField field : containingClass.getFields()) {
                    variables.add(field);
                }
            }
            if (variables.isEmpty()) {
                return null;
            }
            List<String> tokens = moveTargetTokens(targetSymbol, targetHint);
            for (String token : tokens) {
                for (PsiVariable variable : variables) {
                    if (token.equals(variable.getName())) {
                        return variable;
                    }
                }
            }
            for (String token : tokens) {
                for (PsiVariable variable : variables) {
                    if (typeMatches(variable.getType(), token)) {
                        return variable;
                    }
                }
            }
            if (tokens.isEmpty()) {
                return variables.get(0);
            }
            return null;
        });
    }

    private static boolean typeMatches(PsiType type, String token) {
        if (!(type instanceof PsiClassType classType)) {
            return false;
        }
        PsiClass psiClass = classType.resolve();
        String normalized = normalizeTargetToken(token);
        if (normalized.isBlank()) {
            return false;
        }
        String canonical = normalizeTargetToken(type.getCanonicalText());
        String presentable = normalizeTargetToken(type.getPresentableText());
        if (normalized.equals(canonical) || normalized.equals(presentable)) {
            return true;
        }
        if (psiClass == null) {
            return false;
        }
        String simpleName = normalizeTargetToken(psiClass.getName());
        String qualifiedName = normalizeTargetToken(psiClass.getQualifiedName());
        return normalized.equals(simpleName) || normalized.equals(qualifiedName);
    }

    private static List<String> moveTargetTokens(String targetSymbol, String targetHint) {
        Set<String> tokens = new LinkedHashSet<>();
        addMoveTargetToken(tokens, targetSymbol);
        if (targetHint != null && !targetHint.isBlank()) {
            Matcher targetClass = Pattern
                    .compile("(?i)target\\s+class\\s*:?\\s*([A-Za-z_$][\\w$]*(?:\\s*<[^>]+>)?)")
                    .matcher(targetHint);
            while (targetClass.find()) {
                addMoveTargetToken(tokens, targetClass.group(1));
            }
            Matcher toMatcher = Pattern
                    .compile("(?i)\\bto\\s+(?:class\\s+)?([A-Za-z_$][\\w$]*(?:\\s*<[^>]+>)?)")
                    .matcher(targetHint);
            while (toMatcher.find()) {
                addMoveTargetToken(tokens, toMatcher.group(1));
            }
            Matcher fieldMatcher = Pattern
                    .compile("(?i)target\\s+is\\s+field\\s+([A-Za-z_$][\\w$]*)")
                    .matcher(targetHint);
            while (fieldMatcher.find()) {
                addMoveTargetToken(tokens, fieldMatcher.group(1));
            }
        }
        return new ArrayList<>(tokens);
    }

    private static void addMoveTargetToken(Set<String> tokens, String raw) {
        String token = normalizeTargetToken(raw);
        if (!token.isBlank()) {
            tokens.add(token);
        }
    }

    private static String normalizeTargetToken(String raw) {
        if (raw == null) {
            return "";
        }
        return raw.trim().replaceAll("<.*>", "").replaceAll("[^A-Za-z0-9_$.]", "");
    }

    private static Map<String, String> parseOptions(List<String> args) {
        Map<String, String> result = new LinkedHashMap<>();
        for (String arg : args) {
            if (!arg.startsWith("--")) {
                continue;
            }
            int eq = arg.indexOf('=');
            if (eq < 0) {
                result.put(arg.substring(2), "");
            } else {
                result.put(arg.substring(2, eq), arg.substring(eq + 1));
            }
        }
        return result;
    }

    private static String required(Map<String, String> options, String key) {
        String value = options.get(key);
        if (value == null || value.isBlank()) {
            throw new IllegalArgumentException("Missing --" + key);
        }
        return value;
    }

    private static void writeJson(Path output, Result result) throws IOException {
        Files.createDirectories(output.toAbsolutePath().getParent());
        List<String> lines = new ArrayList<>();
        lines.add("{");
        lines.add("  \"status\": \"" + json(result.status) + "\",");
        lines.add("  \"applied\": " + result.applied + ",");
        lines.add("  \"severity\": \"" + json(result.severity) + "\",");
        lines.add("  \"messages\": [\"" + json(result.message) + "\"]");
        lines.add("}");
        Files.write(output, String.join("\n", lines).getBytes(StandardCharsets.UTF_8));
    }

    private static void safeWriteJson(Path output, Result result) {
        try {
            writeJson(output, result);
        } catch (IOException ioException) {
            System.err.println("Failed to write intermediate IntelliJ refactoring result: " + ioException.getMessage());
            ioException.printStackTrace(System.err);
        }
    }

    private static void trace(String message) {
        System.err.println("[ij-refactor-test] " + message);
    }

    private static String compact(String value) {
        if (value == null) {
            return "null";
        }
        String normalized = value.replace("\r", " ").replace("\n", " ").replace("\t", " ").trim();
        if (normalized.length() <= 180) {
            return normalized;
        }
        return normalized.substring(0, 180) + "...";
    }

    private static String describeMethod(PsiMethod method) {
        if (method == null) {
            return "null";
        }
        String containingClass = method.getContainingClass() == null ? "<no-class>" : method.getContainingClass().getQualifiedName();
        return containingClass + "#" + method.getName() + "@" + method.getTextOffset();
    }

    private static String json(String value) {
        StringBuilder builder = new StringBuilder();
        for (int i = 0; i < value.length(); i++) {
            char ch = value.charAt(i);
            switch (ch) {
                case '\\' -> builder.append("\\\\");
                case '"' -> builder.append("\\\"");
                case '\n' -> builder.append("\\n");
                case '\r' -> builder.append("\\r");
                case '\t' -> builder.append("\\t");
                default -> builder.append(ch);
            }
        }
        return builder.toString();
    }

    private static String stackTrace(Throwable throwable) {
        if (throwable == null) {
            return "";
        }
        StringWriter writer = new StringWriter();
        throwable.printStackTrace(new PrintWriter(writer));
        return writer.toString();
    }

    private static boolean isIndexNotReady(Throwable throwable) {
        for (Throwable current = throwable; current != null; current = current.getCause()) {
            String className = current.getClass().getName();
            String message = current.getMessage();
            if (className.contains("IndexNotReadyException")) {
                return true;
            }
            if (message != null && message.contains("IndexNotReadyException")) {
                return true;
            }
        }
        return false;
    }

    private static <T extends Throwable> T findCause(Throwable throwable, Class<T> expectedType) {
        for (Throwable current = throwable; current != null; current = current.getCause()) {
            if (expectedType.isInstance(current)) {
                return expectedType.cast(current);
            }
        }
        return null;
    }

    private static void sleepQuietly(long millis) {
        try {
            Thread.sleep(millis);
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
        }
    }

    private static final class CapturingIntroduceVariableHandler extends IntroduceVariableHandler {
        private final String newName;
        private final boolean replaceAll;
        private final List<String> failures = new ArrayList<>();

        private CapturingIntroduceVariableHandler(String newName, boolean replaceAll) {
            this.newName = newName;
            this.replaceAll = replaceAll;
        }

        @Override
        public IntroduceVariableSettings getSettings(
                Project project,
                Editor editor,
                PsiExpression expression,
                PsiExpression[] occurrences,
                TypeSelectorManagerImpl typeSelectorManager,
                boolean declareFinalIfAll,
                boolean anyAssignmentLhs,
                InputValidator validator,
                PsiElement anchor,
                IntroduceVariableBase.JavaReplaceChoice replaceChoice
        ) {
            return new IntroduceVariableSettings() {
                @Override
                public String getEnteredName() {
                    return newName;
                }

                @Override
                public boolean isReplaceAllOccurrences() {
                    return replaceAll;
                }

                @Override
                public boolean isDeclareFinal() {
                    return false;
                }

                @Override
                public boolean isReplaceLValues() {
                    return replaceAll && anyAssignmentLhs;
                }

                @Override
                public PsiType getSelectedType() {
                    PsiType selected = typeSelectorManager.getTypeSelector().getSelectedType();
                    return selected == null ? typeSelectorManager.getDefaultType() : selected;
                }

                @Override
                public boolean isOK() {
                    return validator == null || validator.isOK(this);
                }
            };
        }

        @Override
        protected void showErrorMessage(Project project, Editor editor, String message) {
            failures.add(message == null || message.isBlank() ? "ExtractVariable is not available for the selected expression." : message);
        }

        @Override
        protected boolean reportConflicts(
                MultiMap<PsiElement, String> conflicts,
                Project project,
                IntroduceVariableSettings settings
        ) {
            failures.addAll(conflicts.values());
            return false;
        }

        private String failureMessage() {
            return failures.isEmpty()
                    ? "IntroduceVariableHandler returned false without a diagnostic."
                    : String.join("\n", new LinkedHashSet<>(failures));
        }
    }

    private static final class CapturingInlineMethodProcessor extends InlineMethodProcessor {
        private final List<String> conflicts = new ArrayList<>();

        private CapturingInlineMethodProcessor(Project project, PsiMethod method, PsiReference reference, boolean isInlineThisOnly) {
            super(project, method, reference, null, isInlineThisOnly, false, false, true);
        }

        @Override
        protected boolean showConflicts(MultiMap<PsiElement, String> detectedConflicts, UsageInfo[] usages) {
            if (!detectedConflicts.isEmpty()) {
                conflicts.clear();
                conflicts.addAll(detectedConflicts.values());
                return false;
            }
            return super.showConflicts(detectedConflicts, usages);
        }

        private void throwIfConflicts() {
            if (!conflicts.isEmpty()) {
                throw new RefactoringConflictException(conflicts);
            }
        }
    }

    private static final class CapturingRenameProcessor extends RenameProcessor {
        private final PsiElement primaryElement;
        private final String newName;

        private CapturingRenameProcessor(Project project,
                                         PsiElement element,
                                         String newName,
                                         boolean isSearchInComments,
                                         boolean isSearchTextOccurrences) {
            super(project, element, newName, isSearchInComments, isSearchTextOccurrences);
            this.primaryElement = element;
            this.newName = newName;
        }

        @Override
        public boolean preprocessUsages(Ref<UsageInfo[]> refUsages) {
            MultiMap<PsiElement, String> conflicts = new MultiMap<>();
            RenameUtil.addConflictDescriptions(refUsages.get(), conflicts);
            RenamePsiElementProcessor.forElement(primaryElement)
                    .findExistingNameConflicts(primaryElement, newName, conflicts, myAllRenames);
            if (!conflicts.isEmpty()) {
                throw new RefactoringConflictException(conflicts.values());
            }
            boolean shouldContinue = super.preprocessUsages(refUsages);
            if (!shouldContinue) {
                throw new RefactoringAbortedException("RenameProcessor.preprocessUsages returned false.");
            }
            return true;
        }
    }

    private static final class CapturingMoveInstanceMethodProcessor extends MoveInstanceMethodProcessor {
        private final List<String> conflicts = new ArrayList<>();

        private CapturingMoveInstanceMethodProcessor(Project project, PsiMethod method, PsiVariable targetVariable) {
            super(project, method, targetVariable, null, MoveInstanceMethodHandler.suggestParameterNames(method, targetVariable));
        }

        @Override
        protected boolean showConflicts(MultiMap<PsiElement, String> detectedConflicts, UsageInfo[] usages) {
            if (!detectedConflicts.isEmpty()) {
                conflicts.clear();
                conflicts.addAll(detectedConflicts.values());
                return false;
            }
            return super.showConflicts(detectedConflicts, usages);
        }

        private void throwIfConflicts() {
            if (!conflicts.isEmpty()) {
                throw new RefactoringConflictException(conflicts);
            }
        }
    }

    private static final class RefactoringConflictException extends RuntimeException {
        private RefactoringConflictException(Collection<String> conflicts) {
            super(String.join("\n", conflicts));
        }
    }

    private static final class RefactoringAbortedException extends RuntimeException {
        private RefactoringAbortedException(String message) {
            super(message);
        }

        private RefactoringAbortedException(String message, Throwable cause) {
            super(message, cause);
        }
    }

    private static final class RefactoringUnsupportedException extends RuntimeException {
        private RefactoringUnsupportedException(String message) {
            super(message);
        }
    }

    private record Target(PsiMethod method, PsiReference reference) {
    }

    private record Result(String status, boolean applied, String severity, String message) {
        static Result success(String message) {
            return new Result("success", true, "OK", message);
        }

        static Result rejected(String message) {
            return new Result("rejected", false, "ERROR", message);
        }

        static Result warning(String message) {
            return new Result("warning", false, "WARNING", message);
        }

        static Result unsupported(String message) {
            return new Result("unsupported", false, "UNKNOWN", message);
        }

        static Result failed(String message) {
            return new Result("failed", false, "ERROR", message);
        }
    }
}
