"""推资源包（tools/push_pack.py，Mac / Windows 共用，11-06 两端合一）：假设备（内存里的 App 容器）上核口径。期望按文件头规则手写，不跑被测实现推导。
Mac 的整条 dev_run 推包回归（假 devicectl）在 tests/dev_run_test.sh，本文件只核规则本身与 Windows 那支（原 tests/oneclick_win_test.py 推包几组搬来）"""
import hashlib, json, struct, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import push_pack as push

RP, INC = "Documents/资源包", "Documents/资源包/.incoming"
WHOLE = b"x" * 512 + b"\0" * 1024   # 推完整的批：512 整倍、末尾 1024 字节全零（同 pack_apply.c 契约 ③）


def sha(b): return hashlib.sha256(b).hexdigest()


class Fake:
    """内存里的 App 容器：get / put / ls / tar_ok / put_tar / listing / free / nospc 同 push.Devpush 接口"""
    kind = "fake"; why = "假设备"; docs = "Documents"; ctx = None

    def __init__(self, files, listing=False, full_disk=False):
        self.files = dict(files); self.puts = []; self.lists = []; self.list_ok = listing; self.full_disk = full_disk

    def get(self, rel, dst, t=None):
        v = self.files.get(rel)
        if v is None: return False
        Path(dst).write_bytes(v if isinstance(v, bytes) else v.encode()); return True

    def put(self, src, rel):
        self.puts.append(rel); self.files[rel] = Path(src).read_bytes()

    def ls(self, rel): return [k[len(rel) + 1:] for k in self.files if k.startswith(rel + "/") and "/" not in k[len(rel) + 1:]]

    def tar_ok(self, rel):
        v = self.files.get(rel, b""); return len(v) >= 1024 and len(v) % 512 == 0 and v[-1024:] == b"\0" * 1024

    def put_tar(self, lst, root, rel):
        if self.full_disk: raise push.Fail("写不进去")
        self.puts.append(rel); self.lists.append(Path(lst).read_text(encoding="utf-8").split()); self.files[rel] = WHOLE; return "stream"

    def listing(self): return {k[len(RP) + 1:]: len(v) for k, v in self.files.items() if k.startswith(RP + "/")} if self.list_ok else None

    def free(self): return None

    def nospc(self): return self.full_disk

    def launch(self, env):   # 起 App：按 on_launch 模拟解包（写 .applied / 解包日志），记下环境
        self.launches = getattr(self, "launches", []) + [env]
        if getattr(self, "on_launch", None): self.on_launch(self)


class Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(); d = Path(self.td.name)
        self.pk = d / "pack"; self.pk.mkdir(); self.w = d / "w"; self.w.mkdir()
        self.data = {"a": b"A", "b": b"BB"}
        for k, v in self.data.items(): (self.pk / k).write_bytes(v)
        (self.pk / "index.tsv").write_text("".join(f"{k}\t{len(v)}\t{sha(v)}\n" for k, v in self.data.items()))
        (self.pk / "manifest.json").write_text('{"version": 2, "index_sha256": "x"}')
        self.log = []

    def tearDown(self): self.td.cleanup()

    def row(self, k): return f"{k}\t{len(self.data[k])}\t{sha(self.data[k])}\n"

    def on_phone(self, *keys, applied=True):   # 机上已解开 keys（文件在、已应用索引记着）
        f = {f"{RP}/manifest.json": '{"version": 2}', f"{RP}/{'index.applied.tsv' if applied else 'index.tsv'}": "".join(self.row(k) for k in keys)}
        f.update({f"{RP}/{k}": self.data[k] for k in keys}); return f

    def go(self, files, full=False, **kw):
        self.dev = Fake(files, **kw)
        return push.run(self.dev, self.pk, self.w, full, say=self.log.append)


class PlanRules(Base):
    def test_batches_cap(self):   # ≤ 上限一批（不超过）；单个超限的文件自成一批；按 index.tsv 顺序
        rows = [["a", "30", "1"], ["b", "30", "2"], ["c", "50", "3"], ["d", "10", "4"], ["e", "90", "5"]]
        old = push.LIM; push.LIM = 60
        try: self.assertEqual([[w[0] for w in b] for b in push.batches_of(self.pk, rows)], [["a", "b"], ["c", "d"], ["e"]])
        finally: push.LIM = old

    def test_new_phone_pushes_all(self):
        self.assertEqual(self.go({})[:2], (1, 2)); self.assertEqual(self.dev.lists, [["a", "b"]])
        self.assertEqual(self.dev.files[f"{RP}/.pushed"].decode(), f"a\t{sha(b'A')}\nb\t{sha(b'BB')}\n")   # 推完这批即记账
        self.assertEqual(self.dev.puts[-2:], [f"{RP}/index.tsv", f"{RP}/manifest.json"])                   # 最后推索引与清单
        # 开推第一件事把机上清单置空（11-09 根修：「清单读得出」= 推送完整，App 据此分清「传完只等解开」和「传断了」）；
        #   在它之前只有推前探针（#333，几字节，不动清单）和给手机游戏库画蒙版的传输清单
        self.assertEqual(self.dev.puts[:3], [f"{RP}/{push.PROBE}", f"{RP}/.transfer", f"{RP}/manifest.json"])
        self.assertEqual(self.dev.files[f"{RP}/manifest.json"], (self.pk / "manifest.json").read_bytes())       # 最后写回的是本包清单

    def test_nothing_to_push_keeps_manifest(self):   # 机上已有全部文件（已应用）、没东西推：不把机上清单置空
        self.go(self.on_phone("a", "b"))
        self.assertNotIn(b"", [self.dev.files.get(f"{RP}/manifest.json")])

    def test_applied_trusted_after_spot_check(self):   # 机上 a 在且 sha 对（抽查过）→ 只推 b
        self.assertEqual(self.go(self.on_phone("a"))[:2], (1, 1)); self.assertEqual(self.dev.lists, [["b"]])
        self.assertTrue(any("机上实况抽查 1 个文件，与已应用索引一致" in l for l in self.log))

    def test_old_app_falls_back_to_index_tsv(self):   # 老 App 没有 index.applied.tsv → 认 index.tsv
        self.go(self.on_phone("a", applied=False)); self.assertEqual(self.dev.lists, [["b"]])

    def test_full_ignores_phone(self):
        self.go(self.on_phone("a", "b"), full=True); self.assertEqual(self.dev.lists, [["a", "b"]])

    def test_not_v2(self):
        (self.pk / "manifest.json").write_text('{"version": 1}')
        with self.assertRaises(push.Fail): self.go({})


class Pending(Base):
    def test_cover_incomplete_keep_complete(self):   # 推到一半的盖成空包；推完整的留着；别的名字不碰
        f = {f"{INC}/20261106061945_000.tar": b"x" * 700, f"{INC}/20261106061945_001.tar": WHOLE, f"{INC}/keep.tar": b"y", f"{INC}/note.txt": b"z"}
        self.go(f)
        self.assertEqual(self.dev.files[f"{INC}/20261106061945_000.tar"], b"\0" * 1024); self.assertEqual(self.dev.files[f"{INC}/20261106061945_001.tar"], WHOLE)
        self.assertEqual((self.dev.files[f"{INC}/keep.tar"], self.dev.files[f"{INC}/note.txt"]), (b"y", b"z"))

    def test_pending_pushed_not_repushed(self):   # 出包重叠：早推的整批还没解 → .pushed 里的文件算已推，只推缺的
        f = {f"{INC}/20261106120000_000.tar": WHOLE, f"{RP}/.pushed": f"a\t{sha(b'A')}\n"}
        self.go(f); self.assertEqual(self.dev.lists, [["b"]])
        self.assertEqual(self.dev.files[f"{RP}/.pushed"].decode(), f"a\t{sha(b'A')}\nb\t{sha(b'BB')}\n")   # 续上旧账

    def test_pushed_ignored_without_pending(self):   # 没有待解批：.pushed 是旧账（解开后机上可能按新索引删了），不算数
        self.go({f"{RP}/.pushed": f"a\t{sha(b'A')}\nb\t{sha(b'BB')}\n"}); self.assertEqual(self.dev.lists, [["a", "b"]])

    def test_stale_distrusts_pushed(self):   # 有截短残批：盖掉、不信 .pushed（推到一半那批的文件可能记过账），只按已应用索引补推
        f = {f"{INC}/1_000.tar": b"x" * 700, f"{INC}/1_001.tar": WHOLE, f"{RP}/.pushed": f"a\t{sha(b'A')}\nb\t{sha(b'BB')}\n"}
        self.go(f); self.assertEqual(self.dev.lists, [["a", "b"]])

    def test_truncated_ignores_index_tsv(self):   # 上次收推送撞截短包：机上 index.tsv 是上次推的目标、文件没落地 → 不退回它，照推
        f = self.on_phone("a", "b", applied=False); f["Documents/unpack.log"] = "10-21 解包失败：解 1_001.tar（截短包，已删）返回 -4\n"
        self.go(f); self.assertEqual(self.dev.lists, [["a", "b"]])


class SpotCheck(Base):
    def test_mismatch_with_listing_keeps_good_rows(self):   # 账记着 a、b，机上 b 丢了：抽查不符 → 按机上清单核账，只补推 b
        f = self.on_phone("a", "b"); del f[f"{RP}/b"]
        self.go(f, listing=True); self.assertEqual(self.dev.lists, [["b"]])
        self.assertTrue(any("按机上实际清单核账：1/2 行在且大小对得上留下" in l for l in self.log))

    def test_mismatch_without_listing_full(self):   # 清单拉不回（Windows AFC 列表不带大小）→ 整包重推
        f = self.on_phone("a", "b"); f[f"{RP}/a"] = b"bad"
        self.go(f); self.assertEqual(self.dev.lists, [["a", "b"]])


class Apply(Base):
    """推完起 App 解包并等完成（push_pack.apply）：期望按头注手写——没推东西且 .applied 已是本包 → 不起 App；
    起了且 .applied = 本包、解包日志本次新增「解开 N 个包 … .applied = 本包」→ 成；同包重推时 .applied 推前就对，日志没新增 → 起 App 后 STALL_S 秒没开始报红。
    11-07 按进度等：慢但包数在涨（每包 60 秒、17 包共 1020 秒，远超旧上限 330 秒）→ 不判失败、每包打「机上解包 k/17」；
    解到第 13 包后停住 → 最后一次进展后 STALL_S（180）秒判失败，报停在第 13 包"""
    WANT = "x"   # Base 的 manifest index_sha256
    def ap(self, files, nb, unpack):
        self.dev = Fake(files); clock = [0.0]
        def done(d):
            if unpack: d.files[f"{RP}/.applied"] = self.WANT; d.files["Documents/unpack.log"] = (d.files.get("Documents/unpack.log", "") + f"11-06 解开 1 个包 2 个文件 0 MB，.applied = {self.WANT}（0.1 秒）\n")
        self.dev.on_launch = done
        def sleep(x): clock[0] += x
        return push.apply(self.dev, self.pk, self.w, nb, say=self.log.append, sleep=sleep, now=lambda: clock[0])

    def test_nothing_pushed_already_applied_skips(self):
        self.assertFalse(self.ap({f"{RP}/.applied": self.WANT}, 0, True)); self.assertFalse(hasattr(self.dev, "launches"))

    def test_pushed_waits_for_new_done_line(self):
        self.assertTrue(self.ap({}, 1, True)); self.assertEqual(self.dev.launches, [{"S2A_PACK_APPLY_ONLY": "1"}])

    def cant_launch(self, player_opens_at=None):
        """电脑拉不起 App（#192）；player_opens_at 给了就在该时刻起算玩家手动打开、解完。返回 (结果或异常, 结束时刻)"""
        dev = Fake({}); self.dev = dev; clock = [0.0]
        def boom(env): raise push.Fail("起 App 失败：RSD 里没有 app_service")
        dev.launch = boom
        def sleep(x):
            clock[0] += x
            if player_opens_at is not None and clock[0] >= player_opens_at:
                dev.files[f"{RP}/.applied"] = self.WANT; dev.files["Documents/unpack.log"] = f"11-08 解开 1 个包 2 个文件 0 MB，.applied = {self.WANT}（0.2 秒）\n"
        try: return push.apply(dev, self.pk, self.w, 1, say=self.log.append, sleep=sleep, now=lambda: clock[0]), clock[0]
        except push.Fail as e: return e, clock[0]

    def test_cant_launch_player_opens_by_hand(self):   # 玩家在第 60 秒手动打开：等到解完算装好，不报错
        r, t = self.cant_launch(player_opens_at=60)
        self.assertIs(r, True); self.assertGreaterEqual(t, 60); self.assertLess(t, 70)
        self.assertTrue(any(l.startswith("资源包已传完。") and "打开 S2Arm" in l for l in self.log))

    def test_cant_launch_nobody_opens_is_needs_player(self):   # 180 秒没人打开：需要玩家操作，文案不说失败，带原因
        r, t = self.cant_launch()
        self.assertIsInstance(r, push.NeedsPlayer); self.assertGreater(t, 180); self.assertLess(t, 190)
        self.assertTrue(str(r).startswith("资源包已传完，还差最后一步：")); self.assertIn("app_service", str(r)); self.assertNotIn("失败，", str(r))

    def test_cant_launch_not_locked_says_how_to_fix(self):   # 报错不是锁屏那句：原因写开发者模式 / 信任开发者，不说锁屏
        r, _ = self.cant_launch()
        self.assertIn("开发者模式", str(r)); self.assertIn("VPN 与设备管理", str(r)); self.assertNotIn("锁着屏", str(r))

    def locked_then(self, unlock_at=None):
        """锁屏（11-09 测试设备实测：锁屏时写文件照样成功，只有拉 App 失败，报 XPC response missing CoreDevice.output）；unlock_at 给了就在该时刻解锁，之后拉得起、拉起即解完。返回 (结果或异常, 结束时刻, 拉了几次)"""
        dev = Fake({}); self.dev = dev; clock = [0.0]; tries = [0]
        unlocked = lambda: unlock_at is not None and clock[0] >= unlock_at
        def launch(env):
            tries[0] += 1
            if not unlocked(): raise push.Fail("起 io.s2arm.x 失败：unexpected response from device: XPC response missing CoreDevice.output")
            dev.files[f"{RP}/.applied"] = self.WANT; dev.files["Documents/unpack.log"] = f"11-09 解开 1 个包 2 个文件 0 MB，.applied = {self.WANT}（0.2 秒）\n"
        dev.launch = launch
        def sleep(x): clock[0] += x
        try: return push.apply(dev, self.pk, self.w, 1, say=self.log.append, sleep=sleep, now=lambda: clock[0]), clock[0], tries[0]
        except push.Fail as e: return e, clock[0], tries[0]

    def test_locked_then_unlocked_resumes_by_itself(self):   # 第 45 秒解锁：电脑自己再拉起来解完，算装好，玩家不用去点
        r, t, n = self.locked_then(unlock_at=45)
        self.assertIs(r, True); self.assertGreaterEqual(t, 45); self.assertLess(t, 45 + push.RELAUNCH_S + 5); self.assertGreater(n, 1)
        self.assertIn(f"资源包已传完。{push.LOCKED_WAIT}", self.log)

    def test_locked_all_along_says_unlock(self):   # 一直锁着：需要玩家操作，原因说锁屏
        r, t, _ = self.locked_then()
        self.assertIsInstance(r, push.NeedsPlayer); self.assertIn("锁着屏", str(r)); self.assertNotIn("开发者模式", str(r))

    def test_no_tunnel_says_developer_mode(self):   # s2a-devpush 建不了隧道（CoreDeviceProxy 连不上）：原因只说开发者模式
        dev = Fake({}); self.dev = dev; clock = [0.0]
        def boom(env): raise push.Fail("CoreDeviceProxy 连不上（要 iOS 17+、开发者模式）：x")
        dev.launch = boom
        def sleep(x): clock[0] += x
        with self.assertRaises(push.NeedsPlayer) as e: push.apply(dev, self.pk, self.w, 1, say=self.log.append, sleep=sleep, now=lambda: clock[0])
        self.assertIn("打开开发者模式", str(e.exception)); self.assertNotIn("锁着屏", str(e.exception))

    def test_same_pack_repush_without_new_line_fails(self):   # .applied 推前已是本包、日志旧行也对得上，但这次没解 → 不能判装好
        old = {f"{RP}/.applied": self.WANT, "Documents/unpack.log": f"10-01 解开 3 个包 9 个文件 1 MB，.applied = {self.WANT}（0.2 秒）\n"}
        with self.assertRaises(push.Fail) as e: self.ap(old, 1, False)
        self.assertIn("起 App 后 180 秒机上解包没有开始", str(e.exception)); self.assertEqual(len(self.dev.launches), 1)

    def slow(self, stop_at=None, per=60.0, n=17):
        """合成进度：起 App 后每 per 秒解完一包（第 k 包写「进度：解开 x_k.tar（k/n）」），stop_at 给了就解到第 stop_at 包后再无进展；
        n 包都解完写完成行与 .applied。返回 (结果或异常, 失败 / 完成时刻)"""
        self.dev = Fake({}); clock = [0.0]; t0 = [None]
        def launched(d): t0[0] = clock[0]
        self.dev.on_launch = launched
        def sleep(x):
            clock[0] += x
            if t0[0] is None: return
            k = min(n, int((clock[0] - t0[0]) // per)); k = min(k, stop_at) if stop_at else k
            log = "".join(f"11-07 进度：解开 x_{i:03d}.tar（{i}/{n}）10 个文件 1000 MB（{per} 秒）\n" for i in range(1, k + 1))
            if k == n: log += f"11-07 解开 {n} 个包 170 个文件 17000 MB，.applied = {self.WANT}（{n * per} 秒）\n"; self.dev.files[f"{RP}/.applied"] = self.WANT
            self.dev.files["Documents/unpack.log"] = log
        try:
            return push.apply(self.dev, self.pk, self.w, 1, say=self.log.append, sleep=sleep, now=lambda: clock[0]), clock[0]
        except push.Fail as e:
            return e, clock[0]

    def test_slow_but_progressing_does_not_fail(self):   # 17 包 × 60 秒 = 1020 秒，旧口径（≥330 秒上限）会判失败
        r, t = self.slow()
        self.assertIs(r, True); self.assertGreaterEqual(t, 1020)
        self.assertIn("机上解包 13/17", self.log); self.assertEqual(self.log[-1], "资源包：机上已解开（.applied = 本包）")   # 末包与完成行同一轮出现：直接判完成

    def test_stalled_fails_after_stall_window(self):   # 第 13 包在 780 秒解完后停住 → 780 + 180 秒后判失败（3 秒一轮，最多多等一轮）
        r, t = self.slow(stop_at=13)
        self.assertIsInstance(r, push.Fail); self.assertIn("180 秒没有新进展（停在第 13 包）", str(r))
        self.assertTrue(780 + 180 < t <= 780 + 180 + 3 + 3, t)


class ContainerDocs(Base):
    """多款容器（--docs / --launch-ctx，pack/ship_ext.py 容器布局）：资源包、待解批、记账、索引、解包日志都在本款文档根 Documents/games/<游戏号> 下，
    根上的 Documents/资源包 一个不碰；推完起 App 解包带 S2A_LAUNCH_CTX=本款启动上下文"""
    D, CTX = "Documents/games/0100000000010000", "@bundle/Games/0100000000010000/launch.ctx"

    def test_paths_and_launch_ctx(self):
        self.dev = Fake({f"{RP}/manifest.json": '{"version": 2}', f"{RP}/index.applied.tsv": self.row("a") + self.row("b")})   # 根上另一份（单款 App 的）不算本款的账
        self.dev.docs, self.dev.ctx = self.D, self.CTX
        nb, nt, _ = push.run(self.dev, self.pk, self.w, False, say=self.log.append)
        self.assertEqual((nb, nt), (1, 2))
        self.assertEqual(self.dev.puts[0], f"{self.D}/资源包/{push.PROBE}")   # 先写探针（#333），也在本款资源包目录
        self.assertEqual(self.dev.puts[1:3], [f"{self.D}/资源包/.transfer", f"{self.D}/资源包/manifest.json"])   # 传输清单 → 置空本款清单（同一份文档根）
        self.assertTrue(self.dev.puts[3].startswith(f"{self.D}/资源包/.incoming/") and self.dev.puts[3].endswith("_000.tar"), self.dev.puts[3])
        self.assertEqual(self.dev.puts[4:], [f"{self.D}/资源包/.pushed", f"{self.D}/资源包/index.tsv", f"{self.D}/资源包/manifest.json"])
        def done(d):
            d.files[f"{self.D}/资源包/.applied"] = "x"; d.files[f"{self.D}/unpack.log"] = "11-07 解开 1 个包 2 个文件 0 MB，.applied = x（0.1 秒）\n"
        self.dev.on_launch = done; clock = [0.0]
        def sleep(x): clock[0] += x
        self.assertTrue(push.apply(self.dev, self.pk, self.w, nb, say=self.log.append, sleep=sleep, now=lambda: clock[0]))
        self.assertEqual(self.dev.launches, [{}])   # 多款容器：正常拉起到游戏库，由库解开（不用只解包模式、不直接起某一款）

    def test_device_factory_carries_docs(self):
        d = push.device("C891", "s2arm.z.games", self.w, via="devicectl", env={}, docs=self.D, ctx=self.CTX)
        self.assertEqual((d.docs, d.ctx), (self.D, self.CTX))
        d = push.device("C891", "s2arm.z.0100000000010000", self.w, via="devicectl", env={})
        self.assertEqual((d.docs, d.ctx), ("Documents", None))   # 单款缺省不变


class SpaceNeed(Base):
    def batches(self, *sizes):
        for i, rows in enumerate(sizes):
            (self.w / f"push_{i:03d}.tsv").write_text("".join(f"f{i}_{k}\t{n}\tsha\n" for k, n in enumerate(rows)))

    def test_peak_not_double(self):   # 手算：10 GB 分两批（6、4 GB）、3 个文件 → 10 + 6 GB + 3 × 4096 + 余量 1 GiB
        self.batches([6 * 10**9], [3 * 10**9, 10**9])
        self.assertEqual(push.need_bytes(self.w, 10**10, {}), 10**10 + 6 * 10**9 + 3 * 4096 + (1 << 30))
        self.assertEqual(push.need_bytes(self.w, 10**10, {"S2A_PUSH_SPARE_GB": "0"}), 10**10 + 6 * 10**9 + 3 * 4096)

    def run_with_free(self, free):
        self.dev = Fake({})
        self.dev.free = lambda: free
        return push.run(self.dev, self.pk, self.w, full=True, say=self.log.append, env={"S2A_PUSH_SPARE_GB": "0"})

    # 手算：两个文件共 3 字节，一批 → 3 + 3 + 2 × 4096 = 8198
    def test_exact_need_passes(self):
        self.assertEqual(self.run_with_free(8198)[2], 3)

    def test_one_byte_short_reports(self):
        with self.assertRaises(push.Fail) as e: self.run_with_free(8197)
        self.assertIn("空间不足", str(e.exception)); self.assertEqual(self.dev.puts, [f"{RP}/{push.PROBE}"])   # 只写了探针（#333），资源一个没推

    def test_devpush_free_parses_or_none(self):
        d = push.Devpush("exe", "U", "b", self.w)
        d.call = lambda *a: (0, "1234\n"); self.assertEqual(d.free(), 1234)
        d.call = lambda *a: (1, "不认识的操作 free"); self.assertIsNone(d.free())


class ImagePush(Base):
    def setUp(self):
        super().setUp()
        self.data = {"romfs.img": b"IMAGE", "b": b"BB"}
        for k, v in self.data.items(): (self.pk / k).write_bytes(v)
        (self.pk / "index.tsv").write_text("".join(self.row(k) for k in self.data))

    def phone(self, applied):
        f = {f"{RP}/manifest.json": '{"version": 2}', f"{RP}/index.applied.tsv": self.row("romfs.img") + self.row("b"), f"{RP}/b": b"BB", f"{RP}/romfs.img": b"IMAGE"}
        if applied is not None: f[f"{RP}/.applied"] = applied
        return f

    def test_image_is_direct_not_tar(self):
        self.dev = Fake({}); self.dev.put_big = lambda src, rel: self.dev.puts.append(rel)
        nb, nt, n = push.plan(self.dev, self.pk, self.w, full=True, say=self.log.append)
        self.assertEqual((nb, nt), (1, 2)); self.assertEqual((self.w / "direct_img.tsv").read_text().split("\t")[0], "romfs.img")
        self.assertNotIn("romfs.img", (self.w / "push_000.tsv").read_text())

    def test_interrupted_image_is_pushed_again(self):   # .applied 被清空 = 上次传到一半：账上有也要重传
        self.dev = Fake(self.phone(""))
        nb, nt, n = push.plan(self.dev, self.pk, self.w, say=self.log.append)
        self.assertEqual((nt, (self.w / "direct_img.tsv").read_text().split("\t")[0]), (1, "romfs.img"))
        self.assertTrue(any("被打断" in l for l in self.log))

    def test_applied_image_is_not_pushed_again(self):
        self.dev = Fake(self.phone("abc"))
        self.assertEqual(push.plan(self.dev, self.pk, self.w, say=self.log.append)[1], 0)

    def test_library_pack_materializes_image_and_deletes_after(self):
        # #329：库里的资源包不存 romfs.img、只存 romfs.files/ 硬链接 + 索引 + 偏移表；要推镜像时现拼、推的字节对、推完删
        sys.path.insert(0, str(push.R / "pack")); import romfs_image
        (self.pk / "romfs.img").unlink(); (self.pk / "romfs.files").mkdir(); (self.pk / "romfs.files" / "x").write_bytes(b"IMA"); (self.pk / "romfs.files" / "y").write_bytes(b"GE")
        idx = b"S2ARFS01" + struct.pack("<qi", 5, 2) + struct.pack("<qqii", 0, 3, 0, 1) + struct.pack("<qqii", 3, 2, 1, 1) + struct.pack("<i", 2) + b"xy" + struct.pack("<i", 0)
        (self.pk / "romfs.s2aidx").write_bytes(idx); (self.pk / "romfs.imgmap").write_bytes(romfs_image.MAGIC + struct.pack("<qi", 5, 2) + struct.pack("<2q", 0, 3))
        self.dev = Fake({}); got = []
        self.dev.put_big = lambda src, rel: got.append((rel, Path(src).read_bytes()))
        push.run(self.dev, self.pk, self.w, full=True, say=self.log.append)
        self.assertEqual(got, [(f"{RP}/romfs.img", b"IMAGE")])
        self.assertFalse((self.pk / "romfs.img").exists())

    def test_library_pack_image_already_on_phone_is_not_built(self):
        (self.pk / "romfs.img").unlink(); self.dev = Fake(self.phone("abc"))   # 机上已应用同一镜像：不拼、不推
        push.run(self.dev, self.pk, self.w, say=self.log.append)
        self.assertFalse(any("现拼" in l for l in self.log)); self.assertFalse((self.pk / "romfs.img").exists())

    def test_send_invalidates_applied_before_writing_image(self):
        self.dev = Fake({}); order = []
        put = self.dev.put; self.dev.put = lambda src, rel: (order.append(rel), put(src, rel))[1]
        self.dev.put_big = lambda src, rel: order.append(rel)
        push.plan(self.dev, self.pk, self.w, full=True, say=self.log.append); push.send(self.dev, self.pk, self.w, say=self.log.append)
        self.assertLess(order.index(f"{RP}/.applied"), order.index(f"{RP}/romfs.img"))


class SendProgress(Base):
    """#266：推送进度计数。3 批不同大小（上限 60 字节：a30+b30 | c50 | e90）；分母 = 本次待传量，不是整包"""
    def setUp(self):
        super().setUp()
        self.data = {"a": b"A" * 30, "b": b"B" * 30, "c": b"C" * 50, "e": b"E" * 90}
        for k, v in self.data.items(): (self.pk / k).write_bytes(v)
        (self.pk / "index.tsv").write_text("".join(self.row(k) for k in self.data))
        self.old = push.LIM; push.LIM = 60
        self.calls = []

    def tearDown(self): push.LIM = self.old; super().tearDown()

    def run_push(self, files):
        self.dev = Fake(files)
        return push.run(self.dev, self.pk, self.w, False, say=self.log.append, progress=lambda *a: self.calls.append(a))

    def test_three_batches_cumulative(self):
        self.run_push({})
        self.assertEqual(self.calls, [(0, 200, 0, 3), (60, 200, 1, 3), (110, 200, 2, 3), (200, 200, 3, 3)])

    def test_denominator_is_this_push_not_whole_pack(self):   # 机上已有 a、b：只推 c、e（两批 50 | 90），总量 140
        self.run_push(self.on_phone("a", "b"))
        self.assertEqual(self.calls, [(0, 140, 0, 2), (50, 140, 1, 2), (140, 140, 2, 2)])

    def test_nothing_to_push_reports_zero_total(self):   # 机上全有：只报一次 0/0，作业层见 total=0 不发计数（不画 0/0）
        self.run_push(self.on_phone("a", "b", "c", "e"))
        self.assertEqual(self.calls, [(0, 0, 0, 0)])

    def test_failed_batch_never_reports_done(self):   # 第一批就写满：不许报任何已完成量
        self.dev = Fake({}, full_disk=True)
        with self.assertRaises(push.Fail):
            push.run(self.dev, self.pk, self.w, False, say=self.log.append, progress=lambda *a: self.calls.append(a))
        self.assertEqual([c for c in self.calls if c[0] > 0], [])

    def test_transfer_list_written_once_before_any_batch(self):
        # 手机游戏库画装机蒙版用的传输清单：开推前写一次（之后不再写），列本次每个包的机上路径与字节数（期望按 3 批手算：a+b 60 | c 50 | e 90）
        self.run_push({})
        tr = f"{RP}/.transfer"
        self.assertEqual(self.dev.puts.count(tr), 1)
        self.assertLess(self.dev.puts.index(tr), min(i for i, r in enumerate(self.dev.puts) if "/.incoming/" in r))
        t = json.loads(self.dev.files[tr])
        self.assertEqual((t["total"], t["base"]), (200, 0))   # 新手机：整包 200 都要传
        self.assertEqual([n for _, n in t["files"]], [60, 50, 90])
        self.assertEqual([p for p, _ in t["files"]], [r[len(RP) + 1:] for r in self.dev.puts if "/.incoming/" in r])

    def test_resume_counts_what_is_already_on_phone(self):   # 机上已有 a、b（60）：这次传 c、e（140），base = 整包 200 − 140
        self.run_push(self.on_phone("a", "b"))
        t = json.loads(self.dev.files[f"{RP}/.transfer"])
        self.assertEqual((t["total"], t["base"]), (140, 60))

    def test_nothing_to_push_writes_no_transfer_list(self):
        self.run_push(self.on_phone("a", "b", "c", "e"))
        self.assertNotIn(f"{RP}/.transfer", self.dev.puts)

    def test_cli_progress_lines(self):   # Mac 装机作业读的机器行
        line = push.PROGRESS_PREFIX + " 60 200 1 3"
        self.assertRegex(line, r"^S2A_PUSH_PROGRESS \d+ \d+ \d+ \d+$")


class LockedProbe(Base):
    """#333：推之前先写几字节探针；写不进（锁屏 / 断开）立刻报 DeviceLocked（code device_locked），不核账、不推一个文件"""
    def test_locked_phone_stops_before_any_push(self):
        dev = Fake(self.on_phone("a"))
        def put(src, rel): raise push.Fail("写不进")
        dev.put = put
        dev.put_tar = lambda *a: self.fail("锁屏时不该推资源")
        with self.assertRaises(push.DeviceLocked) as e:
            push.run(dev, self.pk, self.w, False, say=self.log.append)
        self.assertEqual(e.exception.code, "device_locked")
        self.assertIn("锁屏", str(e.exception))

    def test_probe_goes_into_the_pack_folder_and_push_continues(self):
        self.go({})
        self.assertEqual(self.dev.puts[0], f"{RP}/{push.PROBE}")
        self.assertIn(f"{RP}/index.tsv", self.dev.files)


class InflightProgress(Base):
    def test_child_progress_is_delivered_before_child_exit(self):
        # 子进程必须等回调写确认文件才退出；capture_output 等退出的旧实现必红。
        script = self.w / "child.py"; ack = self.w / "ack"
        script.write_text('''import pathlib, sys, time
print("S2A_DEVPUSH_PROGRESS 40", flush=True)
end = time.monotonic() + 2
while not pathlib.Path(sys.argv[-1]).exists() and time.monotonic() < end:
    time.sleep(0.01)
sys.exit(0 if pathlib.Path(sys.argv[-1]).exists() else 1)
''')
        dev = push.Devpush(sys.executable, "-u", str(script), self.w)
        seen = []
        def report(n): seen.append(n); ack.touch()
        dev.progress = report
        dev.put(self.pk / "a", ack)
        self.assertEqual(seen, [40])

    def test_old_helper_output_still_succeeds_without_progress(self):
        script = self.w / "old.py"
        script.write_text('print("put 1 字节")\n', encoding="utf-8")
        dev = push.Devpush(sys.executable, "-u", str(script), self.w)
        seen = []; dev.progress = seen.append
        dev.put(self.pk / "a", "remote")
        self.assertEqual(seen, [])
        self.assertIn("put 1 字节", (self.w / "devpush.log").read_text())

    def test_streaming_error_keeps_diagnostics(self):
        script = self.w / "fail.py"
        script.write_text('import sys\nprint("S2A_DEVPUSH_PROGRESS 40", flush=True)\nprint("AFC write failed", file=sys.stderr)\nsys.exit(1)\n')
        dev = push.Devpush(sys.executable, "-u", str(script), self.w)
        seen = []; dev.progress = seen.append
        with self.assertRaisesRegex(push.Fail, "AFC write failed"):
            dev.put(self.pk / "a", "remote")
        self.assertEqual(seen, [40])

    def image_plan(self):
        (self.pk / "romfs.img").write_bytes(b"x" * 100)
        (self.w / "direct_img.tsv").write_text("romfs.img\t100\timage-sha\n")

    def test_image_inflight_is_bounded_and_not_completed_or_ledgered(self):
        self.image_plan(); dev = Fake({}); calls = []
        def put_big(*args):
            for n in (40, 20, 100, 999): dev.progress(n)
            self.assertFalse((self.w / "pushed").exists())
        dev.put_big = put_big
        push.send(dev, self.pk, self.w, progress=lambda *a: calls.append(a))
        self.assertEqual(calls, [(0, 100, 0, 1), (40, 100, 0, 1), (99, 100, 0, 1), (100, 100, 1, 1)])
        self.assertIsNone(dev.progress)

    def test_partial_image_failure_never_reports_completion(self):
        self.image_plan(); dev = Fake({}); calls = []
        def put_big(*args):
            dev.progress(40); raise push.Fail("断开")
        dev.put_big = put_big
        with self.assertRaises(push.Fail):
            push.send(dev, self.pk, self.w, progress=lambda *a: calls.append(a))
        self.assertEqual(calls, [(0, 100, 0, 1), (40, 100, 0, 1)])
        self.assertNotIn(f"{RP}/.pushed", dev.files)
        self.assertIsNone(dev.progress)

    def test_batches_add_payload_to_completed_bytes(self):
        (self.w / "push_000.tsv").write_text("a\t1\ta-sha\n")
        (self.w / "push_001.tsv").write_text("b\t2\tb-sha\n")
        dev = Fake({}); calls = []
        put_tar = dev.put_tar
        def streaming(*args):
            dev.progress(1)
            return put_tar(*args)
        dev.put_tar = streaming
        push.send(dev, self.pk, self.w, progress=lambda *a: calls.append(a))
        self.assertEqual(calls, [(0, 3, 0, 2), (1, 3, 1, 2), (2, 3, 1, 2), (3, 3, 2, 2)])

    def test_mac_delegates_inflight_to_devpush(self):
        self.image_plan(); calls = []
        dev = push.Devicectl("device", "bundle", self.w)
        dev.why = "合成流式工具"
        dev.dp = Fake({}); dev.put = lambda *a: None
        dev.dp.put = lambda *a: dev.dp.progress(40)
        push.send(dev, self.pk, self.w, progress=lambda *a: calls.append(a))
        self.assertEqual(calls, [(0, 100, 0, 1), (40, 100, 0, 1), (100, 100, 1, 1)])
        self.assertIsNone(dev.dp.progress)


class SendFail(Base):
    def test_nospc(self):
        with self.assertRaises(push.Fail) as e: self.go({}, full_disk=True)
        self.assertIn("测试设备空间不足", str(e.exception)); self.assertNotIn(f"{RP}/.pushed", self.dev.files)   # 没写完的批不记账


if __name__ == "__main__":
    unittest.main()
