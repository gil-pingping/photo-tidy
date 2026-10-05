"""로그인할 때 서버 자동 실행.

macOS는 전체 디스크 접근 권한을 '서버를 띄운 앱' 기준으로 판단함 → LaunchAgent가 python을 바로 띄우면
권한이 없어 Photos 라이브러리를 못 읽음. 그래서 작은 런처 앱(photo-tidy server.app)을 LaunchServices(open)로
띄우고, 그 앱이 서버를 자식 프로세스로 실행 — 권한은 이 앱에 한 번만 주면 됨.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

from .library import CACHE_DIR, _signing_identity

LABEL = "local.photo-tidy.server"
APP = CACHE_DIR / "photo-tidy server.app"
PLIST = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
ARGS = ["--tailscale", "--no-open"]

LAUNCHER_SWIFT = r"""
import Foundation
let home = FileManager.default.homeDirectoryForCurrentUser.path
let logPath = home + "/.photo_tidy/server.log"
if !FileManager.default.fileExists(atPath: logPath) { FileManager.default.createFile(atPath: logPath, contents: nil) }
let log = FileHandle(forWritingAtPath: logPath)!
log.seekToEndOfFile()
log.write("\n=== \(Date()) photo-tidy server 시작 ===\n".data(using: .utf8)!)
let args = Array(CommandLine.arguments.dropFirst())
let p = Process()
p.executableURL = URL(fileURLWithPath: home + "/.local/bin/photo-tidy")
p.arguments = args.isEmpty ? ["--tailscale", "--no-open"] : args
var env = ProcessInfo.processInfo.environment
env["PYTHONUNBUFFERED"] = "1"
p.environment = env
p.standardOutput = log
p.standardError = log
// 이 앱이 살아 있어야 서버(자식)가 이 앱의 전체 디스크 접근 권한을 씀 → 서버가 끝날 때까지 대기
p.terminationHandler = { exit($0.terminationStatus) }
signal(SIGTERM, SIG_IGN)
let term = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
term.setEventHandler { p.terminate(); exit(0) }  // 런처가 꺼지면 서버도 끔
term.resume()
do { try p.run() } catch {
    log.write("서버 실행 실패: \(error) — ~/.local/bin/photo-tidy 가 있는지 확인 (uv tool install)\n".data(using: .utf8)!)
    exit(1)
}
dispatchMain()
"""

LAUNCHER_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>local.photo-tidy.server</string>
<key>CFBundleName</key><string>photo-tidy server</string>
<key>CFBundleDisplayName</key><string>photo-tidy server</string>
<key>CFBundleExecutable</key><string>PhotoTidyServer</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>LSUIElement</key><true/>
</dict></plist>
"""


def plist_xml(app: Path) -> str:
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": ["/usr/bin/open", "-g", "-a", str(app), "--args", *ARGS],
        "RunAtLoad": True,
    }).decode()


def build_launcher(app: Path = APP) -> Path:
    exe = app / "Contents/MacOS/PhotoTidyServer"
    exe.parent.mkdir(parents=True, exist_ok=True)
    src = app.parent / "launcher.swift"
    src.write_text(LAUNCHER_SWIFT)
    subprocess.run(["swiftc", "-O", str(src), "-o", str(exe)], check=True, capture_output=True, timeout=600)
    (app / "Contents/Info.plist").write_text(LAUNCHER_PLIST)
    subprocess.run(["codesign", "-s", _signing_identity(), "--force", str(app)], check=True, capture_output=True)
    return app


def install(app: Path = APP, plist: Path = PLIST, build=build_launcher, run=subprocess.run) -> None:
    build(app)
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text(plist_xml(app))
    domain = f"gui/{os.getuid()}"
    run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)  # 예전 등록이 있으면 내림
    run(["launchctl", "bootstrap", domain, str(plist)], check=True, capture_output=True)


def uninstall(app: Path = APP, plist: Path = PLIST, run=subprocess.run) -> None:
    run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    run(["pkill", "-TERM", "-f", str(app / "Contents/MacOS")], capture_output=True)  # 런처가 서버도 함께 끔
    plist.unlink(missing_ok=True)
