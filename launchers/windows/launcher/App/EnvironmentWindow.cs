// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 S2Arm contributors
using System.Reflection;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using S2Arm.Launcher.Protocol;

namespace S2Arm.Launcher;

public sealed partial class EnvironmentWindow : Window
{
    private static readonly IBrush Canvas = Brush.Parse("#0E0E10");
    private static readonly IBrush Surface = Brush.Parse("#19191C");
    private static readonly IBrush Ink = Brush.Parse("#F2F0EB");
    private static readonly IBrush Accent = Brush.Parse("#FFF0D6");
    private readonly SharedText text;
    private string language = "zh-Hans";
    private LauncherPaths paths = LauncherPaths.Defaults(AppContext.BaseDirectory);
    private EnvironmentReport? report;
    private string summaryKey = "environment.idle";
    private string unexpectedError = "";

    /// <summary>Program.cs 全局兜底接住界面线程上没处理的异常后调：在顶部报出来，忙碌标记复位（出错的那个操作已经中断），启动器继续可用。</summary>
    internal void ReportUnexpected(Exception error, string logPath)
    {
        unexpectedError = error.GetType().Name + ": " + error.Message + "\n" + T("launcher.unexpected_log") + " " + logPath;
        busy = false; libraryBusy = false;
        try { Render(); } catch (Exception again) { LauncherCrash.Log(again, "render after unexpected"); }
    }
    private string diagnostics = "";
    private bool busy;
    private CancellationTokenSource? active;
    private TextBox engine = new(), runtime = new(), shell = new();

    public EnvironmentWindow()
    {
        using var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream("S2Arm.experience.v1.json")
            ?? throw new FileNotFoundException("Shared experience resource is missing.");
        using var reader = new StreamReader(stream);
        text = new SharedText(reader.ReadToEnd());
        Width = 1080; Height = 820; MinWidth = 760; MinHeight = 600;
        Background = Canvas; Foreground = Ink;
        FontFamily = new FontFamily("avares://S2Arm/Assets/Fonts#Manrope");
        Closed += (_, _) => { active?.Cancel(); ClearCovers(); DisposeRetiredCovers(all: true); CloseControl(); };
        Opened += (_, _) => { StartControl(); FinishPendingUpdate(); _ = CheckForUpdateAsync(); };
        Closing += CloseLibraryAsync;
        Closing += ClosePackAsync;
        // 欢迎页进来自动触发环境检查（回到窗口也会重查）
        Activated += (_, _) => { if (!busy && report == null) _ = CheckAsync(); };
        Render();
    }

    private string T(string key) => text.Get(key, language);
    private static TextBlock Label(string value, double size = 14) => new()
    {
        Text = value, FontSize = size, FontWeight = size >= 18 ? FontWeight.SemiBold : FontWeight.Normal,
        TextWrapping = TextWrapping.Wrap
    };
    private Button Action(string key, Action handler, bool enabled = true)
    {
        var button = new Button { Content = T(key), IsEnabled = enabled, Padding = new Thickness(16, 9),
            Background = Brush.Parse("#29292D"), Foreground = Ink, CornerRadius = new CornerRadius(8) };
        Avalonia.Automation.AutomationProperties.SetAutomationId(button, key);   // 自动化按文案键找按钮（如 library.connect），不靠显示文字
        button.Click += (_, _) => handler();
        return button;
    }

    private void CapturePaths()
    {
        if (page == "settings") paths = new(engine.Text ?? "", runtime.Text ?? "", shell.Text ?? "");
    }

    private void Render()
    {
        try { RenderCore(); }
        finally { DisposeRetiredCovers(); }   // 新界面换上之后才释放旧封面（ClearCovers 说明）
    }

    private void RenderCore()
    {
        if (importDialog?.Targeted == true) { renderDeferred = true; return; }
        Title = T("app.title");
        var body = new StackPanel { Spacing = 20, Margin = new Thickness(32) };
        // ── 顶栏：品牌名（左）+ 语言 / 添加游戏 / 环境检查（右）────────
        var header = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto") };
        var brand = new TextBlock { Text = "S2Arm × NX2Apple", FontSize = 22, FontWeight = FontWeight.SemiBold,
            Foreground = Ink, VerticalAlignment = VerticalAlignment.Center };
        header.Children.Add(brand);

        var rightBar = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 10,
            VerticalAlignment = VerticalAlignment.Center };

        // 语言切换
        var languages = new ComboBox
        {
            ItemsSource = new[] { T("language.zh-Hans"), T("language.en") },
            SelectedIndex = language == "zh-Hans" ? 0 : 1, IsEnabled = !busy && !libraryBusy, MinWidth = 120
        };
        languages.SelectionChanged += (_, _) =>
        {
            CapturePaths();
            if (page == "library") libraryFolder = libraryFolderBox.Text ?? "";
            language = languages.SelectedIndex == 0 ? "zh-Hans" : "en"; Render();
        };
        if (PhonePill() is { } phonePill) rightBar.Children.Add(phonePill);
        AddUpdateControls(rightBar);
        rightBar.Children.Add(languages);

        // 游戏库：任何页面都能回去；出包还在跑就留在后台
        var libraryBtn = new Button
        {
            Content = T("nav.library"), Padding = new Thickness(14, 8), CornerRadius = new CornerRadius(8),
            Background = page == "library" ? Accent : Brush.Parse("#29292D"), Foreground = page == "library" ? Canvas : Ink
        };
        Avalonia.Automation.AutomationProperties.SetAutomationId(libraryBtn, "top.library");
        libraryBtn.Click += (_, _) => { if (packRunning) sessionHidden = true; Navigate("library"); };
        rightBar.Children.Add(libraryBtn);

        // 添加游戏
        var addGameBtn = new Button
        {
            Content = T("action.add_game"), IsEnabled = connectedFolder is not null && !libraryBusy && !busy,
            Padding = new Thickness(14, 8), Background = Brush.Parse("#29292D"), Foreground = Ink,
            CornerRadius = new CornerRadius(8)
        };
        addGameBtn.Click += (_, _) => { if (!libraryBusy) { importDialog = new ImportDialogState(ImportKind.Games); Render(); } };
        rightBar.Children.Add(addGameBtn);

        // 环境检查（弹窗入口：切到 settings 页）
        var envDot = report?.Ready == true ? "● " : report is not null ? "○ " : "";
        var envCheckBtn = new Button
        {
            Content = envDot + T("action.env_check"), IsEnabled = !busy && !libraryBusy,
            Padding = new Thickness(14, 8),
            Background = page == "settings" ? Accent : Brush.Parse("#29292D"),
            Foreground = page == "settings" ? Canvas : Ink,
            CornerRadius = new CornerRadius(8)
        };
        envCheckBtn.Click += (_, _) => Navigate(page == "settings" ? "library" : "settings");
        rightBar.Children.Add(envCheckBtn);

        Grid.SetColumn(rightBar, 1); header.Children.Add(rightBar);
        body.Children.Add(header);
        if (unexpectedError.Length > 0)
        {
            // 全局兜底接住的意外错误（Program.cs）：启动器不退出，在顶部说清楚、给日志位置，玩家可以关掉提示继续用
            var panel = new StackPanel { Spacing = 8 };
            panel.Children.Add(Label(T("launcher.unexpected"), 16));
            panel.Children.Add(Muted(unexpectedError));
            var dismiss = new Button { Content = T("launcher.unexpected_dismiss"), Padding = new Thickness(12, 6), CornerRadius = new CornerRadius(8),
                Background = Brush.Parse("#29292D"), Foreground = Ink };
            Avalonia.Automation.AutomationProperties.SetAutomationId(dismiss, "launcher.unexpected_dismiss");
            dismiss.Click += (_, _) => { unexpectedError = ""; Render(); };
            panel.Children.Add(dismiss);
            body.Children.Add(Card(panel));
        }
        if (RenderPackPages(body)) return;
        if (importDialog is not null)
        {
            RenderImportDialog(body);
            Content = new ScrollViewer { Content = body, HorizontalScrollBarVisibility = Avalonia.Controls.Primitives.ScrollBarVisibility.Disabled };
            return;
        }
        if (page == "library" && ShowsWelcome)
        {
            RenderWelcome(body);
            Content = new ScrollViewer { Content = body, HorizontalScrollBarVisibility = Avalonia.Controls.Primitives.ScrollBarVisibility.Disabled };
            return;
        }
        if (page == "storage")
        {
            RenderStorage(body);
            Content = new ScrollViewer { Content = body, HorizontalScrollBarVisibility = Avalonia.Controls.Primitives.ScrollBarVisibility.Disabled };
            return;
        }
        if (page == "library")
        {
            RenderLibrary(body);
            Content = new ScrollViewer { Content = body, HorizontalScrollBarVisibility = Avalonia.Controls.Primitives.ScrollBarVisibility.Disabled };
            return;
        }
        body.Children.Add(Label(T("environment.title"), 23));
        body.Children.Add(Label(T("environment.description")));

        engine = new TextBox { Text = paths.Engine, IsEnabled = !busy };
        runtime = new TextBox { Text = paths.Runtime, IsEnabled = !busy };
        shell = new TextBox { Text = paths.Shell, IsEnabled = !busy };
        var settings = new StackPanel { Spacing = 8 };
        settings.Children.Add(Label(T("settings.description")));
        settings.Children.Add(Label(T("settings.engine"))); settings.Children.Add(engine);
        settings.Children.Add(Label(T("settings.runtime"))); settings.Children.Add(runtime);
        settings.Children.Add(Label(T("settings.shell"))); settings.Children.Add(shell);
        settings.Children.Add(Action("action.save_settings", CapturePaths, !busy));
        body.Children.Add(new Expander { Header = T("settings.title"), Content = settings,
            HorizontalAlignment = HorizontalAlignment.Stretch });

        var libraryRow = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto") };
        var libraryText = new StackPanel { Spacing = 4 };
        libraryText.Children.Add(Label(T("environment.library.title"), 16));
        libraryText.Children.Add(Label(connectedFolder ?? libraryFolder));
        libraryText.Children.Add(Label(T("environment.library.detail")));
        libraryRow.Children.Add(libraryText);
        var libraryChange = Action("environment.library.change", () => BeginPicker(ChangeLibraryAsync), !libraryBusy && !PackBusy);
        libraryChange.VerticalAlignment = VerticalAlignment.Center;
        Grid.SetColumn(libraryChange, 1); libraryRow.Children.Add(libraryChange);
        body.Children.Add(Card(libraryRow));

        var aiRow = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto") };
        var aiText = new StackPanel { Spacing = 4 };
        aiText.Children.Add(Label(T("environment.ai.title"), 16));
        aiText.Children.Add(Label(aiControlError.Length > 0 ? aiControlError : T("environment.ai.detail")));
        aiRow.Children.Add(aiText);
        var aiToggle = new ToggleSwitch { IsChecked = aiControl, OnContent = "", OffContent = "", VerticalAlignment = VerticalAlignment.Center };
        aiToggle.IsCheckedChanged += (_, _) => { if (aiToggle.IsChecked != aiControl) SetAiControl(aiToggle.IsChecked == true); };
        Grid.SetColumn(aiToggle, 1); aiRow.Children.Add(aiToggle);
        body.Children.Add(Card(aiRow));

        var summary = new StackPanel { Spacing = 12 };
        summary.Children.Add(Label(T(summaryKey), 18));
        var check = new Button { Content = T(report is null ? "action.check" : "action.recheck"), IsEnabled = !busy,
            Background = Accent, Foreground = Canvas, Padding = new Thickness(20, 10), CornerRadius = new CornerRadius(8) };
        check.Click += async (_, _) => await CheckAsync();
        summary.Children.Add(check);
        body.Children.Add(new Border { Background = Surface, CornerRadius = new CornerRadius(10),
            Padding = new Thickness(20), Child = summary });

        if (report is not null)
        {
            foreach (var item in report.Checks)
            {
                var row = new StackPanel { Spacing = 6 };
                var advisory = item.Status != "ok" && !item.Blocking;   // 只提示、不挡流程
                var heading = Label(T(advisory ? "environment.status.advisory" : "environment.status." + item.Status) + " · " + item.Name, 16);
                if (advisory) heading.Foreground = Brush.Parse("#FFC857");
                row.Children.Add(heading);
                if (item.Code is not null && !advisory)
                {
                    row.Children.Add(Label(T("errors." + item.Code + ".title")));
                    row.Children.Add(Label(T("errors." + item.Code + ".detail")));
                }
                // Observed names/messages and recovery text remain backend data.
                row.Children.Add(Label(item.Message));
                if (item.HasAction)
                {
                    row.Children.Add(new Expander { Header = T("action.open_help"), Content = Label(item.Action!),
                        HorizontalAlignment = HorizontalAlignment.Stretch });
                }
                var line = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto"), ColumnSpacing = 20 };
                line.Children.Add(row);
                if (item.OfficialUrl is { } url)
                {
                    var official = new Button { Content = T("action.open_official"),
                        Background = Accent, Foreground = Canvas, Padding = new Thickness(16, 10),
                        CornerRadius = new CornerRadius(8), VerticalAlignment = VerticalAlignment.Center };
                    official.Click += async (_, _) => await OpenOfficialAsync(url);
                    Grid.SetColumn(official, 1); line.Children.Add(official);
                }
                body.Children.Add(new Border { Background = Surface, CornerRadius = new CornerRadius(10),
                    Padding = new Thickness(18), BorderBrush = item.Status == "ok" ? Brush.Parse("#3D8565") : Brush.Parse("#8C7344"),
                    BorderThickness = new Thickness(1), Child = line });
            }
        }
        else body.Children.Add(Label(T("environment.no_report")));

        if (diagnostics.Length != 0)
        {
            body.Children.Add(new Expander { Header = T("diagnostics.title"),
                HorizontalAlignment = HorizontalAlignment.Stretch,
                Content = new TextBox { Text = diagnostics, IsReadOnly = true, AcceptsReturn = true,
                    TextWrapping = TextWrapping.Wrap, MaxHeight = 260 } });
        }
        // Version: the same number the Mac launcher and the S2Arm on the iPhone show (written into the iPhone app at install).
        var version = Muted("S2Arm " + AppVersion, 12); version.HorizontalAlignment = HorizontalAlignment.Center;
        Avalonia.Automation.AutomationProperties.SetAutomationId(version, "env.version");
        body.Children.Add(version);
        Content = new ScrollViewer { Content = body, HorizontalScrollBarVisibility = Avalonia.Controls.Primitives.ScrollBarVisibility.Disabled };
    }

    private async Task OpenOfficialAsync(Uri url)
    {
        try
        {
            if (await Launcher.LaunchUriAsync(url)) return;
            diagnostics = T("process.start_failed");
        }
        catch (Exception error) when (error is IOException or System.ComponentModel.Win32Exception or InvalidOperationException)
        {
            diagnostics = T("process.start_failed") + "\n" + error.Message;
        }
        Render();
    }

    private async Task CheckAsync()
    {
        if (busy) return;
        CapturePaths(); report = null; diagnostics = "";
        if (EnvironmentProbe.BundleIncomplete(paths)) { summaryKey = "environment.unzip_first"; Render(); return; }
        busy = true;
        summaryKey = "environment.checking"; Render();
        using var cancellation = new CancellationTokenSource(TimeSpan.FromMinutes(2));
        active = cancellation;
        try
        {
            var output = await EnvironmentProbe.RunAsync(paths, cancellation.Token);
            diagnostics = T("diagnostics.exit_code") + ": " + output.ExitCode + "\n" + output.StandardError;
            report = EnvironmentReport.Parse(output.StandardOutput, output.ExitCode);
            if (report.Error is not null)
            {
                summaryKey = "environment.failed";
                diagnostics += "\n" + report.Error.Code + "\n" + report.Error.Message;
            }
            else summaryKey = report.Ready ? "environment.ready" : "environment.not_ready";
        }
        catch (EnvironmentProtocolException error)
        {
            summaryKey = "protocol.invalid_report"; diagnostics += "\n" + error.Message;
        }
        catch (OperationCanceledException)
        {
            summaryKey = "process.interrupted";
        }
        catch (Exception error) when (error is IOException or System.ComponentModel.Win32Exception or ArgumentException or InvalidOperationException)
        {
            summaryKey = "process.start_failed"; diagnostics = error.Message;
        }
        finally
        {
            active = null; busy = false; Render();
        }
    }
}
