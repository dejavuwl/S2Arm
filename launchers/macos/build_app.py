#!/usr/bin/env python3
"""Build a development Mac launcher bundle; never bundles games or player data."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
from bundle_engine import bundle
from runtime_audit import verify_minimum_macos, verify_portable_runtime
import runtime_sources
from bundle_runtime import TOOLS, runtime_deps_fingerprint
import frontend_layout

ROOT = Path(__file__).resolve().parents[2]


def copy_launcher_resources(bindir, resources):
    """Copy SwiftPM resources and verify the shipped document is the shared original.
    两个前端都要：作业层文字的翻译（Localizable.json + 共享体验规格）在适配层资源包里（BackendText，.app 里只认 Resources 下这份）。"""
    source_bundle = bindir / "S2ArmAppleAdapter_S2ArmAppleAdapter.bundle"
    specifications = list(source_bundle.rglob("experience.v1.json"))
    if len(specifications) != 1 or specifications[0].is_symlink():
        raise SystemExit("SwiftPM 资源包必须包含一份普通文件形式的共享体验规格")
    if len(list(source_bundle.rglob("Localizable.json"))) != 1:
        raise SystemExit("SwiftPM 资源包必须包含一份作业层文字对照表 Localizable.json")
    relative = specifications[0].relative_to(source_bundle)
    for p in bindir.glob("*.bundle"):
        shutil.copytree(p, resources / p.name)
    for name in ("experience.v1.json", "game_settings.v1.json"):   # 游戏设置定义表（启动设置页按它画）同样只认原件
        packaged = resources / source_bundle.name / relative.parent / name
        if packaged.is_symlink() or not packaged.is_file() or packaged.read_bytes() != (ROOT / "launchers/shared" / name).read_bytes():
            raise SystemExit("Mac 应用中的共享规格 %s 缺失或与源码不一致" % name)


def release_identity(engine_json):
    """版号与包号（10-24 维护者：本地 release 只靠签名分不出新旧，内测回报要填准包号）。
    短版本取最近 tag 的语义段（v0.1.0-beta.2 → 0.1.0；CFBundleShortVersionString 只收数字段），
    CFBundleVersion 取提交数（单调递增，装新包时系统认得出是更新），
    S2ABuildID 取 <短提交号>[-dirty]-<引擎源码指纹前 8>——与 pack/build_id.sh 的「提交号-包键」同一形状，
    设置页、诊断包和 AI 控制口 /help 读的都是这一个字段。"""
    def git(*args):
        try:
            return subprocess.check_output(["git", "-C", str(ROOT), *args], text=True,
                                           stderr=subprocess.DEVNULL).strip()
        except (OSError, subprocess.CalledProcessError):
            return ""
    sys.path.insert(0, str(ROOT / "pack/jobs")); from core import release_version
    short_version = release_version(ROOT) or "0.1.0"   # 同 iPhone 容器、Windows 启动器（三端同号，pack/jobs/core.py）
    build = git("rev-list", "--count", "HEAD") or "1"
    commit = git("rev-parse", "--short=8", "HEAD") or "unknown"
    dirty = "-dirty" if git("status", "--porcelain", "--untracked-files=no") else ""
    try:
        source = json.loads(engine_json.read_text()).get("source", "")
    except (OSError, ValueError):
        source = ""
    return dict(CFBundleShortVersionString=short_version, CFBundleVersion=build,
                S2ABuildID=f"{commit}{dirty}-{source[:8]}")


def bundle_shell(engine_json, dest, env):
    """iOS 预编壳随应用分发（11-07 启动器线：玩家打 iPhone 包不再现出壳 → 不要 .NET SDK；壳里的 NativeAOT C# 库只能用 SDK 编，与游戏无关、按提交编一次）。
    壳编号取引擎清单 shellId；出包机壳缓存（同 pack/ipa.sh：S2A_SHELL_CACHE 或 <运行区>/s2arm/cache/shell/<壳编号>）有就核清单后用，没有就 pack/shell_build.sh 现出进缓存。
    布局同 Windows 发版（launchers/windows/bundle_win.py）：Resources/Shell/{manifest.json, root/}；作业层 ipa.bundled_shell 交给 S2A_SHELL"""
    sid = json.loads(Path(engine_json).read_text())["shellId"]
    cache = Path(os.environ.get("S2A_SHELL_CACHE") or Path(os.environ.get("S2A_RUN_ROOT") or Path.home() / ".s2arm/run") / "s2arm/cache/shell") / sid
    # 玩家拿到的壳必须用本机装着的最新 iOS SDK 链接（11-07 Duo 定因：26.5 SDK 链接的壳在 iOS 27 新机型 iPhone Duo 上整个 App 按旧 iPhone 尺寸缩小居中）。
    #   壳编号不含编译器（跨平台要算得出），缓存里旧 SDK 出的同号壳当没命中、重出；出完再核一遍，不符报红不出 App
    sys.path.insert(0, str(ROOT / "pack/jobs")); from install import linked_sdk; from toolchain import detect_xcode
    newest = detect_xcode()
    want = subprocess.run(["xcrun", "--sdk", "iphoneos", "--show-sdk-version"], env=dict(env, DEVELOPER_DIR=newest or env["DEVELOPER_DIR"]),
                          capture_output=True, text=True).stdout.strip()
    want = tuple(int(x) for x in (want.split(".") + ["0", "0"])[:3]) if want else None
    if not want:
        raise SystemExit(f"【红】读不到最新 Xcode（{newest}）的 iOS SDK 版本，发版壳无从核对")
    if env["DEVELOPER_DIR"] != newest:
        raise SystemExit(f"【红】发版要用装着的最新 Xcode（{newest}）出，这次选的是 {env['DEVELOPER_DIR']}：去掉 --developer-dir / S2A_DEVELOPER_DIR 重来")
    if (cache / "manifest.json").is_file() and linked_sdk(cache / "root/S2Arm") != want:
        print(f"壳缓存 {cache} 是 iOS SDK {linked_sdk(cache / 'root/S2Arm')} 出的，发版要 {want}：挪开重出")
        cache.rename(cache.parent / f".tmp.stale.{sid}.{os.getpid()}")   # 同 pack/ipa.sh：挪开不删（别的出包单可能正读它），.tmp.* 由 tools/gc.sh 清
    if not (cache / "manifest.json").is_file():
        cache.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["bash", str(ROOT / "pack/shell_build.sh"), str(cache)], env=env, check=True)
    sys.path.insert(0, str(ROOT)); from pack.shell import manifest
    doc = manifest.verify(cache / "root", cache / "manifest.json")   # 逐文件核壳清单
    if doc["details"]["shellId"] != sid:
        raise SystemExit(f"【红】壳缓存 {cache} 的壳编号 {doc['details']['shellId']} 与引擎 {sid} 不符")
    if linked_sdk(cache / "root/S2Arm") != want:
        raise SystemExit(f"【红】发版壳 {cache} 链接的 iOS SDK 是 {linked_sdk(cache / 'root/S2Arm')}，不是本机最新 Xcode 的 {want}")
    print(f"发版壳：{sid}，iOS SDK {'.'.join(map(str, want))}（{newest}）")
    shutil.copytree(cache, dest, symlinks=True)


def main():
    os.environ["MACOSX_DEPLOYMENT_TARGET"] = runtime_sources.deployment_target()   # clang、cmake、rustc 都读它：包内自己编的程序不能比声明的系统高
    parser = argparse.ArgumentParser(description=__doc__)
    # 出前端用与门禁 / 出包同一套 Xcode：tools/toolchain.sh 解析（指定 / 本机配置 > xcode-select，10-25 起不读仓库里写的版本）
    picked = subprocess.run(["bash", "-c", 'S2A_TC_QUIET=1 source tools/toolchain.sh > /dev/null && printf %s "$DEVELOPER_DIR"'], cwd=ROOT, capture_output=True, text=True)
    parser.add_argument("--developer-dir", default=picked.stdout.strip() or None)
    args = parser.parse_args()
    # 出哪个前端（S2A_APP_FRONTEND，缺省 apple = S2Arm × NX2Apple；launcher = 旧启动器，留作回退）：见 frontend_layout.py
    front = frontend_layout.pick()
    print(f"前端：{front['key']}（{front['product']} → {front['app']}）")
    if not args.developer_dir:
        raise SystemExit("🔴 没有可用的 Xcode：" + (picked.stderr.strip().splitlines() or ["tools/toolchain.sh 解析失败"])[-1])
    output = ROOT / "build/macos"
    env = dict(os.environ, DEVELOPER_DIR=args.developer_dir,
               CLANG_MODULE_CACHE_PATH=str(output / "clang-cache"),
               SWIFTPM_MODULECACHE_OVERRIDE=str(output / "swift-cache"))
    sdk_version = subprocess.check_output(["xcrun", "--sdk", "macosx", "--show-sdk-version"],
                                          env=env, text=True).strip()
    command = ["swift", "build", "--disable-sandbox", "--cache-path", str(output / "spm-cache"),
               "--package-path", str(ROOT / "launchers/macos"), "--scratch-path", str(output / "spm"), "-c", "release",
               # 不把出包机的源码绝对路径（#filePath / 断言位置）编进发布版：只映射源码目录（整个仓库根会连带改写 build/ 下的模块缓存路径）
               "-Xswiftc", "-file-prefix-map", "-Xswiftc", f"{ROOT / 'launchers/macos/Sources'}=Sources",
               # Swift 驱动经 clang --sysroot 链接时可能把 SDK 版记成部署下限，触发旧式系统 UI。
               # 明确区分运行下限与实际链接 SDK；不提高 Package.swift 的 macOS 15 下限。
               "-Xlinker", "-platform_version", "-Xlinker", "macos",
               "-Xlinker", "15.0", "-Xlinker", sdk_version]
    subprocess.run(command, env=env, check=True)
    bindir = subprocess.check_output(command + ["--show-bin-path"], env=env, text=True).strip()
    linked = subprocess.check_output(["otool", "-l", str(Path(bindir) / front["executable"])],
                                     env=env, text=True)
    sdk_versions = re.findall(r"^\s+sdk (\S+)$", linked, flags=re.MULTILINE)
    normalized = lambda value: tuple((list(map(int, value.split("."))) + [0, 0])[:3])
    if not sdk_versions or any(normalized(value) != normalized(sdk_version) for value in sdk_versions):
        raise SystemExit(f"启动器链接 SDK 与 Xcode 不一致：{sdk_versions}，期望 {sdk_version}")
    app = output / front["app"].replace(".app", "-staging.app")
    if app.exists():
        shutil.rmtree(app)
    macos = app / "Contents/MacOS"
    resources = app / "Contents/Resources"
    macos.mkdir(parents=True)
    resources.mkdir()
    shutil.copy2(Path(bindir) / front["executable"], macos / front["executable"])
    copy_launcher_resources(Path(bindir), resources)
    if front["nx2apple_resources"]:
        print("前端资源：" + " ".join(frontend_layout.stage_apple_resources(ROOT, resources)))
    shutil.copytree(ROOT / "pack/jobs", resources / "Jobs", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # 补丁库随发版（补丁包部署方案 v2，满屏补丁线 11-04）：S2A_RELEASE_PATCHES 指本机补丁库（不进仓库），只收真机验过的（tools/patch_bundle.py）；
    #   启动器出包时 pack/jobs/core.py 见 Resources/Patches/INDEX.tsv 就把它交给 pack/stage_rewrite.sh。没给就不带、响亮说一行
    lib = os.environ.get("S2A_RELEASE_PATCHES")
    if lib:
        sys.path.insert(0, str(ROOT / "tools")); import patch_bundle
        took, skipped = patch_bundle.bundle(lib, str(resources / "Patches"))
        print(f"补丁库：收 {len(took)} 款（{' '.join(took) or '无'}），跳过 {len(skipped)} 款（没验过）")
    else:
        print("🟡 没设 S2A_RELEASE_PATCHES：本版启动器不带补丁库")
    # 脱敏规则只有一份（tools/redact.py，诊断报告与任务日志共用）：任务代码在引擎准备好之前也要脱敏，随 Jobs 一起带
    shutil.copy2(ROOT / "tools/redact.py", resources / "Jobs" / "redact.py")
    shutil.copy2(ROOT / "tools/sha256_fast.py", resources / "Jobs" / "sha256_fast.py")   # 导入算原件 SHA（pack/jobs/core.py digest，Windows 走 CNG）
    shutil.copy2(ROOT / "graphics/nvn/tools/romfs_list.py", resources / "Jobs" / "romfs_list.py")   # clone_tree 统一盘点用：出包后 Jobs 的 parents[2] 是 Contents（没有 gfx），随 Jobs 带（pack/jobs/build.py romfs_list_path 先取同目录）
    shutil.copytree(ROOT / "launchers/macos/gui_jobs", resources / "GUIJobs", ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests"))
    shutil.copy2(ROOT / "launchers/macos/README.md", resources / "README.md")
    # 给玩家的 AI 用（维护者 10-23）：控制客户端 + 说明；s2armctl 用 App 自带 Python 跑，玩家不用另装 Python
    shutil.copy2(ROOT / "launchers/macos/s2armctl.py", resources / "s2armctl.py")
    shutil.copytree(ROOT / "launchers/macos/skill", resources / "skill", dirs_exist_ok=True)   # 玩家自己的 AI 用的 skill（s2arm-operator，10-26）
    (resources / "s2armctl").write_text('#!/bin/sh\nR="$(cd "$(dirname "$0")" && pwd)"\nexec "$R/Runtime/python/bin/python3" -B -I "$R/s2armctl.py" "$@"\n')
    (resources / "s2armctl").chmod(0o755)
    # 内置 Python 一律带 -B：-I 会忽略 PYTHONDONTWRITEBYTECODE，不带 -B 就往 App 包里写 __pycache__，签名封存被破坏（11-02 beta.4 走查：
    #   AI 用 s2armctl 后 codesign 报「a sealed resource is missing or invalid」，系统按新身份看待 App，文件访问授权可能失效）
    shutil.copy2(ROOT / "launchers/macos/Branding/wordmark-transparent.png", resources / "wordmark-transparent.png")   # 没封面时的字标
    # App 图标（#882 定稿 D「交错双柄」）
    subprocess.run(["bash", str(ROOT / "launchers/macos/make_icns.sh"), str(resources / "S2Arm.icns")], check=True)
    # 开源替代共享字体随应用分发（维护者 10-23：玩家不用找字体、不作检测项）：pack/fonts/make_open_fonts.py 生成（来源钉版本 + sha256，产物可重现），
    #   生成器依赖 fonttools / skia-pathops，默认使用当前构建 Python，可用 S2A_FONTS_PY 覆盖；生成器没变就复用上次产物
    fonts = output / "OpenFonts"
    gen = ROOT / "pack/fonts/make_open_fonts.py"
    stamp = fonts / ".generator.sha256"
    gen_hash = hashlib.sha256(gen.read_bytes()).hexdigest()
    if not (stamp.is_file() and stamp.read_text() == gen_hash and len(list(fonts.glob("*.bfttf"))) == 7):
        py = os.environ.get("S2A_FONTS_PY", os.sys.executable)
        subprocess.run([py, str(gen), str(fonts)], check=True)
        stamp.write_text(gen_hash)
    (resources / "Fonts").mkdir()
    for f in sorted(fonts.glob("*.bfttf")) + sorted(fonts.glob("OFL-*.txt")):
        shutil.copy2(f, resources / "Fonts" / f.name)
    shutil.copy2(ROOT / "pack/fonts/LICENSES.md", resources / "Fonts/LICENSES.md")
    runtime = output / "Runtime"
    expected = dict(line.split("==") for line in (ROOT / "launchers/macos/requirements-runtime.txt").read_text().splitlines() if "==" in line and not line.startswith("#"))
    versions = json.loads((runtime / "versions.json").read_text()) if (runtime / "versions.json").is_file() else {}
    # Runtime 跨次复用：schema、依赖指纹或 C 改写器源码变了都要重收。
    # schema 3 的目录仍带 oead 和 GNU timeout，requirements 版本对得上也不许复用。
    # 按包导入（不把 pack/ 目录放进模块路径：那样 pack/pack.py 会顶替 pack 包，后面 bundle_engine.write_manifest 的 from pack.shell import 报
    #   「'pack' is not a package」，11-07 本机打 App 实测）
    os.sys.path.insert(0, str(ROOT))
    from pack import rewriter
    if (versions.get("runtimeSchema") != 4 or versions.get("runtimeDeps") != runtime_deps_fingerprint()
            or versions.get("rewriter") != rewriter.source_digest()
            or any(versions.get(name) != version for name, version in expected.items())
            or any(not (runtime / "bin" / name).is_file() for _, name in TOOLS)):   # 工具表加了新命令（10-26 bash / spirv-cross / glslangValidator）旧运行环境也要重打
        subprocess.run([os.sys.executable, str(ROOT / "launchers/macos/bundle_runtime.py")], env=env, check=True)
    verify_portable_runtime(runtime)
    shutil.copytree(runtime, resources / "Runtime", symlinks=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    bundle(resources / "Engine", args.developer_dir)
    bundle_shell(resources / "Engine/engine.json", resources / "Shell", env)
    info = dict(**frontend_layout.info_identity(front), CFBundlePackageType="APPL",
                CFBundleIconFile="S2Arm", LSMinimumSystemVersion=runtime_sources.deployment_target(),
                NSHighResolutionCapable=True, CFBundleDevelopmentRegion="en", CFBundleLocalizations=["en", "zh-Hans"],
                **release_identity(resources / "Engine/engine.json"))
    info["UTExportedTypeDeclarations"] = [dict(UTTypeIdentifier="org.s2arm.save-backup", UTTypeDescription="S2Arm Save Backup",
        UTTypeConformsTo=["public.zip-archive"], UTTypeTagSpecification={"public.filename-extension": ["s2asave"]})]
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
    # 启动器本体同样去掉调试映射（见 bundle_engine.strip_debug_maps），再签整个 App
    subprocess.run(["xcrun", "strip", "-S", str(app / "Contents/MacOS" / front["executable"])], env=env, check=True)
    subprocess.run(["codesign", "--force", "--sign", "-", str(app)], env=env, check=True)
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], env=env, check=True)
    print("最低系统检查：", verify_minimum_macos(app, runtime_sources.deployment_target()), "个 Mach-O")
    smoke_env = dict(env, PATH="/usr/bin:/bin:/usr/sbin:/sbin", PYTHONNOUSERSITE="1")
    subprocess.run([str(resources / "Runtime/python/bin/python3"), "-B", "-I", "-c",
                    "import capstone,lz4.block,numpy,zstandard; "
                    "assert list(capstone.Cs(capstone.CS_ARCH_ARM64,capstone.CS_MODE_ARM).disasm(bytes.fromhex('c0035fd6'),0))[0].mnemonic == 'ret'; "
                    "assert lz4.block.decompress(lz4.block.compress(b'S2Arm')) == b'S2Arm'; "
                    "assert numpy.arange(4).sum() == 6; "
                    "assert zstandard.ZstdDecompressor().decompress(zstandard.ZstdCompressor().compress(b'S2Arm')) == b'S2Arm'; print('Bundled Python operations OK')"], env=smoke_env, check=True)
    destination = output / front["app"]
    previous = output / front["app"].replace(".app", "-previous.app")
    if previous.exists():
        shutil.rmtree(previous)
    if destination.exists():
        destination.rename(previous)
    try:
        app.rename(destination)
    except OSError:
        if previous.exists():
            previous.rename(destination)
        raise
    print(destination)


if __name__ == "__main__":
    main()
