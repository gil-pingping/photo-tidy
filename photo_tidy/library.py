from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import imagehash
from PIL import Image

from .analyze import Photo, face_score

CACHE_DIR = Path.home() / ".photo_tidy"
ALBUM = "photo-tidy 삭제 후보"


def _derivatives(p) -> tuple[str | None, str | None]:
    # path_derivatives는 큰 것부터 정렬 → (가장 작은 것=썸네일, 가장 큰 것=크게 보기). 원본 다운로드 안 함
    paths = [x for x in p.path_derivatives if os.path.exists(x)]
    return (paths[-1], paths[0]) if paths else (None, None)


def load(library: str | None = None) -> list[Photo]:
    import osxphotos

    db = osxphotos.PhotosDB(dbfile=library) if library else osxphotos.PhotosDB()
    out = []
    for p in db.photos():
        if p.hidden or p.shared:
            continue
        addr = p.place.address if p.place else None
        thumb, preview = _derivatives(p)
        face, eyes_closed, smile = face_score([(f.quality, f.eye_state, f.has_smile) for f in p.face_info])
        out.append(Photo(
            uuid=p.uuid, date=p.date, size=p.original_filesize or 0,
            lat=p.latitude, lon=p.longitude, is_movie=p.ismovie, is_screenshot=p.screenshot,
            favorite=p.favorite, score=p.score.overall if p.score else 0.0,
            city=addr.city if addr else None, country=addr.country if addr else None,
            thumb=thumb, preview=preview, face=face, eyes_closed=eyes_closed, smile=smile,
            has_original=bool(p.path) and os.path.exists(p.path),
        ))
    return out


def _phash(path: str) -> str | None:
    try:
        with Image.open(path) as img:
            return str(imagehash.phash(img))
    except Exception:  # 손상·미지원 포맷 → 묶음 대상에서 제외
        return None


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):  # 없음·잘린 파일 → 기본값 (캐시는 다시 계산)
        return default


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj))
    os.replace(tmp, path)  # 원자적 교체 — 중간에 죽어도 기존 파일 보존


def add_hashes(photos: list[Photo], cache_file: Path = CACHE_DIR / "hashes.json") -> None:
    cache = read_json(cache_file, {})
    hashable = [p for p in photos if not p.is_movie and not p.is_screenshot]
    todo = [p for p in hashable if p.thumb and p.uuid not in cache]
    if todo:
        print(f"해시 계산 {len(todo)}장 (최초 1회, 중단해도 이어서 계산)…", flush=True)
    with ThreadPoolExecutor() as ex:
        for i, (p, h) in enumerate(zip(todo, ex.map(_phash, [p.thumb for p in todo])), 1):
            if h:
                cache[p.uuid] = h
            if i % 500 == 0:
                write_json(cache_file, cache)
                print(f"  {i}/{len(todo)}", flush=True)
    for p in hashable:
        if p.uuid in cache:
            p.hash = int(cache[p.uuid], 16)
    write_json(cache_file, cache)


# osascript 서브프로세스로 실행: Python 안에서 NSAppleScript(photoscript)를 쓰면 macOS가
# 번들 없는 python 바이너리를 자동화 주체로 보고 허용 팝업 없이 -1743 거부함.
ALBUM_SCRIPT = """on run argv
  tell application "Photos"
    set albumName to item 1 of argv
    if not (exists album albumName) then make new album named albumName
    set ps to {}
    repeat with i from 2 to count of argv
      set end of ps to media item id ((item i of argv) & "/L0/001")
    end repeat
    add ps to album albumName
  end tell
end run"""


def add_to_album(uuids: list[str], run=subprocess.run) -> None:
    r = run(["osascript", "-e", ALBUM_SCRIPT, ALBUM, *uuids], capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or f"osascript exit {r.returncode}")


LIST_SCRIPT = """on run argv
  tell application "Photos"
    if not (exists album (item 1 of argv)) then return ""
    set AppleScript's text item delimiters to linefeed
    return (id of media items of album (item 1 of argv)) as text
  end tell
end run"""


def album_uuids(run=subprocess.run) -> list[str]:
    """후보 앨범의 현재 사진 (사용자가 Photos에서 직접 뺀 것도 반영)."""
    r = run(["osascript", "-e", LIST_SCRIPT, ALBUM], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip())
    return [line.split("/")[0] for line in r.stdout.split() if line]


# 사진 헬퍼 (.app 번들, PhotoKit 공식 API). 번들 없는 실행 파일은 사진 권한이 팝업 없이 거부되므로 번들 + `open` 실행.
#  delete <ids파일>                 : 삭제 — Photos AppleScript엔 삭제 명령이 없음. macOS가 "N개 항목 삭제?" 확인 창을
#                                    직접 띄우고, 삭제된 사진은 '최근 삭제된 항목'에 30일 보관.
#  serve <대기열파일> <출력폴더> <N> : 상주하며 크게 보기용 1600px JPEG를 iCloud에서 받음 (저장 공간 최적화로 로컬엔 480px뿐).
#                                    대기열 파일을 0.3초마다 읽어 항상 N장 동시 다운로드. 5분 할 일 없거나
#                                    대기열 파일이 사라지면 진행 중인 요청을 마치고 종료.
#  unalbum <앨범이름> <ids파일>     : 앨범에서 빼기 (후보 취소) — Photos AppleScript엔 앨범에서 빼는 명령이 없음.
#  video <uuid> <출력.mp4>           : 영상 재생용 540p mp4 (iCloud 중간 화질 받아 변환 — 안드로이드 Chrome도 재생).
#                                    실패하면 <출력.mp4>.err 에 사유.
HELPER_SWIFT = r"""
import AppKit
import AVFoundation
import Photos
let args = CommandLine.arguments
let mode = args[1]
func finish(_ obj: [String: Any]) -> Never {
    print(String(data: try! JSONSerialization.data(withJSONObject: obj), encoding: .utf8)!)
    exit(0)
}
func uuidOf(_ a: PHAsset) -> String { String(a.localIdentifier.split(separator: "/")[0]) }
// 진단 로그: ~/.photo_tidy/helper.log
let logURL = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".photo_tidy/helper.log")
let started = Date()
if !FileManager.default.fileExists(atPath: logURL.path) { FileManager.default.createFile(atPath: logURL.path, contents: nil) }
let logQueue = DispatchQueue(label: "log")  // 여러 스레드에서 동시에 써도 덮어쓰지 않게 직렬화
func log(_ s: String) {
    let line = String(format: "%@ pid=%d +%.1fs %@\n", ISO8601DateFormatter().string(from: Date()),
                      ProcessInfo.processInfo.processIdentifier, Date().timeIntervalSince(started), s)
    logQueue.sync {
        guard let h = try? FileHandle(forWritingTo: logURL) else { return }
        h.seekToEndOfFile(); h.write(line.data(using: .utf8)!); try? h.close()
    }
}
// 정식 앱 루프: macOS 삭제 확인 창이 뜰 수 있도록 메인 스레드를 막지 않음
let app = NSApplication.shared
app.setActivationPolicy(.accessory)
func delete(_ idsPath: String) {
    let ids = ((try? String(contentsOfFile: idsPath, encoding: .utf8)) ?? "")
        .split(separator: "\n").map { String($0) + "/L0/001" }
    try? FileManager.default.removeItem(atPath: idsPath)
    let assets = PHAsset.fetchAssets(withLocalIdentifiers: ids, options: nil)
    var found: [String] = []
    assets.enumerateObjects { a, _, _ in found.append(uuidOf(a)) }
    if found.isEmpty { finish(["deleted": []]) }
    NSApp.activate(ignoringOtherApps: true)
    PHPhotoLibrary.shared().performChanges({ PHAssetChangeRequest.deleteAssets(assets) }) { ok, err in
        DispatchQueue.main.async {
            if ok { finish(["deleted": found]) }
            if (err as NSError?)?.code == 3072 { finish(["deleted": []]) }  // 사용자가 확인 창에서 취소
            finish(["error": err?.localizedDescription ?? "삭제 실패"])
        }
    }
}
// 상주 미리보기: 한 장 끝나면 빈자리를 바로 채움 → 느린 사진 1장이 다른 사진을 막지 않고, 앱 재실행 비용도 없음
func serve(_ qPath: String, _ outDir: String, _ conc: Int, _ size: Int) {
    let opts = PHImageRequestOptions()
    opts.deliveryMode = .highQualityFormat
    opts.isNetworkAccessAllowed = true  // iCloud에서 받아옴
    var inflight: [String: PHImageRequestID] = [:]
    var failedAt: [String: Date] = [:]  // 실패·시간초과는 1분 뒤 재시도
    var lastWork = Date()
    func out(_ u: String) -> String { outDir + "/" + u + ".jpg" }
    func start(_ u: String) {
        guard let a = PHAsset.fetchAssets(withLocalIdentifiers: [u + "/L0/001"], options: nil).firstObject else {
            failedAt[u] = Date(); log("missing \(u)"); return
        }
        let t0 = Date()
        var rid: PHImageRequestID = 0
        rid = PHImageManager.default().requestImage(for: a, targetSize: CGSize(width: size, height: size),
                                                    contentMode: .aspectFit, options: opts) { img, info in
            var jpg: Data?  // 인코딩은 콜백 스레드에서 (메인 타이머를 막지 않게)
            if let img = img, let tiff = img.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff) {
                jpg = rep.representation(using: .jpeg, properties: [.compressionFactor: 0.85])
            }
            DispatchQueue.main.async {
                guard inflight[u] == rid else { return }  // 시간초과로 이미 포기한 요청
                inflight[u] = nil
                lastWork = Date()
                if let jpg = jpg, (try? jpg.write(to: URL(fileURLWithPath: out(u)), options: .atomic)) != nil {
                    log(String(format: "done %@ %.1fs", u, Date().timeIntervalSince(t0)))
                } else {
                    failedAt[u] = Date()
                    log("fail \(u) \(String(describing: info))")
                }
            }
        }
        inflight[u] = rid
        DispatchQueue.main.asyncAfter(deadline: .now() + 120) {  // 데몬이 밀려 있으면 수십 초 걸리기도 함
            guard inflight[u] == rid else { return }
            PHImageManager.default().cancelImageRequest(rid)
            inflight[u] = nil
            failedAt[u] = Date()
            log("timeout \(u)")
        }
    }
    log("serve start conc=\(conc) size=\(size)")
    Timer.scheduledTimer(withTimeInterval: 0.3, repeats: true) { _ in
        if inflight.count < conc, let text = try? String(contentsOfFile: qPath, encoding: .utf8) {
            for line in text.split(separator: "\n") where inflight.count < conc {
                let u = String(line)
                if inflight[u] != nil || FileManager.default.fileExists(atPath: out(u)) { continue }
                if let f = failedAt[u], Date().timeIntervalSince(f) < 60 { continue }
                start(u)
                lastWork = Date()
            }
        }
        // 진행 중인 요청은 끝까지 기다렸다 종료 — 도중에 죽이면 사진 데몬에 요청이 남아 다음 요청이 밀림 (실측)
        if inflight.isEmpty && (Date().timeIntervalSince(lastWork) > 300 || !FileManager.default.fileExists(atPath: qPath)) {
            log("exit"); finish(["served": true])
        }
    }
}
func unalbum(_ name: String, _ idsPath: String) {
    let ids = ((try? String(contentsOfFile: idsPath, encoding: .utf8)) ?? "")
        .split(separator: "\n").map { String($0) + "/L0/001" }
    try? FileManager.default.removeItem(atPath: idsPath)
    let o = PHFetchOptions()
    o.predicate = NSPredicate(format: "title = %@", name)
    let assets = PHAsset.fetchAssets(withLocalIdentifiers: ids, options: nil)
    guard let album = PHAssetCollection.fetchAssetCollections(with: .album, subtype: .any, options: o).firstObject,
          assets.count > 0 else { finish(["removed": 0]) }
    PHPhotoLibrary.shared().performChanges({ PHAssetCollectionChangeRequest(for: album)?.removeAssets(assets) }) { ok, err in
        DispatchQueue.main.async {
            if ok { finish(["removed": assets.count]) }
            finish(["error": err?.localizedDescription ?? "앨범에서 빼기 실패"])
        }
    }
}
func video(_ u: String, _ outPath: String, _ quality: Int, _ preset: String) {
    func fail(_ msg: String) -> Never {
        try? msg.write(toFile: outPath + ".err", atomically: true, encoding: .utf8)
        log("video fail \(u) \(msg)"); finish(["error": msg])
    }
    guard let a = PHAsset.fetchAssets(withLocalIdentifiers: [u + "/L0/001"], options: nil).firstObject else { fail("영상을 찾을 수 없음") }
    let o = PHVideoRequestOptions()
    o.isNetworkAccessAllowed = true  // 원본은 대부분 iCloud에만 있음
    o.deliveryMode = [.automatic, .highQualityFormat, .mediumQualityFormat, .fastFormat][min(max(quality, 0), 3)]
    log("video start \(u) quality=\(quality) preset=\(preset) \(a.pixelWidth)x\(a.pixelHeight) \(Int(a.duration))s")
    PHImageManager.default().requestExportSession(forVideo: a, options: o, exportPreset: preset) { s, info in
        log("video session \(u) ready=\(s != nil)")  // 여기까지 = iCloud에서 받는 시간
        guard let s = s else { DispatchQueue.main.async { fail("iCloud에서 받기 실패 \(String(describing: info))") }; return }
        let tmp = URL(fileURLWithPath: outPath + ".part.mp4"), out = URL(fileURLWithPath: outPath)
        try? FileManager.default.removeItem(at: tmp)
        s.shouldOptimizeForNetworkUse = true  // 앞부분만 받아도 재생 시작
        Task {
            do {
                try await s.export(to: tmp, as: .mp4)
                try? FileManager.default.removeItem(at: out)
                try FileManager.default.moveItem(at: tmp, to: out)
                log("video done \(u)"); finish(["ok": true])
            } catch { fail(error.localizedDescription) }
        }
    }
}
if mode == "serve" {  // 하트비트: 권한 팝업을 기다리는 동안에도 살아 있음을 알려 서버가 중복 실행하지 않게
    let alive = URL(fileURLWithPath: args[2] + ".alive")
    Timer.scheduledTimer(withTimeInterval: 0.3, repeats: true) { _ in try? Data().write(to: alive) }
}
PHPhotoLibrary.requestAuthorization(for: .readWrite) { s in
    DispatchQueue.main.async {
        guard s == .authorized else {
            finish(["error": "사진 접근 권한 없음 → 시스템 설정 › 개인정보 보호 및 보안 › 사진 › photo-tidy 허용"])
        }
        // 선택 인자는 개수부터 확인 — 없는 args[5]를 읽으면 즉시 크래시 (서버는 크기를 안 넘김)
        func opt(_ i: Int, _ d: Int) -> Int { args.count > i ? Int(args[i]) ?? d : d }
        if mode == "serve" { serve(args[2], args[3], opt(4, 2), opt(5, 1600)) }
        else if mode == "unalbum" { unalbum(args[2], args[3]) }
        else if mode == "video" { video(args[2], args[3], opt(4, 2), args.count > 5 ? args[5] : AVAssetExportPreset960x540) }
        else { delete(args[2]) }  // 비동기 시작만 하고 돌아옴
    }
}
app.run()
"""

HELPER_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>local.photo-tidy.helper</string>
<key>CFBundleName</key><string>photo-tidy</string>
<key>CFBundleDisplayName</key><string>photo-tidy</string>
<key>CFBundleExecutable</key><string>PhotoTidyHelper</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>LSUIElement</key><true/>
<key>NSPhotoLibraryUsageDescription</key><string>photo-tidy가 크게 보기 이미지를 불러오고 삭제 후보 사진을 삭제하려면 사진 접근 권한이 필요합니다.</string>
</dict></plist>
"""


def _signing_identity() -> str:
    """Apple Development 인증서 → TCC 권한이 팀 ID에 묶여 재빌드해도 유지. 없으면 ad-hoc('-')."""
    r = subprocess.run(["security", "find-identity", "-v", "-p", "codesigning"], capture_output=True, text=True)
    m = re.search(r'"(Apple Development: [^"]+)"', r.stdout)
    return m.group(1) if m else "-"


def _helper(app_dir: Path = CACHE_DIR / "photo-tidy.app") -> Path:
    # 경로 고정 → 권한 팝업에 'photo-tidy'로 표시, 소스가 바뀔 때만 제자리 재빌드
    # (Apple Development 서명이면 재빌드해도 사진 권한 유지, ad-hoc이면 재빌드마다 다시 물음)
    # 스탬프는 번들 밖에 (번들 안의 서명 안 된 파일은 정식 서명을 깨뜨림)
    exe, stamp = app_dir / "Contents/MacOS/PhotoTidyHelper", app_dir.with_suffix(".sha1")
    ident = _signing_identity()
    sha = hashlib.sha1((HELPER_SWIFT + HELPER_PLIST + ident).encode()).hexdigest()
    if exe.exists() and stamp.exists() and stamp.read_text() == sha:
        return app_dir
    exe.parent.mkdir(parents=True, exist_ok=True)
    src = app_dir.parent / "helper.swift"
    src.write_text(HELPER_SWIFT)
    subprocess.run(["swiftc", "-O", str(src), "-o", str(exe)], check=True, capture_output=True, timeout=600)
    (app_dir / "Contents/Info.plist").write_text(HELPER_PLIST)
    subprocess.run(["codesign", "-s", ident, "--force", str(app_dir)], check=True, capture_output=True)
    stamp.write_text(sha)
    return app_dir


def _run_helper(mode: str, uuids: list[str], *args: str) -> dict:
    """헬퍼를 한 번 실행하고 끝날 때까지 기다려 JSON 결과 반환 (uuid 목록은 파일로 넘김)."""
    tag = uuid4().hex[:8]
    ids_file, out_file = CACHE_DIR / f"{mode}-{tag}.txt", CACHE_DIR / f"{mode}-{tag}.out"
    ids_file.write_text("\n".join(uuids))
    try:
        subprocess.run(["open", "-W", "-n", "--stdout", str(out_file), "--stderr", str(out_file), str(_helper()),
                        "--args", mode, *args, str(ids_file)], check=True, timeout=600)
        lines = [ln for ln in out_file.read_text().splitlines() if ln.startswith("{")]
    finally:
        out_file.unlink(missing_ok=True)
        ids_file.unlink(missing_ok=True)
    if not lines:
        raise RuntimeError("사진 헬퍼 응답 없음")
    res = json.loads(lines[-1])
    if "error" in res:
        raise RuntimeError(res["error"])
    return res


def delete_photos(uuids: list[str]) -> list[str]:
    """PhotoKit으로 삭제 (macOS 확인 창 → 최근 삭제된 항목). 실제 삭제된 uuid 반환."""
    return _run_helper("delete", uuids)["deleted"] if uuids else []


def remove_from_album(uuids: list[str]) -> int:
    """후보 취소: 삭제 후보 앨범에서 빼기 (사진은 그대로). 뺀 개수 반환."""
    return _run_helper("unalbum", uuids, ALBUM)["removed"] if uuids else 0


PREVIEW_DIR = CACHE_DIR / "previews"


class PreviewQueue:
    """고화질 미리보기 대기열. 서버는 우선순위 순서를 파일에 쓰기만 하고, 상주 헬퍼 1개(serve)가
    0.3초마다 읽어 항상 `conc`장을 동시에 받는다.

    앞선 방식의 실패 (2026-10-05 실측):
    - 요청마다 헬퍼 실행 → 사진 데몬 iCloud 대기열에 수백 건 적체(헬퍼를 죽여도 남음), 지금 사진이 몇 분 밀림
    - 4장 배치마다 헬퍼 재실행 → 실행 비용 + 배치 안 가장 느린 1장(가끔 60초+)이 전체를 묶음"""

    HEARTBEAT_S = 5  # 헬퍼가 0.3초마다 갱신. 이보다 오래되면 종료된 것으로 보고 다시 실행
    RELAUNCH_S = 15  # 방금 띄운 헬퍼는 뜨는 중일 수 있으니 이 안에는 다시 띄우지 않음

    # conc: 사진 데몬이 iCloud 사진을 사실상 한 줄로 처리 — 장당 ~3초(분당 ~20장), 요청 크기(1024/1600)·동시 개수와
    # 무관 (2026-10-05 실측; 초당 ~1장은 원본이 아직 Mac에 남은 최근 사진). 동시 2장이면 처리량은 같고,
    # 크게 보기로 연 사진이 앞 요청 뒤에서 기다리는 시간은 최소.
    def __init__(self, conc: int = 2, cap: int = 400, launch=None, qfile: Path | None = None):
        self.q: list[str] = []
        self.lock = threading.Lock()
        self.conc, self.cap = conc, cap
        self.qfile = qfile or CACHE_DIR / "preview-queue.txt"
        self.alive = Path(f"{self.qfile}.alive")
        self.launch = launch or self._launch
        self.launched_at = 0.0

    def push(self, uuids: list[str]) -> int:
        """맨 앞에 순서대로 넣음(이미 있으면 앞으로 이동, 상한을 넘으면 오래된 것부터 버림). 반환: 새로 받을 장수."""
        front = [u for u in dict.fromkeys(uuids) if not self._have(u)]
        with self.lock:
            first = set(front)
            self._write(front + [u for u in self.q if u not in first])
        return len(front)

    def forget(self, uuids: list[str]) -> None:
        """다 본 사진은 받을 필요 없음."""
        drop = set(uuids)
        with self.lock:
            self._write([u for u in self.q if u not in drop])

    @staticmethod
    def _have(u: str) -> bool:
        return (PREVIEW_DIR / f"{u}.jpg").exists()

    def _write(self, q: list[str]) -> None:
        self.q = [u for u in q if not self._have(u)][: self.cap]
        self.qfile.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.qfile.with_suffix(".tmp")
        tmp.write_text("\n".join(self.q))
        os.replace(tmp, self.qfile)  # 헬퍼는 이전 파일이나 새 파일 중 하나만 봄
        if self.q:
            self._ensure_helper()

    def _ensure_helper(self) -> None:
        now = time.time()
        try:
            if now - self.alive.stat().st_mtime < self.HEARTBEAT_S:
                return
        except FileNotFoundError:
            pass
        if now - self.launched_at < self.RELAUNCH_S:
            return
        self.launched_at = now
        self.launch()

    def _launch(self) -> None:
        PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.run(["open", "-g", "-n", str(_helper()), "--args", "serve", str(self.qfile), str(PREVIEW_DIR),
                        str(self.conc)], capture_output=True, timeout=30)


VIDEO_DIR = CACHE_DIR / "videos"


class VideoJobs:
    """영상 재생용 mp4 변환 — 요청마다 헬퍼(video)를 백그라운드로 실행, 진행 중 중복 실행은 막음."""

    STALE_S = 600  # 이보다 오래 소식 없는 작업은 죽은 것으로 보고 다시 실행
    RETRY_S = 60  # 실패 사유를 이만큼 보여준 뒤 다시 열면 재시도

    def __init__(self, launch=None):
        self.running: dict[str, float] = {}
        self.lock = threading.Lock()
        self.launch = launch or self._launch

    @staticmethod
    def path(u: str) -> Path:
        return VIDEO_DIR / f"{u}.mp4"

    def status(self, u: str) -> str:
        """ready | working | none | failed: <사유> — 실행은 안 함."""
        out, err = self.path(u), Path(f"{self.path(u)}.err")
        if out.exists():
            return "ready"
        with self.lock:
            if err.exists():
                if time.time() - err.stat().st_mtime < self.RETRY_S:
                    self.running.pop(u, None)
                    return "failed: " + err.read_text().strip()
                err.unlink(missing_ok=True)
            return "working" if time.time() - self.running.get(u, 0) < self.STALE_S else "none"

    def start(self, u: str) -> str:
        """없으면 변환 시작. 반환은 status()와 같음."""
        if (s := self.status(u)) != "none":
            return s
        with self.lock:
            self.running[u] = time.time()
        self.launch(u)
        return "working"

    def _launch(self, u: str) -> None:
        VIDEO_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.run(["open", "-g", "-n", str(_helper()), "--args", "video", u, str(self.path(u))],
                       capture_output=True, timeout=30)
