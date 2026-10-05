"""로그인할 때 서버 자동 실행.

macOS는 전체 디스크 접근 권한을 '서버를 띄운 앱' 기준으로 판단함 → LaunchAgent가 python을 바로 띄우면
권한이 없어 Photos 라이브러리를 못 읽음. 그래서 작은 런처 앱(photo-tidy server.app)을 LaunchServices(open)로
띄우고, 그 앱이 서버를 자식 프로세스로 실행 — 권한은 이 앱에 한 번만 주면 됨.
"""
from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import time
from pathlib import Path

from .library import CACHE_DIR, build_app

LABEL = "local.photo-tidy.server"
APP = CACHE_DIR / "photo-tidy server.app"
PLIST = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
ARGS = ["--tailscale", "--no-open"]

LAUNCHER_SWIFT = r"""
import Foundation
let home = FileManager.default.homeDirectoryForCurrentUser.path
let logPath = home + "/.photo_tidy/server.log"
var log: FileHandle!
// 상주 앱이라 로그가 끝없이 자람 → 서버를 (다시) 띄울 때마다 5MB 넘으면 새로 시작
func openLog() {
    if let size = (try? FileManager.default.attributesOfItem(atPath: logPath))?[.size] as? Int, size > 5_000_000 {
        try? FileManager.default.removeItem(atPath: logPath)
    }
    if !FileManager.default.fileExists(atPath: logPath) { FileManager.default.createFile(atPath: logPath, contents: nil) }
    try? log?.close()
    log = FileHandle(forWritingAtPath: logPath)!
    log.seekToEndOfFile()
}
func say(_ s: String) { log.write("\(s)\n".data(using: .utf8)!) }
let args = Array(CommandLine.arguments.dropFirst())
var env = ProcessInfo.processInfo.environment
env["PYTHONUNBUFFERED"] = "1"
var server: Process?
// 이 앱이 살아 있어야 서버(자식)가 이 앱의 전체 디스크 접근 권한을 씀 → 계속 떠 있으면서
// 서버가 끝나면(오류·Tailscale 늦게 켜짐·업데이트 후 kill) 30초 뒤 다시 실행
func start() {
    openLog()
    say("\n=== \(Date()) photo-tidy server 시작 ===")
    let p = Process()
    p.executableURL = URL(fileURLWithPath: @EXE@)
    p.arguments = args.isEmpty ? ["--tailscale", "--no-open"] : args
    p.environment = env
    p.standardOutput = log
    p.standardError = log
    p.terminationHandler = { proc in
        say("서버 종료 (코드 \(proc.terminationStatus)) — 30초 뒤 다시 실행")
        DispatchQueue.main.asyncAfter(deadline: .now() + 30) { start() }
    }
    do { try p.run(); server = p } catch {
        say("서버 실행 실패: \(error) — \(p.executableURL!.path) 가 있는지 확인 (uv tool install 후 --install-autostart 다시)")
        exit(1)
    }
}
signal(SIGTERM, SIG_IGN)
let term = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
term.setEventHandler { server?.terminate(); exit(0) }  // 런처가 꺼지면 서버도 끔
term.resume()
start()
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
    # 설치된 실제 photo-tidy 경로를 박아 넣음 (uv tool 기본 ~/.local/bin 이 아닐 수도 있음)
    exe = shutil.which("photo-tidy") or str(Path.home() / ".local/bin/photo-tidy")
    swift = LAUNCHER_SWIFT.replace("@EXE@", json.dumps(os.path.abspath(exe), ensure_ascii=False))
    return build_app(app, "PhotoTidyServer", swift, LAUNCHER_PLIST)


def install(app: Path = APP, plist: Path = PLIST, build=build_launcher, run=subprocess.run) -> None:
    build(app)
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text(plist_xml(app))
    domain = f"gui/{os.getuid()}"
    run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)  # 예전 등록이 있으면 내림
    _stop(app, run)  # 떠 있는 서버는 새 코드로 다시 뜨도록 종료 (open은 이미 실행 중인 앱을 다시 안 띄움)
    run(["launchctl", "bootstrap", domain, str(plist)], check=True, capture_output=True)


def uninstall(app: Path = APP, plist: Path = PLIST, run=subprocess.run) -> None:
    run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    _stop(app, run)
    plist.unlink(missing_ok=True)


def _stop(app: Path, run) -> None:
    # 런처가 서버도 함께 끔. 새 서버가 포트를 못 잡으면 런처가 30초 뒤 다시 시도
    run(["pkill", "-TERM", "-f", str(app / "Contents/MacOS")], capture_output=True)
    # pkill은 신호만 보내고 바로 돌아옴 → 옛 런처가 아직 살아 있으면 뒤이은 open이 새로 안 띄움. 최대 5초 대기
    for _ in range(50):
        if getattr(run(["pgrep", "-f", str(app / "Contents/MacOS")], capture_output=True), "returncode", 1):
            return
        time.sleep(0.1)
