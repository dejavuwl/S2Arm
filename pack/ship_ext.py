#!/usr/bin/env python3
"""阶段 0（11-04 Windows 出包线）：预编壳 + 游戏镜像包组装未签名 iOS App，全程只用 Python，Windows / Mac 同一份。

前提：make ios 出的主程序不含任何游戏镜像（11-04 全外置：全部模块写进 Frameworks/S2AImages.dylib，runtime/build.mk），主程序与游戏无关。
壳目录布局与发版缓存（pack/shell/cache.py）相同：<壳目录>/root/ = 壳本体，<壳目录>/manifest.json = 壳清单（pack/shell/manifest.py，壳编号 = 源码提交 + 直连链号 + iOS 最低版本）。
  shell  从未签名 App 出壳（pack/shell_build.sh 调；发版时在 Mac / gate-host 上做）：
         剥按款文件（外置镜像包、<模块>.fixups/.gates/.widen.tsv、main.npdm、随包表、save-identity.json、图标 PNG；make ios 出的 App 本来就没有）；
         写 S2AShell.tables（随包表清单，组装时照拷），Info.plist 包名 / 显示名换成占位；
         主程序 ad-hoc 签名并带 pack/app.entitlements（提高内存上限）：Windows 上不签、交侧载工具重签时，权限声明要已在主程序里，否则 SideStore 等会丢（社区调研 11-04）；
         出壳门：主程序里不许有内嵌偏移表节 __TEXT,__swidx（旧的编进主程序布局）、壳里不许有游戏镜像；最后封壳清单。
  build  壳 + 产物根 → 未签名 App：先逐文件核壳（manifest verify）并要求壳编号 = 本打包器源码算出的编号（不配套报红），再 app_bundle.assemble（复制壳、包名 / 显示名 / 图标 / 元数据）→ embed.py --ext 写 S2AImages.dylib（纯 Python Mach-O）
         → 拷随行表（与 runtime/build.mk ios 目标同一份：每模块 fixups / gates / widen、main.npdm、随包表）。签名、IML 核对、打 ipa 由调用方接着做。
用法：
  ship_ext.py shell --app <未签名 App> --out <壳目录> --tables "<make print-app-tables 的输出>" [--entitlements <文件>]
  ship_ext.py build --shell <壳目录> --root <产物根> --out <新 App 路径> --bundle-id <包名> [--suffix <显示名后缀>] --icon <图标> [--set K=V …]
  ship_ext.py container-drop --base <已有容器 App> --game <游戏号> --out <新 App 路径>   从容器摘掉一款（其余各款逐字节不变，不换壳）
  ship_ext.py container --shell <壳目录> --out <新 App 路径> [--base <已有容器 App>] --game <产物根> [--game …] --bundle-id <包名> --icon <图标>
              [--display-name S2Arm] [--sign none|adhoc|dev] [--team <团队>]
         多款容器（布局见「多款容器」段）：每款随行表进 Games/<游戏号>/、镜像包写成 Frameworks/S2AImages-<游戏号>.dylib，生成 s2a_catalogue.json，
         整包统一签名；--base 给已有容器 = 加一款 / 换一款，其余各款原样带过来。地址窗口需求不在组装期算（宿主构造期对包内各款自己算、取最大，runtime/src/vawin.c），
         组装期只核各款 NPDM 地址空间类型一致（与宿主同一判据），全程纯 Python、Windows / Mac 同一份
退出码：0 成功；1 失败（报红一行）；2 用法错
"""
import argparse
import fnmatch
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
if sys.path and Path(sys.path[0]).resolve() == HERE:   # 直接按脚本跑时 pack/ 在路径首位，pack/pack.py 会顶掉仓库的 pack 包（同 app_bundle.py）
    sys.path.pop(0)
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / 'tools'))
from pack import app_bundle  # noqa: E402
from pack.container_custom import apply_custom  # noqa: E402
from pack.shell import manifest as shell_manifest  # noqa: E402
from pack.display_name import NoTitle, ascii_display_name, display_name, localized_names, nacp_path, title  # noqa: E402
from module_order import load_order  # noqa: E402  Horizon 装载序（main → subsdk0..N → sdk）：排序规则只有这一份
import struct  # noqa: E402

TABLES = "S2AShell.tables"
PLACEHOLDER_ID, PLACEHOLDER_NAME = "dev.s2arm.shell", "S2Arm"
IMAGES = "Frameworks/S2AImages.dylib"


def red(msg):
    print(f"【红】{msg}", file=sys.stderr)
    sys.exit(1)


def program_dirs(root):
    """多程序合集序号 1..：从 1 起连续存在 programs/<n>/main.npdm 且有改写产物的（同 runtime/src/programs.c s2a_programs_count 的连续段规则）"""
    out = []
    for n in range(1, 16):
        d = Path(root) / "programs" / str(n)
        if not (d / "main.npdm").is_file() or not modules(d): break
        out.append((n, d))
    return out


def modules(root):
    # 偏移表顺序与运行时装载序一致（host_main.c 候选名单 main → subsdk0..9 → sdk）：字母序会把 sdk 排到 subsdk* 前
    return load_order(p.name[:-len(".fixups.tsv")] for p in Path(root).glob("*.fixups.tsv"))


def per_game(app, tables):
    """App 里按款的文件（相对路径）：与 runtime/build.mk ios 目标拷进去的同一份，加图标、存档身份、外置镜像包"""
    app = Path(app); out = []
    for p in app.iterdir():
        n = p.name
        if p.is_file() and (n.endswith((".fixups.tsv", ".gates.tsv", ".widen.tsv")) or n in ("main.npdm", "save-identity.json")
                            or any(fnmatch.fnmatchcase(n, t) for t in tables) or (n.startswith("AppIcon") and n.endswith(".png"))):
            out.append(n)
    if (app / IMAGES).exists():
        out.append(IMAGES)
    return sorted(out)


def swidx_count(exe):
    """主程序（arm64 单切片 Mach-O）内嵌偏移表 __TEXT,__swidx 的项数；没有这个节回 None"""
    d = Path(exe).read_bytes()
    if struct.unpack_from("<I", d, 0)[0] != 0xFEEDFACF:
        red(f"{exe} 不是 64 位单切片 Mach-O")
    ncmds = struct.unpack_from("<I", d, 16)[0]; o = 32
    for _ in range(ncmds):
        cmd, sz = struct.unpack_from("<II", d, o)
        if cmd == 0x19:
            for k in range(struct.unpack_from("<I", d, o + 64)[0]):
                q = o + 72 + 80 * k
                if d[q:q + 16].rstrip(b"\0") == b"__swidx" and d[q + 16:q + 32].rstrip(b"\0") == b"__TEXT":
                    off = struct.unpack_from("<I", d, q + 48)[0]
                    if d[off:off + 8] != b"S2AEMB01":
                        red(f"{exe} 内嵌偏移表魔数不符")
                    return struct.unpack_from("<I", d, off + 8)[0]
        o += sz
    return None


def cmd_shell(a):
    app, out = Path(a.app), Path(a.out)
    if (app / "_CodeSignature").exists():
        red(f"{app} 已签名：壳要从未签名 App 出")
    if out.exists():
        red(f"{out} 已存在，不覆盖")
    tables = sorted(set(a.tables.split()))
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".shell-", dir=out.parent))
    try:
        shutil.copytree(app, tmp / "root", symlinks=True)
        r = tmp / "root"
        for n in per_game(r, tables):
            (r / n).unlink()
        info = plistlib.loads((r / "Info.plist").read_bytes())
        for k in ("CFBundleIcons", "CFBundleIcons~ipad", "S2ABuildID", "S2APsoTarget", "S2AGraphicsMode",
                  "S2ADirectLibVersion", "S2APackTime", "S2ADevFacilities"):
            info.pop(k, None)
        info.update(CFBundleIdentifier=PLACEHOLDER_ID, CFBundleDisplayName=PLACEHOLDER_NAME)
        (r / "Info.plist").write_bytes(plistlib.dumps(info, fmt=plistlib.FMT_XML, sort_keys=True))
        (r / TABLES).write_text("".join(t + "\n" for t in tables), encoding="utf-8", newline="\n")
        exe = r / info["CFBundleExecutable"]
        n = swidx_count(exe)
        if n is not None:
            red(f"出壳门：主程序里还有内嵌偏移表节（{n} 个游戏模块）：要本提交 make ios 出的壳")
        left = [str(p.relative_to(r)) for p in r.rglob("*") if p.name == "S2AImages.dylib" or p.name.endswith((".fixups.tsv", ".patched.bin"))]
        if left:
            red(f"出壳门：壳里还有游戏镜像 {left}")
        ent = Path(a.entitlements or REPO / "pack/app.entitlements")
        with tempfile.TemporaryDirectory() as t:   # 在包外签单个文件：不生成 _CodeSignature（壳是未签名模板，整包签名留给玩家的侧载工具）
            c = Path(t) / exe.name; shutil.copy2(exe, c)
            rc = subprocess.run(["codesign", "-f", "-s", "-", "--entitlements", str(ent), str(c)], capture_output=True, text=True)
            if rc.returncode:
                red(f"主程序 ad-hoc 签名失败：{rc.stderr.strip()}")
            shutil.copy2(c, exe)
        root_final = tmp / "shell" / "root"; root_final.parent.mkdir()
        os.replace(r, root_final)
        doc = shell_manifest.seal(root_final, tmp / "shell" / "manifest.json", *shell_manifest._defaults())
        os.replace(tmp / "shell", out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"🟢 壳 {doc['details']['shellId']}（{out}）：{len(doc['files'])} 个文件，随包表 {len(tables)} 种，主程序不含游戏镜像、ad-hoc 签名带 {ent.name}")


def shell_checked(shell_dir):
    """壳目录 → (壳 root, 随包表, 壳编号)：逐文件核壳、要求壳编号 = 本打包器源码算出的编号（不配套报红）"""
    shell_dir = Path(shell_dir); shell, man = shell_dir / "root", shell_dir / "manifest.json"
    try:
        shell_manifest.verify(shell, man)   # 逐文件重算：被改 / 缺失 / 多出点名
        need = shell_manifest.current_id()
        shell_manifest.require(man, need)   # 壳与本打包器同一提交 / 链号 / iOS 最低版本，不配套就停
    except Exception as e:   # noqa: BLE001 —— ShellError 等，统一报红
        red(f"壳 {shell_dir} 不能用：{e}")
    return shell, [t for t in (shell / TABLES).read_text(encoding="utf-8").split("\n") if t], need


def shell_chain(shell_dir):
    """壳清单里的直连链号（容器清单每款记它，供只换宿主判兼容：pack/jobs/host_update.py）"""
    return json.loads((Path(shell_dir) / "manifest.json").read_text(encoding="utf-8"))["details"]["chain"]


def check_root(root):
    mods = modules(root)
    if not mods:
        red(f"{root} 没有改写产物（*.fixups.tsv）")
    if not (root / "main.npdm").is_file():
        red(f"{root} 没有 main.npdm")
    return mods


def place_game(root, dest, images, tables):
    """一款的按款文件：镜像全部写进 images（embed.py），随行表 / main.npdm / 随包表拷进 dest（单款 = App 根，容器 = Games/<游戏号>/）。→ (模块, 多程序序号)
    多程序合集（runtime/include/s2a_programs.h）：序号 n ≥ 1 的模块在 programs/<n>/（与根同结构），镜像进同一个包、包内名 p<n>/<名>（装载器 g_img_ns），
      随行表与 main.npdm 拷到 dest/programs/<n>/——运行时模块目录 = dest，s2a_programs_count 数 programs/<n>/main.npdm"""
    mods = check_root(root)
    progs = program_dirs(root)
    emb = list(mods) + [f"p{n}/{m}=programs/{n}/{m}" for n, d in progs for m in modules(d)]
    Path(images).parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([sys.executable, str(REPO / "ios/embed.py"), str(root), str(images), *emb], capture_output=True, text=True)
    if r.returncode:
        red(f"写外置镜像包失败：{(r.stderr or r.stdout).strip().splitlines()[-1:]}")
    dest.mkdir(parents=True, exist_ok=True)
    for m in mods:
        for suf in (".fixups.tsv", ".gates.tsv", ".widen.tsv"):
            src = root / (m + suf)
            if src.is_file() or suf != ".widen.tsv":
                shutil.copyfile(src, dest / src.name)
    shutil.copyfile(root / "main.npdm", dest / "main.npdm")
    for n, d in progs:
        pa = dest / "programs" / str(n); pa.mkdir(parents=True, exist_ok=True)
        for m in modules(d):
            for suf in (".fixups.tsv", ".gates.tsv", ".widen.tsv"):
                src = d / (m + suf)
                if src.is_file() or suf != ".widen.tsv":
                    shutil.copyfile(src, pa / src.name)
        shutil.copyfile(d / "main.npdm", pa / "main.npdm")
        for t in tables:
            for src in sorted(d.glob(t)):
                if src.is_file():
                    shutil.copyfile(src, pa / src.name)
    for t in tables:   # 随包表可以是通配（「// 随包表：widen_enable.*」，make 那边由 shell 展开），存在才拷
        for src in sorted(root.glob(t)):
            if src.is_file():
                shutil.copyfile(src, dest / src.name)
    return mods, progs


def cmd_build(a):
    shell, tables, _ = shell_checked(a.shell)
    root = Path(a.root)
    check_root(root)
    meta = dict(kv.split("=", 1) for kv in a.set)
    try:   # 桌面显示名唯一来源（11-04 维护者，一次成功线 pack/display_name.py）：产物根 ctl/control.nacp 的游戏自带标题 +「 S2Arm」+ 可选后缀；取不到报红，不退回程序号
        name = display_name(root, a.suffix)
    except NoTitle as e:
        red(f"取不到桌面显示名：{e}")
    # Info.plist 的显示名用 ASCII（侧载工具拿它建 App ID，苹果只收字母数字空格；11-04 S0-5 中文名报「The name for this app is invalid」），
    #   游戏名「<游戏名> S2Arm」按 nacp 各语言槽放 <语言>.lproj 本地化：日 / 韩 / 简繁 / 英等系统各显示游戏自带的那种语言名
    plain = ascii_display_name(root, a.suffix)
    loc = localized_names(root, a.suffix)   # 每种系统语言显示游戏自带的那种语言名（nacp 有哪个语言出哪个 .lproj，11-05 维护者）
    app = app_bundle.assemble(shell, Path(a.out), bundle_id=a.bundle_id, display_name=plain, icon=a.icon, metadata=meta or None, localized_names=loc)
    (app / TABLES).unlink()
    mods, progs = place_game(root, app, app / IMAGES, tables)
    extra = "".join(f"；序号 {n}：{len(modules(d))} 个模块" for n, d in progs)
    print(f"🟢 未签名 App {app}：模块 {len(mods)} 个（{' '.join(mods)}）{extra}，外置镜像包 {(app / IMAGES).stat().st_size} 字节")


# ---------- 多款容器（启动器线 11-07：一个 App 装多款游戏 = 公共宿主 + 每款镜像包 + 启动上下文，一进程一款） ----------
# 共用布局约定 v1（宿主侧 runtime/src/launchctx.c、vawin.c s2a_window_container_need 与 frontend-ios S2ArmShellHost Catalogue 按同一份读）：
#   S2Arm.app/<主程序>                              壳主程序（公共宿主）
#   S2Arm.app/s2a_catalogue.json                    {"schema":1,"shell_id":…,"games":[{title_id,name,display_version,icon,state}]}（不认识的键宿主忽略）
#   S2Arm.app/Games/<游戏号>/                       每款随行表（单款放 App 根的那些，含 main.npdm）+ icon.jpg + launch.ctx（启动上下文 v1）
#   S2Arm.app/Frameworks/S2AImages-<游戏号>.dylib   每款镜像包
#   设备 Documents/games/<游戏号>/                  本款文档根：资源包/ = game_data，S2A/ryujinx/ = ryu_data（pack/jobs/install.py game_docs 同口径）
# 容器判定：App 包根有 s2a_catalogue.json 且有 Games/。游戏号 = main.npdm 的程序号（16 位大写十六进制，同 Catalogue isTitleID）
# 地址窗口：宿主构造期（UIKit 之前、玩家选游戏之前）对 Games/ 下每一款按单款同一套算法算需求、取最大占窗口（runtime/src/vawin.c s2a_window_container_need），
#   组装期不算、包里没有窗口表；各款 NPDM 地址空间类型不一致宿主报红不占窗口，组装期按同一判据（npdm_ident）先拒
CATALOGUE, GAMES, CTX = "s2a_catalogue.json", "Games", "launch.ctx"
DOCS_GAMES = "Documents/games"


def game_images(gid):
    return f"Frameworks/S2AImages-{gid}.dylib"


def is_container(app):
    return (Path(app) / CATALOGUE).is_file() and (Path(app) / GAMES).is_dir()


def npdm_ident(npdm):
    """main.npdm → (游戏号, 地址空间类型)：类型 = 头 +0x0C 位 1–3（同 runtime/src/vawin.c astype_of / svc_mem.c npdm_read），
    游戏号 = ACI0（头 +0x70 偏移处，魔数 ACI0）+0x10 的程序号，16 位大写十六进制"""
    try:
        b = Path(npdm).read_bytes()
    except OSError as e:
        red(f"读不了 {npdm}：{e}")
    if len(b) < 0x80 or b[:4] != b"META":
        red(f"{npdm} 不是 NPDM（没有 META 头）")
    aci = int.from_bytes(b[0x70:0x74], "little")
    if b[aci:aci + 4] != b"ACI0" or len(b) < aci + 0x18:
        red(f"{npdm} 没有 ACI0（读不到程序号）")
    return f"{int.from_bytes(b[aci + 0x10:aci + 0x18], 'little'):016X}", (b[0x0C] >> 1) & 7


def ctx_text(gid):
    """本款启动上下文 v1（runtime/include/s2a_launch_ctx.h）：宿主起这一款时 S2A_LAUNCH_CTX=@bundle/Games/<游戏号>/launch.ctx"""
    docs = f"@home/{DOCS_GAMES}/{gid}"
    return (f"version\t1\ngame_id\t{gid}\nimages\t@bundle/{game_images(gid)}\nmodule_dir\t@bundle/{GAMES}/{gid}\n"
            f"game_data\t{docs}/资源包\ndocs\t{docs}\n")


def display_version(root):
    """control.nacp 的 DisplayVersion（+0x3060，16 字节，\\0 结尾）；读不到给空"""
    try:
        b = Path(nacp_path(str(root))).read_bytes()[0x3060:0x3070]
    except (NoTitle, OSError):
        return ""
    return b.split(b"\0", 1)[0].decode("utf-8", "replace").strip()


def sign_app(app, mode, bundle_id, team):
    """整包统一签名（同 pack/ipa_tail.py 单款口径）：none = 不签（壳主程序与各款镜像包各自带 ad-hoc 签名，侧载工具重签）；
    adhoc = pack/oneclick/adhoc_sign.sh；dev = pack/dev_sign.py（缺描述文件退 3 时 ensure_profile.sh 补一次）。两种签法都先签全部内嵌 *.dylib（深的先签）
    再带权限签 App，之后 iml_check 核「提高内存上限」"""
    if mode == "none":
        return
    if sys.platform != "darwin":
        red(f"签名方式 {mode} 只能在 Mac 上（Windows 用 none，交给侧载工具签）")
    ent = str(HERE / "app.entitlements")
    if mode == "adhoc":
        r = subprocess.run(["bash", str(HERE / "oneclick/adhoc_sign.sh"), str(app), ent], capture_output=True, text=True)
    else:
        dev = ["/usr/bin/python3", str(HERE / "dev_sign.py"), str(app), bundle_id, team, ent]
        r = subprocess.run(dev, capture_output=True, text=True)
        if r.returncode == 3 and not subprocess.run(["bash", str(HERE / "ensure_profile.sh"), bundle_id, team, ent], capture_output=True).returncode:
            r = subprocess.run(dev, capture_output=True, text=True)
    if r.returncode:
        red(f"签名失败：{(r.stdout + r.stderr).strip().splitlines()[-1:]}")
    r = subprocess.run([sys.executable, str(HERE / "iml_check.py"), "check_app", str(app)], capture_output=True, text=True)
    if r.returncode:
        red(f"签名核实失败：{(r.stdout + r.stderr).strip().splitlines()[-1:]}")


def _place_banner(gid, root, gd, entry, banner=None):
    """横版封面落位：横图是游戏库的元数据，作业层导入时抓好放在这款文件夹（pack/jobs/library.store_meta），组装只读、不联网。
    给了 banner（--banner 游戏号=路径，游戏库那份）就拷它；否则条目已有 banner 且文件在就不动；产物根带 banner.jpg 就拷；都没有该款只有 icon。"""
    rel = f"{GAMES}/{gid}/banner.jpg"
    for src in (banner, root / "banner.jpg" if root is not None else None):
        if src is not None and Path(src).is_file():
            shutil.copyfile(src, gd / "banner.jpg")
            entry["banner"] = rel
            return
    if entry.get("banner") == rel and (gd / "banner.jpg").is_file():
        return


def cmd_container(a):
    shell, tables, sid = shell_checked(a.shell)
    roots = [Path(r) for r in a.game]
    banners = {k.upper(): Path(v) for k, _, v in (b.partition("=") for b in a.banner) if v}
    compat = dict(host_abi=a.host_abi, chain=shell_chain(a.shell))
    if not roots:
        red("至少给一款 --game <产物根>")
    if a.sign == "dev" and not a.team:
        red("开发直签要 --team")
    out = Path(a.out)
    if os.path.lexists(out):
        red(f"不覆盖已有目标：{out}")
    out.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".container-", dir=out.parent))
    try:
        new = {}   # 游戏号 → (产物根, 地址空间类型)
        for r in roots:
            check_root(r)
            gid, ast = npdm_ident(r / "main.npdm")
            if gid in new:
                red(f"两份产物根是同一款 {gid}：{new[gid][0]} 与 {r}")
            new[gid] = (r, ast)
        games, kinds = [], {}   # 目录条目（按序）；游戏号 → 地址空间类型
        base = Path(a.base) if a.base else None
        if base:
            if not is_container(base):
                red(f"{base} 不是多款容器（要有 {CATALOGUE} 与 {GAMES}/）")
            cat = json.loads((base / CATALOGUE).read_text(encoding="utf-8"))
            keep = [e for e in cat.get("games", []) if e.get("title_id") not in new]
            if keep and cat.get("shell_id") != sid:
                red(f"已有容器是壳 {cat.get('shell_id')} 组装的、本壳 {sid}：已有各款（{' '.join(e['title_id'] for e in keep)}）要按本壳重出，--game 给齐它们的产物根")
            for e in cat.get("games", []):
                gid = e["title_id"]
                if gid in new:
                    games.append(dict(title_id=gid))   # 换这一款：占住原位置，下面填新条目
                    continue
                if not (base / GAMES / gid).is_dir() or not (base / game_images(gid)).is_file():
                    red(f"已有容器里 {gid} 不完整（{GAMES}/{gid}/ 或 {game_images(gid)} 缺）")
                kinds[gid] = npdm_ident(base / GAMES / gid / "main.npdm")[1]
                games.append(dict(e, **compat))   # 同壳原样带过来：配本壳的约定号与链号
        for gid, (r, ast) in new.items():
            try:
                name = title(str(r))
            except NoTitle as e:
                red(f"{gid} 取不到游戏名：{e}")
            e = dict(title_id=gid, name=name, **compat)
            v = display_version(r)
            if v:
                e["display_version"] = v
            if (r / "icon.jpg").is_file():
                e["icon"] = f"{GAMES}/{gid}/icon.jpg"
            e["state"] = "playable"
            kinds[gid] = ast
            at = next((i for i, x in enumerate(games) if x["title_id"] == gid), None)
            if at is None:
                games.append(e)
            else:
                games[at] = e
        if len(set(kinds.values())) != 1:   # 宿主构造期同一判据报红不占窗口（vawin.c s2a_window_container_need）：组装期先拒，不出起不了局的包
            red("各款 NPDM 地址空间类型不一致，装不进同一个窗口：" + "、".join(f"{g} 类型 {t}" for g, t in sorted(kinds.items())))
        app = app_bundle.assemble(shell, work / "S2Arm.app", bundle_id=a.bundle_id, display_name=a.display_name, icon=a.icon,
                                  metadata={"CFBundleShortVersionString": a.version} if a.version else None)   # 发版号：同电脑上的 S2Arm（三端同号）
        (app / TABLES).unlink()
        for e in games:
            gid = e["title_id"]; gd = app / GAMES / gid
            if gid not in new:   # 已有款原样带过来（加一款 = 在已有容器上重组装，已有各款逐字节不变；仅横版封面缺了会补抓）
                shutil.copytree(base / GAMES / gid, gd, symlinks=True)
                shutil.copyfile(base / game_images(gid), app / game_images(gid))
                _place_banner(gid, None, gd, e, banners.get(gid))
                continue
            r = new[gid][0]
            place_game(r, gd, app / game_images(gid), tables)
            if (r / "icon.jpg").is_file():
                shutil.copyfile(r / "icon.jpg", gd / "icon.jpg")
            _place_banner(gid, r, gd, e, banners.get(gid))
            si = r / "save-identity.json"
            if si.is_file():
                c = subprocess.run([sys.executable, str(HERE / "save_identity.py"), "--check", str(si), "--output", str(gd / "save-identity.json")], capture_output=True, text=True)
                if c.returncode:
                    red(f"{gid} 存档游戏身份校验失败：{(c.stderr or c.stdout).strip()[-200:]}")
            (gd / CTX).write_text(ctx_text(gid), encoding="utf-8", newline="\n")
        apply_custom(app, games, a.custom, GAMES)
        (app / CATALOGUE).write_text(json.dumps(dict(schema=1, shell_id=sid, games=games), ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
        sign_app(app, a.sign, a.bundle_id, a.team)
        os.replace(app, out)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f"🟢 多款容器 {out}：{len(games)} 款（{' '.join(e['title_id'] for e in games)}；本次组装 {len(new)} 款），地址空间类型 {next(iter(kinds.values()))}，签名 {a.sign}")


def cmd_container_drop(a):
    """从容器摘掉一款：其余各款逐字节不变，不换壳（壳和已有各款配套，升级启动器后也能删）。旧的整包签名随之失效，装机时重签"""
    base, out = Path(a.base), Path(a.out)
    if not is_container(base):
        red(f"{base} 不是多款容器（要有 {CATALOGUE} 与 {GAMES}/）")
    if os.path.lexists(out):
        red(f"不覆盖已有目标：{out}")
    cat = json.loads((base / CATALOGUE).read_text(encoding="utf-8"))
    left = [e for e in cat.get("games", []) if e.get("title_id") != a.game]
    if len(left) == len(cat.get("games", [])):
        red(f"容器里没有 {a.game}")
    # 摘到 0 款也行（#227）：空容器只作游戏库里下次加款的底子，装机端不装它（pack/jobs/install.py remove_from_phone）
    work = Path(tempfile.mkdtemp(prefix=".container-", dir=out.parent))
    try:
        app = work / "S2Arm.app"
        shutil.copytree(base, app, symlinks=True)
        shutil.rmtree(app / GAMES / a.game)
        (app / game_images(a.game)).unlink()
        (app / CATALOGUE).write_text(json.dumps(dict(cat, games=left), ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
        os.replace(app, out)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f"🟢 已摘掉 {a.game}，容器剩 {len(left)} 款：{out}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("shell"); s.add_argument("--app", required=True); s.add_argument("--out", required=True); s.add_argument("--tables", required=True); s.add_argument("--entitlements")
    b = sub.add_parser("build"); b.add_argument("--shell", required=True); b.add_argument("--root", required=True); b.add_argument("--out", required=True)
    b.add_argument("--bundle-id", required=True); b.add_argument("--suffix", default=""); b.add_argument("--icon", required=True)
    b.add_argument("--set", action="append", default=[])
    c = sub.add_parser("container"); c.add_argument("--shell", required=True); c.add_argument("--out", required=True); c.add_argument("--base"); c.add_argument("--custom")
    c.add_argument("--game", action="append", default=[]); c.add_argument("--bundle-id", required=True); c.add_argument("--icon", required=True)
    c.add_argument("--banner", action="append", default=[], help="游戏号=横图路径（游戏库那份；组装不联网）")
    c.add_argument("--version", default="", help="写进 Info.plist CFBundleShortVersionString 的发版号（同电脑上的 S2Arm）")
    c.add_argument("--host-abi", type=int, required=True)    # 宿主读产物约定号（pack/jobs/host_update.py HOST_ABI），写进每款条目
    c.add_argument("--display-name", default=PLACEHOLDER_NAME); c.add_argument("--sign", choices=("none", "adhoc", "dev"), default="none"); c.add_argument("--team", default="")
    d = sub.add_parser("container-drop"); d.add_argument("--base", required=True); d.add_argument("--game", required=True); d.add_argument("--out", required=True)
    a = ap.parse_args()
    {"shell": cmd_shell, "build": cmd_build, "container": cmd_container, "container-drop": cmd_container_drop}[a.cmd](a)


if __name__ == "__main__":
    main()
