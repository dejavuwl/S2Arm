// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 S2Arm contributors
using Avalonia;
using Avalonia.Media.Imaging;
using Avalonia.Threading;
using S2Arm.Launcher.Protocol;

namespace S2Arm.Launcher;

// 给玩家的 AI 用的本机控制口命令表（协议同 Mac S2ArmControl.swift，客户端同一份 s2armctl）：每个命令调界面按钮背后的同一个方法，屏幕上照常显示；
// 选文件的对话框改成直接给路径。Windows 只出 iPhone / iPad 包，Mac 专属命令（prepare、launch、选机型）明确回 409。
public sealed partial class EnvironmentWindow
{
    private ControlPort? control;
    private DispatcherTimer? controlTimer;
    private bool aiControl;
    private string aiControlError = "";

    private static readonly bool ControlForced = Environment.GetCommandLineArgs().Contains("--control") ||
        Environment.GetEnvironmentVariable("S2ARM_CONTROL") == "1";
    private string ControlSwitchFile => Path.Combine(privateRoot, "ai-control.on");

    /// <summary>崩溃日志用（LauncherCrash）：同 ResolvePrivateRoot，算不出（参数坏了）退回系统临时目录。</summary>
    internal static string PrivateRootForLogs()
    {
        try { return ResolvePrivateRoot(); } catch (Exception) { return Path.Combine(Path.GetTempPath(), "S2Arm"); }
    }

    private static string ResolvePrivateRoot()
    {
        const string prefix = "--data-root=";
        return Environment.GetCommandLineArgs().FirstOrDefault(a => a.StartsWith(prefix, StringComparison.Ordinal)) is { } argument
            ? Path.GetFullPath(argument[prefix.Length..])
            : LibraryPaths.StandardPrivateRoot;
    }

    private void StartControl()
    {
        aiControl = ControlForced || File.Exists(ControlSwitchFile);
        if (aiControl) OpenControl();
    }

    private void SetAiControl(bool on)
    {
        aiControlError = "";
        try
        {
            PrivateJobRequest.EnsurePrivateRoot(privateRoot);
            if (on) File.WriteAllBytes(ControlSwitchFile, []); else File.Delete(ControlSwitchFile);
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException) { aiControlError = error.Message; }
        aiControl = on || ControlForced;
        if (aiControl) OpenControl(); else CloseControl();
        Render();
    }

    private void OpenControl()
    {
        if (control is not null) return;
        try { control = new ControlPort(privateRoot, r => Dispatcher.UIThread.InvokeAsync(async () =>
        {
            var reply = await HandleControl(r);
            control?.UpdateState(ControlReply.Json(ControlState()).Body);   // 命令做完立刻更新快照：客户端接着读 /state，不能读到命令之前的错误
            return reply;
        })); }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or System.Net.Sockets.SocketException)
        { aiControl = false; aiControlError = error.Message; ControlPort.ReportStartFailure(privateRoot, error.Message); return; }
        control.UpdateState(ControlReply.Json(ControlState()).Body);
        controlTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
        controlTimer.Tick += (_, _) => control?.UpdateState(ControlReply.Json(ControlState()).Body);
        controlTimer.Start();
        // AI 接手时游戏库要已连上，它才看得到游戏
        if (!libraryBusy) EnsureLibrary(() => { });
    }

    private void CloseControl()
    {
        controlTimer?.Stop(); controlTimer = null;
        control?.Dispose(); control = null;
    }

    private bool ControlBusy => PackBusy || libraryBusy || importBusy || busy;
    private static ControlReply Busy() => ControlReply.Bad("有作业在跑：等它结束或 cancel 再来", 409);
    private static ControlReply MacOnly(string what) => ControlReply.Bad($"Windows 版没有{what}：Windows 只出 iPhone / iPad 包", 409);
    private LibraryGame? ControlGame(ControlRequest r) =>
        r.Text("gameId") is { } id ? catalog?.Games.FirstOrDefault(g => !g.Hidden && g.Id.Equals(id, StringComparison.OrdinalIgnoreCase)) : null;
    private static readonly ControlReply NoGame = ControlReply.Bad("没有这个 gameId");

    private async Task<bool> ControlConnected()
    {
        if (connectedFolder is null || libraryId is null)
        {
            if (!libraryBusy) EnsureLibrary(() => { });
            if (libraryOperation is { } operation) await operation;
        }
        return connectedFolder is not null && libraryId is not null;
    }

    private ControlReply ControlExport(LibraryGame game, bool spaceOverride = false)
    {
        selectedGame = game.Id;
        if (ExportBlocker(game) is { } why) return ControlReply.Bad(why, 409);
        StartPack(game, spaceOverride);
        return ControlReply.Ok();
    }

    private ControlReply ControlInstall(LibraryGame game, bool acceptDropped = false)
    {
        if (ControlBusy) return Busy();
        if (readinessFor != game.Id || !InstallAllowed) return ControlReply.Bad($"还不能装：先 install-check {game.Id}；这款的 iPhone 版没做好时要先出包");
        selectedGame = game.Id;
        Navigate("install");
        _ = InstallAsync(game, acceptDropped, ask: false);
        return ControlReply.Ok();
    }

    private async Task<ControlReply> HandleControl(ControlRequest r)
    {
        switch (r.Method, r.Path)
        {
            case ("GET", "/help"):
                return ControlReply.Json(new Dictionary<string, object?>
                {
                    ["version"] = AppVersion, ["frontend"] = "S2Arm Windows",
                    ["endpoints"] = Endpoints.Select(e => new Dictionary<string, string> { ["call"] = e.Call, ["what"] = e.What }).ToList()
                });
            case ("GET", "/targets"): case ("POST", "/target"):
                return MacOnly("选机型（iPhone 包通用，不用选）");
            case ("GET", "/snapshot"):
            {
                var size = new PixelSize(Math.Max(1, (int)Bounds.Width), Math.Max(1, (int)Bounds.Height));
                using var bitmap = new RenderTargetBitmap(size);
                bitmap.Render(this);
                using var stream = new MemoryStream();
                bitmap.Save(stream, new PngBitmapEncoderOptions());
                return new ControlReply(200, "image/png", stream.ToArray());
            }
            case ("POST", "/keys"):
            {
                if (r.Text("path") is not { Length: > 0 } path) return ControlReply.Bad("缺 path");
                if (ControlBusy) return Busy();
                if (!await ControlConnected()) return ControlReply.Bad(importError.Length > 0 ? importError : "游戏库没建好", 409);
                importDialog = new ImportDialogState(ImportKind.Keys);
                _ = RunImportKeysAsync(path);
                return ControlReply.Ok();
            }
            case ("POST", "/games"):
            {
                if (r.Strings("paths") is not { Length: > 0 } selected) return ControlReply.Bad("缺 paths");
                if (ControlBusy) return Busy();
                if (!await ControlConnected()) return ControlReply.Bad(importError.Length > 0 ? importError : "游戏库没建好", 409);
                importDialog = new ImportDialogState(ImportKind.Games);
                _ = RunImportGamesAsync(selected);
                return ControlReply.Ok();
            }
            case ("POST", "/ipa"):
                return ControlGame(r) is { } ipaGame ? ControlExport(ipaGame, r.Flag("spaceOverride")) : NoGame;
            case ("POST", "/build"):
                switch (r.Text("target") ?? "ios")
                {
                    case "ios" or "iphone" or "ipad": return ControlGame(r) is { } buildGame ? ControlExport(buildGame, r.Flag("spaceOverride")) : NoGame;
                    case "mac" or "macos": return MacOnly("Mac 版导出");
                    case "android" or "harmony" or "ohos": return ControlReply.Json(new Dictionary<string, object?> { ["error"] = "即将上线，敬请期待", ["comingSoon"] = true }, 409);
                    default: return ControlReply.Bad("target 只认 ios / android / harmony");
                }
            case ("POST", "/prepare"): return MacOnly("Mac 编译");
            case ("POST", "/launch"): return MacOnly("在电脑上启动游戏");
            case ("POST", "/fonts"): return ControlReply.Bad("Windows 版用应用自带的开源字体，不能换", 409);
            case ("POST", "/customize"):
            {
                if (ControlBusy) return Busy();
                if (ControlGame(r) is not { } customized) return NoGame;
                _ = CustomizeAsync(customized, r.Text("name"), r.Text("coverPath"));
                return ControlReply.Ok();
            }
            case ("POST", "/delete"):   // 同界面「删除这款游戏」（delete 作业）；控制口不弹确认，由 AI 先问过玩家
            {
                if (ControlBusy) return Busy();
                if (ControlGame(r) is not { } doomed) return NoGame;
                BeginGameJob("delete", doomed);
                return ControlReply.Ok();
            }
            case ("POST", "/diagnostics"):
            {
                if (r.Text("action") == "pull") return ControlReply.Bad("Windows 版不能拉回 iPhone 诊断：装机日志用 s2armctl logs 读", 409);
                if (r.Text("action") != "export" || r.Text("path") is not { Length: > 0 } output) return ControlReply.Bad("action 要 export，并带 path");
                if (ControlBusy) return Busy();
                if (!await ControlConnected()) return ControlReply.Bad(importError.Length > 0 ? importError : "游戏库没建好", 409);
                var job = await LibraryJobClient.RunAsync(paths, privateRoot, connectedFolder!, libraryId, "diagnostics", [output], CancellationToken.None);
                return job.Report.Succeeded ? ControlReply.Ok() : ControlReply.Bad(job.Report.Message, 500);
            }
            case ("POST", "/installCheck"):
            {
                if (ControlGame(r) is not { } game) return NoGame;
                selectedGame = game.Id; readinessFor = game.Id; readiness = null;
                Navigate("install");
                _ = CheckInstallAsync();
                return ControlReply.Ok();
            }
            case ("POST", "/install"):
                return ControlGame(r) is { } installGame ? ControlInstall(installGame, r.Flag("acceptDropped")) : NoGame;
            case ("POST", "/uninstall"):
            {
                if (ControlGame(r) is not { } game) return NoGame;
                if (ControlBusy) return Busy();
                selectedGame = game.Id;
                Navigate("install");
                _ = RunPhoneJobAsync(game, "installRemove", "install.remove", r.Flag("acceptDropped"), ask: false);
                return ControlReply.Ok();
            }
            case ("POST", "/restore-iphone"):   // s2armctl restore-iphone：装回上一版手机 S2Arm（同 Mac 控制口）
            {
                if (ControlGame(r) is not { } game) return NoGame;
                if (ControlBusy) return Busy();
                selectedGame = game.Id;
                Navigate("install");
                _ = RunPhoneJobAsync(game, "installRestore", "install.restore");
                return ControlReply.Ok();
            }
            case ("POST", "/cancel"):
            {
                if (r.Text("taskId") is null) return ControlReply.Bad("缺 taskId");
                var running = packCancellation ?? importCancellation ?? libraryCancellation;
                if (running is null) return ControlReply.Bad("没有能取消的作业");
                running.Cancel();
                return ControlReply.Ok();
            }
            case ("POST", "/retry"):
            {
                var task = ControlJournal.Tasks(privateRoot, 500).FirstOrDefault(t => t.GetProperty("id").GetString() == r.Text("taskId"));
                if (task.ValueKind == System.Text.Json.JsonValueKind.Undefined) return ControlReply.Bad("没有这个 taskId");
                var id = task.TryGetProperty("gameId", out var value) ? value.GetString() : null;
                var game = catalog?.Games.FirstOrDefault(g => g.Id == id && !g.Hidden);
                return (task.GetProperty("operation").GetString(), game) switch
                {
                    ("ipa", { } g) => ControlExport(g),
                    ("install", { } g) => ControlInstall(g),
                    _ => ControlReply.Bad("这条任务不能直接重试：照原样再发一次命令（keys / games 带路径）")
                };
            }
            case ("POST", "/update"):
                switch (r.Text("action"))
                {
                    case "restart":
                        if (updateReady is null) return ControlReply.Bad("新版还没下载好", 409);
                        if (ControlBusy) return Busy();
                        Dispatcher.UIThread.Post(RestartToUpdate, DispatcherPriority.Background);   // 先把回复发出去，再退出
                        return ControlReply.Ok();
                    case "retry":
                        _ = DownloadUpdateAsync();
                        return ControlReply.Ok();
                    default: return ControlReply.Bad("action 只认 restart / retry");
                }
            case ("POST", "/library"):
                if (r.Text("path") is not { Length: > 0 } library) return ControlReply.Bad("缺 path");
                if (ControlBusy) return Busy();
                libraryMoveFrom = r.Flag("move") ? connectedFolder : null;
                ConnectLibrary(Path.GetFullPath(library));
                return ControlReply.Ok();
            case ("POST", "/select"):
                if (r.Text("gameId") is { } selection) selectedGame = selection;
                return ControlReply.Ok();
            case ("POST", "/onboarding"): return ControlReply.Ok("Windows 版没有引导页，已忽略");
            case ("POST", "/environment"):
                if (!busy) _ = CheckAsync();
                return ControlReply.Ok();
            case ("POST", "/refresh"):
                if (ControlBusy) return Busy();
                libraryFolder = connectedFolder ?? libraryFolder;
                libraryOperation = RunLibraryAsync("catalog", null);
                return ControlReply.Ok();
            case ("POST", "/dismiss"):
                importError = ""; keysRejected = false; Render();
                return ControlReply.Ok();
            default:
                return ControlReply.Bad($"不认识 {r.Method} {r.Path}", 404);
        }
    }

    private static readonly (string Call, string What)[] Endpoints =
    [
        ("GET /state", "当前界面状态：环境检查（checks）、密钥、游戏列表、作业记录（tasks）"),
        ("GET /snapshot", "主窗口截图（PNG）"),
        ("POST /keys {path}", "导入 prod.keys"),
        ("POST /games {paths:[…]}", "放入游戏（NSP / XCI 文件、游戏文件夹或解包好的游戏文件夹）"),
        ("POST /build {gameId, target, spaceOverride?}", "出包：spaceOverride = true 同界面「仍然继续」；target = ios（iPhone / iPad，缺省）；android / harmony 回 409「即将上线」"),
        ("POST /customize {gameId, name?, coverPath?}", "改游戏显示名 / 封面（JPG、PNG）；不传的不动，传空字符串恢复默认；下次装到 iPhone 时带上"),
        ("POST /ipa {gameId, spaceOverride?}", "打 iPhone 包（出 IPA + 资源包）；spaceOverride = true 同界面「仍然继续」"),
        ("POST /installCheck {gameId}", "装机前清单：几秒后看 GET /state 的 installReadiness"),
        ("POST /install {gameId, acceptDropped?}", "用 Impactor 装到 iPhone（清单全齐才能装；Apple ID 由玩家本人在 Impactor 里登录）；失败码 container_would_drop = 手机上有别的电脑装的游戏这次带不上，先问玩家，同意了带 acceptDropped = true 再发"),
        ("POST /uninstall {gameId, acceptDropped?}", "从 iPhone 上的 S2Arm 删一款；acceptDropped 同 /install"),
        ("POST /cancel {taskId} | /retry {taskId}", "停掉在跑的作业 / 重试出包、装机任务"),
        ("POST /diagnostics {action:export, path}", "导出诊断包（已脱敏的 zip）"),
        ("POST /environment", "重新检查环境"), ("POST /refresh", "刷新游戏库"),
        ("POST /update {action:restart|retry}", "更新：restart = 重启并换上已下载好的新版，retry = 重试下载；进度看 GET /state 的 update"),
        ("POST /library {path}", "换游戏库位置（外接盘也行，换后要重新放游戏）"),
        ("POST /dismiss", "关掉提示"), ("POST /select {gameId}", "选中一款"),
    ];

    private Dictionary<string, object?> ControlState()
    {
        var keysReady = catalog?.KeysReady;
        var games = (catalog?.Games ?? []).Where(g => !g.Hidden).Select(g => new Dictionary<string, object?>
        {
            ["id"] = g.Id, ["name"] = g.Name, ["customName"] = g.CustomName, ["customCover"] = g.CustomCover, ["display"] = g.Version, ["compatibility"] = "", ["ready"] = g.IphoneReady, ["iphoneReady"] = g.IphoneReady,
            ["needsPreparation"] = false, ["needsKeys"] = g.NeedsKeys, ["buildable"] = g.Missing.Count == 0, ["missing"] = g.Missing,
            ["baseBytes"] = (g.Packages ?? []).Where(p => p.Type == "base").Sum(g.OwnBytes), ["bytes"] = g.TotalBytes
        }).ToList();
        object? session = null;
        if (packRunning) session = new Dictionary<string, object?> { ["kind"] = "ipa", ["gameId"] = packGame?.Id, ["name"] = packGame?.Name, ["phase"] = "running" };
        else if (importBusy) session = new Dictionary<string, object?> { ["kind"] = "import", ["gameId"] = null, ["name"] = importJobName, ["phase"] = "running" };
        else if (installBusy) session = new Dictionary<string, object?> { ["kind"] = "install", ["gameId"] = installGameId, ["name"] = installGameName, ["phase"] = "running",   // 装的那款，不是当前选中的（#308）
            ["done"] = installDone, ["total"] = installTotal, ["unit"] = installUnit };
        object? install = null;
        if (readiness is { } ready && readinessFor is { } readyFor)
            install = new Dictionary<string, object?>
            {
                ["gameId"] = readyFor, ["ready"] = ready.Ready, ["device"] = ready.Device, ["account"] = ready.Account,
                ["items"] = ready.Items.Select(i => new Dictionary<string, object?>
                    { ["id"] = i.Id, ["ok"] = i.Ok, ["waiting"] = i.Waiting, ["title"] = i.Title, ["detail"] = i.Detail, ["who"] = i.Who }).ToList()
            };
        return new()
        {
            ["frontend"] = "S2Arm Windows", ["platform"] = "windows",
            ["selection"] = page, ["selectedGame"] = selectedGame, ["showOnboarding"] = false, ["onboardingStep"] = 0,
            ["keyStatus"] = keysReady == true ? "已导入" : keysReady == false ? "没有密钥" : "还没读到", ["hasImportedKeys"] = keysReady == true,
            ["waitingForAccess"] = null, ["environmentReady"] = report?.Ready == true, ["runtimeReady"] = report?.Ready == true,
            ["checkingEnvironment"] = busy,
            ["checks"] = (report?.Checks ?? []).Select(c => new Dictionary<string, object?>
                { ["name"] = c.Name, ["ok"] = c.Status == "ok" || !c.Blocking, ["message"] = c.Message, ["group"] = "", ["action"] = c.Action }).ToList(),
            ["library"] = connectedFolder, ["games"] = games, ["session"] = session, ["busy"] = ControlBusy,
            ["tasks"] = ControlJournal.Tasks(privateRoot), ["queue"] = Array.Empty<string>(), ["notice"] = null,
            ["error"] = importError.Length > 0 ? importError : aiControlError.Length > 0 ? aiControlError : null,
            ["installReadiness"] = install, ["update"] = UpdateStatus, ["cachedAt"] = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0,
            ["lastSnapshotPath"] = "", ["windows"] = Array.Empty<object>(),
        };
    }
}
