// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 S2Arm contributors
using System.Diagnostics;
using System.Text;
using System.Text.Json;

namespace S2Arm.Launcher.Protocol;

/// <summary>
/// Incremental check of one jobs NDJSON stream (same rules as <see cref="LibraryJobResult.Parse"/>): schema 1, the request's
/// jobId / operation, strictly increasing seq, non-result events still running, exactly one terminal result and nothing after it,
/// and the process exit agreeing with that result. Long jobs (ipa / install) need each event as it arrives, not after exit.
/// </summary>
public sealed class JobEventReader(string jobId, string operation)
{
    private long previous;
    public LibraryJobResult? Result { get; private set; }

    /// <summary>Validates one stdout line; returns the event (detached from the parser) or null for a blank line.</summary>
    public JsonElement? Accept(string line)
    {
        if (string.IsNullOrWhiteSpace(line)) return null;
        try
        {
            if (Result is not null) throw new LibraryProtocolException("Event follows the terminal result.");
            using var document = JsonDocument.Parse(line);
            var e = document.RootElement;
            LibraryJobResult.UniqueFields(e);
            if (e.GetProperty("schema").GetInt32() != 1 || LibraryJobResult.Text(e, "jobId") != jobId ||
                LibraryJobResult.Text(e, "operation") != operation ||
                (e.TryGetProperty("taskId", out var task) && task.GetString() != jobId))
                throw new LibraryProtocolException("Event schema, task or operation does not match the request.");
            var seq = e.GetProperty("seq").GetInt64();
            if (seq <= previous) throw new LibraryProtocolException("Event sequence is not increasing.");
            previous = seq;
            var type = LibraryJobResult.Text(e, "type");
            var state = LibraryJobResult.Text(e, "state");
            if (type != "result")
            {
                if (state != "running") throw new LibraryProtocolException("Terminal state lacks a result event.");
                return e.Clone();
            }
            if (state is not ("succeeded" or "failed" or "cancelled"))
                throw new LibraryProtocolException("Unknown terminal result state.");
            var payload = e.GetProperty("result");
            if (payload.ValueKind != JsonValueKind.Object) throw new LibraryProtocolException("Result must be an object.");
            Result = new(state, LibraryJobResult.Text(e, "message", allowEmpty: true), payload.Clone(),
                e.TryGetProperty("diagnostics", out var diagnostics) ? diagnostics.Clone() : default);
            return e.Clone();
        }
        catch (Exception error) when (error is JsonException or InvalidOperationException or KeyNotFoundException or FormatException or OverflowException)
        {
            throw new LibraryProtocolException("Invalid jobs event stream.", error);
        }
    }

    /// <summary>End of stream: a terminal result must exist and agree with the process exit code.</summary>
    public LibraryJobResult Finish(int exitCode)
    {
        if (Result is null) throw new LibraryProtocolException("EOF without a terminal result.");
        if (exitCode != (Result.State == "succeeded" ? 0 : Result.State == "cancelled" ? 130 : 1))
            throw new LibraryProtocolException("Terminal result disagrees with process exit.");
        return Result;
    }
}

/// <summary>
/// Export / install operations of <c>pack/jobs/main.py run --request</c>:
/// the iPhone package (ipa), the pre-install checklist (installCheck) and install, plus the per-game library operations of
/// library layout v3 (moveGame / relinkGame / forgetGame, clean = clear cache). Same private request, same fixed
/// command line and same cancel path as the library operations; events stream to the caller as they arrive.
/// </summary>
public static class PackJobClient
{
    public static readonly IReadOnlySet<string> Operations =
        new HashSet<string>(StringComparer.Ordinal) { "ipa", "installCheck", "install", "installRemove", "installRestore", "customize",
            "moveGame", "relinkGame", "forgetGame", "clean", "delete", "spaceFree" };

    public sealed record Request(LauncherPaths Paths, string PrivateRoot, string Library, string LibraryId, string Operation,
        string GameId, IReadOnlyDictionary<string, object?>? Extra = null);

    /// <summary>The request body the backend reads (pure; unit-tested).</summary>
    public static Dictionary<string, object?> Payload(Request request, string jobId)
    {
        if (!Operations.Contains(request.Operation)) throw new ArgumentException("Unsupported pack operation.", nameof(request));
        if (string.IsNullOrWhiteSpace(request.GameId)) throw new ArgumentException("A game is required.", nameof(request));
        var payload = new Dictionary<string, object?>
        {
            ["schema"] = 1, ["jobId"] = jobId, ["operation"] = request.Operation,
            ["privateRoot"] = Path.GetFullPath(request.PrivateRoot), ["library"] = Path.GetFullPath(request.Library),
            ["engineDir"] = Path.GetFullPath(request.Paths.Engine), ["libraryId"] = request.LibraryId, ["gameId"] = request.GameId,
            // The job stops itself when this launcher process is gone (main.py supervise).
            ["parentPid"] = Environment.ProcessId
        };
        // The bundled iPhone shell is a launcher path setting (Settings → Shell); ipa.bundled_shell reads shellDir.
        if (request.Operation is "ipa" or "install") payload["shellDir"] = Path.GetFullPath(request.Paths.Shell);
        // Bundled resources next to S2Arm.exe (bundle_win.py layout): ipa reads the shared fonts from resources/Fonts (core.fonts_dir; without it
        // "shared fonts missing", tianyan 11-07); installCheck / install use the Impactor + plumesign shipped in resources/ThirdParty/Impactor
        // (pack/jobs/impactor.py bundled, 11-07 维护者: no download detour for players).
        if (request.Operation is "ipa" or "installCheck" or "install" or "installRemove" or "installRestore") payload["resources"] = Path.GetFullPath(AppContext.BaseDirectory);
        foreach (var (key, value) in request.Extra ?? new Dictionary<string, object?>())
        {
            if (payload.ContainsKey(key)) throw new ArgumentException("Extra field overrides the request envelope: " + key);
            payload[key] = value;
        }
        // Library layout v3 (pack/jobs/library.py): a storage location / game folder is an absolute path chosen by the player;
        // moveGame / relinkGame need theirs, ipa may carry one (export page), clean is offered only as "clear cache".
        foreach (var (operation, key, required) in new[] { ("ipa", "location", false), ("moveGame", "location", true), ("relinkGame", "folder", true) })
            if (request.Operation == operation && (payload.TryGetValue(key, out var value) ? value is not string path || !Path.IsPathFullyQualified(path) : required))
                throw new ArgumentException("An absolute folder is required: " + key, nameof(request));
        if (request.Operation == "clean" && (payload.TryGetValue("kind", out var kind) ? kind as string : null) != "cache")
            throw new ArgumentException("Only the cache can be cleared here.", nameof(request));
        return payload;
    }

    public static async Task<LibraryJobOutput> RunAsync(Request request, Action<JsonElement>? onEvent, CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        var jobId = "win-" + Guid.NewGuid().ToString("N");
        var privateRoot = Path.GetFullPath(request.PrivateRoot);
        using var file = PrivateJobRequest.Create(privateRoot, JsonSerializer.Serialize(Payload(request, jobId)));
        var info = EnvironmentProbe.StartInfo(request.Paths, file.FilePath);
        info.Arguments = "/d /v:off /s /c \"\"%S2A_UI_RUNTIME%\\s2arm_env.cmd\" && cd /d \"%S2A_UI_ENGINE%\" && python -X utf8 pack/jobs/main.py run --request \"%S2A_UI_REQUEST%\"\"";
        var reader = new JobEventReader(jobId, request.Operation);
        using var process = EnvironmentProbe.StartProcess(info);
        var stderr = new StringBuilder();
        var errors = Task.Run(async () =>
        {
            var buffer = new char[4096];
            int count;
            while ((count = await process.StandardError.ReadAsync(buffer.AsMemory())) != 0)
                lock (stderr)
                {
                    stderr.Append(buffer, 0, count);
                    if (stderr.Length > 128 * 1024) stderr.Remove(0, stderr.Length - 128 * 1024);   // keep the tail only
                }
        });
        var events = Task.Run(async () =>
        {
            // One event per line; a single line over 4 Mi characters is not a jobs event.
            try
            {
                while (await process.StandardOutput.ReadLineAsync() is { } line)
                {
                    if (line.Length > 4 * 1024 * 1024) throw new LibraryProtocolException("Event line exceeded the limit.");
                    if (reader.Accept(line) is { } e) onEvent?.Invoke(e);
                }
            }
            catch { KillOwned(process); throw; }   // an invalid stream ends this invocation and its own process tree
        });
        var interrupted = Task.Delay(Timeout.Infinite, cancellationToken);
        var finished = Task.WhenAll(events, errors, process.WaitForExitAsync());
        try
        {
            if (await Task.WhenAny(finished, interrupted) != finished)
            {
                await CancelAsync(request.Paths, file.FilePath, privateRoot, jobId);
                await Task.WhenAny(finished, Task.Delay(TimeSpan.FromSeconds(3)));
                throw new OperationCanceledException(cancellationToken);
            }
            await finished;
        }
        catch (LibraryProtocolException error)
        {
            throw new LibraryProtocolException(error.Message + "\n" + Tail(stderr), error);
        }
        finally { KillOwned(process); }
        var output = new ProbeOutput(process.ExitCode, "", Tail(stderr));
        try { return new(reader.Finish(process.ExitCode), output); }
        catch (LibraryProtocolException error)
        {
            throw new LibraryProtocolException(error.Message + "\nExit: " + process.ExitCode + "\n" + output.StandardError, error);
        }
    }

    // Ask the job control plane first (it verifies the journal's pid / start time), then stop only our own process tree.
    private static async Task CancelAsync(LauncherPaths paths, string requestFile, string privateRoot, string jobId)
    {
        using var limit = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        try
        {
            var control = EnvironmentProbe.StartInfo(paths, requestFile);
            control.Environment["S2A_UI_PRIVATE"] = privateRoot;
            control.Environment["S2A_UI_JOB"] = jobId;
            control.Arguments = "/d /v:off /s /c \"\"%S2A_UI_RUNTIME%\\s2arm_env.cmd\" && cd /d \"%S2A_UI_ENGINE%\" && python -X utf8 pack/jobs/main.py cancel --private-root \"%S2A_UI_PRIVATE%\" --task \"%S2A_UI_JOB%\"\"";
            await EnvironmentProbe.RunProcessAsync(control, limit.Token);
        }
        catch (Exception error) when (error is IOException or OperationCanceledException or
            System.ComponentModel.Win32Exception or InvalidOperationException) { }
    }

    private static void KillOwned(Process process)
    {
        try { if (!process.HasExited) process.Kill(entireProcessTree: true); }
        catch (InvalidOperationException) { }
    }

    private static string Tail(StringBuilder stderr)
    {
        lock (stderr) return stderr.ToString();
    }
}
