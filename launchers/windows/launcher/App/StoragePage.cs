// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 S2Arm contributors
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using System.Text.Json;
using S2Arm.Launcher.Protocol;

namespace S2Arm.Launcher;

/// <summary>游戏库结构 v3「查」（照 Mac StorageOverview.swift）：游戏卡片上这款的各部分大小（catalog sizes，作业收尾量好的，不现场量），
/// 和「存储总览」页（storageOverview 作业）：各存放位置的 S2Arm 占用 / 剩余 / 总量、公共文件、各款大小。
/// Windows 只出 iPhone 包，没有 Mac 版本，大小只列 iPhone 版、存档、缓存、原件四项。</summary>
public sealed partial class EnvironmentWindow
{
    private StorageOverview? overview;
    private bool overviewLoading;
    private string overviewError = "";
    // 释放空间（同 Mac FreeSpaceView：spaceSurvey 每款按部件列占用、勾选后 spaceFree 直接删；存档和日志只报不删）
    private JsonElement? survey;
    private readonly Dictionary<string, HashSet<string>> freeChosen = new();
    private bool freeing;
    private string freeNote = "";

    private static readonly string[] SizeParts = ["imageIos", "saves", "cache", "sources"];

    private static long SizeOf(GameSizes s, string part) => part switch
    {
        "imageIos" => s.ImageIos, "saves" => s.Saves, "cache" => s.Cache, "sources" => s.Sources, _ => s.ImageMac
    };

    /// <summary>卡片上的一行：「iPhone 版 2 GB · 存档 3 MB · 缓存 … · 原件 …」；没量过只有原件有数，其余显示 -。</summary>
    private Control GameSizesLine(GameSizes? sizes)
    {
        var s = sizes ?? new GameSizes(0, 0, 0, 0, 0, false);
        var parts = SizeParts.Select(p => T("library.sizes." + p) + " " + (s.Measured || p == "sources" ? FormatBytes(SizeOf(s, p)) : "-"));
        var line = Muted(string.Join(" · ", parts), 12);
        ToolTip.SetTip(line, s.Measured ? T("library.sizes.sourcesNote") : T("library.sizes.unmeasured"));
        return line;
    }

    private void OpenStorage()
    {
        libraryFolder = libraryFolderBox.Text ?? "";
        Navigate("storage");
        _ = LoadOverviewAsync();
        _ = LoadSurveyAsync();
    }

    private async Task LoadSurveyAsync()
    {
        if (connectedFolder is null || libraryId is null) return;
        try
        {
            var job = await LibraryJobClient.RunAsync(paths, privateRoot, connectedFolder, libraryId, "spaceSurvey", null, CancellationToken.None);
            if (job.Report.Succeeded) survey = job.Report.Result.Clone();
            else overviewError = job.Report.Message;
        }
        catch (Exception error) when (error is LibraryProtocolException or IOException or UnauthorizedAccessException or ArgumentException or
            InvalidOperationException or System.ComponentModel.Win32Exception or KeyNotFoundException)
        { overviewError = error.Message; }
        if (page == "storage") Render();
    }

    private async Task FreeAsync(string gameId, string name, IReadOnlyList<string> parts, long bytes)
    {
        if (freeing || packRunning || installBusy) { freeNote = T("space.busy"); Render(); return; }
        var body = T("space.free_body").Replace("{0}", string.Join("、", parts.Select(p => T("space.part." + p)))).Replace("{1}", name);
        if (!await ConfirmInstallActionAsync("space.free", body.Replace("{2}", FormatBytes(bytes)))) return;
        freeing = true; freeNote = ""; Render();
        try
        {
            var job = await PackJobClient.RunAsync(PackRequest("spaceFree", gameId, new Dictionary<string, object?> { ["parts"] = parts }), null, CancellationToken.None);
            freeNote = job.Report.Succeeded && job.Report.Result.TryGetProperty("freedBytes", out var f) && f.TryGetInt64(out var freed)
                ? T("space.freed").Replace("{0}", FormatBytes(freed)) : job.Report.Message;
            freeChosen.Remove(gameId);
        }
        catch (Exception error) when (error is LibraryProtocolException or IOException or UnauthorizedAccessException or ArgumentException or
            InvalidOperationException or OperationCanceledException or System.ComponentModel.Win32Exception)
        { freeNote = error.Message; }
        finally { freeing = false; }
        await LoadSurveyAsync(); await LoadOverviewAsync(); await RunLibraryAsync("catalog", null);
    }

    /// <summary>释放空间卡片：每款列可删的部件（勾选）、保留的部件（只报大小），勾了才给「释放」。</summary>
    private Control FreeSpaceCard(JsonElement result)
    {
        var card = new StackPanel { Spacing = 12 };
        card.Children.Add(Label(T("space.title"), 18));
        card.Children.Add(Muted(T("space.direct"), 12));
        if (freeNote.Length > 0) card.Children.Add(Label(freeNote));
        foreach (var g in result.GetProperty("games").EnumerateArray())
        {
            var gid = g.GetProperty("gameId").GetString() ?? ""; var name = g.GetProperty("name").GetString() ?? gid;
            var parts = g.GetProperty("parts").EnumerateArray().Select(p => (Id: p.GetProperty("id").GetString() ?? "", Bytes: p.GetProperty("bytes").GetInt64()))
                .Where(p => p.Bytes > 0).ToList();
            var row = new StackPanel { Spacing = 4 };
            row.Children.Add(Label(name, 15));
            var chosen = freeChosen.TryGetValue(gid, out var set) ? set : freeChosen[gid] = new HashSet<string>();
            foreach (var (id, bytes) in parts)
            {
                var box = new CheckBox { IsChecked = chosen.Contains(id), IsEnabled = !freeing,
                    Content = new StackPanel { Spacing = 1, Children = { Label(T("space.part." + id) + " · " + FormatBytes(bytes)), Muted(T("space.part." + id + ".rebuild"), 12) } } };
                AutomationId(box, "space." + gid + "." + id);
                box.IsCheckedChanged += (_, _) => { if (box.IsChecked == true) chosen.Add(id); else chosen.Remove(id); Render(); };
                row.Children.Add(box);
            }
            var kept = g.GetProperty("kept").EnumerateArray().Select(k => T("space.kept." + k.GetProperty("id").GetString()) + " " + FormatBytes(k.GetProperty("bytes").GetInt64()));
            row.Children.Add(Muted(T("space.kept") + " · " + string.Join(" · ", kept), 12));
            if (parts.Count == 0) row.Children.Add(Muted(T("space.nothing"), 12));
            else
            {
                var total = parts.Where(p => chosen.Contains(p.Id)).Sum(p => p.Bytes);
                var free = Action("space.free", () => _ = FreeAsync(gid, name, parts.Where(p => chosen.Contains(p.Id)).Select(p => p.Id).ToList(), total),
                    chosen.Count > 0 && !freeing);
                AutomationId(free, "space.free." + gid);
                if (total > 0) row.Children.Add(Muted(T("space.total").Replace("{0}", FormatBytes(total)), 12));
                row.Children.Add(free);
            }
            card.Children.Add(row);
        }
        card.Children.Add(Muted(T("space.keptNote"), 12));
        return Card(card);
    }

    private async Task LoadOverviewAsync()
    {
        if (overviewLoading || connectedFolder is null || libraryId is null) return;
        overviewLoading = true; overviewError = ""; Render();
        try
        {
            var job = await LibraryJobClient.RunAsync(paths, privateRoot, connectedFolder, libraryId, "storageOverview", null, CancellationToken.None);
            if (job.Report.Succeeded) overview = StorageOverview.Parse(job.Report.Result);
            else overviewError = job.Report.Message;
        }
        catch (Exception error) when (error is LibraryProtocolException or IOException or UnauthorizedAccessException or ArgumentException or
            InvalidOperationException or System.ComponentModel.Win32Exception or KeyNotFoundException)
        { overviewError = error.Message; }
        finally { overviewLoading = false; }
        if (page == "storage") Render();
    }

    private void RenderStorage(StackPanel body)
    {
        var head = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto,Auto"), ColumnSpacing = 10 };
        var title = Label(T("storage.title"), 23); title.VerticalAlignment = VerticalAlignment.Center;
        head.Children.Add(title);
        var again = Action("storage.refresh", () => { _ = LoadOverviewAsync(); _ = LoadSurveyAsync(); }, !overviewLoading && libraryId is not null);
        AutomationId(again, "storage.refresh");
        Grid.SetColumn(again, 1); head.Children.Add(again);
        var back = Action("nav.library", () => Navigate("library"));
        Grid.SetColumn(back, 2); head.Children.Add(back);
        body.Children.Add(head);
        if (overviewLoading)
        {
            var loading = new StackPanel { Spacing = 8 };
            loading.Children.Add(Muted(T("storage.loading")));
            loading.Children.Add(new ProgressBar { IsIndeterminate = true, Height = 4 });
            body.Children.Add(loading);
        }
        if (overviewError.Length > 0) body.Children.Add(new TextBlock { Text = overviewError, TextWrapping = TextWrapping.Wrap, Foreground = Brush.Parse("#E05C5C") });
        if (overview is not { } o) return;

        var places = new StackPanel { Spacing = 12 };
        places.Children.Add(Label(T("storage.locations"), 18));
        foreach (var l in o.Locations)
        {
            var row = new StackPanel { Spacing = 3 };
            row.Children.Add(Label((l.Volume.Length > 0 ? l.Volume : l.Path) + (l.IsDefault ? " · " + T("storage.location.default") : ""), 15));
            row.Children.Add(Muted(l.Connected && l.TotalBytes is { } total && l.FreeBytes is { } free
                ? T("storage.location.usage").Replace("{0}", FormatBytes(l.UsedBytes ?? 0)).Replace("{1}", FormatBytes(free)).Replace("{2}", FormatBytes(total))
                : T("storage.location.offline")));
            row.Children.Add(Muted(l.Path, 12));
            places.Children.Add(row);
        }
        body.Children.Add(Card(places));

        var shared = new StackPanel { Spacing = 8 };
        shared.Children.Add(Label(T("storage.shared"), 18));
        foreach (var part in o.Shared)
        {
            var row = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto") };
            row.Children.Add(Label(T("storage.shared." + part.Id)));
            var size = Label(FormatBytes(part.Bytes)); Grid.SetColumn(size, 1); row.Children.Add(size);
            shared.Children.Add(row);
        }
        body.Children.Add(Card(shared));

        var games = new StackPanel { Spacing = 10 };
        games.Children.Add(Label(T("storage.games"), 18));
        foreach (var g in o.Games)
        {
            var row = new StackPanel { Spacing = 3 };
            row.Children.Add(Label(g.Name, 15));
            row.Children.Add(GameSizesLine(g.Sizes));
            games.Children.Add(row);
        }
        games.Children.Add(Muted(T("library.sizes.sourcesNote"), 12));
        body.Children.Add(Card(games));
        if (survey is { } s) body.Children.Add(FreeSpaceCard(s));
    }
}
