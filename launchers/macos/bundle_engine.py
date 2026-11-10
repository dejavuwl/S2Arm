"""Package S2Arm's own engine, reader and preparation source closure.

Only repository inputs enter the snapshot; ignored outputs and user data never do.
The fresh Git snapshot preserves existing HEAD-based pipeline keys, with no remote
or developer history. At runtime it is cloned to the app's private writable cache.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "pack/jobs"))
from core import Context, atomic, digest, tree_hash
from build import compile_host
import toolchain

INPUTS = ["runtime", "ios", "graphics", "translator", "services", "compat", "pack", "tools", "Makefile",
          "platform/build.mk", "launchers/macos/build.mk", "macos", "tests/visual/galrec",
          # 打 iPhone 包（make ios）要编 platform/apple/plat_apple.c（10-23 Spare-host 前端打 iPhone 包撞「no such file」）
          "platform/apple", "shell", "launchers/shared/SaveSync", "launchers/macos/frontend", "launchers/macos/adapter", "platform/plat.h", "platform/plat_asm.h",
          # Android target uses the same immutable engine snapshot as its pack stages.
          "android", "LICENSE", "README.md",
          # 随包 Python 的钉版依赖清单：Windows 环境检查（pack/jobs/winenv.py）按它核随包 Python 的库版本（11-07 发版产物端到端：快照缺它、整份清单报错误码合同无法核实）
          "launchers/macos/requirements-runtime.txt",
          # 游戏设置定义表（pack/jobs/community.py 按它读写 launch.json、转宿主开关；11-09 启动设置）
          "launchers/shared/game_settings.v1.json",
          # iPhone 宿主包的图标（pack/ipa_tail.py、pack/jobs/ipa.py）：引擎快照里没有它，容器就退回用游戏图标（11-08 维护者）
          "launchers/macos/Branding/AppIcon-1024.png",
          # 根 .gitignore（11-02 beta.3 走查）：出包会在快照副本里编出 bin/ obj/，没有忽略规则时 pack/ipa.sh「构建相关目录有未提交改动」
          #   把这些编译产物当成改动、拒绝出包——玩家导出 iPhone 版必死在最后一步
          ".gitignore"]


def include_source(relative):
    """Do not ship Android/Vulkan test trees in the app's production engine."""
    parts = Path(relative).parts
    if parts[:1] == ("android",):
        directories = parts[1:-1]
    elif parts[:2] == ("graphics", "vulkan"):
        directories = parts[2:-1]
    else:
        return True
    return not any(p in ("test", "tests", "androidTest", "testFixtures") or p.endswith("-tests")
                   for p in directories)


def source_fingerprint(root):
    """Apply the same tree hash as the other jobs to the shipped inputs."""
    root = Path(root)
    paths = []
    for folder in INPUTS:
        top = root / folder
        if top.is_dir():
            paths.extend(str(p.relative_to(root)) for p in sorted(top.rglob("*"))
                         if p.is_file() and include_source(p.relative_to(root)))
        elif include_source(Path(folder)):
            paths.append(folder)
    return tree_hash(root, paths)


MACHO_MAGIC = (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca")


def bundle_tzdata(c, destination, source_root=ROOT):
    """Freeze verified IANA sources and their checked output into the App engine."""
    env = c.env()
    if os.environ.get("S2A_TZDATA_CACHE"):
        env["S2A_TZDATA_CACHE"] = os.environ["S2A_TZDATA_CACHE"]
    c.run(["bash", source_root / "services/systemdata/make_tzdata.sh", "--bundle-to", destination],
          c.logs / "tzdata-bundle.log", env=env)


# 随 App 分发的预编静态库（11-03 维护者：编解码库等与游戏无关，不在玩家机器上编）：发版机按 services/<库>/build.sh 照常构建（整机缓存命中秒过），
#   静态库 + 头文件 + 许可原文 + build.txt（构建键、版本、源码与库的 sha256）放进 Binaries/Prepare/libs/<库>/<平台>/；玩家出包时 build.sh 见 S2A_PREBUILT_TOOLS
#   就核键与 sha256、直接回这份库（services/prebuilt_lib.sh），不构建。opus、C# 库（nxaot）还在玩家机器上现编，另作一单。
# 追加一个库：在下面的表里加一项（库名 = services/<库名>/build.sh；静态库文件名），它的 build.sh 照 vpx / dav1d 开头那三行接 services/prebuilt_lib.sh
PREBUILT_LIBS = [
    ("vpx", "libs2a_vpx.a"),
    ("dav1d", "libs2a_dav1d.a"),
]
PREBUILT_RIDS = ("ios-arm64", "osx-arm64")


def bundle_prebuilt_libs(tools, source_root=ROOT, env=None):
    import re
    env = dict(env if env is not None else os.environ)
    env.pop("S2A_PREBUILT_TOOLS", None)
    for name, lib in PREBUILT_LIBS:
        script = source_root / "services" / name / "build.sh"
        pin = re.search(r"^VER=([0-9.]+); SHA=([0-9a-f]{64})$", script.read_text(), re.M)
        if not pin:
            raise RuntimeError("%s 的版本 / sha256 钉行认不出" % script)
        for rid in PREBUILT_RIDS:
            key = subprocess.check_output(["bash", str(script), rid, "--key"], env=env, text=True).strip()
            built = Path(subprocess.check_output(["bash", str(script), rid], env=env, text=True).strip().splitlines()[-1])
            out = tools / "libs" / name / rid
            out.mkdir(parents=True)
            shutil.copy2(built, out / lib)
            shutil.copytree(built.parent.parent / "include", out / "include")
            for notice in ("COPYING", "LICENSE", "PATENTS"):
                if (built.parent.parent / notice).is_file():
                    shutil.copy2(built.parent.parent / notice, out / notice)
            (out / "build.txt").write_text("key=%s\nversion=%s\nsource_sha256=%s\nlib_sha256=%s\n" % (key, pin.group(1), pin.group(2), digest(out / lib)))


def strip_debug_maps(root, developer_dir):
    """发布进 App 的二进制去掉调试映射（N_OSO / N_SO 记着出包机 .o 与源码目录的绝对路径）：strip -S 只删调试信息，符号表保留、崩溃栈照样有函数名。
    strip 会让签名失效，所以随后由里到外重签（保留原 entitlements / 标识）。玩家机器上现编的宿主不走这里，照常可调试。"""
    env = dict(os.environ, DEVELOPER_DIR=developer_dir)
    def is_macho(p):
        with p.open("rb") as f:
            return f.read(4) in MACHO_MAGIC
    machos = [p for p in sorted(root.rglob("*")) if p.is_file() and not p.is_symlink() and is_macho(p)]
    keep = ["--preserve-metadata=identifier,entitlements,flags,runtime"]
    for p in machos:
        subprocess.run(["xcrun", "strip", "-S", str(p)], env=env, check=True, capture_output=True)
        subprocess.run(["codesign", "--force", "--sign", "-", *keep, str(p)], env=env, check=True, capture_output=True)
    bundles = sorted((p for p in root.rglob("*") if p.is_dir() and p.suffix in (".app", ".framework")), key=lambda p: -len(p.parts))
    for b in bundles:
        subprocess.run(["codesign", "--force", "--sign", "-", *keep, str(b)], env=env, check=True, capture_output=True)
    return len(machos)


def bundle(destination, developer_dir):
    c = Context(dict(schema=1, taskId="bundle-engine", operation="prepareEngine", engineDir=str(ROOT),
                     privateRoot=str(ROOT / "build/macos/engine-build"), developerDir=developer_dir))
    fingerprint = source_fingerprint(ROOT)
    # 发布进 App 的 .NET 程序集不带出包机路径：MSBuild 把环境变量当属性读，PathMap 让编译器把仓库根（含 build/ 下的 obj）写成 /_/（C 侧同理见各 -ffile-prefix-map）
    os.environ["PathMap"] = "%s=/_/" % ROOT
    toolchain.prepare(c)
    host = compile_host(c, fingerprint, c.env(), c.logs)
    prepare = c.private / "tools/prepare"
    prepare.mkdir(parents=True, exist_ok=True)
    c.run(["make", "-C", ROOT / "graphics/metal", str(ROOT / "build/gal/gal_xc"),
           "DEVELOPER_DIR=" + developer_dir], c.logs / "gal-tools.log")
    c.run(["make", "-C", ROOT / "graphics/dsx", str(ROOT / "build/dsx/libdsx.dylib"),
           "DEVELOPER_DIR=" + developer_dir], c.logs / "dsx.log")
    c.run(["dotnet", "build", ROOT / "graphics/tools/shbake/S2A.ShBake.csproj", "-c", "Release", "--nologo"],
          c.logs / "shbake.log")
    c.run(["dotnet", "build", ROOT / "services/romfs_index/romfs_index.csproj", "-c", "Release", "--nologo",
           "-o", prepare / "romfs"], c.logs / "romfs.log")
    # 直连库的 C# 批量进程（translator/dsh_batch.py）随发版编好：玩家首次出包不再现编（11-06 维护者「玩家体验至上」，Spare-host 约 25 秒 CPU）
    c.run(["dotnet", "build", ROOT / "graphics/tools/direct/dsh/batch/dsh_batch.csproj", "-c", "Release", "--nologo",
           "-p:UseSharedCompilation=false", "-o", prepare / "dsh_batch"], c.logs / "dsh_batch.log")
    # 推资源包流式写手机（pack/devpush，Rust，只依赖 idevice MIT；11-06 维护者：玩家 Mac / Windows 两端都要）：tools/push_pack.py 见 S2A_PREBUILT_TOOLS/devpush/s2a-devpush 就用，
    #   不在就退回 devicectl copy。--locked：按仓库里的 Cargo.lock 取依赖，发版间不漂
    cargo = shutil.which("cargo") or str(Path.home() / ".cargo/bin/cargo")
    c.run([cargo, "build", "--release", "--locked", "--manifest-path", ROOT / "pack/devpush/Cargo.toml", "--target-dir", ROOT / "build/devpush"],
          c.logs / "devpush.log")
    c.run(["clang", "-ffile-prefix-map=%s=." % ROOT, "-fobjc-arc", "-O1", ROOT / "graphics/tools/shbake/gpu_arch.m", "-o", prepare / "gpu_arch",
           "-framework", "Metal", "-framework", "Foundation"], c.logs / "gpu-arch.log")
    destination.mkdir(parents=True)
    names = subprocess.check_output(["git", "-C", str(ROOT), "ls-files", "-co", "--exclude-standard", "-z", "--", *INPUTS]).split(b"\0")
    for name in sorted(set(names) - {b""}):
        relative = Path(os.fsdecode(name))
        if not include_source(relative):
            continue
        source = ROOT / relative
        if source.is_symlink():
            raise RuntimeError("Engine source symlinks must be resolved explicitly: " + str(relative))
        if not source.is_file():
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    # Include license notices from every source subtree in the repository closure.
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
               GIT_AUTHOR_NAME="S2Arm", GIT_AUTHOR_EMAIL="bundle@localhost",
               GIT_COMMITTER_NAME="S2Arm", GIT_COMMITTER_EMAIL="bundle@localhost")
    for args in (["init", "-q", "--template="], ["config", "gc.auto", "0"],
                 ["config", "maintenance.auto", "false"], ["add", "--force", "--all"],
                 ["-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "-qm", "Bundled S2Arm engine inputs"]):
        subprocess.run(["git", "-C", str(destination), *args], env=env, check=True)
    # Finish compaction synchronously before hashing/signing. Background Git
    # maintenance would otherwise invalidate the signed resource seal.
    subprocess.run(["git", "-C", str(destination), "-c", "gc.autoDetach=false", "gc", "--prune=now"], env=env, check=True)
    shutil.copytree(host, destination / "Binaries/S2ArmGame.app")
    shutil.copytree(toolchain.reader(c).parent, destination / "Binaries/Reader",
                    ignore=shutil.ignore_patterns("obj", "*.pdb", "ready.json"))
    tools = destination / "Binaries/Prepare"
    shutil.copytree(ROOT / "build/shbake", tools / "shbake", ignore=shutil.ignore_patterns("*.pdb", "obj"))
    bundle_tzdata(c, tools / "Tzdata")
    shutil.copy2(prepare / "gpu_arch", tools / "shbake/gpu_arch")
    shutil.copytree(prepare / "romfs", tools / "romfs", ignore=shutil.ignore_patterns("*.pdb"))
    bundle_prebuilt_libs(tools, env=c.env())
    shutil.copytree(prepare / "dsh_batch", tools / "dsh_batch", ignore=shutil.ignore_patterns("*.pdb"))
    # 摘要按 App 里这份源码算（dsh_batch.py 同一函数）：玩家那边摘要相同才用内置件，不同照旧现编
    digest_out = subprocess.run([sys.executable, "-I", str(destination / "translator/dsh_batch.py"), "--src-digest"],
                                env=c.env(), check=True, capture_output=True, text=True).stdout.strip()
    (tools / "dsh_batch/.src_digest").write_text(digest_out + "\n", encoding="utf-8")
    # 同一份 App 内源码的翻译器指纹（dmsh_chain.py --xfp 按 DshXfpInput 现算）：玩家机只带 .NET 运行时，展不开清单，direct_lib.sh 出库后核库印章取这份当「本树」值
    #   （dmsh_chain.tree_xfp ← dsh_batch.prebuilt_xfp，摘要相同才认）。与内置件自报的编译期 Xfp 不同就是发版件没按这份源码编，当场停
    xfp_out = subprocess.run([sys.executable, "-I", str(destination / "translator/dmsh_chain.py"), "--xfp"],
                             env=c.env(), check=True, capture_output=True, text=True).stdout.strip()
    xfp_bin = subprocess.run(["dotnet", str(tools / "dsh_batch/dsh_batch.dll"), "--xfp"], env=c.env(), check=True, capture_output=True, text=True).stdout.strip()
    if xfp_out != xfp_bin:
        raise RuntimeError(f"Bundled dsh_batch reports translator fingerprint {xfp_bin}, the bundled sources give {xfp_out}")
    (tools / "dsh_batch/.xfp").write_text(xfp_out + "\n", encoding="utf-8")
    (tools / "devpush").mkdir()
    shutil.copy2(ROOT / "build/devpush/release/s2a-devpush", tools / "devpush/s2a-devpush")
    (tools / "gal").mkdir()
    shutil.copy2(ROOT / "build/gal/gal_xc", tools / "gal/gal_xc")
    (tools / "dsx").mkdir()   # 直连翻译库（pack/stage_direct.sh 认 dsx/libdsx.dylib，11-08 从 libnxgal 拆出）
    shutil.copy2(ROOT / "build/dsx/libdsx.dylib", tools / "dsx/libdsx.dylib")
    strip_debug_maps(destination / "Binaries", developer_dir)
    if source_fingerprint(ROOT) != fingerprint:
        raise RuntimeError("Engine sources changed during packaging; retry with stable inputs")
    return write_manifest(destination, fingerprint)


def write_manifest(destination, fingerprint):
    """按引擎目录里文件的最终字节写 prepare.json 与 engine.json（逐文件大小 + sha256，启动时 embedded.artifact 校验）。
    打包时调一次；发版签名（sign_release.sh）重签了里面的二进制、字节变了，签完整包之前再调一次（不重算会报「准备产物缺失或大小不符」）"""
    destination = Path(destination)
    tools = destination / "Binaries/Prepare"
    tool_files = {str(p.relative_to(tools)): [p.stat().st_size, digest(p)] for p in tools.rglob("*") if p.is_file() and p.name != "prepare.json"}
    atomic(tools / "prepare.json", dict(schema=1, source=fingerprint, files=tool_files))
    # Binaries are hashed too. engine.json itself is the signed bundle's manifest.
    files = {str(p.relative_to(destination)): [p.stat().st_size, digest(p)]
             for p in sorted(destination.rglob("*")) if p.is_file() and p != destination / "engine.json"}
    identity = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    # 壳编号（pack/shell/manifest.py current_id）：在开发树按壳输入表算好写进来，快照里的出包直接读（快照按目录挑文件、剔测试目录，重算会对不上发布的壳）
    sys.path.insert(0, str(ROOT)); from pack.shell import manifest as shell_manifest
    # 发版号（三端同号）：在开发树按最近 tag 算好写进来，快照里组装 iPhone 容器直接读（core.release_version）
    from core import release_version
    atomic(destination / "engine.json", dict(schema=1, id=identity, source=fingerprint, version=release_version(ROOT),
                                           memoryRequirementsVersion=2, shellId=shell_manifest.current_id(), files=files))
    return identity
