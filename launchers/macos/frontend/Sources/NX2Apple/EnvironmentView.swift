// S2Arm（不是上游文件）：环境检查页（preflight 作业的清单）。没过的在前、按组放进 NXLauncher 的卡片（CardGroup / CardRow），
// 每项给一键处理：缺 Xcode → 去 App Store 安装；装了没打开过 → 打开 Xcode；缺 Metal 组件 → 一键下载（downloadMetal 作业）；缺 iOS 平台组件 → 一键下载（downloadIosPlatform 作业，约 8 GB）；
// 缺 .NET SDK → 一键安装（installDotnet 作业）；游戏库在云盘同步目录 / 非 APFS 盘上 → 换游戏库（选目录 → setup 作业）。
// 作业跑在作业页，做完自动重新检查。通过的折起来。
import AppKit
import SwiftUI
import NXCore
import NXModel
import NXLauncher
import S2ArmAppleAdapter

struct EnvironmentView: View {
    @ObservedObject var model: S2ArmLauncherModel
    @EnvironmentObject var loc: Localizer
    @Environment(\.dismiss) private var dismiss
    @State private var showPassed = false

    /// 组的顺序同旧启动器 ToolChecks。
    private static let groups = ["system", "apple", "engine", "bundled", "storage"]

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(loc.t("s2arm.env.title")).font(.manrope(26, .heavy)).tracking(-0.6)
                    Text(summary).font(.manrope(14)).foregroundStyle(.secondary)
                }
                Spacer()
                if model.checking { ProgressView().controlSize(.small) }
            }
            .padding(.horizontal, 30).padding(.top, 28).padding(.bottom, 20)
            if let error = model.environmentError {
                Text(loc.backend(error)).font(.manrope(12)).foregroundStyle(LauncherPalette.danger)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, 30).padding(.bottom, 12)
            }
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    CardGroup(loc.t("s2arm.env.library.group")) {
                        CardRow(loc.t("s2arm.env.library.title"), detail: model.provider.client.environment.library + "\n" + loc.t("s2arm.env.library.detail")) {
                            Button(loc.t("s2arm.env.library.change")) { chooseLibrary() }
                                .buttonStyle(LauncherSecondaryButtonStyle()).disabled(model.busy)
                        }
                    }
                    let failing = model.checks.filter { !$0.ok }
                    if !failing.isEmpty || model.environmentError != nil {
                        CopyReportButton(model: model, gameID: nil, operations: nil,
                                         fallback: (["S2Arm 环境检查未通过", ProcessInfo.processInfo.operatingSystemVersionString, model.environmentError ?? ""] + failing.map { "\($0.name)：\($0.message)" }).joined(separator: "\n"))
                    }
                    ForEach(ordered(failing), id: \.self) { group in
                        let items = failing.filter { groupOf($0) == group }
                        CardGroup(groupName(group)) {
                            ForEach(items) { check in
                                row(check)
                                if check.id != items.last?.id { CardDivider() }
                            }
                        }
                    }
                    // iCloud 存档同步（S2ArmLauncherModel+CloudSaves）：选 iCloud 云盘里的文件夹，iPhone / iPad 上选同一个
                    CardGroup(loc.t("s2arm.env.cloud.group")) {
                        CardRow(loc.t("s2arm.env.cloud.title"), detail: model.cloudFolder.map { $0.path + "\n" + loc.t("s2arm.env.cloud.on") } ?? loc.t("s2arm.env.cloud.off")) {
                            HStack(spacing: 8) {
                                if model.cloudFolder != nil {
                                    Button(loc.t("s2arm.env.cloud.turnOff")) { try? model.setCloudFolder(nil) }
                                        .buttonStyle(LauncherSecondaryButtonStyle()).accessibilityIdentifier("env.cloud.off")
                                }
                                Button(loc.t("s2arm.env.cloud.choose")) { chooseCloudFolder() }
                                    .buttonStyle(LauncherSecondaryButtonStyle()).accessibilityIdentifier("env.cloud.choose")
                            }
                        }
                    }
                    // 允许 AI 操作 S2Arm（本机控制口，S2ArmControl）：玩家用自己的 AI 照 App 自带的操作手册做导入 → 出包 → 装机（维护者 10-23）
                    CardGroup(loc.t("s2arm.env.ai.group")) {
                        CardRow(loc.t("s2arm.env.ai.title"), detail: model.aiControlError.map(loc.backend) ?? loc.t("s2arm.env.ai.detail")) {
                            Toggle("", isOn: Binding(get: { model.aiControl }, set: { model.setAIControl($0) }))
                                .toggleStyle(.switch).labelsHidden().disabled(ControlPort.forced)
                                .accessibilityIdentifier("env.aiControl")
                        }
                    }
                    let passed = model.checks.filter(\.ok)
                    if !passed.isEmpty {
                        CardGroup(loc.t("s2arm.env.passedGroup")) {
                            CardDisclosure(String(format: loc.t("s2arm.env.passed"), passed.count), isOpen: showPassed) { showPassed.toggle() }
                            if showPassed {
                                ForEach(passed) { check in
                                    CardDivider()
                                    row(check)
                                }
                            }
                        }
                    }
                    // 版本号（三端同号：Windows 启动器、iPhone 上的 S2Arm 显示同一个，装机时写进 iPhone 那份）
                    Text(verbatim: "S2Arm " + (Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "-"))
                        .font(.manrope(12)).foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .center)
                        .accessibilityIdentifier("env.version")
                }
                .padding(.horizontal, 30).padding(.bottom, 24)
            }
            Divider().opacity(0.3)
            HStack(spacing: 14) {
                Text(loc.t("s2arm.env.note")).font(.manrope(12)).foregroundStyle(.secondary).lineLimit(2)
                Spacer(minLength: 8)
                Button(loc.t("s2arm.env.recheck")) { model.checkEnvironment() }
                    .buttonStyle(LauncherSecondaryButtonStyle()).disabled(model.checking || model.busy)
                Button { dismiss() } label: {
                    Text(loc.t("launch.done")).font(.manrope(13.5, .bold)).foregroundStyle(.black)
                        .padding(.horizontal, 22).padding(.vertical, 9)
                        .background(LauncherPalette.cream, in: Capsule())
                }
                .buttonStyle(.plain).keyboardShortcut(.defaultAction)
            }
            .padding(.horizontal, 30).padding(.vertical, 16)
        }
        .onAppear { if model.checks.isEmpty { model.checkEnvironment() } }
        .frame(width: 640, height: 600)
        .background(LauncherPalette.background)
        .font(.manrope(13))
        .preferredColorScheme(.dark)
        .tint(LauncherPalette.cream)
    }

    /// 顶部口径要说清是哪一种「齐了」：只差打 iPhone 包的项时不能写「都齐了」（11-08：iOS 平台组件红着、顶部却全绿）。
    private var summary: String {
        switch model.environmentState {
        case .unknown: return loc.t("s2arm.env.checking")
        case .ready: return loc.t("s2arm.env.ready")
        case .readyMac:
            let left = model.checks.filter { !$0.ok && $0.required(for: "ipa") }.count
            return String(format: loc.t(left == 1 ? "s2arm.env.readyMacOne" : "s2arm.env.readyMac"), left)
        case .missing: return loc.t("s2arm.env.missing")
        }
    }

    private func groupOf(_ check: ToolCheck) -> String { Self.groups.contains(check.group) ? check.group : "system" }
    private func ordered(_ checks: [ToolCheck]) -> [String] { Self.groups.filter { g in checks.contains { groupOf($0) == g } } }
    private func groupName(_ group: String) -> String { loc.t("s2arm.env.group." + group) }

    /// 换游戏库：选一个本机、不同步的文件夹（作业层 setup 再拦一遍云盘 / 非 APFS），交给 model.changeLibrary。
    private func chooseCloudFolder() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false; panel.canChooseDirectories = true; panel.canCreateDirectories = true
        panel.allowsMultipleSelection = false
        let drive = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Mobile Documents/com~apple~CloudDocs")
        if FileManager.default.fileExists(atPath: drive.path) { panel.directoryURL = drive }
        guard panel.runModal() == .OK, let url = panel.url else { return }
        try? model.setCloudFolder(url)
    }

    private func chooseLibrary() {
        let panel = NSOpenPanel()
        panel.title = loc.t("s2arm.library.choosePanel")
        panel.prompt = loc.t("s2arm.library.choosePrompt")
        panel.canChooseFiles = false; panel.canChooseDirectories = true; panel.canCreateDirectories = true
        panel.allowsMultipleSelection = false
        guard panel.runModal() == .OK, let url = panel.url else { return }
        // 有游戏、选的是空文件夹才问；已有游戏库或含旧产物的文件夹由作业层直接打开 / 认领
        let contents = (try? FileManager.default.contentsOfDirectory(atPath: url.path)) ?? []
        // 系统自动生成的文件不算数：同作业层 layout.OS_JUNK / OS_JUNK_PREFIX（三端一份，tests/os_junk_sync_test.sh 核一致）
        let osJunk: Set<String> = [".DS_Store", "desktop.ini", "Thumbs.db"]
        guard !model.games.isEmpty, contents.allSatisfy({ osJunk.contains($0) || $0.hasPrefix("._") }) else { model.changeLibrary(to: url); return }
        let alert = NSAlert()
        alert.messageText = loc.t("s2arm.library.moveTitle")
        alert.informativeText = loc.t("s2arm.library.moveBody")
        alert.addButton(withTitle: loc.t("s2arm.library.moveConfirm"))
        alert.addButton(withTitle: loc.t("s2arm.library.moveEmpty"))
        alert.addButton(withTitle: loc.t("fail.confirm.cancel"))
        switch alert.runModal() {
        case .alertFirstButtonReturn: model.changeLibrary(to: url, move: true)
        case .alertSecondButtonReturn: model.changeLibrary(to: url)
        default: break
        }
    }

    /// 检查项的名字与说明是作业层的定句（中文），按界面语言显示（loc.backend）。
    @ViewBuilder private func row(_ check: ToolCheck) -> some View {
        CardRow(loc.backend(check.name), detail: check.ok ? "" : loc.backend(check.message), dot: check.ok ? LauncherPalette.ready : LauncherPalette.danger) {
            if !check.ok, let action = check.action, case let key = "s2arm.env.action." + action, loc.t(key) != key {
                Button(loc.t(key)) {
                    if model.provider.environment.repairStep(for: action) == .chooseLibrary { chooseLibrary() } else { model.repair(check) }
                }
                    .buttonStyle(LauncherSecondaryButtonStyle()).disabled(model.busy || model.checking)
            }
        }
    }
}
