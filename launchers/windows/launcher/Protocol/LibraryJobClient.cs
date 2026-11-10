// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 S2Arm contributors
using System.Text.Json;

namespace S2Arm.Launcher.Protocol;

public sealed record LibraryJobOutput(LibraryJobResult Report, ProbeOutput Output);

public static class LibraryJobClient
{
    public static async Task<LibraryJobOutput> RunAsync(LauncherPaths paths, string privateRoot, string library,
        string? libraryId, string operation, IReadOnlyList<string>? selectedPaths, CancellationToken cancellationToken, string? moveFrom = null)
    {
        if (operation is not ("setup" or "catalog" or "keys" or "import" or "diagnostics" or "locations" or "storageOverview" or "spaceSurvey"))
            throw new ArgumentException("Unsupported library operation.", nameof(operation));
        cancellationToken.ThrowIfCancellationRequested();
        privateRoot = Path.GetFullPath(privateRoot);
        var jobId = "win-" + Guid.NewGuid().ToString("N");
        var payload = new Dictionary<string, object?>
        {
            ["schema"] = 1, ["jobId"] = jobId, ["operation"] = operation,
            ["privateRoot"] = privateRoot, ["library"] = Path.GetFullPath(library),
            ["engineDir"] = Path.GetFullPath(paths.Engine), ["libraryId"] = libraryId
        };
        if (selectedPaths is not null) payload["paths"] = selectedPaths;
        if (moveFrom is not null) payload["moveFrom"] = Path.GetFullPath(moveFrom);
        if (operation is "keys" or "diagnostics")
        {
            if (selectedPaths?.Count != 1) throw new ArgumentException("Choose exactly one file.");
            payload["path"] = selectedPaths[0];
        }
        using var request = PrivateJobRequest.Create(privateRoot, JsonSerializer.Serialize(payload));
        var info = EnvironmentProbe.StartInfo(paths, request.FilePath);
        info.Arguments = "/d /v:off /s /c \"\"%S2A_UI_RUNTIME%\\s2arm_env.cmd\" && cd /d \"%S2A_UI_ENGINE%\" && python -X utf8 pack/jobs/main.py run --request \"%S2A_UI_REQUEST%\"\"";
        using var forcedStop = new CancellationTokenSource();
        var running = EnvironmentProbe.RunProcessAsync(info, forcedStop.Token, 8 * 1024 * 1024);
        var interrupted = Task.Delay(Timeout.Infinite, cancellationToken);
        if (await Task.WhenAny(running, interrupted) != running)
        {
            // Ask the owning job control plane first; it verifies the journal's pid/start time.
            using var controlLimit = new CancellationTokenSource(TimeSpan.FromSeconds(5));
            try
            {
                var control = EnvironmentProbe.StartInfo(paths, request.FilePath);
                control.Environment["S2A_UI_PRIVATE"] = privateRoot;
                control.Environment["S2A_UI_JOB"] = jobId;
                control.Arguments = "/d /v:off /s /c \"\"%S2A_UI_RUNTIME%\\s2arm_env.cmd\" && cd /d \"%S2A_UI_ENGINE%\" && python -X utf8 pack/jobs/main.py cancel --private-root \"%S2A_UI_PRIVATE%\" --task \"%S2A_UI_JOB%\"\"";
                await EnvironmentProbe.RunProcessAsync(control, controlLimit.Token);
                await Task.WhenAny(running, Task.Delay(TimeSpan.FromSeconds(3)));
            }
            catch (Exception error) when (error is IOException or OperationCanceledException or
                System.ComponentModel.Win32Exception or InvalidOperationException) { }
            finally
            {
                forcedStop.Cancel(); // Last resort only for the process tree this invocation owns.
                try { await running; }
                catch (Exception error) when (error is OperationCanceledException or IOException) { }
            }
            throw new OperationCanceledException(cancellationToken);
        }
        var output = await running;
        try { return new(LibraryJobResult.Parse(output.StandardOutput, output.ExitCode, jobId, operation), output); }
        catch (LibraryProtocolException error)
        {
            throw new LibraryProtocolException(error.Message + "\nExit: " + output.ExitCode + "\n" + output.StandardError, error);
        }
    }
}
