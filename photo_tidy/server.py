from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import webbrowser
from dataclasses import asdict, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address, ip_network
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import analyze, library

STATIC = Path(__file__).parent / "static"
mimetypes.add_type("image/heic", ".heic")


def brief(p: analyze.Photo) -> dict:
    return {"uuid": p.uuid, "date": p.date.isoformat(), "size": p.size, "movie": p.is_movie,
            "favorite": p.favorite, "score": round(p.score, 3), "city": p.city,
            "face": None if p.face is None else round(p.face, 3), "eyes_closed": p.eyes_closed, "smile": p.smile}


# ⚙️ 패널 값 검증: (최소, 최대, 타입)
RANGES = {"hash_max": (0, 64, int), "gap_min": (0.1, 1440, float), "near_m": (1, 100_000, float),
          "min_group": (2, 1000, int), "trip_km": (1, 20_000, float), "min_trip": (1, 100_000, int),
          "home_days": (1, 10_000, int)}


def parse_params(body, base: analyze.Params) -> analyze.Params:
    if not isinstance(body, dict):
        raise ValueError("설정은 객체여야 함")
    out = {}
    for k, v in body.items():
        if k not in RANGES:
            raise ValueError(f"알 수 없는 설정: {k}")
        lo, hi, typ = RANGES[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise ValueError(f"{k}: {lo}~{hi} 숫자")
        out[k] = typ(v)
    return replace(base, **out)


# 📷 전체 탭 필터: (조건, 정렬 키). failure < -0.1 = 실 라이브러리 표본을 눈으로 본 결과 대부분 실패작
# (하얗거나 새까만 화면, 실수로 찍힘, 흔들림). Apple 점수 overall=0은 '미분석'이 섞여 있어 기준으로 못 씀.
FAIL_MAX = -0.1
FILTERS = {
    "all": (lambda p: True, lambda p: -p.date.timestamp()),
    "old": (lambda p: True, lambda p: p.date.timestamp()),
    "screenshot": (lambda p: p.is_screenshot, lambda p: -p.date.timestamp()),
    "fail": (lambda p: not p.is_movie and p.failure < FAIL_MAX, lambda p: p.failure),
    "burst": (lambda p: p.burst_extra, lambda p: -p.date.timestamp()),
    "movie": (lambda p: p.is_movie, lambda p: -p.size),
}


def locked(fn):
    """상태를 바꾸는 작업은 한 번에 하나씩 (멀티스레드 서버). 읽기는 재할당만 하므로 잠금 불필요."""
    def wrapper(self, *a, **kw):
        with self.lock:
            return fn(self, *a, **kw)
    return wrapper


class App:
    def __init__(self, photos, add_to_album, state_file: Path, *, settings_file: Path | None = None,
                 list_album=lambda: [], delete=None, prefetch=lambda uuids: None, forget=lambda uuids: None,
                 preview_dir: Path | None = None, videos=None, reload=None, remove_from_album=None,
                 stats_file: Path | None = None):
        self.lock = threading.RLock()
        self.remove_from_album = remove_from_album
        self.stats_file = stats_file  # 지금까지 이 앱으로 지운 장수·용량 (공간 확보 안내용)
        self.stats = library.read_json(stats_file, {}) if stats_file else {}
        self.last_marks: dict[str, list[str]] = {}  # 묶음 id → 후보로 보낸 사진 (이번 실행 동안 되돌리기용)
        self.photos = photos
        self.videos, self.reload_fn = videos, reload
        self.prefetch, self.forget, self.preview_dir = prefetch, forget, preview_dir
        self.add_to_album = add_to_album
        self.list_album, self.delete = list_album, delete
        self.state_file, self.settings_file = state_file, settings_file
        # 검토 끝난 사진 uuid. 묶음 id가 아닌 사진 단위로 저장 → 후보 삭제 후 묶음이 줄어도 다시 안 뜸
        self.reviewed = set(library.read_json(state_file, []))
        try:
            self.params = parse_params(library.read_json(settings_file, {}) if settings_file else {}, analyze.DEFAULT)
        except ValueError:  # 손상·옛 형식 → 기본값
            self.params = analyze.DEFAULT
        self.recompute()

    def recompute(self):
        self.by_uuid = {p.uuid: p for p in self.photos}
        self.groups = {g[0].uuid: g for g in analyze.similar_groups(self.photos, self.params)}
        self.trips = analyze.trips(self.photos, self.params)

    @locked
    def set_params(self, body) -> dict:
        new = parse_params(body, self.params)
        if self.settings_file:
            library.write_json(self.settings_file, asdict(new))
        self.params = new
        self.recompute()
        return self.get("settings", {})

    def preview_path(self, uuid: str) -> tuple[str | None, bool]:
        """(파일, 고화질 여부). iCloud에서 받아둔 고화질 → 로컬 미리보기 → 썸네일 순."""
        p = self.by_uuid.get(uuid)
        if not p:
            return None, False
        hi = self.preview_dir / f"{uuid}.jpg" if self.preview_dir else None
        if hi and hi.exists():
            return str(hi), True
        return p.preview or p.thumb, False

    def _evict(self, uuids: list[str]) -> None:
        """다 본 사진의 고화질 캐시 삭제 + 대기열에서 제거 → 캐시는 앞으로 볼 묶음 위주로 작게 유지."""
        self.forget(uuids)
        for u in uuids:
            files = [self.preview_dir / f"{u}.jpg"] if self.preview_dir else []
            if self.videos:
                files.append(self.videos.path(u))
            for f in files:
                try:
                    f.unlink(missing_ok=True)
                except OSError:  # 캐시 정리 실패가 확정/삭제를 실패시키지 않게
                    pass

    def prune_previews(self) -> int:
        """시작 시: 남은 묶음에 없는 사진의 고화질 캐시 삭제. 반환: 지운 개수."""
        if not self.preview_dir or not self.preview_dir.exists():
            return 0
        keep = {p.uuid for g in self.pending().values() for p in g}
        stale = [f.stem for f in self.preview_dir.glob("*.jpg") if f.stem not in keep]
        self._evict(stale)
        return len(stale)

    @locked
    def unmark(self, group_id: str) -> dict:
        """확정 되돌리기: 그 묶음에서 후보로 보낸 사진을 앨범에서 빼고, 묶음을 다시 정리 대상으로."""
        if group_id not in self.last_marks or group_id not in self.groups:
            raise ValueError("되돌릴 수 있는 확정이 없음")
        reject = self.last_marks[group_id]
        removed = self.remove_candidates(reject)["removed"] if reject else 0
        reviewed = self.reviewed - {p.uuid for p in self.groups[group_id]}
        library.write_json(self.state_file, sorted(reviewed))
        self.reviewed = reviewed
        del self.last_marks[group_id]
        return {"ok": True, "removed": removed}

    def remove_candidates(self, uuids: list[str]) -> dict:  # 앱 상태를 안 바꿈 → 잠금 없이 (확정을 막지 않게)
        """후보 취소: 삭제 후보 앨범에서 빼기 (사진은 그대로)."""
        if self.remove_from_album is None:
            raise RuntimeError("후보 취소 기능 없음")
        try:
            return {"ok": True, "removed": self.remove_from_album(uuids)}
        except Exception as e:
            raise RuntimeError(f"앨범에서 빼기 실패: {e}") from e

    def add_candidates(self, uuids: list[str]) -> dict:  # 앱 상태를 안 바꿈 → 잠금 없이 (대량 추가 중에도 확정 가능)
        """묶음 밖(용량·위치·여행 탭)에서 사진·영상을 삭제 후보 앨범에 바로 넣기."""
        if not uuids or not all(u in self.by_uuid for u in uuids):
            raise ValueError("모르는 사진")
        try:
            for i in range(0, len(uuids), 500):  # osascript 인자·시간 한계 → 나눠서
                self.add_to_album(uuids[i:i + 500])
        except Exception as e:
            raise RuntimeError(f"앨범 추가 실패: {e}") from e
        return {"ok": True, "added": len(uuids)}

    def video(self, uuid: str, start: bool = False) -> dict:
        """영상 상태 + 장면 미리보기 수. iCloud 원본 다운로드는 몇 분이고 사진 데몬(직렬)을 막으므로
        재생 버튼(start)을 눌렀을 때만 — 원본이 이미 Mac에 있으면 몇 초라 바로 준비."""
        p = self.by_uuid.get(uuid)
        if p is None:
            raise ValueError("모르는 사진")
        if not p.is_movie:
            return {"status": "not_video"}
        if self.videos is None:
            status = "failed: 영상 재생 기능 없음"
        else:
            status = self.videos.start(uuid) if start or p.has_original else self.videos.status(uuid)
        return {"status": status, "frames": len(self.frames(uuid)), "local": p.has_original}

    def frames(self, uuid: str) -> list[Path]:
        """Photos가 영상마다 로컬에 두는 장면 미리보기 8장 (resources/derivatives/cvt/X/<uuid>/) — 원본 없이 내용 확인."""
        p = self.by_uuid.get(uuid)
        if not p or not p.is_movie or not p.thumb:
            return []
        cvt = Path(p.thumb).parents[2] / "cvt" / uuid[0] / uuid  # thumb = …/derivatives/masters/X/<uuid>_….jpeg
        return sorted(cvt.glob("*_cvt_t*.jpeg"))

    def video_path(self, uuid: str) -> Path | None:
        p = self.by_uuid.get(uuid)
        return self.videos.path(uuid) if p and p.is_movie and self.videos else None

    def reload(self) -> dict:
        """Photos 라이브러리 다시 읽기 (새로 찍은 사진·다른 기기에서 지운 사진 반영)."""
        if self.reload_fn is None:
            raise RuntimeError("새로고침 기능 없음")
        photos = self.reload_fn()  # 느림(~15초) → 잠금 밖에서
        with self.lock:
            self.photos = photos
            self.recompute()
        return {"count": len(photos)}

    @locked
    def delete_candidates(self) -> dict:
        if self.delete is None:
            raise RuntimeError("삭제 기능 없음")
        gone = set(self.delete(self.list_album()))  # macOS 확인 창 → 최근 삭제된 항목
        freed = sum(self.by_uuid[u].size for u in gone if u in self.by_uuid)
        self.photos = [p for p in self.photos if p.uuid not in gone]
        self.recompute()
        self._evict(sorted(gone))
        self.stats = {"deleted": self.stats.get("deleted", 0) + len(gone), "bytes": self.stats.get("bytes", 0) + freed}
        if self.stats_file:
            library.write_json(self.stats_file, self.stats)
        return {"deleted": len(gone), "bytes": freed}

    def pending(self) -> dict:
        return {gid: g for gid, g in self.groups.items() if not all(p.uuid in self.reviewed for p in g)}

    def get(self, name: str, q: dict):
        if name == "summary":
            return {"count": len(self.photos), "size": sum(p.size for p in self.photos),
                    "movies": sum(p.is_movie for p in self.photos),
                    "by_year": analyze.size_by_year(self.photos),
                    "groups": len(self.pending()), "trips": len(self.trips),
                    "deleted_bytes": self.stats.get("bytes", 0)}
        if name == "size":
            kind = q.get("kind", ["all"])[0]
            ps = [p for p in self.photos if kind == "all" or p.is_movie == (kind == "movie")]
            return [brief(p) for p in sorted(ps, key=lambda p: -p.size)[:200]]
        if name == "places":
            return analyze.places(self.photos)
        if name == "trips":
            return [{"name": t["name"], "start": t["start"].isoformat(), "end": t["end"].isoformat(),
                     "count": len(t["photos"]), "size": sum(p.size for p in t["photos"]),
                     "uuids": [p.uuid for p in t["photos"]]} for t in reversed(self.trips)]
        if name == "groups":
            out = []
            for gid, g in self.pending().items():
                b = analyze.best(g)
                people = any(p.face is not None for p in g)
                out.append({"id": gid, "best": b.uuid, "size_saving": sum(p.size for p in g if p is not b),
                            "people": people,
                            "photos": [brief(p) | {"rank": round(analyze.rank(p, people), 3)} for p in g]})
            return out
        if name == "settings":
            return {"values": asdict(self.params), "defaults": asdict(analyze.DEFAULT),
                    "groups": len(self.pending()), "trips": len(self.trips)}
        if name == "videos":  # 영상 정리 화면: 큰 순, 장면 미리보기 수 (원본 없이 내용 확인)
            vs = sorted((p for p in self.photos if p.is_movie), key=lambda p: -p.size)
            offset, limit = int(q.get("offset", ["0"])[0]), min(int(q.get("limit", ["40"])[0]), 200)
            return {"total": len(vs), "size": sum(p.size for p in vs),
                    "items": [brief(p) | {"frames": len(self.frames(p.uuid)), "duration": round(p.duration)}
                              for p in vs[offset:offset + limit]]}
        if name == "filters":
            out = {}
            for key, (keep, _) in FILTERS.items():
                ps = [p for p in self.photos if keep(p)]
                out[key] = {"count": len(ps), "size": sum(p.size for p in ps)}
            return out
        if name == "all":
            key = q.get("filter", ["all"])[0]
            if key not in FILTERS:
                raise KeyError(key)
            keep, order = FILTERS[key]
            ps = sorted((p for p in self.photos if keep(p)), key=order)
            offset, limit = int(q.get("offset", ["0"])[0]), min(int(q.get("limit", ["300"])[0]), 1000)
            out = {"total": len(ps), "size": sum(p.size for p in ps), "items": [brief(p) for p in ps[offset:offset + limit]]}
            if q.get("ids") == ["1"]:  # 필터 전체 선택용
                out["uuids"] = [p.uuid for p in ps]
            return out
        if name == "candidates":
            uuids = self.list_album()
            return {"count": len(uuids), "size": sum(self.by_uuid[u].size for u in uuids if u in self.by_uuid),
                    "uuids": uuids, "items": [brief(self.by_uuid[u]) for u in uuids if u in self.by_uuid]}
        raise KeyError(name)

    @locked
    def mark(self, group_id: str, reject: list[str]) -> dict:
        group = self.groups.get(group_id)
        if group is None or not set(reject) <= {p.uuid for p in group}:
            raise ValueError("묶음에 없는 사진")
        if reject:
            try:
                self.add_to_album(reject)  # 실패 시 완료 처리 안 함
            except Exception as e:
                raise RuntimeError(f"앨범 추가 실패: {e}") from e
        reviewed = self.reviewed | {p.uuid for p in group}
        try:
            library.write_json(self.state_file, sorted(reviewed))
        except OSError as e:
            raise RuntimeError(f"앨범에는 추가됐지만 진행 상태 저장 실패: {e}") from e
        self.reviewed = reviewed  # 디스크 저장 성공 후에만 메모리 반영
        self.last_marks[group_id] = list(reject)
        self._evict([p.uuid for p in group])
        return {"ok": True, "added": len(reject)}


TAILNET = ip_network("100.64.0.0/10")  # Tailscale 기기 주소 대역


def client_ok(ip: str, nets) -> bool:
    """nets가 있으면 그 대역(+ Mac 자신)에서 온 접속만 허용. None이면 검사 안 함."""
    if nets is None:
        return True
    addr = ip_address(ip)
    return addr.is_loopback or any(addr in n for n in nets)


def tailscale_info() -> tuple[str, str]:
    """(Tailscale IPv4, MagicDNS 이름). 꺼져 있으면 RuntimeError."""
    exe = shutil.which("tailscale") or "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    # TERM이 없으면(로그인 항목 등) Tailscale 앱 실행 파일이 CLI가 아니라 GUI로 동작하려다 실패함 (실측)
    env = {"TERM": "xterm", **os.environ}
    r = subprocess.run([exe, "status", "--json"], capture_output=True, text=True, timeout=10, env=env)
    me = json.loads(r.stdout or "{}").get("Self") or {}
    ips = [i for i in me.get("TailscaleIPs", []) if "." in i]
    if r.returncode != 0 or not ips:
        raise RuntimeError(r.stderr.strip() or "Tailscale이 꺼져 있음")
    return ips[0], me.get("DNSName", "").rstrip(".")


def make_handler(app: App, allowed_hosts: set[str] | None = None, allowed_nets=None):
    """allowed_hosts: Host 헤더 허용 목록 (DNS rebinding 차단). allowed_nets: 접속 허용 대역. None이면 검사 안 함."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code: int, body: bytes, ctype: str, cache: bool = False):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if cache:
                self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(body)

        def _send_range(self, path: Path, ctype: str):
            """영상: Range 요청(구간 이동) 지원, 64KB씩 전송."""
            size = path.stat().st_size
            start, end, code = 0, size - 1, 200
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
            if m and (m[1] or m[2]):
                start, end = (int(m[1]), int(m[2]) if m[2] else size - 1) if m[1] else (size - int(m[2]), size - 1)
                end, code = min(end, size - 1), 206
                if start < 0 or start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if code == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            try:
                with open(path, "rb") as fh:
                    fh.seek(start)
                    left = end - start + 1
                    while left > 0 and (chunk := fh.read(min(65536, left))):
                        self.wfile.write(chunk)
                        left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):  # 재생 중 구간 이동 → 브라우저가 이전 요청을 끊음
                pass

        def _json(self, code: int, obj):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _host_ok(self) -> bool:
            if client_ok(self.client_address[0], allowed_nets) and \
                    (allowed_hosts is None or self.headers.get("Host", "") in allowed_hosts):
                return True
            self._json(403, {"error": "forbidden host"})
            return False

        def do_GET(self):
            if not self._host_ok():
                return
            url = urlparse(self.path)
            if url.path == "/":
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            for prefix in ("/thumb/", "/preview/"):
                if url.path.startswith(prefix):
                    uuid = url.path.removeprefix(prefix)  # uuid 조회만 → 경로 조작 불가
                    if prefix == "/preview/":
                        path, cache = app.preview_path(uuid)  # 저화질은 캐시 안 함 → 고화질 도착 시 교체
                    else:
                        p = app.by_uuid.get(uuid)
                        path, cache = (p.thumb if p else None), True
                    if not path or not Path(path).exists():
                        return self._send(404, b"", "text/plain")
                    return self._send(200, Path(path).read_bytes(),
                                      mimetypes.guess_type(path)[0] or "image/jpeg", cache=cache)
            if url.path.startswith("/frame/"):  # /frame/<uuid>/<번호> — 목록 안의 번호만 → 경로 조작 불가
                uuid, _, idx = url.path.removeprefix("/frame/").partition("/")
                fs = app.frames(uuid)
                if not idx.isdigit() or int(idx) >= len(fs):
                    return self._send(404, b"", "text/plain")
                return self._send(200, fs[int(idx)].read_bytes(), "image/jpeg", cache=True)
            if url.path.startswith("/video/"):
                path = app.video_path(url.path.removeprefix("/video/"))
                if not path or not path.exists():
                    return self._send(404, b"", "text/plain")
                return self._send_range(path, "video/mp4")
            if url.path.startswith("/api/"):
                try:
                    return self._json(200, app.get(url.path.removeprefix("/api/"), parse_qs(url.query)))
                except KeyError:
                    pass
                except ValueError:  # offset·limit 숫자 아님
                    return self._json(400, {"error": "잘못된 요청"})
            self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._host_ok():
                return
            if self.path not in ("/api/mark", "/api/settings", "/api/delete", "/api/prefetch",
                                 "/api/candidate", "/api/video", "/api/reload", "/api/uncandidate", "/api/unmark"):
                return self._json(404, {"error": "not found"})
            # 다른 사이트의 text/plain 폼 POST 차단 (application/json은 CORS preflight 필요)
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                return self._json(415, {"error": "application/json only"})
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) if n > 0 else b"{}")
                if self.path == "/api/mark":
                    group_id, reject = body["group_id"], body.get("reject", [])
                    if not isinstance(group_id, str) or not isinstance(reject, list) \
                            or not all(isinstance(u, str) for u in reject):
                        raise ValueError
                if self.path in ("/api/prefetch", "/api/candidate", "/api/uncandidate"):
                    uuids = body["uuids"]
                    if not isinstance(uuids, list) or not all(isinstance(u, str) for u in uuids):
                        raise ValueError
                if self.path == "/api/unmark" and not isinstance(body["group_id"], str):
                    raise ValueError
                if self.path == "/api/video" and (not isinstance(body["uuid"], str)
                                                  or not isinstance(body.get("start", False), bool)):
                    raise ValueError
            except (ValueError, KeyError, TypeError, AttributeError):
                return self._json(400, {"error": "잘못된 요청"})
            try:
                if self.path == "/api/settings":
                    return self._json(200, app.set_params(body))
                if self.path == "/api/delete":
                    return self._json(200, app.delete_candidates())
                if self.path == "/api/prefetch":
                    app.prefetch([u for u in uuids if u in app.by_uuid][:300])  # 백그라운드 헬퍼, 즉시 반환
                    return self._json(200, {"ok": True})
                if self.path == "/api/candidate":
                    return self._json(200, app.add_candidates(uuids))
                if self.path == "/api/video":
                    return self._json(200, app.video(body["uuid"], body.get("start", False)))
                if self.path == "/api/reload":
                    return self._json(200, app.reload())
                if self.path == "/api/uncandidate":
                    return self._json(200, app.remove_candidates(uuids))
                if self.path == "/api/unmark":
                    return self._json(200, app.unmark(body["group_id"]))
                return self._json(200, app.mark(group_id, reject))
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            except Exception as e:
                return self._json(500, {"error": str(e)})

    return Handler


def already_running(port: int) -> bool:
    """이 포트에 이미 서버가 있나 — bind로는 못 앎 (macOS는 0.0.0.0 서버가 있어도 127.0.0.1 bind 허용)."""
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="photo-tidy", description="Photos 라이브러리 정리 웹 UI")
    ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 이면 같은 Wi-Fi 기기에서 접속 가능")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--library", help="Photos 라이브러리 경로 (기본: 시스템 라이브러리)")
    ap.add_argument("--no-open", action="store_true", help="브라우저 자동 열기 끄기")
    ap.add_argument("--tailscale", action="store_true",
                    help="내 Tailscale 기기(태블릿·폰)에서 접속 — 같은 Wi-Fi의 다른 기기는 차단")
    ap.add_argument("--install-autostart", action="store_true", help="로그인할 때 자동 실행 (--tailscale)")
    ap.add_argument("--uninstall-autostart", action="store_true", help="자동 실행 해제")
    a = ap.parse_args(argv)
    if a.install_autostart or a.uninstall_autostart:
        from . import autostart
        if a.uninstall_autostart:
            autostart.uninstall()
            print("자동 실행을 해제했습니다.")
            return
        print("런처 앱 빌드 중… (swiftc)", flush=True)
        autostart.install()
        subprocess.run(["open", "-R", str(autostart.APP)])  # Finder에서 앱 보여주기 (권한 목록에 끌어 넣기용)
        subprocess.run(["open", "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_AllFiles"])
        print(f"자동 실행 등록 완료 — 로그인할 때 '{autostart.APP.stem}' 앱이 서버를 띄웁니다.\n"
              "→ 열린 '전체 디스크 접근 권한' 목록에 Finder의 'photo-tidy server' 앱을 끌어 넣고 켜 주세요 (최초 1회).\n"
              f"→ 로그: {autostart.CACHE_DIR / 'server.log'}")
        return
    host, nets = a.host, None
    loopback = host in ("127.0.0.1", "localhost")
    allowed = {f"localhost:{a.port}", f"127.0.0.1:{a.port}"} if loopback else None
    url = f"http://{'localhost' if loopback else host}:{a.port}"
    if a.tailscale:  # 모든 인터페이스에서 받되 Mac 자신과 Tailscale 기기만 허용
        # 로그인 직후 Tailscale이 아직 안 켜졌으면 여기서 끝남 → 자동 실행 런처가 30초 뒤 다시 띄움
        try:
            ts_ip, ts_name = tailscale_info()
        except Exception as e:
            sys.exit(f"Tailscale을 쓸 수 없음: {e}\n→ Mac에서 Tailscale을 켜고 다시 실행하세요.")
        host, nets = "0.0.0.0", [TAILNET]
        short = ts_name.split(".")[0]
        allowed = {f"{h}:{a.port}" for h in ("localhost", "127.0.0.1", ts_ip, short, ts_name)}
        url = f"http://{short}:{a.port}"
    # 포트는 라이브러리 읽기(수십 초) 전에 잡음 → 이미 실행 중이면 바로 끝남 (런처 재시도마다 헛로딩 방지)
    # 핸들러는 앱이 준비된 뒤 연결; 그 사이 들어온 요청은 대기열에서 기다림
    if already_running(a.port):  # 자동 실행 서버와 수동 실행이 겹치면 상태 파일을 둘이 따로 고쳐 씀
        sys.exit(f"이미 실행 중 — http://localhost:{a.port} 를 여세요.")
    try:
        srv = ThreadingHTTPServer((host, a.port), BaseHTTPRequestHandler)
    except OSError as e:
        sys.exit(f"포트 {a.port}을(를) 쓸 수 없음 ({e.strerror}) — 이미 실행 중이면 http://localhost:{a.port} 를 여세요.")
    print("Photos 라이브러리 읽는 중…", flush=True)
    try:
        photos = library.load(a.library)
    except Exception as e:
        sys.exit(f"라이브러리를 읽을 수 없음: {e}\n→ 시스템 설정 > 개인정보 보호 및 보안 > 전체 디스크 접근 권한에 "
                 "서버를 띄운 앱(터미널, 또는 자동 실행이면 'photo-tidy server')을 추가하고 다시 실행하세요.")
    library.add_hashes(photos)

    def reload_library():
        ps = library.load(a.library)
        library.add_hashes(ps)
        return ps

    shutil.rmtree(library.VIDEO_DIR, ignore_errors=True)  # 재생용 mp4는 언제든 다시 만듦 → 시작 시 비움
    previews = library.PreviewQueue()  # 상주 헬퍼 1개가 앞으로 볼 사진의 고화질을 미리 받음
    app = App(photos, library.add_to_album, library.CACHE_DIR / "state.json",
              settings_file=library.CACHE_DIR / "settings.json",
              list_album=library.album_uuids, delete=library.delete_photos,
              prefetch=previews.push, forget=previews.forget, preview_dir=library.PREVIEW_DIR,
              videos=library.VideoJobs(), reload=reload_library, remove_from_album=library.remove_from_album,
              stats_file=library.CACHE_DIR / "stats.json")
    if n := app.prune_previews():
        print(f"다 본 사진의 고화질 캐시 {n}장 정리", flush=True)
    try:
        library._helper()  # 사진 헬퍼 미리 빌드 (최초 1회 swiftc ~10초)
    except Exception as e:
        print(f"주의: 사진 헬퍼 빌드 실패 → 고화질 크게 보기·삭제 불가 ({e})")
    # 멀티스레드: 영상 전송·새로고침 중에도 썸네일이 막히지 않게. 상태 변경은 App.lock으로 직렬화
    srv.RequestHandlerClass = make_handler(app, allowed, nets)
    srv.daemon_threads = True
    print(f"사진 {len(photos)}장, 묶음 {len(app.groups)}개, 여행 {len(app.trips)}개 → {url}", flush=True)
    if a.tailscale:
        print(f"태블릿/폰(Tailscale 로그인): {url}  ·  이 Mac: http://localhost:{a.port}", flush=True)
    elif not loopback:
        print("주의: 같은 네트워크의 누구나 사진 썸네일을 볼 수 있음")
    if not loopback or a.tailscale:  # 다른 기기에서 쓰는 동안 Mac이 잠들면 끊김 → 서버가 살아 있는 동안 잠자기 방지
        subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
    if not a.no_open:
        webbrowser.open(f"http://localhost:{a.port}" if a.tailscale else url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
