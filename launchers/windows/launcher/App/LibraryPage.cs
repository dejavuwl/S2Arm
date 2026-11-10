// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 S2Arm contributors
using System.Diagnostics;
using System.Text.Json;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Media.Imaging;
using Avalonia.Platform.Storage;
using Avalonia.VisualTree;
using S2Arm.Launcher.Protocol;

namespace S2Arm.Launcher;

public sealed partial class EnvironmentWindow
{
    private string page = "library";
    private string libraryFolder = "";
    private string? libraryMoveFrom;
    private string? connectedFolder, libraryId, selectedGame;
    private LibraryCatalog? catalog;
    private string librarySummary = "library.disconnected", libraryDiagnostics = "";
    private bool libraryBusy, closingLibrary;
    private CancellationTokenSource? libraryCancellation;
    private Task? libraryOperation;
    private TextBox libraryFolderBox = new();
    private readonly Dictionary<string, Bitmap> covers = new(StringComparer.OrdinalIgnoreCase);
    private readonly string privateRoot = ResolvePrivateRoot();

    private void Navigate(string destination)
    {
        CapturePaths();
        if (page == "library") libraryFolder = libraryFolderBox.Text ?? "";
        page = destination; Render();
    }

    /// <summary>
    /// 换库 / 刷新时丢掉封面缓存。不能当场 Dispose：这时窗口里还挂着上一次 Render 建的界面，封面 Image 引用着这些位图；
    /// 一让出界面线程（await 作业）排版就去量已释放的位图，Avalonia 渲染循环里抛 ObjectDisposedException，0.1.7 上整个窗口空掉
    /// （玩家反馈「点连接或创建游戏库闪退」，Windows 实机按原样复现：库里有一款带封面就必崩，事件 1026 堆栈 Bitmap.get_Size ← Image.MeasureOverride）。
    /// 先记下它们和当时的窗口内容，等窗口内容换成新界面（旧界面脱离窗口、不再排版）再释放；窗口关掉时一并释放。
    /// </summary>
    private void ClearCovers()
    {
        if (covers.Count == 0) return;
        retiredCovers.Add((Content, covers.Values.ToList()));
        covers.Clear();
    }

    private readonly List<(object? Content, List<Bitmap> Covers)> retiredCovers = [];

    /// <summary>窗口内容已经不是退役时那份的封面才释放（Render 末尾、窗口关闭时调）</summary>
    private void DisposeRetiredCovers(bool all = false)
    {
        for (var i = retiredCovers.Count - 1; i >= 0; i--)
        {
            if (!all && ReferenceEquals(retiredCovers[i].Content, Content)) continue;
            foreach (var cover in retiredCovers[i].Covers) cover.Dispose();
            retiredCovers.RemoveAt(i);
        }
    }

    private async void CloseLibraryAsync(object? sender, WindowClosingEventArgs args)
    {
        if (!libraryBusy) return;
        args.Cancel = true;
        if (closingLibrary) return;
        closingLibrary = true;
        libraryCancellation?.Cancel();
        if (libraryOperation is { } operation) await operation;
        Close();
    }

    private Border Card(Control content, int padding = 20) => new()
    {
        Background = Surface, CornerRadius = new CornerRadius(12), Padding = new Thickness(padding), Child = content
    };

    private Control GameCover(LibraryGame game, int size)
    {
        // Only a backend-provided local cover in this library is considered; never load a remote URL.
        try
        {
            // 只认这款自己文件夹里的封面（LibraryGame.CoverFile：按 catalog 给的文件夹认，英文名文件夹也行，#330）
            if (connectedFolder is not null && game.CoverFile() is { } path && new FileInfo(path).Length <= 16 * 1024 * 1024)
            {
                if (!covers.TryGetValue(path, out var image))
                {
                    using var file = File.OpenRead(path);
                    image = Bitmap.DecodeToWidth(file, 240);
                    covers.Add(path, image);
                }
                return new Image { Source = image, Width = size, Height = size, Stretch = Stretch.UniformToFill };
            }
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or ArgumentException or NotSupportedException) { }
        return new Border { Width = size, Height = size, CornerRadius = new CornerRadius(8),
            Background = Brush.Parse("#303035"), Padding = new Thickness(size / 8),
            Child = new Image
            {
                Source = LoadWordmark(),
                Stretch = Stretch.Uniform
            } };
    }

    private Bitmap? wordmarkBitmap;
    private Bitmap LoadWordmark()
    {
        if (wordmarkBitmap is not null) return wordmarkBitmap;
        using var stream = Avalonia.Platform.AssetLoader.Open(new Uri("avares://S2Arm/Assets/Wordmark.png"));
        return wordmarkBitmap = Bitmap.DecodeToWidth(stream, 480);
    }

    private void RenderLibrary(StackPanel body)
    {
        body.Children.Add(Label(T("library.title"), 23));
        body.Children.Add(Label(T("library.description")));

        // In-progress capsule: shown when a pack is running and the session is hidden
        if (packRunning && sessionHidden)
        {
            var elapsed = (DateTime.UtcNow - packStarted).TotalSeconds;
            var estimate = packTrack.Estimate(Clock);
            var fraction = OverallFraction(estimate, packTrack.Plan);
            var capsuleBar = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto"), ColumnSpacing = 16 };
            var left = new StackPanel { Spacing = 4, VerticalAlignment = VerticalAlignment.Center };
            left.Children.Add(Muted(T("progress.running") + " · " + (packGame?.Name ?? ""), 13));
            left.Children.Add(new ProgressBar { Minimum = 0, Maximum = 1, Value = fraction, Height = 4, Foreground = Accent });
            capsuleBar.Children.Add(left);
            var back = Primary(T("export.pill.running"), () => { sessionHidden = false; Navigate("progress"); });
            back.Padding = new Thickness(14, 8); back.CornerRadius = new CornerRadius(16);
            Grid.SetColumn(back, 1); capsuleBar.Children.Add(back);
            body.Children.Add(new Border { Background = Brush.Parse("#1D1D20"), CornerRadius = new CornerRadius(12),
                Padding = new Thickness(16, 12), Child = capsuleBar });
        }

        var games = catalog?.Games.Where(g => !g.Hidden).ToArray() ?? [];
        var hero = games.FirstOrDefault(g => g.Id == selectedGame) ?? games.FirstOrDefault();
        if (hero is not null)
        {
            var banner = new Grid { ColumnDefinitions = new ColumnDefinitions("100,*"), ColumnSpacing = 24 };
            banner.Children.Add(GameCover(hero, 100));
            var title = new StackPanel { Spacing = 8 };
            title.Children.Add(Label(T(selectedGame is null ? "library.title" : "library.selected")));
            title.Children.Add(Label(hero.Name, 28));
            title.Children.Add(Label(T("library.version") + " · " + hero.Version));
            Grid.SetColumn(title, 1); banner.Children.Add(title); body.Children.Add(Card(banner, 20));
        }

        var connection = new StackPanel { Spacing = 10 };
        connection.Children.Add(Label(T("library.folder")));
        libraryFolderBox = new TextBox { Text = libraryFolder, IsEnabled = !libraryBusy };
        Avalonia.Automation.AutomationProperties.SetAutomationId(libraryFolderBox, "library.folder");
        connection.Children.Add(libraryFolderBox);
        var buttons = new WrapPanel { Orientation = Orientation.Horizontal };
        void AddButton(string key, Action action, bool enabled = true)
        {
            var button = Action(key, action, enabled && !libraryBusy); button.Margin = new Thickness(0, 0, 10, 8);
            buttons.Children.Add(button);
        }
        AddButton("library.choose_folder", () => BeginPicker(ChooseLibraryAsync));
        AddButton("library.connect", () => ConnectTypedFolder(libraryFolderBox.Text));
        AddButton("library.refresh", () => BeginLibrary("catalog"), libraryId is not null);
        connection.Children.Add(buttons);
        connection.Children.Add(Label(T("library.source_hint")));
        if (catalog is null) body.Children.Add(Card(connection));
        else body.Children.Add(new Expander { Header = T("library.folder") + " · " + connectedFolder,
            HorizontalAlignment = HorizontalAlignment.Stretch, Content = Card(connection) });

        var status = new StackPanel { Spacing = 8 };
        status.Children.Add(Label(T(librarySummary), 18));
        // 失败时原因直接写在这里（第一行就是作业 / 输入校验给的人话），不用再展开「本次操作与处理建议」才看得到
        if (librarySummary is "library.failed" or "library.invalid_result" && libraryDiagnostics.Split('\n', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries).FirstOrDefault() is { Length: > 0 } why)
            status.Children.Add(Muted(why, 14));
        if (libraryBusy) status.Children.Add(new ProgressBar { IsIndeterminate = true, Height = 4 });
        if (catalog is not null)
        {
            status.Children.Add(Label(T("library.keys_status") + " · " + catalog.KeysMessage));
            var imports = new WrapPanel { Orientation = Orientation.Horizontal };
            foreach (var (key, handler) in new (string, Func<Task>)[] {
                ("library.import_keys", () => ChooseFilesAsync(true)),
                ("library.import_files", () => ChooseFilesAsync(false)),
                ("library.import_folder", ChooseSourceFolderAsync) })
            {
                var button = Action(key, () => BeginPicker(handler), !libraryBusy);
                button.Margin = new Thickness(0, 0, 10, 8); imports.Children.Add(button);
            }
            var storage = Action("storage.open", OpenStorage, !libraryBusy && libraryId is not null);
            storage.Margin = new Thickness(0, 0, 10, 8); AutomationId(storage, "library.storage"); imports.Children.Add(storage);
            status.Children.Add(imports);
        }
        body.Children.Add(Card(status));

        foreach (var game in games)
        {
            var line = new Grid { ColumnDefinitions = new ColumnDefinitions("72,*,Auto"), ColumnSpacing = 18 };
            line.Children.Add(GameCover(game, 72));
            var detail = new StackPanel { Spacing = 5 };
            detail.Children.Add(Label(game.Name, 18));
            detail.Children.Add(Label(T("library.version") + " · " + game.Version));
            if (game.Missing.Count > 0)
                detail.Children.Add(Label(T("library.requirements") + " · " + string.Join(" / ", game.Missing)));
            // iPhone / Mac dual status line
            detail.Children.Add(GameStatusLine(game));
            detail.Children.Add(GameSizesLine(game.Sizes));   // 这款各部分大小（游戏库结构 v3「查」，StoragePage.cs）
            Grid.SetColumn(detail, 1); line.Children.Add(detail);
            // Primary button: if iPhone pack is ready → install; otherwise export (storage not connected → disabled, game files missing → relink / remove)
            var primaryKey = game.IphoneReady ? "library.hero.install" : "library.select";
            var primaryAction = game.IphoneReady
                ? (Action)(() => { libraryFolder = libraryFolderBox.Text ?? ""; selectedGame = game.Id; readinessFor = null; Navigate("install"); })
                : game.LocationState == "missing" ? () => _ = MissingAsync(game)
                : () => { libraryFolder = libraryFolderBox.Text ?? ""; selectedGame = game.Id; OpenBuild(); };
            // 装机在跑时别的游戏照常能选、看详情、出包（只锁正在装的那款；同时只装一款：点别的「装到 iPhone」进装机页提示「另一款正在装 · 查看那一款」）
            var select = Action(primaryKey, primaryAction, !BusyFor(game) && (game.IphoneReady || game.LocationState != "offline"));
            select.VerticalAlignment = VerticalAlignment.Center;
            // secondary: show past result if one exists and game is not currently running
            if (finishedPacks.TryGetValue(game.Id, out _) && !(packRunning && packGame?.Id == game.Id))
            {
                var actionCol = new StackPanel { Spacing = 6, VerticalAlignment = VerticalAlignment.Center };
                actionCol.Children.Add(select);
                var results = Action("library.hero.results", () =>
                { libraryFolder = libraryFolderBox.Text ?? ""; selectedGame = game.Id; Navigate("progress"); }, !BusyFor(game));
                actionCol.Children.Add(results);
                Grid.SetColumn(actionCol, 2); line.Children.Add(actionCol);
            }
            else
            {
                Grid.SetColumn(select, 2); line.Children.Add(select);
            }
            var card = Card(line, 12);
            card.ContextMenu = CardMenu(game);
            // 找不到游戏文件：点卡片（按钮以外）弹「重新指定文件夹 / 从游戏库移除」
            if (game.LocationState == "missing")
                card.Tapped += (_, e) => { if ((e.Source as Visual)?.FindAncestorOfType<Button>(includeSelf: true) is null) _ = MissingAsync(game); };
            body.Children.Add(card);
        }
        if (catalog is not null && games.Length == 0) body.Children.Add(Label(T("library.empty"), 18));
        if (libraryDiagnostics.Length > 0)
            body.Children.Add(new Expander { Header = T("library.job_diagnostics"), IsExpanded = librarySummary is "library.failed" or "library.invalid_result" or "library.interrupted",
                HorizontalAlignment = HorizontalAlignment.Stretch,
                Content = new TextBox { Text = libraryDiagnostics, IsReadOnly = true, AcceptsReturn = true,
                    TextWrapping = TextWrapping.Wrap, MaxHeight = 240 } });
    }

    /// <summary>iPhone / Mac dual status line for a game card — mirrors Mac statusLine(game).</summary>
    private Control GameStatusLine(LibraryGame game)
    {
        string text; string color;
        // Running or queued
        if (packRunning && packGame?.Id == game.Id)
        {
            text = T("s2arm.job.running"); color = "#FFC857";
        }
        else if (finishedPacks.ContainsKey(game.Id) && packRunning && packGame?.Id == game.Id)
        {
            text = T("export.status.queued"); color = "#FFC857";
        }
        // 存放位置（游戏库结构 v3）：移动硬盘没插 → 灰色「未连接」，插回来重读游戏库就恢复；文件夹没了 → 红字，点卡片重新指定或移除
        else if (game.LocationState == "offline")
        {
            text = T("library.location.offline").Replace("{0}", game.Location!.Volume.Length > 0 ? game.Location.Volume : game.Location.Path); color = "#4A4A50";
        }
        else if (game.LocationState == "missing")
        {
            text = T("library.location.missing"); color = "#E05C5C";
        }
        else
        {
            var parts = new List<string>();
            if (game.IphoneReady)
            {
                if (game.IphoneInstalledAt is not null)
                {
                    // days left (free signing = 7 days from install)
                    if (game.IphoneFree)
                    {
                        var daysLeft = (int)Math.Ceiling((game.IphoneInstalledAt.Value + 7 * 86400 - DateTimeOffset.UtcNow.ToUnixTimeSeconds()) / 86400.0);
                        daysLeft = Math.Max(0, daysLeft);
                        if (daysLeft == 0)
                            parts.Add(T("library.status.expired"));
                        else
                            parts.Add(T("library.status.installed").Replace("{0}", daysLeft.ToString()));
                    }
                    else parts.Add(T("library.status.installedPaid"));
                }
                else parts.Add(T("library.status.iphone"));
            }
            if (parts.Count == 0 && finishedPacks.TryGetValue(game.Id, out var fin) && fin.state == "succeeded")
                parts.Add(T("library.status.iphone"));
            if (parts.Count == 0)
            {
                text = T("s2arm.library.notPrepared"); color = "#4A4A50";
                goto render;
            }
            text = string.Join(" · ", parts);
            color = game.IphoneInstalledAt is not null && game.IphoneFree &&
                (int)Math.Ceiling((game.IphoneInstalledAt.Value + 7 * 86400 - DateTimeOffset.UtcNow.ToUnixTimeSeconds()) / 86400.0) == 0
                ? "#E05C5C" : "#52C87A";
        }
        render:
        var dot = new Border { Width = 7, Height = 7, CornerRadius = new CornerRadius(4),
            Background = Brush.Parse(color), Margin = new Thickness(0, 3, 6, 0), VerticalAlignment = VerticalAlignment.Top };
        var lbl = new TextBlock { Text = text, FontSize = 12, Foreground = Brush.Parse(color), TextWrapping = TextWrapping.Wrap };
        var row = new StackPanel { Orientation = Orientation.Horizontal };
        row.Children.Add(dot); row.Children.Add(lbl);
        return row;
    }


    /// <summary>游戏卡片右键菜单（游戏库结构 v3，照 Mac cardMenu；Windows 只出 iPhone 包，没有开始游戏 / 启动设置）：
    /// 出包、在资源管理器中显示、移动到其他位置、清除缓存、从 iPhone 删除这款、删除这款游戏（delete 作业：删游戏文件夹与出包产物，存档和原件保留，同 Mac）。
    /// 位置未连接 / 找不到时出包、移动、清缓存不可用。</summary>
    private ContextMenu CardMenu(LibraryGame game)
    {
        var idle = !BusyFor(game);
        var menu = new ContextMenu();
        void Add(string text, string id, Action action, bool enabled)
        {
            var item = new MenuItem { Header = text, IsEnabled = enabled };
            item.Click += (_, _) => action();
            AutomationId(item, "library.card." + id);
            menu.Items.Add(item);
        }
        Add(T("nav.build"), "export", () => { libraryFolder = libraryFolderBox.Text ?? ""; selectedGame = game.Id; OpenBuild(); }, idle && game.Reachable);
        menu.Items.Add(new Separator());
        Add(T("library.menu.revealWindows"), "reveal", () => RevealGame(game), (game.Location?.Path ?? "").Length > 0 && game.LocationState != "offline");
        Add(T("library.menu.move"), "move", () => BeginPicker(() => MoveGameAsync(game)), idle && game.Reachable && game.LocationState != "none");
        Add(T("library.menu.clearCache"), "clearCache", () => BeginGameJob("clean", game, new() { ["kind"] = "cache" }), idle && game.Reachable);
        menu.Items.Add(new Separator());
        Add(T("install.remove_game"), "remove", () => { libraryFolder = libraryFolderBox.Text ?? ""; selectedGame = game.Id; Navigate("install"); _ = ConfirmRemoveAsync(game); },
            !libraryBusy && !installBusy && !packRunning);
        Add(T("game.diag"), "diag", () => _ = ExportDiagnosticsAsync(), idle);   // 同 Mac「更多 → 导出诊断包」
        Add(T("game.delete"), "delete", () => _ = ConfirmDeleteAsync(game), idle);
        return menu;
    }

    /// <summary>在资源管理器中显示这款的文件夹（选中它）；还没有文件夹（找不到 / 没出过包）就打开所在的存放位置。</summary>
    private void RevealGame(LibraryGame game)
    {
        if (!ShowGameFolder(game)) { librarySummary = "library.failed"; libraryDiagnostics = T("reveal.folder_missing"); Render(); }
    }

    /// <summary>出包完成页 / 装机页的「打开游戏文件夹」：找不到时在按钮下方提示（revealProblem），不打开空窗口</summary>
    private void OpenGameFolder(LibraryGame game)
    {
        revealProblem = ShowGameFolder(game) ? "" : T("reveal.folder_missing");
        Render();
    }

    /// <summary>在资源管理器里选中这款游戏的文件夹（catalog 给的 location.folder：按索引解析、可能在玩家另选的存放位置，不按游戏号拼）。
    /// 文件夹不在时打开它所在的存放位置；两个都不在 → false（调用方提示）</summary>
    private bool ShowGameFolder(LibraryGame game)
    {
        var folder = game.Location?.Folder ?? "";
        var select = folder.Length > 0 && Directory.Exists(folder);
        var target = select ? folder : game.Location?.Path ?? "";
        // 路径来自后端目录：只认存在的绝对路径；Windows 路径不会带引号，带了就不拼进命令行
        if (target.Length == 0 || !Path.IsPathFullyQualified(target) || target.Contains('"') || !Directory.Exists(target)) return false;
        try
        {
            using var process = Process.Start(new ProcessStartInfo("explorer.exe", select ? "/select,\"" + target + "\"" : "\"" + target + "\"") { UseShellExecute = false });
        }
        catch (Exception error) when (error is System.ComponentModel.Win32Exception or InvalidOperationException or IOException)
        { librarySummary = "library.failed"; libraryDiagnostics = T("process.start_failed") + " " + error.Message; Render(); }
        return true;
    }

    private async Task MoveGameAsync(LibraryGame game)
    {
        var folders = await StorageProvider.OpenFolderPickerAsync(new FolderPickerOpenOptions { Title = T("library.menu.move") });
        if (folders.Count == 0) return;
        await RunGameJobAsync("moveGame", game, new() { ["location"] = LocalPath(folders[0]) });
    }

    private async Task RelinkGameAsync(LibraryGame game)
    {
        var folders = await StorageProvider.OpenFolderPickerAsync(new FolderPickerOpenOptions { Title = T("library.missing.pick") });
        if (folders.Count == 0) return;
        await RunGameJobAsync("relinkGame", game, new() { ["folder"] = LocalPath(folders[0]) });
    }

    private bool deleteOpen;

    /// <summary>「删除这款游戏」确认（同 Mac 的 game.delete 弹窗）：确认后跑 delete 作业。</summary>
    /// <summary>导出诊断包（diagnostics 作业，同 Mac / 控制口 /diagnostics export）：玩家选保存位置，作业层脱敏打包。</summary>
    private async Task ExportDiagnosticsAsync()
    {
        if (connectedFolder is null || libraryId is null || libraryBusy) return;
        var file = await StorageProvider.SaveFilePickerAsync(new FilePickerSaveOptions { Title = T("game.diag"), SuggestedFileName = "S2Arm-diagnostics.zip", DefaultExtension = "zip" });
        if (file?.TryGetLocalPath() is not { } output) return;
        libraryBusy = true; librarySummary = "library.working"; Render();
        try
        {
            var job = await LibraryJobClient.RunAsync(paths, privateRoot, connectedFolder, libraryId, "diagnostics", [output], CancellationToken.None);
            DisplayJob(job);
            librarySummary = job.Report.Succeeded ? "library.completed" : "library.failed";
        }
        catch (Exception error) when (error is LibraryProtocolException or IOException or UnauthorizedAccessException or ArgumentException or
            InvalidOperationException or OperationCanceledException or System.ComponentModel.Win32Exception)
        { librarySummary = "library.failed"; libraryDiagnostics += "\n" + error.Message; }
        finally { libraryBusy = false; Render(); }
    }

    private async Task ConfirmDeleteAsync(LibraryGame game)
    {
        if (deleteOpen || BusyFor(game)) return;
        deleteOpen = true;
        var confirmed = false;
        try
        {
            var dialog = new Window
            {
                Title = T("game.delete.title").Replace("{0}", game.Name), Width = 460, SizeToContent = SizeToContent.Height,
                CanResize = false, WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = Canvas, Foreground = Ink,
                FontFamily = new FontFamily("avares://S2Arm/Assets/Fonts#Manrope")
            };
            var body = new StackPanel { Spacing = 16, Margin = new Thickness(28) };
            body.Children.Add(Label(T("game.delete.title").Replace("{0}", game.Name), 20));
            body.Children.Add(Muted(T("game.delete.body"), 14));
            var buttons = new WrapPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right };
            var cancel = new Button { Content = T("action.cancel"), Padding = new Thickness(16, 9), CornerRadius = new CornerRadius(8), Margin = new Thickness(12, 0, 0, 0),
                Background = Brush.Parse("#29292D"), Foreground = Ink };
            cancel.Click += (_, _) => dialog.Close();
            AutomationId(cancel, "library.delete.cancel");
            var delete = new Button { Content = T("game.delete.confirm"), Padding = new Thickness(16, 9), CornerRadius = new CornerRadius(8), Margin = new Thickness(12, 0, 0, 0),
                Background = Brush.Parse("#C0392B"), Foreground = Ink };
            delete.Click += (_, _) => { confirmed = true; dialog.Close(); };
            AutomationId(delete, "library.delete.confirm");
            buttons.Children.Add(cancel); buttons.Children.Add(delete);
            body.Children.Add(buttons);
            dialog.Content = body;
            await dialog.ShowDialog(this);
        }
        finally { deleteOpen = false; }
        if (confirmed) BeginGameJob("delete", game);
    }

    private bool missingOpen;

    /// <summary>「找不到游戏文件」（照 Mac missingFor 弹窗）：重新指定文件夹… / 从游戏库移除（磁盘上的文件不动）/ 取消。</summary>
    private async Task MissingAsync(LibraryGame game)
    {
        if (missingOpen || BusyFor(game)) return;
        missingOpen = true;
        string? answer = null;
        try
        {
            var dialog = new Window
            {
                Title = T("library.missing.title").Replace("{0}", game.Name), Width = 460, SizeToContent = SizeToContent.Height,
                CanResize = false, WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = Canvas, Foreground = Ink,
                FontFamily = new FontFamily("avares://S2Arm/Assets/Fonts#Manrope")
            };
            var body = new StackPanel { Spacing = 16, Margin = new Thickness(28) };
            body.Children.Add(Label(T("library.missing.title").Replace("{0}", game.Name), 20));
            body.Children.Add(Muted(T("library.missing.body"), 14));
            var buttons = new WrapPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right };
            void Add(string text, string? result, bool primary, string id)
            {
                var button = new Button { Content = text, Padding = new Thickness(16, 9), CornerRadius = new CornerRadius(8), Margin = new Thickness(12, 0, 0, 0),
                    Background = primary ? Accent : Brush.Parse("#29292D"), Foreground = primary ? Canvas : Ink };
                button.Click += (_, _) => { answer = result; dialog.Close(); };
                AutomationId(button, "library.missing." + id);
                buttons.Children.Add(button);
            }
            Add(T("action.cancel"), null, false, "cancel");
            Add(T("library.missing.forget"), "forget", false, "forget");
            Add(T("library.missing.relink"), "relink", true, "relink");
            body.Children.Add(buttons);
            dialog.Content = body;
            await dialog.ShowDialog(this);
        }
        finally { missingOpen = false; }
        if (answer == "relink") BeginPicker(() => RelinkGameAsync(game));
        else if (answer == "forget") BeginGameJob("forgetGame", game);
    }

    private void BeginGameJob(string operation, LibraryGame game, Dictionary<string, object?>? extra = null)
    {
        if (libraryBusy) return;
        libraryFolder = libraryFolderBox.Text ?? "";
        libraryOperation = RunGameJobAsync(operation, game, extra);
    }

    /// <summary>一款游戏的游戏库作业（游戏库结构 v3：moveGame / relinkGame / forgetGame / clean kind=cache / delete）：忙碌、结果与诊断同 RunLibraryAsync，成功后重读游戏库。</summary>
    private async Task RunGameJobAsync(string operation, LibraryGame game, Dictionary<string, object?>? extra = null)
    {
        if (BusyFor(game) || connectedFolder is null || libraryId is null) return;
        libraryBusy = true; librarySummary = "library.working"; libraryDiagnostics = ""; Render();
        using var cancellation = new CancellationTokenSource();
        libraryCancellation = cancellation;
        var succeeded = false;
        try
        {
            var job = await PackJobClient.RunAsync(PackRequest(operation, game.Id, extra), null, cancellation.Token);
            DisplayJob(job);
            succeeded = job.Report.Succeeded;
            librarySummary = succeeded ? "library.completed" : "library.failed";
        }
        catch (LibraryProtocolException error)
        { librarySummary = "library.invalid_result"; libraryDiagnostics += "\n" + error.Message; }
        catch (OperationCanceledException)
        { librarySummary = "library.interrupted"; }
        catch (Exception error)
        {
            // 游戏库作业由玩家输入驱动（目录填什么都有可能：末尾反斜杠、不存在、不可写、非法字符、在启动器自己的目录里）：
            //   任何异常都报成「操作未完成」并带原因，不让它逃出去（玩家反馈：点「连接或创建游戏库」闪退）
            librarySummary = "library.failed"; libraryDiagnostics += "\n" + error.GetType().Name + ": " + error.Message;
            LauncherCrash.Log(error, "library job");
        }
        finally
        { libraryCancellation = null; libraryBusy = false; Render(); }
        if (!succeeded || closingLibrary) return;
        if ((operation == "forgetGame" || operation == "delete") && selectedGame == game.Id) selectedGame = null;
        locationsFor = null;   // 位置表的剩余空间 / 这款的位置变了：出包页下次重读
        await RunLibraryAsync("catalog", null);
    }


    private void BeginPicker(Func<Task> picker)
    {
        if (libraryBusy) return;
        libraryFolder = libraryFolderBox.Text ?? "";
        libraryOperation = GuardPickerAsync(picker);
    }

    private async Task GuardPickerAsync(Func<Task> picker)
    {
        try { await picker(); }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or InvalidOperationException or ArgumentException)
        { librarySummary = "library.failed"; libraryDiagnostics = error.Message; Render(); }
    }

    // 系统自动生成的文件（Windows 资源管理器 desktop.ini / Thumbs.db、从 Mac 拷来的 .DS_Store）：判空文件夹时不算数（同作业层 layout.OS_JUNK）
    private static readonly HashSet<string> OsJunk = new(StringComparer.OrdinalIgnoreCase) { ".DS_Store", "desktop.ini", "Thumbs.db" };
    private static bool IsOsJunk(string name) => OsJunk.Contains(name) || name.StartsWith("._", StringComparison.Ordinal);   // ._ 开头：访达往非 APFS 盘拷东西留下的附属文件

    private static string LocalPath(IStorageItem item) => item.Path.IsFile ? Path.GetFullPath(item.Path.LocalPath)
        : throw new IOException("The selected item does not have a local filesystem path.");

    private async Task ChooseLibraryAsync()
    {
        var folders = await StorageProvider.OpenFolderPickerAsync(new FolderPickerOpenOptions { Title = T("library.choose_folder") });
        if (folders.Count == 0) return;
        libraryFolder = LocalPath(folders[0]); Render();
    }

    private string LibraryPreference => LibraryPaths.PreferenceFile(privateRoot);

    // 旧默认库（私有目录下的 DefaultLibrary）被作业层同盘改名到 %USERPROFILE%\S2Arm 后，设置里的旧路径按搬库标记换成新位置（游戏库结构 P3）
    private string SavedLibrary() => LibraryPaths.SavedLibrary(privateRoot);

    private async Task ChangeLibraryAsync()
    {
        var folders = await StorageProvider.OpenFolderPickerAsync(new FolderPickerOpenOptions { Title = T("library.choose_folder") });
        if (folders.Count == 0) return;
        var folder = LocalPath(folders[0]);
        // 有游戏、选的是空文件夹才问；已有游戏库或含旧产物的文件夹由作业层直接打开 / 认领
        libraryMoveFrom = null;
        if (connectedFolder is not null && catalog is { Games.Count: > 0 } && !Directory.EnumerateFileSystemEntries(folder).Any(e => !IsOsJunk(Path.GetFileName(e))))
        {
            var choice = await AskMoveLibraryAsync();
            if (choice is null) return;
            if (choice.Value) libraryMoveFrom = connectedFolder;
        }
        ConnectLibrary(folder);
    }

    /// <summary>true = 搬过去，false = 新建空游戏库，null = 取消。</summary>
    private async Task<bool?> AskMoveLibraryAsync()
    {
        var dialog = new Window
        {
            Title = T("library.move_title"), Width = 460, SizeToContent = SizeToContent.Height,
            CanResize = false, WindowStartupLocation = WindowStartupLocation.CenterOwner,
            Background = Canvas, Foreground = Ink,
            FontFamily = new FontFamily("avares://S2Arm/Assets/Fonts#Manrope")
        };
        bool? answer = null;
        var body = new StackPanel { Spacing = 16, Margin = new Thickness(28) };
        body.Children.Add(Label(T("library.move_title"), 20));
        body.Children.Add(Muted(T("library.move_body"), 14));
        var buttons = new WrapPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right };
        void Add(string text, bool? result, bool primary)
        {
            var button = new Button { Content = text, Padding = new Thickness(16, 9), CornerRadius = new CornerRadius(8), Margin = new Thickness(12, 0, 0, 0),
                Background = primary ? Accent : Brush.Parse("#29292D"), Foreground = primary ? Canvas : Ink };
            button.Click += (_, _) => { answer = result; dialog.Close(); };
            buttons.Children.Add(button);
        }
        Add(T("action.cancel"), null, false);
        Add(T("library.move_empty"), false, false);
        Add(T("library.move_confirm"), true, true);
        body.Children.Add(buttons);
        dialog.Content = body;
        await dialog.ShowDialog(this);
        return answer;
    }

    /// <summary>「连接或创建游戏库」：输入框里的路径先规范（LibraryFolderInput），再走和「选择游戏库目录」同一条会记住位置的路径。
    /// 原先这个按钮走 BeginLibrary("setup")，连上了却不写设置，重开启动器又回到缺省库（玩家反馈「游戏库位置改不了」）。</summary>
    private void ConnectTypedFolder(string? input)
    {
        if (libraryBusy) return;
        var r = LibraryFolderInput.Normalize(input, AppContext.BaseDirectory);
        if (r.Folder is null)
        {
            libraryFolder = input ?? ""; librarySummary = "library.failed"; libraryDiagnostics = T(r.ErrorKey!); Render();
            return;
        }
        libraryMoveFrom = null;
        ConnectLibrary(r.Folder);
    }

    /// <summary>换到新位置：连上后记下来，下次启动还用它。路径同样过一遍规范（选目录 / 控制口来的也一样）。</summary>
    private void ConnectLibrary(string folder)
    {
        var r = LibraryFolderInput.Normalize(folder, AppContext.BaseDirectory);
        if (r.Folder is null) { librarySummary = "library.failed"; libraryDiagnostics = T(r.ErrorKey!); Render(); return; }
        folder = r.Folder;
        connectedFolder = null; libraryId = null;
        libraryFolder = folder;
        libraryOperation = SwitchLibraryAsync(folder);
    }

    private async Task SwitchLibraryAsync(string folder)
    {
        await EnsureLibraryAsync(folder, () => { });
        if (connectedFolder is null) return;
        try { PrivateJobRequest.EnsurePrivateRoot(privateRoot); File.WriteAllText(LibraryPreference, folder); }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException) { importError = error.Message; Render(); }
    }

    private async Task ChooseSourceFolderAsync()
    {
        var folders = await StorageProvider.OpenFolderPickerAsync(new FolderPickerOpenOptions { Title = T("library.import_folder") });
        if (folders.Count == 0) return;
        await RunLibraryAsync("import", [LocalPath(folders[0])]);
    }

    private async Task ChooseFilesAsync(bool keys)
    {
        var files = await StorageProvider.OpenFilePickerAsync(new FilePickerOpenOptions {
            Title = T(keys ? "library.choose_keys" : "library.import_files"), AllowMultiple = !keys,
            FileTypeFilter = [new FilePickerFileType(T(keys ? "library.import_keys" : "library.import_files"))
                { Patterns = keys ? ["prod.keys", "*.keys"] : ["*.nsp", "*.xci", "*.nsz", "*.xcz"] }] });
        if (files.Count == 0) return;
        await RunLibraryAsync(keys ? "keys" : "import", files.Select(LocalPath).ToArray());
    }

    private void BeginLibrary(string operation)
    {
        if (libraryBusy) return;
        libraryFolder = libraryFolderBox.Text ?? "";
        libraryOperation = RunLibraryAsync(operation, null);
    }

    private async Task RunLibraryAsync(string operation, string[]? selectedPaths)
    {
        if (libraryBusy) return;
        if (operation != "setup")
        {
            if (connectedFolder is null || libraryId is null) return;
            string pending;
            try { pending = Path.GetFullPath(libraryFolder); }
            catch (ArgumentException) { pending = ""; }
            if (!string.Equals(pending, connectedFolder, StringComparison.OrdinalIgnoreCase))
            {
                // A changed output field must never send an import to the previous library.
                connectedFolder = null; libraryId = null; catalog = null; selectedGame = null; ClearCovers();
                librarySummary = "library.disconnected"; Render(); return;
            }
        }
        libraryBusy = true; librarySummary = operation == "catalog" ? "library.loading" : "library.working";
        libraryDiagnostics = ""; Render();
        using var cancellation = new CancellationTokenSource();
        libraryCancellation = cancellation;
        try
        {
            if (operation == "setup")
            {
                connectedFolder = null; libraryId = null; catalog = null; selectedGame = null; ClearCovers();
                var folder = Path.GetFullPath(libraryFolder);
                var setup = await LibraryJobClient.RunAsync(paths, privateRoot, folder, null, "setup", null, cancellation.Token);
                DisplayJob(setup);
                if (!setup.Report.Succeeded) { librarySummary = "library.failed"; return; }
                var connection = LibraryConnection.Parse(setup.Report.Result);
                connectedFolder = connection.Folder; libraryId = connection.Id; libraryFolder = connectedFolder;
            }
            else if (operation != "catalog")
            {
                var output = await LibraryJobClient.RunAsync(paths, privateRoot, connectedFolder!, libraryId, operation, selectedPaths, cancellation.Token);
                DisplayJob(output);
                if (!output.Report.Succeeded) { librarySummary = "library.failed"; return; }
            }
            var listing = await LibraryJobClient.RunAsync(paths, privateRoot, connectedFolder!, libraryId, "catalog", null, cancellation.Token);
            DisplayJob(listing);
            if (!listing.Report.Succeeded) { librarySummary = "library.failed"; return; }
            catalog = LibraryCatalog.Parse(listing.Report.Result); ClearCovers();
            if (!catalog.Games.Any(g => g.Id == selectedGame && !g.Hidden)) selectedGame = null;
            librarySummary = "library.completed";
        }
        catch (LibraryProtocolException error)
        { librarySummary = "library.invalid_result"; libraryDiagnostics += "\n" + error.Message; }
        catch (OperationCanceledException)
        { librarySummary = "library.interrupted"; }
        catch (Exception error)
        {
            // 游戏库作业由玩家输入驱动（目录填什么都有可能：末尾反斜杠、不存在、不可写、非法字符、在启动器自己的目录里）：
            //   任何异常都报成「操作未完成」并带原因，不让它逃出去（玩家反馈：点「连接或创建游戏库」闪退）
            librarySummary = "library.failed"; libraryDiagnostics += "\n" + error.GetType().Name + ": " + error.Message;
            LauncherCrash.Log(error, "library job");
        }
        finally
        { libraryCancellation = null; libraryBusy = false; Render(); }
    }

    private void DisplayJob(LibraryJobOutput job)
    {
        libraryDiagnostics = job.Report.Message + "\n" + T("diagnostics.exit_code") + ": " + job.Output.ExitCode;
        if (job.Report.Diagnostics.ValueKind == JsonValueKind.Array)
            foreach (var diagnostic in job.Report.Diagnostics.EnumerateArray())
            {
                if (diagnostic.TryGetProperty("code", out var code))
                {
                    var value = code.GetString();
                    libraryDiagnostics += "\n" + T("errors." + value + ".title") + "\n" + T("errors." + value + ".detail");
                }
                foreach (var key in new[] { "summary", "suggestion" })
                    if (diagnostic.TryGetProperty(key, out var message) && message.ValueKind == JsonValueKind.String)
                        libraryDiagnostics += "\n" + message.GetString();
            }
        if (!string.IsNullOrWhiteSpace(job.Output.StandardError)) libraryDiagnostics += "\n" + job.Output.StandardError;
    }
}
