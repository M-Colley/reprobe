// Unity T1 (compile) entry point — Phase 3, requires the institution's own Unity seat.
// Injected into a unityci/editor container and invoked head-less:
//   unity-editor -batchmode -nographics -quit -projectPath /work \
//     -executeMethod ReprobeCompileCheck.Run
// Forces a script compile and exits non-zero on any compilation failure, so the
// runner can assert "scripts compile under Unity X.Y" — and nothing more.
using UnityEditor;
using UnityEditor.Compilation;
using UnityEngine;

public static class ReprobeCompileCheck
{
    public static void Run()
    {
        bool hadFailure = false;
        CompilationPipeline.compilationFinished += (object _) => { };
        CompilationPipeline.assemblyCompilationFinished += (string asm, CompilerMessage[] msgs) =>
        {
            foreach (var m in msgs)
                if (m.type == CompilerMessageType.Error)
                {
                    hadFailure = true;
                    Debug.LogError($"[reprobe] {asm}: {m.message}");
                }
        };
        CompilationPipeline.RequestScriptCompilation();
        EditorApplication.Exit(hadFailure ? 1 : 0);
    }
}
