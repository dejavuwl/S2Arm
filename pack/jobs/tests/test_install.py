"""装到 iPhone（pack/jobs/install.py，10-26）：选团队、包名、报错分类、整条流程（签名 / 装机 / 拷资源包打桩，IPA 夹具用 ditto 真解）。期望按规则手写"""
import json, os, plistlib, shutil, subprocess, sys, tempfile, types, unittest, zipfile
import unittest.mock as um
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import install
from core import Failure, atomic
import tests.test_jobs as tj   # 只借 JobsTests 的夹具；不 from-import 类名，免得 unittest 把它整组再跑一遍
from tests.jobcase import ios_fixture
import layout


class ContainerVariantTest(unittest.TestCase):
    """S2A_CONTAINER_SUFFIX / S2A_CONTAINER_NAME：开发用的并存安装，不设时与玩家一致"""
    def test_suffix_changes_bundle_id_only_when_set(self):
        with um.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("S2A_CONTAINER_SUFFIX", None)
            self.assertEqual(install.container_bundle_id("TEAM1"), "io.s2arm.games.team1")
        with um.patch.dict(os.environ, {"S2A_CONTAINER_SUFFIX": "Feat/iCloud_Save Sync"}):
            self.assertEqual(install.container_bundle_id("TEAM1"), "io.s2arm.games.team1.featicloudsavesync")   # 只留小写字母 / 数字 / 连字符

    def test_display_name_rewrites_plist_strings_only_when_set(self):
        with tempfile.TemporaryDirectory() as d:
            app = Path(d) / "S2Arm.app"; (app / "zh-Hans.lproj").mkdir(parents=True)
            strings = app / "zh-Hans.lproj/InfoPlist.strings"
            strings.write_text('"CFBundleDisplayName" = "S2Arm";\n"Other" = "x";\n', encoding="utf-8")
            with um.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("S2A_CONTAINER_NAME", None)
                self.assertIsNone(install.container_display_name(app))
                self.assertIn('"S2Arm"', strings.read_text(encoding="utf-8"))
            with um.patch.dict(os.environ, {"S2A_CONTAINER_NAME": "S2Arm·iCloud"}):
                self.assertEqual(install.container_display_name(app), "S2Arm·iCloud")
            text = strings.read_text(encoding="utf-8")
            self.assertIn('"CFBundleDisplayName" = "S2Arm·iCloud";', text)
            self.assertIn('"Other" = "x";', text)


class CableFirstTest(unittest.TestCase):
    """线连的优先：插线的一台 + 同网配对的另一台 = 一台（手算：只留 wired=True 的那行）；没有线连的就看网络的"""
    def test_cable_first(self):
        import target
        a, b = dict(id="USB", wired=True), dict(id="NET", wired=False)
        self.assertEqual(target.cable_first([b, a]), [a])
        self.assertEqual(target.cable_first([b]), [b])
        self.assertEqual(target.cable_first([]), [])

    def test_windows_usb_first(self):
        W = type("W", (), {"mux_devices": staticmethod(lambda: [("U1", "USB", 1), ("U2", "Network", 2), ("U1", "Network", 3)])})
        self.assertEqual(install.win_udids(W), ["U1"])
        W2 = type("W", (), {"mux_devices": staticmethod(lambda: [("U2", "Network", 2)])})
        self.assertEqual(install.win_udids(W2), ["U2"])


class PickTests(unittest.TestCase):
    def test_team_choice_and_container_bundle_id(self):
        free = dict(id="TEAM1", name="个人", free=True); paid = dict(id="PAID1", name="付费", free=False)
        self.assertEqual(install.pick_team([free, paid])["id"], "PAID1")   # 有付费团队用付费的
        self.assertEqual(install.pick_team([free])["id"], "TEAM1")          # 只有免费的用免费的
        self.assertEqual(install.pick_team([free, paid], "TEAM1")["id"], "TEAM1")
        with um.patch.object(install, "xcode_account", return_value=(False, [])), self.assertRaises(Failure) as e: install.pick_team([])
        self.assertEqual(e.exception.code, "xcode_account_missing")   # 没登录
        with um.patch.object(install, "xcode_account", return_value=(True, [])), self.assertRaises(Failure) as e: install.pick_team([])
        self.assertEqual(e.exception.code, "xcode_team_pending")      # 登录了但团队没同步（11-08 Spare-host 实走）
        self.assertEqual(install.classify("error: No signing certificate \"iOS Development\" found"), "signing_certificate_missing")
        self.assertEqual(install.container_bundle_id("TEAM1"), "io.s2arm.games.team1")                  # 多款容器：团队专属固定包名，与款无关
        self.assertEqual(install.game_docs("0100c2500fc20000"), "Documents/games/0100C2500FC20000")            # 每款设备侧文档根（游戏号大写，同容器 Games/<游戏号>）
        self.assertEqual(install.game_ctx("0100c2500fc20000"), "@bundle/Games/0100C2500FC20000/launch.ctx")

    def test_classify_signing_errors(self):
        # Apple 9120（7 天 10 个包名，删 App 不退）与设备同时 3 个自签 App 是两件事，各报各的码
        self.assertEqual(install.classify("error: Your maximum App ID limit has been reached. You may create up to 10 App IDs every 7 days."), "free_appid_limit")
        self.assertEqual(install.classify("The maximum number of apps for free development profiles has been reached."), "free_app_limit")   # 较早 iOS 的装机原文：说的是装上的 apps
        self.assertEqual(install.classify("This device has reached the maximum number of installed apps using a free developer profile"), "free_app_limit")   # 11-06 iOS 27 原文
        self.assertEqual(install.classify('error: No Account for Team "TEAM1".'), "xcode_account_missing")
        self.assertEqual(install.classify("error: something else"), "signing_failed")

    FAKE_TEAM = "ABCDE" + "12345"
    FAKE_OU = "TEAM12" + "3456"

    def test_manual_team_id_is_tried_even_when_not_detected(self):
        self.assertEqual(install.pick_team([], self.FAKE_TEAM), dict(id=self.FAKE_TEAM, name="", free=True))
        with self.assertRaises(Failure) as e: install.pick_team([], "bad id")
        self.assertIn("Team ID", str(e.exception))

    @unittest.skipUnless(shutil.which("openssl") and os.path.exists("/usr/bin/openssl"), "需要 /usr/bin/openssl")
    def test_keychain_certificate_gives_team_without_xcode_cache(self):
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(["/usr/bin/openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-subj", f"/CN=Apple Development: X ({self.FAKE_TEAM})/OU={self.FAKE_OU}/O=X/C=US",
                            "-keyout", d + "/k", "-out", d + "/c"], check=True, capture_output=True)
            pem = open(d + "/c").read()
        good, other = "A" * 40, "B" * 40
        def security(args, env):
            if args[0] == "find-identity":
                return f'  1) {good} "Apple Development: X ({self.FAKE_TEAM})"\n  2) {other} "Developer ID Application: X ({self.FAKE_OU})"\n'
            return f"SHA-1 hash: {good}\n{pem}SHA-1 hash: {other}\n{pem}"
        with um.patch.object(install, "_security_text", security):
            self.assertEqual(install.keychain_teams(), [dict(id=self.FAKE_OU, name="", free=True)])   # 同一团队去重，Developer ID 不算
            with um.patch.object(install, "_xcode_prefs", return_value={}), um.patch.object(install, "profile_teams", return_value=[]):
                self.assertEqual(install.xcode_account()[1], [dict(id=self.FAKE_OU, name="", free=True)])
                self.assertEqual(install.pick_team(install.xcode_account()[1])["id"], self.FAKE_OU)
        with um.patch.object(install, "_security_text", return_value=""), um.patch.object(install, "_xcode_prefs", return_value={}):
            self.assertEqual(install.xcode_account(), (False, []))

    def test_signing_summary_has_counts_not_names(self):
        prefs = {"DVTDeveloperAccountManagerAppleIDLists": {"a": [{"identifier": "secret@example.com"}]}, "IDEProvisioningTeamByIdentifier": {"x": [{"teamID": "SECRET" + "1234", "teamName": "秘密团队"}]}}
        with um.patch.object(install, "_xcode_prefs", return_value=prefs), um.patch.object(install, "development_identities", return_value={"A"}), \
                um.patch.object(install.subprocess, "run", return_value=um.Mock(stdout="Xcode 27.1\nBuild")):
            text = install.signing_summary()
        self.assertEqual(text, "Xcode 27.1；账号 1 个；团队缓存键有；可用团队 1 个；开发证书 1 张")
        self.assertNotIn("secret", text); self.assertNotIn("秘密", text)

    def test_signing_project_is_stable_and_buildable_shape(self):
        import types, plistlib as _p
        with tempfile.TemporaryDirectory() as d:
            c = types.SimpleNamespace(private=Path(d))
            path = install.signing_project(c)["path"]
            text = (Path(path) / "project.pbxproj").read_text(encoding="utf-8")
            self.assertRegex(text, r'PRODUCT_BUNDLE_IDENTIFIER = "io\.s2arm\.signing\.h[0-9a-f]{10}"')
            self.assertNotIn("@BUNDLE@", text); self.assertNotIn("DEVELOPMENT_TEAM", text)
            self.assertEqual(install.signing_project(c)["path"], path)
            self.assertTrue((Path(d) / "signing-helper/App.swift").is_file())

    def test_prefs_with_control_characters_still_show_account(self):
        body = plistlib.dumps(dict(DVTDeveloperAccountManagerAppleIDLists=dict(a=[dict(identifier="x")]))).replace(b"<dict>", b"<dict><key>ext\x1bkey</key><string>v</string>", 1)
        with um.patch.object(install.subprocess, "run", return_value=um.Mock(returncode=0, stdout=body)):
            self.assertIn("DVTDeveloperAccountManagerAppleIDLists", install._xcode_prefs(None))
        with um.patch.object(install.subprocess, "run", return_value=um.Mock(returncode=0, stdout=b"not a plist")):
            self.assertEqual(install._xcode_prefs(None), {install.PREFS_UNREADABLE: True})

    def test_xcode_teams_reads_prefs(self):
        d = tempfile.mkdtemp(); fake = Path(d) / "defaults"
        prefs = plistlib.dumps({"IDEProvisioningTeamByIdentifier": {"acc": [{"teamID": "TEAM1", "teamName": "我", "isFreeProvisioningTeam": True}, {"teamID": "TEAM1"}]}})
        (Path(d) / "p.plist").write_bytes(prefs)
        fake.write_text(f'#!/bin/sh\ncat "{d}/p.plist"\n'); fake.chmod(0o755)
        self.assertEqual(install.xcode_teams(dict(os.environ, PATH=d + ":" + os.environ["PATH"])), [dict(id="TEAM1", name="我", free=True)])


class ShellSdkTests(unittest.TestCase):
    # 11-07 Duo 实测：旧 SDK 链接的壳在 iOS 新一代机型上按旧尺寸缩小居中 → 装机时比「壳链接 SDK 大版本」与「iPhone 系统大版本」
    def _app(self, sdk):
        import struct
        d = Path(tempfile.mkdtemp()) / "S2Arm.app"; d.mkdir()
        (d / "Info.plist").write_bytes(plistlib.dumps({"CFBundleExecutable": "S2Arm"}))
        seg = struct.pack("<II", 0x19, 72) + bytes(64)                                   # 先放一条无关的 LC_SEGMENT_64，确认按 cmdsize 往后走
        bv = struct.pack("<IIIIII", 0x32, 24, 2, 0x00120000, sdk, 0) if sdk is not None else b""   # LC_BUILD_VERSION：platform 2（iOS）、minos 18.0
        cmds = seg + bv
        hdr = struct.pack("<IiiIIIII", 0xFEEDFACF, 0x0100000C, 0, 2, 2 if bv else 1, len(cmds), 0, 0)
        (d / "S2Arm").write_bytes(hdr + cmds); return d

    def test_linked_sdk_decodes_nibbles(self):
        self.assertEqual(install.linked_sdk(self._app(0x001A0500) / "S2Arm"), (26, 5, 0))   # 0x1A=26、0x05=5
        self.assertEqual(install.linked_sdk(self._app(0x001B0100) / "S2Arm"), (27, 1, 0))
        self.assertIsNone(install.linked_sdk(self._app(None) / "S2Arm"))                  # 没有 LC_BUILD_VERSION
        bad = Path(tempfile.mkdtemp()) / "x"; bad.write_bytes(b"#!/bin/sh\n"); self.assertIsNone(install.linked_sdk(bad))

    def test_warns_only_when_phone_newer_than_sdk(self):
        for sdk, os_ver, want in ((0x001A0500, "27.1", 1), (0x001B0100, "27.1", 0), (0x001A0500, "26.4", 0), (0x001A0500, "", 0)):
            c = um.Mock(); install.check_shell_sdk(c, self._app(sdk), {"os": os_ver})
            self.assertEqual(c.diagnostic.call_count, want, (hex(sdk), os_ver))
            if want:
                self.assertEqual(c.diagnostic.call_args.kwargs, dict(code="shell_sdk_old", severity="warning"))
                self.assertIn("iOS 26.5", c.diagnostic.call_args.args[0]); self.assertIn("iOS 27.1", c.diagnostic.call_args.args[0])


def _jobs_setup_no_phone_games(self):
    """JobsTests 的夹具 + 手机上没有别的游戏（install.phone_games 替身：这些用例不测「读手机游戏清单」，那条另有用例）"""
    tj.JobsTests.setUp(self)
    p = um.patch.object(install, "phone_games", lambda c, ph: set()); p.start(); self.addCleanup(p.stop)


class FlowTests(unittest.TestCase):
    setUp = _jobs_setup_no_phone_games; tearDown = tj.JobsTests.tearDown   # 只借夹具（临时游戏库 / 私有目录 / 任务上下文），不继承它的用例

    def _fixture(self, gid):
        """这款出好的 iPhone 版（本壳 SHELL1 出的容器件 + 资源包 cafe01）→ (image/ios, 资源包)"""
        kit, pack = ios_fixture(self.c, gid, shell="SHELL1")
        return kit.parent, pack

    def _real_ipa(self, path):
        """写真 zip ipa（sign_and_install 用 ditto 真解，要 PKZip 签名）"""
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("Payload/S2Arm.app/Info.plist", plistlib.dumps({"CFBundleIdentifier": "io.s2arm.games", "CFBundleExecutable": "S2Arm"}))
            z.writestr("Payload/S2Arm.app/S2Arm", b"bin")

    def _install(self, gid, pushed, applied_before, mbps=0, unpack_ok=True, existing=(), cant_open=False, locked=False):
        """跑 install.install（容器流程）：替身 assemble 出容器（含已有各款 + 本款）、签名装机、只推 fresh 的款。
        existing = 这台手机上已有的款（同壳、资源包号与设备记录一致），现组时带上、不再推。替身按 push_pack.apply 口径记起 App 解包"""
        if existing:
            self.c.catalog = lambda: {"games": [{"id": g, "name": g} for g in (gid, *existing)]}
            layout.save_device(self.c, "00000000-0000000000000002", dict(games={g: dict(pack="cafe01") for g in existing}))
        shell = self.c.private / "shell"; shell.mkdir(exist_ok=True); (shell / "manifest.json").write_text("{}")
        runs, seen, launches, state = [], {}, [], {"applied": applied_before, "unpack": "10-01 09:00:00 解开 3 个包 9 个文件 1 MB，.applied = cafe01（0.2 秒）\n"}
        all_games = list(existing) + [gid]
        def run(args, log=None, env=None, cwd=None, on_line=None):
            args = [str(a) for a in args]; runs.append(args)
            if len(args) > 5 and args[4].endswith("ship_ext.py") and args[5] == "container":
                o = Path(args[args.index("--out") + 1]); o.mkdir(parents=True, exist_ok=True)
                (o / "s2a_catalogue.json").write_text(json.dumps({"schema": 1, "shell_id": "SHELL1", "games": [{"title_id": x} for x in all_games]}))
            if len(args) > 5 and args[4].endswith("app_bundle.py") and args[5] == "write_ipa":
                self._real_ipa(Path(args[7]))
            if args[1].endswith("dev_sign.py"): seen["bid"] = plistlib.loads((Path(args[2]) / "Info.plist").read_bytes())["CFBundleIdentifier"]
            if args[2].endswith("tools/push_pack.py"):
                seen["env"] = env; seen["progress_flag"] = "--progress-lines" in args
                if seen["progress_flag"]:   # 推包工具每推完一批打的机器读行（tools/push_pack.py --progress-lines）
                    for d, k in ((0, 0), (1 << 20, 1), (3 << 20, 2)): on_line(f"S2A_PUSH_PROGRESS {d} {3 << 20} {k} 2")
                on_line(f"[12:00:00] 资源包：推 {pushed} 个文件 3 MB，分 1 批 + 索引 + 清单（1 秒）")
                if "--apply" in args and not (pushed == 0 and state["applied"] == "cafe01"):
                    launches.append({"S2A_PACK_APPLY_ONLY": "1"})
                    on_line("[12:00:05] 机上解包 13/17")
                    if locked:   # #333：推之前写探针就写不进
                        Path(log).write_text("【红】iPhone 写不进资源包：多半是手机锁屏了或数据线松了。解锁手机、保持亮屏，插稳数据线后再点一次。\n", encoding="utf-8")
                        raise install.Failure("tool", code="tool_exit")
                    if cant_open:
                        Path(log).parent.mkdir(parents=True, exist_ok=True)
                        Path(log).write_text("【红】资源包已传完，还差最后一步：在 iPhone 上打开 S2Arm（电脑这边打不开手机上的 App：起 App 失败：RSD 里没有 app_service）\n", encoding="utf-8")
                        raise install.Failure("tool", code="tool_exit")
                    if not unpack_ok:
                        Path(log).parent.mkdir(parents=True, exist_ok=True)
                        Path(log).write_text("【红】资源包推进去了，但机上解包 180 秒没有新进展（停在第 13 包）：打开这款游戏、保持在前台等它导入完\n", encoding="utf-8")
                        raise install.Failure("tool", code="tool_exit")
                    state["applied"] = "cafe01"
        def devicectl(c, *a, timeout=0):
            if a[:3] == ("device", "process", "launch"):
                launches.append(a)
                if unpack_ok: state["applied"] = "cafe01"; state["unpack"] += "11-06 12:00:00 解开 1 个包 2 个文件 0 MB，.applied = cafe01（0.1 秒）\n"
                return 0, {}, ""
            if a[:3] == ("device", "copy", "from"):
                src = a[a.index("--source") + 1]
                Path(a[a.index("--destination") + 1]).write_text(state["unpack"] if src.endswith("unpack.log") else state["applied"] + "\n", encoding="utf-8"); return 0, {}, ""
            return 0, {}, ""
        import core, diag, itertools, ipa as packer
        tick = itertools.count(1000.0, 1.0)   # 作业层进度节流按单调时钟 0.2 秒：替身里各行紧挨着打，让每次取时钟走 1 秒（真实推送每批几十秒、解包每包十几秒）
        with um.patch.object(install, "xcode_teams", return_value=[dict(id="TEAM1", name="我", free=True)]), \
             um.patch.object(install, "phone", return_value=dict(id="C891", name="测试设备2")), um.patch.object(install, "udid_of", return_value="00000000-0000000000000002"), \
             um.patch.object(install, "usb_mbps", return_value=mbps), \
             um.patch.object(packer, "current_shell_id", return_value="SHELL1"), um.patch.object(packer, "container_shell", return_value=shell), \
             um.patch.object(self.c, "run", run), um.patch.object(diag, "devicectl", devicectl), um.patch.object(install.time, "sleep"), \
             um.patch.object(core.time, "monotonic", lambda: next(tick)):
            r = install.install(self.c)
        return r, runs, seen, launches

    def test_repush_same_pack_unpack_timeout_maps_to_player_message(self):   # 推包工具报解包超时 → 玩家看到「打开游戏保持前台」、job_failed（不是推送失败）
        gid = "0100535012974000"; self._fixture(gid); self.c.r.update(gameId=gid)
        with self.assertRaises(install.Failure) as cm:
            self._install(gid, pushed=2, applied_before="cafe01", unpack_ok=False)
        self.assertIn("机上解包 180 秒没有新进展（停在第 13 包）", str(cm.exception)); self.assertEqual(cm.exception.code, "job_failed")
        r, runs, seen, launches = self._install(gid, pushed=2, applied_before="cafe01", unpack_ok=True)
        self.assertEqual(len(launches), 1)
        ev = [json.loads(l) for l in self.c.events.read_text(encoding="utf-8").splitlines() if l.strip()]   # 推包工具的「机上解包 k/n」→ 作业进度事件（启动器显示）
        self.assertTrue(any(e["type"] == "progress" and e["message"] == "机上解包 13/17" and (e["done"], e["total"], e.get("unit")) == (13, 17, "包") for e in ev), ev[-3:])

    def test_push_batches_become_byte_progress_then_unpack_counts(self):   # #266：推送阶段按批发字节计数（分母 = 本次待传 3 MB），之后机上解包另起「包」计数
        gid = "0100535012974000"; self._fixture(gid); self.c.r.update(gameId=gid)
        r, runs, seen, launches = self._install(gid, pushed=2, applied_before="", unpack_ok=True)
        self.assertTrue(seen["progress_flag"])
        ev = [json.loads(l) for l in self.c.events.read_text(encoding="utf-8").splitlines() if l.strip()]
        push = [(e["done"], e["total"], e.get("unit")) for e in ev if e.get("message") == install.PUSHING and e["type"] == "progress"]
        self.assertEqual(push[-1], (3 << 20, 3 << 20, "B")); self.assertTrue(all(t == 3 << 20 and u == "B" for _, t, u in push))
        last_push = max(i for i, e in enumerate(ev) if e.get("message") == install.PUSHING)
        unpack = next(i for i, e in enumerate(ev) if e.get("message") == "机上解包 13/17")
        self.assertLess(last_push, unpack)                     # 传完之后才进解包
        self.assertEqual((ev[unpack]["done"], ev[unpack]["total"], ev[unpack]["unit"]), (13, 17, "包"))   # 解包计数另起，不沿用传输 100%

    def test_locked_phone_push_reports_device_locked(self):   # #333：推包探针写不进 → device_locked，原因原话上屏（不是笼统的 resource_transfer_failed）
        gid = "0100535012974000"; self._fixture(gid); self.c.r.update(gameId=gid)
        with self.assertRaises(install.Failure) as e:
            self._install(gid, pushed=2, applied_before="", locked=True)
        self.assertEqual(e.exception.code, "device_locked")
        self.assertIn("锁屏", str(e.exception))

    def test_cant_open_app_is_warning_not_failure(self):   # #192：资源传完、电脑拉不起 App → 装机完成 + 黄字；推了多少照实报（替身：2 个文件 3 MB）
        gid = "0100535012974000"; out, pack = self._fixture(gid); self.c.r.update(gameId=gid)
        r, *_ = self._install(gid, pushed=2, applied_before="", cant_open=True)
        d = [x for x in self.c.job["diagnostics"] if x["code"] == "manual_unpack_required"]
        self.assertEqual(len(d), 1); self.assertEqual(d[0]["severity"], "warning"); self.assertIn("资源包已传完", d[0]["summary"])
        self.assertEqual(r["packFiles"], 2)
        self.assertIn("资源包已传完", r["unpackPending"])   # 结果带还差的一步（11-09 #336）：前端据此不说「已装到 iPhone」

    def test_install_signs_installs_and_pushes(self):
        gid = "0100535012974000"; out, pack = self._fixture(gid); self.c.r.update(gameId=gid)
        r, runs, seen, launches = self._install(gid, pushed=2, applied_before="")
        bid = "io.s2arm.games.team1"   # 容器包名：团队 + .games，与款无关
        ensure_profile = next(a for a in runs if "ensure_profile.sh" in a[1])
        self.assertEqual(ensure_profile[-2:], [str(self.c.engine / "pack/app.entitlements"), "00000000-0000000000000002"])   # 描述文件申请带设备 UDID（登记这台）
        self.assertIn(bid, ensure_profile); self.assertEqual(seen["bid"], bid)                                    # 签名前 Info.plist 已换成团队专属包名
        push = next(a for a in runs if a[2].endswith("tools/push_pack.py"))
        self.assertIn("run", push[3:]); self.assertIn("--apply", push)
        self.assertIn(f"Documents/games/{gid}", push)   # 资源包推进本款文档根
        self.assertTrue(seen["env"]["S2A_DEVICECTL"].endswith("Resources/bin/devicectl"))
        self.assertEqual(seen["env"]["S2A_PUSH_UDID"], "00000000-0000000000000002")   # 推包脚本据 UDID 走 s2a-devpush 流式（11-06）
        self.assertEqual(launches, [{"S2A_PACK_APPLY_ONLY": "1"}])                                           # 推了就起一次 App 只解包
        self.assertEqual((r["device"], r["bundleId"], r["packFiles"], r["free"]), ("测试设备2", bid, 2, True))
        self.assertNotIn("unpackPending", r)   # 解开了：结果不带还差的一步


    def test_container_install_paths(self):
        """装机走容器流程：包名 s2arm.<团队>.games；装前按 Documents/games/<游戏号> 拉见过的键、列存档；装后本款资源包推进本款文档根带启动上下文"""
        A, B = "0100000000010000", "0100000000020000"
        packs = {g: self._fixture(g)[1] for g in (A, B)}
        self.c.r.update(gameId=A)
        r, runs, seen, launches = self._install(A, pushed=1, applied_before="", existing=(B,))
        bid = "io.s2arm.games.team1"
        self.assertEqual(seen["bid"], bid)
        self.assertEqual(r["bundleId"], bid)
        push = [a[3:] for a in runs if a[2].endswith("tools/push_pack.py")]
        # put_game 只对 fresh 的款（本次 = A）推资源包；B 已在容器里，不再推
        self.assertEqual(len(push), 1)
        self.assertIn(f"Documents/games/{A}", push[0])
        self.assertIn(f"@bundle/Games/{A}/launch.ctx", push[0])

    def test_container_missing_pack_refuses_before_install(self):
        gid = "0100000000010000"
        # 有容器件但没有资源包（image/ios/pack）：ios_pack 拒绝，不走到找设备 / 组装
        kit = layout.image_ios(self.c, gid) / "kit"; kit.mkdir(parents=True); (kit / "main.npdm").write_bytes(b"META")
        self.c.r.update(gameId=gid)
        with um.patch.object(install, "phone_for", side_effect=AssertionError("不该走到找设备")):
            with self.assertRaises(Failure) as e: install.install(self.c)
        self.assertEqual(e.exception.code, "pack_missing")
        # 连容器件也没有（没出过 iPhone 版）：报「先打 iPhone 包」
        self.c.r.update(gameId="0100000000090000")
        with self.assertRaises(Failure) as e: install.install(self.c)
        self.assertEqual(e.exception.code, "job_failed"); self.assertIn("打 iPhone 包", str(e.exception))

    def test_container_assemble_uses_each_games_kit(self):
        """assemble：每款都从这款文件夹的 image/ios/kit 现组（--game），不带 --base 底包；--host-abi 必传；产物出到游戏库里的临时目录，只留 S2Arm.ipa"""
        import ipa as packer, host_update
        A, B = "0100000000010000", "0100000000020000"
        kits = {g: ios_fixture(self.c, g, shell="SID1")[0] for g in (A, B)}
        self.c.catalog = lambda: {"games": [{"id": A, "name": "甲"}, {"id": B, "name": "乙"}]}
        shell = self.c.private / "shell"; shell.mkdir(); (shell / "manifest.json").write_text("{}")
        runs = []
        def run(args, log=None, env=None, cwd=None, on_line=None):
            args = [str(a) for a in args]; runs.append(args)
            if len(args) > 5 and args[4].endswith("ship_ext.py") and args[5] == "container":
                o = Path(args[args.index("--out") + 1]); o.mkdir(parents=True, exist_ok=True)
                gs = [A] if args.count("--game") == 1 else [A, B]
                (o / "s2a_catalogue.json").write_text(json.dumps({"schema": 1, "shell_id": "SID1", "games": [{"title_id": x} for x in gs]}))
            if len(args) > 5 and args[4].endswith("app_bundle.py") and args[5] == "write_ipa":
                Path(args[7]).write_bytes(b"ipa")
        with um.patch.object(packer, "current_shell_id", return_value="SID1"), \
             um.patch.object(packer, "container_shell", return_value=shell), \
             um.patch.object(packer, "release_version", return_value="9.8.7"), \
             um.patch.object(self.c, "run", run):
            ipa1, games1, sid1 = packer.assemble(self.c, A)
            ipa2, games2, sid2 = packer.assemble(self.c, A, [B])
        c1, c2 = [a for a in runs if len(a) > 5 and a[5] == "container"]
        for cmd in (c1, c2):
            self.assertNotIn("--base", cmd)
            self.assertEqual(cmd[cmd.index("--host-abi") + 1], str(host_update.HOST_ABI))
            self.assertEqual(cmd[cmd.index("--version") + 1], "9.8.7")   # 三端同号：电脑上 S2Arm 的发版号写进容器
            self.assertEqual((cmd[cmd.index("--sign") + 1], cmd[cmd.index("--shell") + 1]), ("adhoc", str(shell)))
        self.assertEqual([c1[i + 1] for i, x in enumerate(c1) if x == "--game"], [str(kits[A].resolve())])
        self.assertEqual([c2[i + 1] for i, x in enumerate(c2) if x == "--game"], [str(kits[A].resolve()), str(kits[B].resolve())])
        self.assertEqual((games1, games2, sid1, sid2), ([A], [A, B], "SID1", "SID1"))
        for ipa in (ipa1, ipa2):
            self.assertEqual(ipa.parent.parent, self.c.library); self.assertTrue(ipa.parent.name.startswith(".container-"))
            self.assertEqual(sorted(p.name for p in ipa.parent.iterdir()), ["S2Arm.ipa", "custom.json"])   # 组完删 S2Arm.app
        self.assertFalse(layout.phone_ipa(self.c).exists())   # 组装不碰默认位置根（装上之后才 commit_phone_ipa）
        # 没出过 iPhone 版的款报 container_stale（没有容器件）
        with um.patch.object(packer, "current_shell_id", return_value="SID1"), \
             um.patch.object(packer, "container_shell", return_value=shell), \
             um.patch.object(self.c, "run", run):
            with self.assertRaises(Failure) as e: packer.assemble(self.c, "0100000000090000")
        self.assertEqual(e.exception.code, "container_stale")

    def _ready(self, devices, teams=(dict(id="team-a", name="我", free=True),), rc=0, usb=(), raises=None):
        """跑装机前清单：devicectl list devices 回 devices（手造的 devicectl 行），Xcode 团队 teams"""
        import diag
        data = {"result": {"devices": list(devices)}}
        import target
        def devicectl(c, *a, timeout=0):
            if raises: raise raises
            if a[:3] == ("device", "info", "details"):
                return rc, {"result": next((d for d in devices if d["identifier"] == a[-1]), {})}, ""
            return rc, data, ""
        with um.patch.object(install, "xcode_teams", return_value=list(teams)), um.patch.object(diag, "devicectl", devicectl), \
             um.patch.object(target, "usb_idevices", return_value=list(usb)):
            r = install.readiness(self.c)
        return r, {i["id"]: i for i in r["items"]}

    @staticmethod
    def _dev(name="测试设备2", transport="wired", pairing="paired", devmode="enabled", tunnel="disconnected", platform="iOS", reality="physical"):
        return {"identifier": name, "hardwareProperties": {"reality": reality, "platform": platform},
                "connectionProperties": {"transportType": transport, "pairingState": pairing, "tunnelState": tunnel},
                "deviceProperties": {"name": name, "developerModeStatus": devmode}}

    def test_readiness_all_ready(self):
        gid = "0100535012974000"; self._fixture(gid); self.c.r.update(gameId=gid)
        r, it = self._ready([self._dev()])
        self.assertTrue(r["ready"]); self.assertEqual(r["device"], "测试设备2")
        self.assertEqual([i["id"] for i in r["items"]], ["package", "appleId", "connected", "trusted", "developerMode", "deviceReady"])
        self.assertEqual(r["ipaPath"], "")   # 「显示 IPA」只指真有的那份：还没装过、默认位置根没有 S2Arm.ipa → 空（原先指这款 image/ios，打开的是游戏目录）
        layout.phone_ipa(self.c).write_bytes(b"ipa")                                  # 装过一次：默认位置根有整库共用的那份
        self.assertEqual(self._ready([self._dev()])[0]["ipaPath"], str(layout.phone_ipa(self.c)))
        self.assertEqual((r["account"], it["appleId"]["title"]), ("我", "已登录 Apple ID"))
        self.assertTrue(all(i["detail"] == "" for i in r["items"]))   # 好了的项不带说明

    def test_readiness_steps_in_order(self):
        gid = "0100535012974000"; self.c.r.update(gameId=gid)   # 没出 iPhone 包
        r, it = self._ready([], teams=())                       # 没登录、没连
        self.assertFalse(r["ready"])
        self.assertEqual([i["id"] for i in r["items"] if i["blocking"]], ["package"])
        self.assertEqual(r["ipaPath"], "")
        self.assertEqual((it["package"]["ok"], it["package"]["who"]), (False, "app"))
        self.assertFalse(it["appleId"]["ok"]); self.assertIn("Apple Accounts", it["appleId"]["detail"]); self.assertIn("团队列表", it["appleId"]["detail"])
        self.assertFalse(it["connected"]["ok"])
        self.assertTrue(it["trusted"]["waiting"]); self.assertTrue(it["developerMode"]["waiting"])   # 没连上：后两项是「等前一步」，不报没打开
        self.assertEqual((it["trusted"]["detail"], it["developerMode"]["detail"]), ("", ""))
        # 连上了但没点信任：信任这项要玩家做，开发者模式仍在等
        r, it = self._ready([self._dev(pairing="unpaired", devmode=None)])
        self.assertTrue(it["connected"]["ok"]); self.assertFalse(it["trusted"]["ok"]); self.assertFalse(it["trusted"]["waiting"])
        self.assertTrue(it["developerMode"]["waiting"])
        # 信任了、开发者模式没开
        r, it = self._ready([self._dev(devmode="disabled")])
        self.assertTrue(it["trusted"]["ok"]); self.assertFalse(it["developerMode"]["ok"]); self.assertFalse(it["developerMode"]["waiting"])
        self.assertIn("开发者模式", it["developerMode"]["detail"])

    def test_readiness_only_package_blocks(self):
        gid = "0100535012974000"; self._fixture(gid); self.c.r.update(gameId=gid)   # 出好了包，但没登录、没连手机
        r, it = self._ready([], teams=())
        self.assertTrue(r["ready"])
        self.assertFalse(it["appleId"]["ok"]); self.assertFalse(it["connected"]["ok"])
        self.assertEqual([i["id"] for i in r["items"] if i["blocking"]], [])

    def test_readiness_device_filtering(self):
        gid = "0100535012974000"; self._fixture(gid); self.c.r.update(gameId=gid)
        # 模拟器、Mac、只走无线隧道没通的不算连上
        r, it = self._ready([self._dev(reality="simulated"), self._dev(platform="macOS"), self._dev(transport=None, tunnel="unavailable")])
        self.assertFalse(it["connected"]["ok"])
        # 两台都连着：要只留一台
        r, it = self._ready([self._dev(), self._dev(name="测试设备1")])
        self.assertFalse(it["connected"]["ok"]); self.assertIn("不止一台", it["connected"]["detail"]); self.assertEqual(r["device"], ""); self.assertTrue(it["trusted"]["waiting"])
        # 读不到设备列表（devicectl 失败）= 当没连
        r, it = self._ready([self._dev()], rc=1)
        self.assertFalse(it["connected"]["ok"]); self.assertIn("读不到设备列表", it["connected"]["detail"])

    def test_readiness_single_paired_network_device_counts(self):
        gid = "0100535012974000"; self._fixture(gid); self.c.r.update(gameId=gid)
        r, it = self._ready([self._dev(transport="localNetwork")])
        self.assertTrue(it["connected"]["ok"]); self.assertEqual(r["device"], "测试设备2")
        _, it = self._ready([self._dev(transport="localNetwork"), self._dev(name="另一台", transport="localNetwork")])
        self.assertFalse(it["connected"]["ok"]); self.assertIn("多台配对过", it["connected"]["detail"])
        _, it = self._ready([self._dev(transport="localNetwork"), self._dev(name="线连的")])
        self.assertTrue(it["connected"]["ok"])   # 有线优先，同网另一台不算两台

    def _link(self, states, want="", polling=False, usb=(), results=None, raises=None, details=None):
        """states 是列表快照；details 独立给详情回包，缺省沿用下一张快照的同一设备；results 可给错误或异常"""
        import diag, target
        calls, seen, detail_seen = [], [0], [0]
        def devicectl(c, *a, timeout=0):
            if a[:2] == ("list", "devices"):
                if raises: raise raises
                state = states[min(seen[0], len(states) - 1)]; seen[0] += 1
                return state if isinstance(state, tuple) else (0, {"result": {"devices": state}}, "")
            calls.append(a[:3])
            result = (results or {}).get(a[1], (0, ""))
            if isinstance(result, Exception): raise result
            rc, raw = result
            if a[:3] == ("device", "info", "details"):
                if details is not None:
                    row = details[min(detail_seen[0], len(details) - 1)]
                else:
                    rows = states[min(detail_seen[0] + 1, len(states) - 1)]
                    row = next((d for d in rows if d["identifier"] == a[-1]), {})
                detail_seen[0] += 1
                return rc, {"result": row}, raw
            return rc, {}, raw
        with um.patch.object(diag, "devicectl", devicectl), um.patch.object(target, "usb_idevices", return_value=list(usb)):
            return install.device_link(self.c, want, polling=polling), calls

    def _p(self, name="甲", transport="wired", pairing="paired", tunnel="disconnected", devmode="enabled", ddi=True, ident="D1", udid="SAMPLE-UDID-00112233"):
        d = self._dev(name=name, transport=transport, pairing=pairing, tunnel=tunnel, devmode=devmode)
        d["identifier"] = ident; d["hardwareProperties"]["udid"] = udid; d["deviceProperties"]["ddiServicesAvailable"] = ddi
        return d

    def test_link_reports_why_the_list_is_unreadable(self):
        self.assertIn("超时", self._link([], raises=subprocess.TimeoutExpired("devicectl", 60), usb=["iPad"])[0]["problem"])   # 读不到优先：不说成「没插」
        self.assertIn("CoreDevice", self._link([(127, {}, "x")])[0]["problem"])
        link, _ = self._link([(1, {}, "boom")])
        self.assertIn("读不到设备列表", link["problem"]); self.assertEqual(link["trace"][0]["rc"], 1); self.assertEqual(link["trace"][0]["raw"], "boom")

    def test_link_usb_branches(self):
        self.assertIn("USB 上没看到", self._link([[]])[0]["problem"])
        link, _ = self._link([[self._p(transport=None, tunnel="unavailable")]], usb=["iPad"])
        self.assertEqual(link["stage"], "connected"); self.assertIn("还没列出", link["problem"])

    def test_link_pairs_an_untrusted_phone(self):
        link, calls = self._link([[self._p(pairing="unpaired")], [self._p()]])
        self.assertEqual(link["stage"], ""); self.assertIn(("manage", "pair", "--device"), calls)
        link, _ = self._link([[self._p(pairing="unpaired")]], results={"pair": (1, "user denied")})
        self.assertEqual(link["stage"], "trusted"); self.assertIn("信任", link["problem"]); self.assertIn("user denied", link["problem"])

    def test_link_polling_does_not_repeat_the_trust_prompt(self):
        states = [[self._p(pairing="unpaired")]]
        self.assertIn(("manage", "pair", "--device"), self._link(states, polling=True)[1])
        link, calls = self._link(states, polling=True)
        self.assertEqual(calls, []); self.assertEqual(link["stage"], "trusted")

    def test_link_rechecks_disabled_mode_from_details_not_stale_list(self):
        stale = self._p(devmode="disabled", ddi=False)
        fresh = self._p(tunnel="connected")
        other = self._p(ident="D2", transport="localNetwork")
        for polling in (False, True):
            with self.subTest(polling=polling):
                link, calls = self._link([[stale, other]], details=[fresh], polling=polling)
                self.assertEqual(link["stage"], "")
                self.assertEqual(calls, [("device", "info", "details")])
                self.assertEqual(link["device"], fresh)
                self.assertEqual(link["devices"], [fresh, other])

    def test_link_confirmed_disabled_mode_stops_before_ddi_update(self):
        link, calls = self._link([[self._p(devmode="disabled", ddi=False)]])
        self.assertEqual(link["stage"], "developerMode")
        self.assertEqual(calls, [("device", "info", "details")]); self.assertIn("开发者模式", link["problem"])

    def test_link_detail_query_failure_does_not_claim_mode_disabled(self):
        for result, rc in [((1, "device locked"), 1), (subprocess.TimeoutExpired("devicectl", 20), "timeout"), (OSError("disconnected"), "oserror")]:
            with self.subTest(result=result):
                link, calls = self._link([[self._p(devmode="disabled")]], results={"info": result})
                self.assertEqual(link["stage"], "prepare")
                self.assertIn("开发连接", link["problem"])
                self.assertEqual(link["trace"][-1]["rc"], rc)
                self.assertEqual(calls, [("device", "info", "details")])

    def test_link_details_must_identify_same_phone_and_mode(self):
        stale = self._p(devmode="disabled")
        for row in (None, [], {}, {"identifier": "D1", "deviceProperties": None}, self._p(ident="D2"), self._p(devmode=None), self._p(devmode="unknown")):
            with self.subTest(row=row):
                link, calls = self._link([[stale]], details=[row])
                self.assertEqual(link["stage"], "prepare")
                self.assertEqual(calls, [("device", "info", "details")])

    def test_link_fresh_disabled_mode_overrides_enabled_list(self):
        link, calls = self._link([[self._p(ddi=False)]], details=[self._p(devmode="disabled", ddi=False)])
        self.assertEqual(link["stage"], "developerMode")
        self.assertEqual(calls, [("device", "info", "details")])

    def test_link_rechecked_enabled_mode_can_prepare_ddi(self):
        stale = self._p(devmode="disabled", ddi=False)
        down = self._p(tunnel="connected", ddi=False)
        fresh = self._p(tunnel="connected")
        link, calls = self._link([[stale]], details=[down, fresh])
        self.assertEqual(link["stage"], "")
        self.assertEqual(link["device"], fresh)
        self.assertEqual(calls, [("device", "info", "details"), ("manage", "ddis", "update"), ("device", "info", "details")])

    def test_link_ddi_recheck_does_not_accept_unreadable_or_disabled_details(self):
        down = self._p(tunnel="connected", ddi=False)
        for row, stage in [({}, "prepare"), (self._p(devmode="disabled", ddi=False), "developerMode")]:
            with self.subTest(row=row):
                link, calls = self._link([[down]], details=[down, row])
                self.assertEqual(link["stage"], stage)
                self.assertEqual(calls, [("device", "info", "details"), ("manage", "ddis", "update"), ("device", "info", "details")])

    def test_readiness_failed_mode_query_waits_and_shows_connection_error(self):
        link, _ = self._link([[self._p(devmode="disabled")]], results={"info": (1, "device locked")})
        with um.patch.object(install, "device_link", return_value=link):
            _, items = self._ready([])
        self.assertFalse(items["developerMode"]["ok"])
        self.assertTrue(items["developerMode"]["waiting"])
        self.assertEqual(items["developerMode"]["detail"], "")
        self.assertFalse(items["deviceReady"]["ok"])
        self.assertFalse(items["deviceReady"]["waiting"])
        self.assertIn("device locked", items["deviceReady"]["detail"])

    def test_link_brings_up_an_idle_tunnel(self):
        link, calls = self._link([[self._p(ddi=False)], [self._p(tunnel="connected")]])
        self.assertEqual(link["stage"], ""); self.assertEqual(calls, [("device", "info", "details")])
        link, calls = self._link([[self._p(tunnel="connected")]])
        self.assertEqual((link["stage"], calls), ("", []))   # 已经通了不折腾
        link, _ = self._link([[self._p(transport="localNetwork", ddi=False)]], results={"info": (1, "device locked")})
        self.assertEqual(link["stage"], "prepare"); self.assertIn("开发连接", link["problem"]); self.assertIn("device locked", link["problem"])

    def test_link_updates_disk_images_when_services_stay_down(self):
        down = [self._p(tunnel="connected", ddi=False)]
        link, calls = self._link([down, down, [self._p(tunnel="connected")]])
        self.assertEqual(link["stage"], ""); self.assertEqual(calls, [("device", "info", "details"), ("manage", "ddis", "update"), ("device", "info", "details")])
        link, _ = self._link([down], results={"info": (0, "still no ddi")})
        self.assertEqual(link["stage"], "prepare"); self.assertIn("开发者服务", link["problem"])

    def test_link_stale_choice(self):
        link, _ = self._link([[self._p(tunnel="connected")]], want="旧手机")
        self.assertEqual((link["stage"], link["device"]["identifier"]), ("", "D1"))
        link, _ = self._link([[self._p(name="甲"), self._p(name="乙", ident="D2")]], want="旧手机")
        self.assertEqual(link["stage"], "connected")
        for text in ("旧手机", "甲", "乙"): self.assertIn(text, link["problem"])
        link, _ = self._link([[self._p(name="甲"), self._p(name="乙", ident="D2"), self._p(name="网上的", ident="D3", transport="localNetwork")]], want="旧手机")
        self.assertNotIn("网上的", link["problem"])   # 有线优先：同网配对的不列
        link, _ = self._link([[self._p(name="线连的", tunnel="connected"), self._p(name="网上的", ident="D3", transport="localNetwork")]], want="旧手机")
        self.assertEqual((link["stage"], link["device"]["identifier"]), ("", "D1"))
        link, _ = self._link([[self._p(name="甲", tunnel="connected"), self._p(name="乙", ident="D2", tunnel="connected")]], want="乙")
        self.assertEqual(link["device"]["identifier"], "D2")

    def test_phone_failure_writes_a_redacted_report(self):
        import diag, target
        udid = "00008140-" + "AB" * 8
        data = {"result": {"devices": [self._p(name="张三的手机", ddi=False, udid=udid)]}}
        def devicectl(c, *a, timeout=0):
            return (0, data, "") if a[:2] == ("list", "devices") else (1, {}, f"error on {udid}")
        with um.patch.object(diag, "devicectl", devicectl), um.patch.object(target, "usb_idevices", return_value=[]), self.assertRaises(Failure) as e:
            install.phone(self.c)
        self.assertEqual(e.exception.code, "device_not_ready"); self.assertNotIn(udid, str(e.exception))
        report = (self.c.logs / f"{self.c.task}-device.log").read_text(encoding="utf-8")
        self.assertNotIn(udid, report); self.assertNotIn("张三", report)
        for field in ("rc=1", "transport=wired", "pairing=paired", "tunnel=disconnected", "developerMode=enabled", "ddi=False", "boot="): self.assertIn(field, report)

    def test_no_pack_dir_fails_before_install(self):
        # 审计 18:00 P1-58：没有「…资源包」目录不装、不报成功（原先照装，启动器写死「资源包也拷好了」）
        gid = "0100535012974000"; out, pack = self._fixture(gid); self.c.r.update(gameId=gid)
        import shutil; shutil.rmtree(pack)
        with self.assertRaises(install.Failure) as e:
            self._install(gid, pushed=0, applied_before="")
        self.assertEqual(e.exception.code, "pack_missing"); self.assertIn("资源包", str(e.exception))

    def test_reinstall_nothing_changed_skips_apply(self):
        # 免费账号 7 天到期重装：资源包没变（推 0 个文件）且机上 .applied 已是本包 → 不起 App 解包
        gid = "0100535012974000"; out, pack = self._fixture(gid); self.c.r.update(gameId=gid)
        r, runs, seen, launches = self._install(gid, pushed=0, applied_before="cafe01")
        self.assertEqual(launches, []); self.assertEqual(r["packFiles"], 0)

    def test_usb2_link_warns_usb3_hint(self):
        # 10-26：测试设备经集线器只有 480 Mb/s → 黄字 usb_slow 提示换 USB 3；5 Gb/s 不提示；装机照常完成
        gid = "0100535012974000"
        for mbps, want in ((480, 1), (5000, 0), (0, 0)):
            self.tearDown(); self.setUp(); out, pack = self._fixture(gid); self.c.r.update(gameId=gid)
            r, *_ = self._install(gid, pushed=1, applied_before="", mbps=mbps)
            d = [x for x in self.c.job["diagnostics"] if x["code"] == "usb_slow"]
            self.assertEqual(len(d), want, mbps); self.assertEqual(r["packFiles"], 1)
            if want:
                self.assertEqual(d[0]["severity"], "warning"); self.assertIn("USB 2.0（480 Mb/s）", d[0]["summary"])

    def test_usb_mbps_matches_serial(self):
        # system_profiler 的序列号 = 去掉横杠的 UDID；别的设备、没插的不算
        d = tempfile.mkdtemp(); fake = Path(d) / "system_profiler"
        data = {"SPUSBHostDataType": [{"_name": "hub", "_items": [
            {"_name": "iPhone", "USBDeviceKeySerialNumber": "000000000000000000000002", "USBDeviceKeyLinkSpeed": "480 Mb/s"},
            {"_name": "iPhone", "USBDeviceKeySerialNumber": "000000000000000000000001", "USBDeviceKeyLinkSpeed": "5 Gb/s"}]}]}
        (Path(d) / "o.json").write_text(json.dumps(data)); fake.write_text(f'#!/bin/sh\ncat "{d}/o.json"\n'); fake.chmod(0o755)
        env = dict(os.environ, PATH=d + ":" + os.environ["PATH"])
        self.assertEqual(install.usb_mbps("00000000-0000000000000002", env), 480)
        self.assertEqual(install.usb_mbps("00000000-0000000000000001", env), 5000)
        self.assertEqual(install.usb_mbps("00000000-0000000000000004", env), 0)
        self.assertEqual(install.usb_mbps("", env), 0)

    def test_locked_phone_maps_device_locked(self):
        # 10-26 测试设备2 锁屏：ensure_profile.sh 带出 xcodebuild 的目的设备报错，归成「iPhone 锁屏了」（原先是含糊的「签名没成」）
        line = "{ platform:iOS, arch:arm64, id:00000000-0000000000000002, name:测试设备2, error:The device needs to be unlocked to continue Unlock the device"
        self.assertEqual(install.classify(line), "device_locked")

    def test_profile_failure_maps_free_limit(self):
        gid = "0100535012974000"; out, pack = self._fixture(gid); self.c.r.update(gameId=gid)
        import ipa as packer
        def run(args, log=None, env=None, cwd=None, on_line=None):
            Path(log).parent.mkdir(parents=True, exist_ok=True); Path(log).write_text("error: Your maximum App ID limit has been reached.\n")
            raise Failure("S2Arm 工具退出，当前阶段未完成。", code="tool_exit")
        def fake_assemble(c, g, others=(), failed=(), root=None, dest=None):
            ipa = out / "t.ipa"; self._real_ipa(ipa)
            return ipa, [gid], "SID1"
        with um.patch.object(install, "xcode_teams", return_value=[dict(id="TEAM1", name="我", free=True)]), \
             um.patch.object(install, "phone", return_value=dict(id="C891", name="测试设备2")), um.patch.object(install, "udid_of", return_value=""), \
             um.patch.object(packer, "assemble", side_effect=fake_assemble), \
             um.patch.object(self.c, "run", run):
            with self.assertRaises(Failure) as e: install.install(self.c)
        self.assertEqual(e.exception.code, "free_appid_limit")   # 签名前注册包名被拒 = 包名额度，不是设备上装满
        self.assertIn("10 个包名", str(e.exception))
        self.assertNotIn("删掉", str(e.exception))


class PullSeenTests(unittest.TestCase):
    setUp = tj.JobsTests.setUp; tearDown = tj.JobsTests.tearDown

    def _pull(self, device_text):
        got = {}
        def devicectl(c, *a, timeout=0):
            got["src"] = a[a.index("--source") + 1]
            if device_text is None: return 1, {}, "no such file"
            Path(a[a.index("--destination") + 1]).write_text(device_text); return 0, {}, ""
        import diag
        with um.patch.object(diag, "devicectl", devicectl):
            r = install.pull_seen(self.c, "C891", "s2arm.z.0100c2500fc20000", "0100c2500fc20000", "00000000-0000000000000002")
        return r, got

    def test_append_overwrite_and_fork(self):
        seen = self.c.library / "games/0100c2500fc20000/seen"
        r, got = self._pull(None)
        self.assertEqual((r, got["src"], seen.exists()), (False, "Documents/wire/0100C2500FC20000/pso_user.tsv", False))   # 机上没有不算错
        self.assertTrue(self._pull("S\ta\n")[0]); self.assertEqual((seen / "00000000-0000000000000002.tsv").read_text(), "S\ta\n")
        self.assertFalse(self._pull("S\ta\n")[0])                                       # 没变
        self.assertTrue(self._pull("S\ta\nS\tb\n")[0])                                  # 续写 → 覆盖
        self.assertEqual((seen / "00000000-0000000000000002.tsv").read_text(), "S\ta\nS\tb\n")
        self.assertTrue(self._pull("S\tc\n")[0])                                        # 换包后重新记 → 另存，旧的保留
        self.assertEqual(sorted(p.read_text() for p in seen.glob("*.tsv")), ["S\ta\nS\tb\n", "S\tc\n"])


class SaveBackupTests(unittest.TestCase):
    setUp = tj.JobsTests.setUp; tearDown = tj.JobsTests.tearDown

    def _run(self, files, fail=None, list_rc=0, installed=False, apps_rc=0):
        got = []
        def devicectl(c, *a, timeout=0):
            if a[:3] == ("device", "info", "apps"):
                if apps_rc:
                    return apps_rc, {}, "devicectl 60 秒没有响应：手机可能锁屏了或连接断开"
                return 0, {"result": {"apps": [{"bundleIdentifier": "s2arm.z.0100c2500fc20000"}] if installed else []}}, ""
            if a[:3] == ("device", "info", "files") and list_rc:
                return list_rc, {}, "The operation couldn’t be completed"
            if a[:3] == ("device", "info", "files"):
                return 0, {"result": {"files": [dict(relativePath=r, resources=dict(isDirectory=d)) for r, d in files]}}, ""
            src = a[a.index("--source") + 1]; got.append(src)
            if fail and src.endswith(fail): return 1, {}, "Device locked"
            Path(a[a.index("--destination") + 1]).write_text("内容:" + src); return 0, {}, ""
        import diag
        with um.patch.object(diag, "devicectl", devicectl):
            return install.backup_saves(self.c, "C891", "s2arm.z.0100c2500fc20000", "0100C2500FC20000"), got

    def test_backs_up_every_file(self):
        d, got = self._run([("0000000000000001", True), ("0000000000000001/0", True), ("0000000000000001/0/save.dat", False),
                            ("0000000000000001/1/save.dat", False), ("0000000000000001/.lock", False)])
        self.assertEqual(got, [install.SAVE_DIR + "/0000000000000001/0/save.dat", install.SAVE_DIR + "/0000000000000001/1/save.dat"])   # 目录与 .lock 不拷
        self.assertEqual((d / "0000000000000001/0/save.dat").read_text(), "内容:" + got[0])
        self.assertEqual(d.parent, self.c.library / "games/0100C2500FC20000/save_backup")

    def test_nothing_on_device(self):
        self.assertEqual(self._run([])[0], None)                       # 第一次装：机上没有存档，不备份不报错

    def test_list_failure(self):
        self.assertEqual(self._run([], list_rc=1, installed=False)[0], None)   # App 不在机上：第一次装，照装
        with self.assertRaises(Failure) as e:
            self._run([], list_rc=1, installed=True)                           # App 在却列不出：停装
        self.assertEqual(e.exception.code, "device_locked")

    def test_unknown_install_state_stops_instead_of_skipping_backup(self):
        # #333：列存档超时（124）后连「装没装过」都问不出：不能当成第一次装（会跳过备份直接覆盖丢档），停装并报 device_locked
        with self.assertRaises(Failure) as e:
            self._run([], list_rc=124, apps_rc=124)
        self.assertEqual(e.exception.code, "device_locked")
        self.assertIn("解锁", str(e.exception))

    def _tree(self, tree, docs, locked=()):
        # 假设备文件系统（11-07 满屏线 Duo 实测）：列不存在的目录 devicectl 也报「failed to get a list of files」——和锁屏同一个错，只能靠父目录分辨
        def devicectl(c, *a, timeout=0):
            if a[:3] == ("device", "info", "apps"):
                return 0, {"result": {"apps": [{"bundleIdentifier": "s2arm.z.games"}]}}, ""   # App 在机上
            if a[:3] == ("device", "info", "files"):
                sub = a[a.index("--subdirectory") + 1]
                if sub in locked or not any(t == sub or t.startswith(sub + "/") for t in tree):
                    return 1, {}, "StreamingAction: The system failed to get a list of files on the remote device."
                return 0, {"result": {"files": [dict(relativePath=t[len(sub) + 1:], resources=dict(isDirectory=True)) for t in tree if t.startswith(sub + "/")]}}, ""
            raise AssertionError(a)
        import diag
        with um.patch.object(diag, "devicectl", devicectl):
            return install.backup_saves(self.c, "C891", "s2arm.z.games", "0100C2500FC20000", docs=docs)

    def test_missing_save_dir_under_listable_parent_installs(self):
        # 容器第一次装半途失败 / 从没进过游戏：机上 Documents/games 是空目录，存档目录不在 → 没有存档，照装（原先每次都拒装，玩家永久卡死）
        g = "Documents/games/0100C2500FC20000"
        self.assertIsNone(self._tree({"Documents", "Documents/games"}, g))
        self.assertIsNone(self._tree({"Documents", "Documents/games", g, g + "/资源包"}, g))       # 本款目录在、S2A 不在
        self.assertIsNone(self._tree({"Documents", "Documents/logs"}, "Documents"))                # 单款：Documents 在、S2A 不在

    def test_unlistable_parent_or_existing_save_dir_stops(self):
        g = "Documents/games/0100C2500FC20000"
        with self.assertRaises(Failure):   # Documents 也列不出（锁屏 / 隧道断）：分不清有没有存档，停装
            self._tree({"Documents", "Documents/games"}, g, locked={"Documents", "Documents/games", g, g + "/" + install.SAVE_REL})
        with self.assertRaises(Failure):   # 父目录里看得到存档目录、它本身却列不出：有档读不到，停装
            self._tree({"Documents", g + "/" + install.SAVE_REL + "/0000000000000001"}, g, locked={g + "/" + install.SAVE_REL})

    def test_copy_failure_stops_install(self):
        with self.assertRaises(Failure):
            self._run([("0000000000000001/0/save.dat", False)], fail="save.dat")




class AssembleTests(unittest.TestCase):
    """现组容器（assemble）/ put_game / 手机侧 S2Arm.ipa 与上一版（commit_phone_ipa、restore_previous）/ remove_from_phone 的细粒度测试（游戏库结构 v3）"""
    setUp = _jobs_setup_no_phone_games; tearDown = tj.JobsTests.tearDown
    A, B, C = "0100000000010000", "0100000000020000", "0100000000030000"
    COMPAT = dict(hostAbi=1, dmshChain="aaaa")   # 替身：本版引擎（新宿主）的兼容判据

    def _library(self, *games):
        """游戏库清单：(游戏号, 名字)…"""
        self.c.catalog = lambda: {"games": [{"id": g, "name": n} for g, n in games]}

    def _run(self, runs):
        """ship_ext container / app_bundle write_ipa 的替身：按 --game 容器件所在游戏文件夹的 game.json 认出各款，出真 zip ipa（带 s2a_catalogue.json）"""
        def run(args, log=None, env=None, cwd=None, on_line=None):
            args = [str(a) for a in args]; runs.append(args)
            if any(a.endswith("ship_ext.py") for a in args) and "container" in args:
                o = Path(args[args.index("--out") + 1]); o.mkdir(parents=True, exist_ok=True)
                gs = [json.loads((Path(args[i + 1]).parents[2] / "game.json").read_text())["id"] for i, x in enumerate(args) if x == "--game"]
                (o / "s2a_catalogue.json").write_text(json.dumps({"schema": 1, "shell_id": self.sid, "games": [{"title_id": g} for g in gs]}))
            if "write_ipa" in args:
                cat = json.loads((Path(args[-2]) / "s2a_catalogue.json").read_text())
                self._ipa(Path(args[-1]), cat["shell_id"], [g["title_id"] for g in cat["games"]])
        return run

    @staticmethod
    def _ipa(path, sid, games):
        """手机侧装机产物（真 zip，ipa_games 读 Payload/*.app/s2a_catalogue.json）"""
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("Payload/S2Arm.app/Info.plist", plistlib.dumps({"CFBundleIdentifier": "io.s2arm.games", "CFBundleExecutable": "S2Arm"}))
            z.writestr("Payload/S2Arm.app/s2a_catalogue.json", json.dumps({"schema": 1, "shell_id": sid, "games": [{"title_id": g} for g in games]}))
        return path

    def _assemble(self, gid, others=(), failed=(), sid="SID1", runs=None):
        import ipa as packer, host_update
        self.sid = sid
        shell = self.c.private / "shell"; shell.mkdir(exist_ok=True); (shell / "manifest.json").write_text("{}")
        runs = [] if runs is None else runs
        with um.patch.object(packer, "current_shell_id", return_value=sid), um.patch.object(packer, "container_shell", return_value=shell), \
             um.patch.object(host_update, "engine_compat", return_value=dict(self.COMPAT)), um.patch.object(self.c, "run", self._run(runs)):
            return packer.assemble(self.c, gid, others, failed=failed)

    @staticmethod
    def _games_arg(runs):
        cmd = next(a for a in runs if any(x.endswith("ship_ext.py") for x in a))
        return [json.loads((Path(cmd[i + 1]).parents[2] / "game.json").read_text())["id"] for i, x in enumerate(cmd) if x == "--game"], cmd

    def test_assemble_this_game_only(self):
        """第一次装：只组这一款，不带 --base；→ (ipa, [这款], 本壳号)"""
        self._library((self.A, "甲")); ios_fixture(self.c, self.A)
        runs = []
        ipa, games, sid = self._assemble(self.A, runs=runs)
        got, cmd = self._games_arg(runs)
        self.assertNotIn("--base", cmd)
        self.assertEqual((got, games, sid), ([self.A], [self.A], "SID1"))
        self.assertEqual(install_ipa_games(ipa), ([self.A], "SID1"))

    def test_assemble_passes_library_banner(self):
        """横图是游戏库的元数据（导入时抓好放在这款文件夹）：组装命令带 --banner 游戏号=那份；没有横图的款不带"""
        import layout
        self._library((self.A, "甲"), (self.B, "乙"))
        ios_fixture(self.c, self.A); ios_fixture(self.c, self.B)
        banner = layout.path(self.c, self.A, "banner.jpg"); banner.write_bytes(b"BANNER")
        runs = []
        self._assemble(self.A, others=(self.B,), runs=runs)
        _, cmd = self._games_arg(runs)
        self.assertEqual([cmd[i + 1] for i, x in enumerate(cmd) if x == "--banner"], [f"{self.A}={banner}"])

    def test_assemble_carries_phone_games_of_same_shell(self):
        """这台手机已有的款（同壳出的）现组时一起带上，容器件取各自的 image/ios/kit"""
        self._library((self.A, "甲"), (self.B, "乙"))
        ios_fixture(self.c, self.A); kb, _ = ios_fixture(self.c, self.B)
        runs = []
        ipa, games, sid = self._assemble(self.A, [self.B], runs=runs)
        got, cmd = self._games_arg(runs)
        self.assertEqual((got, games), ([self.A, self.B], [self.A, self.B]))
        self.assertIn(str(kb.resolve()), cmd)

    def test_assemble_other_shell_compatible_swaps_host_only(self):
        """乙是旧壳 SID1 出的、兼容判据与新宿主一致 → 原样带进新壳 SID2（只换宿主，不要求重出）；
        这台手机对 SID2 装回过上一版（failedShells 含 SID2）→ 不带、报 container_stale 点名；failedShells 里是别的壳不影响"""
        self._library((self.A, "甲"), (self.B, "乙款"))
        ios_fixture(self.c, self.A, shell="SID2"); kb, _ = ios_fixture(self.c, self.B, shell="SID1", compat=self.COMPAT)
        runs = []
        _, games, sid = self._assemble(self.A, [self.B], sid="SID2", runs=runs)
        self.assertEqual((games, sid), ([self.A, self.B], "SID2"))
        self.assertIn(str(kb.resolve()), self._games_arg(runs)[1])
        _, games, _ = self._assemble(self.A, [self.B], failed=["SID1"], sid="SID2")
        self.assertEqual(games, [self.A, self.B])
        with self.assertRaises(Failure) as e:
            self._assemble(self.A, [self.B], failed=["SID2"], sid="SID2")
        self.assertEqual(e.exception.code, "container_stale"); self.assertIn("乙款", str(e.exception))

    def test_assemble_other_shell_incompatible_is_stale(self):
        """乙是旧壳出的、新宿主认不得（直连链号不同 / 没记判据按旧版口径）→ container_stale 点名乙，不装半套"""
        self._library((self.A, "甲"), (self.B, "乙款"))
        ios_fixture(self.c, self.A, shell="SID2")
        for compat in (dict(hostAbi=1, dmshChain="bbbb"), dict(hostAbi=2, dmshChain="aaaa"), None):
            ios_fixture(self.c, self.B, shell="SID1", compat=compat)
            if compat is None:   # 没记判据的旧款：game.json 的 ios 里没有 hostAbi / dmshChain
                layout.update_info(self.c, self.B, ios=dict(shellId="SID1", pack="cafe01"))
            with self.assertRaises(Failure) as e:
                self._assemble(self.A, [self.B], sid="SID2")
            self.assertEqual(e.exception.code, "container_stale", compat); self.assertIn("乙款", str(e.exception))

    def test_assemble_skips_games_deleted_from_library(self):
        """#194：手机上的某款已从游戏库删除：不点名、不挡，新容器不带它，黄字说明"""
        self._library((self.A, "甲")); ios_fixture(self.c, self.A)
        notes, runs = [], []
        with um.patch.object(self.c, "diagnostic", lambda m, **k: notes.append((m, k.get("severity")))):
            _, games, _ = self._assemble(self.A, [self.B], runs=runs)
        self.assertEqual(games, [self.A]); self.assertEqual(self._games_arg(runs)[0], [self.A])
        self.assertEqual(len(notes), 1); self.assertIn(self.B, notes[0][0]); self.assertEqual(notes[0][1], "warning")

    def test_assemble_own_game_no_kit(self):
        """本款没有容器件 / 容器件不是本壳出的且不兼容 → container_stale"""
        self._library((self.A, "甲"))
        with self.assertRaises(Failure) as e: self._assemble(self.A)
        self.assertEqual(e.exception.code, "container_stale")
        ios_fixture(self.c, self.A, shell="SID1", compat=dict(hostAbi=1, dmshChain="bbbb"))
        with self.assertRaises(Failure) as e: self._assemble(self.A, sid="SID2")
        self.assertEqual(e.exception.code, "container_stale")

    def _phone(self, key="dev1"):
        pushed, signed = [], []
        ph = um.Mock()
        ph.key = key; ph.bid = install.container_bundle_id("TEAM1"); ph.info = dict(device="d", team="t", free=True)
        ph.install.side_effect = lambda ipa, games, steps=True: signed.append((list(games), install_ipa_games(ipa)))
        ph.push.side_effect = lambda g, pack: (pushed.append(g), (1, 0))[1]
        ph.before_push.return_value = None
        return ph, pushed, signed

    def test_put_game_pushes_this_game_and_changed_packs_only(self):
        """put_game：甲（这款）照推，即使资源包号和手机上记的一样；乙资源包号没变不推；丙在游戏库重出过（号变了）要推。
        装上后设备记录记下各款资源包号、壳号、包名，装机产物放默认位置根 S2Arm.ipa"""
        A, B, C = self.A, self.B, self.C
        self._library((A, "甲"), (B, "乙"), (C, "丙"))
        ios_fixture(self.c, A, pack="pa"); ios_fixture(self.c, B, pack="pb"); ios_fixture(self.c, C, pack="pc-new")
        layout.save_device(self.c, "dev1", dict(games={A: dict(pack="pa"), B: dict(pack="pb"), C: dict(pack="pc-old")}, shellId="SID1"))
        ph, pushed, signed = self._phone()
        import ipa as packer, host_update
        self.sid = "SID1"
        shell = self.c.private / "shell"; shell.mkdir(exist_ok=True); (shell / "manifest.json").write_text("{}")
        with um.patch.object(packer, "current_shell_id", return_value="SID1"), um.patch.object(packer, "container_shell", return_value=shell), \
             um.patch.object(host_update, "engine_compat", return_value=dict(self.COMPAT)), um.patch.object(self.c, "run", self._run([])):
            r = install.put_game(self.c, A, ph)
        self.assertEqual(pushed, [A, C])
        self.assertEqual(signed, [([A, B, C], ([A, B, C], "SID1"))])
        rec = layout.load_device(self.c, "dev1")
        self.assertEqual(rec["games"], {A: dict(pack="pa"), B: dict(pack="pb"), C: dict(pack="pc-new")})
        self.assertEqual((rec["shellId"], rec["bundleId"]), ("SID1", "io.s2arm.games.team1"))
        self.assertEqual(install_ipa_games(layout.phone_ipa(self.c)), ([A, B, C], "SID1"))
        self.assertEqual((r["outputPath"], r["games"], r["packFiles"]), (str(layout.phone_ipa(self.c)), [A, B, C], 2))

    def test_commit_phone_ipa_keeps_previous_only_when_shell_changes(self):
        """③ 同壳覆盖 S2Arm.ipa（不动上一版）；换壳时原来的 S2Arm.ipa 留作 S2Arm.previous.ipa；组装用的临时目录删掉"""
        import ipa as packer
        cur, prev = self.c.library / "S2Arm.ipa", self.c.library / "S2Arm.previous.ipa"
        def staged(sid, games):
            return self._ipa(self.c.library / (".container-" + sid + "-" + "-".join(games)) / "S2Arm.ipa", sid, games)
        t1 = staged("SID1", [self.A])
        packer.commit_phone_ipa(self.c, t1, "SID1")                    # 第一次：没有原来的
        self.assertEqual(install_ipa_games(cur), ([self.A], "SID1")); self.assertFalse(prev.exists()); self.assertFalse(t1.parent.exists())
        packer.commit_phone_ipa(self.c, staged("SID1", [self.A, self.B]), "SID1")   # 同壳加款：覆盖
        self.assertEqual(install_ipa_games(cur), ([self.A, self.B], "SID1")); self.assertFalse(prev.exists())
        packer.commit_phone_ipa(self.c, staged("SID2", [self.A]), "SID2")           # 换壳：原来的留作上一版
        self.assertEqual((install_ipa_games(cur), install_ipa_games(prev)), (([self.A], "SID2"), ([self.A, self.B], "SID1")))
        packer.commit_phone_ipa(self.c, staged("SID2", [self.A, self.C]), "SID2")   # 新壳上再加款：上一版不被冲掉
        self.assertEqual((install_ipa_games(cur), install_ipa_games(prev)), (([self.A, self.C], "SID2"), ([self.A, self.B], "SID1")))

    def test_restore_previous_swaps_and_records_failed_shell(self):
        """④ 装回上一版：装的是 S2Arm.previous.ipa；装上后现行与上一版对调；被换下的壳 SID2 记进 failedShells；设备记录壳号改回 SID1、只留上一版里有的款（资源包号保留）"""
        A, B = self.A, self.B
        self._ipa(self.c.library / "S2Arm.ipa", "SID2", [A, B]); self._ipa(self.c.library / "S2Arm.previous.ipa", "SID1", [A])
        layout.save_device(self.c, "dev1", dict(games={A: dict(pack="pa"), B: dict(pack="pb")}, shellId="SID2", bundleId="io.s2arm.games.team1", failedShells=["SID0"]))
        ph, _, signed = self._phone()
        with um.patch.object(install, "phone_for", return_value=ph):
            r = install.restore_previous(self.c)
        self.assertEqual(signed, [([A], ([A], "SID1"))])
        self.assertEqual(install_ipa_games(self.c.library / "S2Arm.ipa"), ([A], "SID1"))
        self.assertEqual(install_ipa_games(self.c.library / "S2Arm.previous.ipa"), ([A, B], "SID2"))
        rec = layout.load_device(self.c, "dev1")
        self.assertEqual((rec["shellId"], rec["failedShells"], rec["games"]), ("SID1", ["SID0", "SID2"], {A: dict(pack="pa")}))
        self.assertEqual((r["restoredShell"], r["games"]), ("SID1", [A]))
        # 再点一次 = 换回 SID2：SID2 从 failedShells 去掉（装回的这一版不算不好用），换下的 SID1 记进去
        with um.patch.object(install, "phone_for", return_value=ph):
            install.restore_previous(self.c)
        rec = layout.load_device(self.c, "dev1")
        self.assertEqual((rec["shellId"], rec["failedShells"]), ("SID2", ["SID0", "SID1"]))
        self.assertEqual(install_ipa_games(self.c.library / "S2Arm.ipa"), ([A, B], "SID2"))
        # 没有上一版：不找设备、不装
        (self.c.library / "S2Arm.previous.ipa").unlink()
        with um.patch.object(install, "phone_for", side_effect=AssertionError("不该找设备")):
            with self.assertRaises(Failure) as e: install.restore_previous(self.c)
        self.assertEqual(e.exception.code, "job_failed")

    def test_remove_from_phone_not_in_container(self):
        """这台手机的记录里没有这款 → 失败，不装"""
        layout.save_device(self.c, "dev1", dict(games={self.A: {}}))
        ph, _, _ = self._phone()
        self.c.r.update(gameId=self.B)
        with um.patch.object(install, "phone_for", return_value=ph):
            with self.assertRaises(Failure) as e: install.remove_from_phone(self.c)
        self.assertEqual(e.exception.code, "job_failed"); self.assertIn("没有这款", str(e.exception))
        ph.install.assert_not_called()

    def test_remove_from_phone_last_game(self):
        """最后一款（#227）：备份存档、删手机上它的数据，不装空容器；设备记录摘空"""
        layout.save_device(self.c, "dev1", dict(games={self.A: dict(pack="pa")}))
        ph, _, _ = self._phone(); ph.backup.return_value = None; ph.purge.return_value = (True, "")
        self.c.r.update(gameId=self.A)
        with um.patch.object(install, "phone_for", return_value=ph):
            r = install.remove_from_phone(self.c)
        ph.install.assert_not_called()
        ph.backup.assert_called_once_with(self.A); ph.purge.assert_called_once_with(self.A)
        self.assertEqual((r["games"], layout.load_device(self.c, "dev1")["games"]), ([], {}))

    def test_remove_from_phone_reassembles_rest_and_purge_failure_warns(self):
        """删甲：用剩下的乙现组容器装上，设备记录只剩乙，S2Arm.ipa 换成新组的；机上数据没删掉只报 container_purge_failed 黄字"""
        A, B = self.A, self.B
        self._library((A, "甲"), (B, "乙")); ios_fixture(self.c, B)
        layout.save_device(self.c, "dev1", dict(games={A: dict(pack="pa"), B: dict(pack="cafe01")}, shellId="SID1"))
        ph, _, signed = self._phone(); ph.backup.return_value = None; ph.purge.return_value = (False, "清不掉")
        self.c.r.update(gameId=A)
        import ipa as packer, host_update
        self.sid = "SID1"
        shell = self.c.private / "shell"; shell.mkdir(exist_ok=True); (shell / "manifest.json").write_text("{}")
        with um.patch.object(install, "phone_for", return_value=ph), um.patch.object(packer, "current_shell_id", return_value="SID1"), \
             um.patch.object(packer, "container_shell", return_value=shell), um.patch.object(host_update, "engine_compat", return_value=dict(self.COMPAT)), \
             um.patch.object(self.c, "run", self._run([])):
            r = install.remove_from_phone(self.c)
        self.assertEqual(signed, [([B], ([B], "SID1"))])
        self.assertEqual(layout.load_device(self.c, "dev1")["games"], {B: dict(pack="cafe01")})
        self.assertEqual(install_ipa_games(layout.phone_ipa(self.c)), ([B], "SID1"))
        self.assertFalse(r["dataRemoved"])
        diags = [d for d in self.c.job["diagnostics"] if d["code"] == "container_purge_failed"]
        self.assertEqual([d["severity"] for d in diags], ["warning"])

    def test_remove_from_phone_falls_back_to_drop_when_rest_is_stale(self):
        """剩下的款新运行层认不得（升级后没重出）：不报错，退回从 S2Arm.ipa 里摘掉这款（drop_from_ipa，不换壳）"""
        A, B = self.A, self.B
        self._library((A, "甲"), (B, "乙"))
        self._ipa(layout.phone_ipa(self.c), "SID1", [A, B])
        layout.save_device(self.c, "dev1", dict(games={A: {}, B: {}}, shellId="SID1"))
        ph, _, signed = self._phone(); ph.backup.return_value = None; ph.purge.return_value = (True, "")
        self.c.r.update(gameId=A)
        import ipa as packer
        dropped = []
        def drop(c, ipa, gid):
            dropped.append((Path(ipa), gid))
            return self._ipa(self.c.library / ".container-drop" / "S2Arm.ipa", "SID1", [B]), [B], "SID1"
        stale = Failure("认不得", code="container_stale")
        with um.patch.object(install, "phone_for", return_value=ph), um.patch.object(packer, "assemble", side_effect=stale), \
             um.patch.object(packer, "drop_from_ipa", side_effect=drop):
            r = install.remove_from_phone(self.c)
        self.assertEqual(dropped, [(layout.phone_ipa(self.c), A)])
        self.assertEqual(signed, [([B], ([B], "SID1"))])
        self.assertEqual((r["games"], install_ipa_games(layout.phone_ipa(self.c))), ([B], ([B], "SID1")))


def install_ipa_games(ipa):
    import ipa as packer
    return packer.ipa_games(ipa)


class ShipExtContainerDropTests(unittest.TestCase):
    """④ ship_ext container-drop：其余款字节不变、catalogue 少一款、最后一款拒绝"""

    def setUp(self):
        self.t = tempfile.mkdtemp()
        import shutil; self.addCleanup(lambda: shutil.rmtree(self.t, ignore_errors=True))
        self.base = Path(self.t) / "base.app"
        self.base.mkdir()

    def _make_container(self, games):
        import struct
        for g in games:
            gdir = self.base / "Games" / g; gdir.mkdir(parents=True, exist_ok=True)
            # 写一个伪可执行文件，带游戏特定字节，用于验证字节不变
            (gdir / "main.npdm").write_bytes(g.encode() + bytes(16))
            (self.base / "Frameworks").mkdir(exist_ok=True)
            (self.base / f"Frameworks/S2AImages-{g}.dylib").write_bytes(g.encode())
        (self.base / "s2a_catalogue.json").write_text(json.dumps({"schema": 1, "games": [{"title_id": g} for g in games]}))
        return self.base

    def _drop(self, game):
        import sys; sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        import ship_ext as se
        import argparse
        out = Path(self.t) / "out.app"
        a = argparse.Namespace(base=str(self.base), game=game, out=str(out))
        se.cmd_container_drop(a)
        return out

    def test_drop_removes_game_and_keeps_others(self):
        A, B = "0100000000010000", "0100000000020000"
        self._make_container([A, B])
        out = self._drop(A)
        cat = json.loads((out / "s2a_catalogue.json").read_text())
        left = [g["title_id"] for g in cat["games"]]
        self.assertEqual(left, [B])
        # B 的文件字节不变
        self.assertEqual((out / "Games" / B / "main.npdm").read_bytes(), B.encode() + bytes(16))
        # A 的目录已删
        self.assertFalse((out / "Games" / A).exists())

    def test_drop_last_game_leaves_empty_container(self):
        """#227：摘最后一款不再拒，得到 0 款的容器（游戏库侧下次加款的底子），它的目录和镜像包都没了"""
        A = "0100000000010000"
        self._make_container([A])
        out = self._drop(A)
        self.assertEqual(json.loads((out / "s2a_catalogue.json").read_text())["games"], [])
        self.assertFalse((out / "Games" / A).exists()); self.assertFalse((out / f"Frameworks/S2AImages-{A}.dylib").exists())


if __name__ == "__main__":
    unittest.main()


class DevicectlTimeoutTests(unittest.TestCase):
    """#333：devicectl 超时（锁屏 / 隧道断时一直挂）转成非零返回码 124 + 人话原因，不再抛 TimeoutExpired 让装机任务裸 traceback 收场"""
    def test_timeout_becomes_rc_124(self):
        import subprocess, diag
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d) / "Resources/bin/devicectl"; exe.parent.mkdir(parents=True); exe.write_text("#!/bin/sh\n"); exe.chmod(0o755)
            c = types.SimpleNamespace(private=Path(d), env=lambda: dict(os.environ))
            with um.patch.object(diag, "COREDEVICE", Path(d)), \
                 um.patch.object(diag.subprocess, "run", side_effect=subprocess.TimeoutExpired("devicectl", 120)):
                rc, data, err = diag.devicectl(c, "device", "info", "files", timeout=120)
        self.assertEqual((rc, data), (124, {}))
        self.assertIn("锁屏", err)


class PhoneGamesTruthTests(unittest.TestCase):
    """两端混装（同一台手机 Mac 装几款、Windows 再装一款）：手机上有哪些游戏以手机为准（期望按规则手写）"""
    A, B, C, D = "0100000000010000", "0100000000020000", "0100000000030000", "0100000000040000"
    setUp = tj.JobsTests.setUp; tearDown = tj.JobsTests.tearDown

    def client(self, games_ls, applied, ls_error=None):
        test = self
        class Cl:
            def call(self, op, rel):
                if ls_error: return 1, ls_error
                return 0, "\n".join(games_ls)
            def ls(self, rel):
                g = rel.split("/")[2]
                return [".applied", "index.tsv"] if g in applied else ["index.tsv"]
            def get(self, rel, dst): return False
        return lambda c, udid, bid, work: Cl()

    def ph(self, missing=False):
        return types.SimpleNamespace(udid="U1", bid="io.s2arm.games.abcde12345", app_missing=lambda bid, why: missing)

    def test_only_game_ids_with_applied_pack_count(self):
        with um.patch.object(install, "devpush_client", self.client([self.A + "/", "notes", self.B, "0100000000099999x", self.C], {self.A, self.C})):
            self.assertEqual(install.phone_games(self.c, self.ph()), {self.A, self.C})   # B 没解开、别的名字跳过

    def test_app_not_installed_is_empty_other_errors_stop(self):
        err = self.client([], set(), ls_error="取不到 x 的容器（App 没装或不是开发签名）：device socket io failed")
        with um.patch.object(install, "devpush_client", err):
            self.assertEqual(install.phone_games(self.c, self.ph(missing=True)), set())   # 平台确认没装
            with self.assertRaises(Failure) as e:
                install.phone_games(self.c, self.ph(missing=False))                    # 装着却进不去 = 锁屏 / 断开
        self.assertEqual(e.exception.code, "device_locked")

    def test_name_read_from_phone_with_the_bundle_being_installed(self):
        # Windows 第一次装（这台还没记录包名）：确认框里的名字照样按这次要装的包名去手机上读 control.nacp（Windows 实机曾显示成游戏号）
        seen = []
        class Cl:
            def get(self, rel, dst): seen.append(rel); return False
        ph = types.SimpleNamespace(udid="U1", bid=None, bundle_for_signing=lambda: "io.s2arm.games.abcde12345")
        with um.patch.object(install, "devpush_client", lambda c, udid, bid, work: (seen.append(bid), Cl())[1]):
            self.assertEqual(install.phone_game_name(self.c, ph, self.B, {}), self.B)
        self.assertEqual(seen[0], "io.s2arm.games.abcde12345")

    def test_platform_says_whether_app_is_missing(self):
        # Mac：问 devicectl 应用列表（devpush 的原文和断线分不开）；问不出就停。Windows：只认 ApplicationLookupFailed 原文
        mac = install.MacPhone.__new__(install.MacPhone); mac.c = self.c; mac.dev = {"id": "D1"}
        bid = "io.s2arm.games.abcde12345"
        with um.patch("diag.devicectl", return_value=(0, {"result": {"apps": []}}, "")):
            self.assertTrue(mac.app_missing(bid, "device socket io failed"))
        with um.patch("diag.devicectl", return_value=(0, {"result": {"apps": [{"bundleIdentifier": bid}]}}, "")):
            self.assertFalse(mac.app_missing(bid, "device socket io failed"))
        with um.patch("diag.devicectl", return_value=(1, {}, "The device is locked")), self.assertRaises(Failure) as e:
            mac.app_missing(bid, "device socket io failed")
        self.assertEqual(e.exception.code, "device_locked")
        win = install.WinPhone.__new__(install.WinPhone)
        self.assertTrue(win.app_missing(bid, "unknown error `ApplicationLookupFailed` returned from device"))
        self.assertFalse(win.app_missing(bid, "connection reset"))

    def test_include_what_we_can_ask_before_dropping_the_rest(self):
        # 本机记录 A；手机上 A、B、C；本机游戏库有 C（另一台电脑装的、这台也导入过）→ 带 A、C；B 只在手机上 → 先问
        atomic(self.c.library / "catalog.json", dict(schema=1, games=[dict(id=self.A, name="甲"), dict(id=self.C, name="丙")]))
        rec = dict(games={self.A: {}})
        with um.patch.object(install, "phone_games", lambda c, ph: {self.A, self.B, self.C}), \
             um.patch.object(install, "phone_game_name", lambda c, ph, g, names: "乙游戏"):
            with self.assertRaises(Failure) as e:
                install.container_others(self.c, self.ph(), rec, leave=self.D)
            self.assertEqual(e.exception.code, "container_would_drop")
            self.assertIn("1 款", str(e.exception)); self.assertIn("乙游戏", str(e.exception))
            self.c.r["acceptDropped"] = True
            self.assertEqual(install.container_others(self.c, self.ph(), rec, leave=self.D), [self.A, self.C])

