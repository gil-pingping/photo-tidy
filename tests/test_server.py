import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer

import pytest

from photo_tidy.analyze import Photo
from photo_tidy.server import App, make_handler

T0 = datetime(2026, 7, 1, 12, 0)


@pytest.fixture
def env(tmp_path):
    thumb = tmp_path / "t.jpg"
    thumb.write_bytes(b"\xff\xd8jpeg")
    photos = [Photo(uuid=f"u{i}", date=T0 + timedelta(minutes=i), size=100 * (i + 1), hash=0, score=i / 10,
                    thumb=str(thumb)) for i in range(4)]
    photos.append(Photo(uuid="m", date=T0 + timedelta(days=1), size=10_000, is_movie=True))
    calls, servers = [], []
    state = tmp_path / "state.json"

    def start(album):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(App(photos, album, state)))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_port}"

    yield start(calls.append), calls, start
    for s in servers:
        s.shutdown()


def get(url):
    with urllib.request.urlopen(url) as r:
        return r.status, r.read()


def post(url, body, ctype="application/json"):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_summary_groups_size(env):
    base, *_ = env
    s = json.loads(get(base + "/api/summary")[1])
    assert (s["count"], s["movies"], s["groups"], s["size"]) == (5, 1, 1, 11_000)
    (g,) = json.loads(get(base + "/api/groups")[1])
    assert g["id"] == "u0" and g["best"] == "u3" and len(g["photos"]) == 4
    assert g["size_saving"] == 100 + 200 + 300
    assert [p["uuid"] for p in json.loads(get(base + "/api/size?kind=movie")[1])] == ["m"]
    assert get(base + "/")[0] == 200


def test_thumb_only_by_uuid(env):
    base, *_ = env
    assert get(base + "/thumb/u0") == (200, b"\xff\xd8jpeg")
    for bad in ["/thumb/m", "/thumb/../../etc/passwd", "/thumb/nope", "/api/nope"]:
        with pytest.raises(urllib.error.HTTPError) as e:
            get(base + bad)
        assert e.value.code == 404


def test_mark_flow_and_persistence(env):
    base, calls, start = env
    assert post(base + "/api/mark", {"group_id": "u0", "reject": ["m"]})[0] == 400  # 묶음 밖 uuid
    assert post(base + "/api/mark", {"group_id": "u0", "reject": ["u0"]}, ctype="text/plain")[0] == 415  # CSRF
    assert calls == []
    assert post(base + "/api/mark", {"group_id": "u0", "reject": ["u0", "u1"]}) == (200, {"ok": True, "added": 2})
    assert calls == [["u0", "u1"]]
    assert json.loads(get(base + "/api/groups")[1]) == []
    base2 = start(calls.append)  # 재시작해도 완료 유지
    assert json.loads(get(base2 + "/api/groups")[1]) == []


def test_album_failure_keeps_group(env):
    *_, start = env

    def boom(uuids):
        raise RuntimeError("Photos 자동화 권한 거부")

    base = start(boom)
    code, body = post(base + "/api/mark", {"group_id": "u0", "reject": ["u1"]})
    assert code == 500 and "권한" in body["error"]
    assert len(json.loads(get(base + "/api/groups")[1])) == 1


def test_reviewed_group_stays_hidden_after_rejects_deleted(tmp_path):
    photos = [Photo(uuid=f"u{i}", date=T0 + timedelta(minutes=i), size=1, hash=0) for i in range(5)]
    state = tmp_path / "state.json"
    App(photos, lambda u: None, state).mark("u0", ["u0", "u1"])
    # Photos에서 u0,u1 삭제 후 재시작 → 남은 [u2,u3,u4]는 id가 u2로 바뀌어도 다시 뜨면 안 됨
    assert App(photos[2:], lambda u: None, state).get("groups", {}) == []


def test_corrupt_state_treated_as_empty(tmp_path):
    state = tmp_path / "state.json"
    state.write_text('["u0", ')  # 잘린 파일
    photos = [Photo(uuid=f"u{i}", date=T0 + timedelta(minutes=i), size=1, hash=0) for i in range(3)]
    assert len(App(photos, lambda u: None, state).get("groups", {})) == 1


def test_bad_post_bodies_get_400(env):
    base, calls, _ = env
    host = base.removeprefix("http://")
    for raw in [b"{not json", b"[1,2]", b'{"group_id": "u0", "reject": null}', b'{"group_id": 5}', b"{}"]:
        req = urllib.request.Request(base + "/api/mark", raw, {"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 400, raw
    import http.client
    c = http.client.HTTPConnection(host)
    c.putrequest("POST", "/api/mark")
    c.putheader("Content-Type", "application/json")
    c.putheader("Content-Length", "abc")
    c.endheaders()
    assert c.getresponse().status == 400
    assert calls == []


def test_foreign_host_header_rejected(tmp_path):
    allowed = set()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(App([], lambda u: None, tmp_path / "s.json"), allowed))
    port = srv.server_port
    allowed.add(f"127.0.0.1:{port}")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert get(f"http://127.0.0.1:{port}/api/summary")[0] == 200
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/summary", headers={"Host": "evil.example"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 403
    finally:
        srv.shutdown()


def test_empty_library(tmp_path):
    app = App([], lambda u: None, tmp_path / "s.json")
    assert app.get("summary", {})["count"] == 0
    assert app.get("trips", {}) == [] and app.get("groups", {}) == []


def serve(app):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_port}", srv


def series(n=4):
    return [Photo(uuid=f"u{i}", date=T0 + timedelta(minutes=i), size=10, hash=(1 << i)) for i in range(n)]


def test_settings_recompute_validate_persist(tmp_path):
    sf = tmp_path / "settings.json"
    base, srv = serve(App(series(), lambda u: None, tmp_path / "s.json", settings_file=sf))
    try:
        s = json.loads(get(base + "/api/settings")[1])
        assert s["values"]["hash_max"] == 22 and s["defaults"]["hash_max"] == 22 and s["groups"] == 1
        code, r = post(base + "/api/settings", {"hash_max": 1})  # 이웃 해밍 2 > 1 → 안 묶임
        assert code == 200 and r["groups"] == 0 and r["values"]["hash_max"] == 1
        assert json.loads(get(base + "/api/summary")[1])["groups"] == 0
        for bad in [{"hash_max": "x"}, {"hash_max": 999}, {"nope": 1}, {"min_group": True}, [1]]:
            assert post(base + "/api/settings", bad)[0] == 400, bad
    finally:
        srv.shutdown()
    assert App(series(), lambda u: None, tmp_path / "s.json", settings_file=sf).get("settings", {})["values"]["hash_max"] == 1
    sf.write_text("{broken")  # 손상 → 기본값
    assert App(series(), lambda u: None, tmp_path / "s.json", settings_file=sf).params.hash_max == 22


def test_preview_falls_back_to_thumb(tmp_path):
    big, small = tmp_path / "big.jpg", tmp_path / "small.jpg"
    big.write_bytes(b"BIG")
    small.write_bytes(b"SMALL")
    ps = [Photo(uuid="a", date=T0, size=1, thumb=str(small), preview=str(big)),
          Photo(uuid="b", date=T0, size=1, thumb=str(small))]
    base, srv = serve(App(ps, lambda u: None, tmp_path / "s.json"))
    try:
        assert get(base + "/preview/a")[1] == b"BIG" and get(base + "/preview/b")[1] == b"SMALL"
    finally:
        srv.shutdown()


def test_candidates_and_delete(tmp_path):
    deleted_calls = []

    def fake_delete(uuids):
        deleted_calls.append(uuids)
        return uuids[:2]  # 앨범 3장 중 2장만 라이브러리에 실재해서 삭제됐다고 가정

    app = App(series(5), lambda u: None, tmp_path / "s.json",
              list_album=lambda: ["u0", "u1", "u2"], delete=fake_delete)
    base, srv = serve(app)
    try:
        c = json.loads(get(base + "/api/candidates")[1])
        assert (c["count"], c["size"], c["uuids"]) == (3, 30, ["u0", "u1", "u2"])
        code, r = post(base + "/api/delete", {})
        assert code == 200 and r == {"deleted": 2, "bytes": 20}
        assert deleted_calls == [["u0", "u1", "u2"]]
        s = json.loads(get(base + "/api/summary")[1])
        assert s["count"] == 3 and "u0" not in app.by_uuid
        with pytest.raises(urllib.error.HTTPError) as e:
            get(base + "/thumb/u0")
        assert e.value.code == 404
    finally:
        srv.shutdown()


def test_delete_failure_reports_500(tmp_path):
    def boom(uuids):
        raise RuntimeError("사진 접근 권한 없음")

    base, srv = serve(App(series(), lambda u: None, tmp_path / "s.json", list_album=lambda: ["u0"], delete=boom))
    try:
        code, r = post(base + "/api/delete", {})
        assert code == 500 and "권한" in r["error"]
        assert json.loads(get(base + "/api/summary")[1])["count"] == 4
    finally:
        srv.shutdown()


def test_prefetch_and_hires_preview(tmp_path):
    thumb, hires_dir = tmp_path / "t.jpg", tmp_path / "previews"
    thumb.write_bytes(b"LOW")
    hires_dir.mkdir()
    asked = []
    ps = [Photo(uuid=f"u{i}", date=T0, size=1, thumb=str(thumb)) for i in range(3)]
    app = App(ps, lambda u: None, tmp_path / "s.json", prefetch=asked.append, preview_dir=hires_dir)
    base, srv = serve(app)
    try:
        assert get(base + "/preview/u0")[1] == b"LOW"
        (hires_dir / "u0.jpg").write_bytes(b"HIGH")  # 헬퍼가 iCloud에서 받아옴
        assert get(base + "/preview/u0")[1] == b"HIGH"
        assert post(base + "/api/prefetch", {"uuids": ["u1", "nope", "u2"]}) == (200, {"ok": True})
        assert asked == [["u1", "u2"]]  # 모르는 uuid는 걸러냄
        assert post(base + "/api/prefetch", {"uuids": "u1"})[0] == 400
    finally:
        srv.shutdown()


def test_groups_expose_rank_and_face_flags(tmp_path):
    ps = series(3)
    ps[1].face, ps[1].eyes_closed = 0.2, True
    ps[2].face, ps[2].smile = 0.9, True
    (g,) = App(ps, lambda u: None, tmp_path / "s.json").get("groups", {})
    assert g["people"] and g["best"] == "u2"
    by = {p["uuid"]: p for p in g["photos"]}
    assert by["u2"]["rank"] == max(p["rank"] for p in g["photos"])
    assert by["u1"]["eyes_closed"] and by["u2"]["smile"] and by["u0"]["face"] is None


def test_mark_evicts_previews_and_startup_prune(tmp_path):
    pdir = tmp_path / "previews"
    pdir.mkdir()
    ps = series(4) + [Photo(uuid="x", date=T0 + timedelta(days=9), size=1, hash=1 << 40)]  # x: 묶음 밖
    for u in ["u0", "u1", "x", "gone"]:
        (pdir / f"{u}.jpg").write_bytes(b"j")
    forgot = []
    app = App(ps, lambda u: None, tmp_path / "s.json", preview_dir=pdir, forget=forgot.extend)
    assert app.prune_previews() == 2  # 남은 묶음에 없는 x, 라이브러리에 없는 gone
    assert sorted(f.stem for f in pdir.glob("*.jpg")) == ["u0", "u1"]
    assert sorted(forgot) == ["gone", "x"]  # 대기열에서도 뺌
    forgot.clear()
    app.mark("u0", ["u1"])
    assert list(pdir.glob("*.jpg")) == [] and sorted(forgot) == ["u0", "u1", "u2", "u3"]


def test_delete_evicts_previews(tmp_path):
    pdir = tmp_path / "previews"
    pdir.mkdir()
    (pdir / "u0.jpg").write_bytes(b"j")
    app = App(series(4), lambda u: None, tmp_path / "s.json", preview_dir=pdir,
              list_album=lambda: ["u0"], delete=lambda uuids: uuids)
    app.delete_candidates()
    assert not (pdir / "u0.jpg").exists()


def test_client_ok_only_loopback_and_tailnet():
    from photo_tidy.server import TAILNET, client_ok

    assert client_ok("127.0.0.1", [TAILNET]) and client_ok("100.88.1.2", [TAILNET])  # Mac 자신, 내 태블릿
    assert not client_ok("192.168.0.23", [TAILNET])  # 같은 Wi-Fi의 다른 기기
    assert client_ok("192.168.0.23", None)  # 필터 없음(기존 --host 0.0.0.0)


def test_loopback_always_allowed_with_net_filter(tmp_path):
    from ipaddress import ip_network

    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(App([], lambda u: None, tmp_path / "s.json"),
                                                             None, [ip_network("10.0.0.0/8")]))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:  # 필터가 다른 대역이어도 Mac 자신(루프백)은 항상 허용
        assert get(f"http://127.0.0.1:{srv.server_port}/api/summary")[0] == 200
    finally:
        srv.shutdown()


class FakeVideos:
    def __init__(self, vdir):
        self.vdir, self.started = vdir, []

    def path(self, u):
        return self.vdir / f"{u}.mp4"

    def status(self, u):
        return "ready" if self.path(u).exists() else ("working" if u in self.started else "none")

    def start(self, u):
        if self.status(u) == "none":
            self.started.append(u)
        return self.status(u)


def test_add_candidates_from_any_tab(tmp_path):
    added = []
    ps = series(2) + [Photo(uuid="m", date=T0 + timedelta(days=1), size=10, is_movie=True)]
    base, srv = serve(App(ps, added.append, tmp_path / "s.json", list_album=lambda: ["m"]))
    try:
        assert post(base + "/api/candidate", {"uuids": ["m"]}) == (200, {"ok": True, "added": 1})
        assert added == [["m"]]
        assert post(base + "/api/candidate", {"uuids": ["nope"]})[0] == 400  # 모르는 사진
        assert json.loads(get(base + "/api/candidates")[1])["uuids"] == ["m"]  # ✕ 표시용
    finally:
        srv.shutdown()


def test_video_storyboard_explicit_play_and_range(tmp_path):
    vdir = tmp_path / "videos"
    vdir.mkdir()
    lib = tmp_path / "lib/resources/derivatives"  # Photos 라이브러리 구조 흉내
    thumb = lib / "masters/m/m_4_5005_c.jpeg"
    cvt = lib / "cvt/m/m"
    cvt.mkdir(parents=True)
    thumb.parent.mkdir(parents=True)
    thumb.write_bytes(b"T")
    for i in range(3):
        (cvt / f"m_cvt_t000{i}.jpeg").write_bytes(b"F%d" % i)
    ps = series(1) + [Photo(uuid="m", date=T0, size=10, is_movie=True, thumb=str(thumb)),
                      Photo(uuid="loc", date=T0, size=10, is_movie=True, has_original=True)]
    videos = FakeVideos(vdir)
    base, srv = serve(App(ps, lambda u: None, tmp_path / "s.json", videos=videos))
    try:
        assert post(base + "/api/video", {"uuid": "u0"}) == (200, {"status": "not_video"})
        # 열기만 하면 받지 않음 (iCloud 원본 다운로드는 몇 분 + 사진 데몬을 막음) → 장면 미리보기만
        assert post(base + "/api/video", {"uuid": "m"}) == (200, {"status": "none", "frames": 3, "local": False})
        assert videos.started == []
        assert get(base + "/frame/m/2")[1] == b"F2"
        assert post(base + "/api/video", {"uuid": "m", "start": True})[1]["status"] == "working"
        assert videos.started == ["m"]
        # 원본이 이미 Mac에 있으면 몇 초면 되니 바로 준비
        assert post(base + "/api/video", {"uuid": "loc"})[1]["status"] == "working" and "loc" in videos.started
        (vdir / "m.mp4").write_bytes(bytes(range(256)) * 4)  # 1024바이트 가짜 영상
        assert post(base + "/api/video", {"uuid": "m"})[1]["status"] == "ready"
        req = urllib.request.Request(base + "/video/m", headers={"Range": "bytes=10-19"})
        with urllib.request.urlopen(req) as r:  # 구간 이동(seek)용 부분 응답
            assert r.status == 206 and r.headers["Content-Range"] == "bytes 10-19/1024"
            assert r.read() == bytes(range(10, 20))
        status, body = get(base + "/video/m")
        assert status == 200 and len(body) == 1024
        for bad in ["/frame/m/9", "/frame/m/x", "/frame/u0/0", "/frame/m/../../etc"]:
            with pytest.raises(urllib.error.HTTPError) as e:
                get(base + bad)
            assert e.value.code == 404, bad
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(urllib.request.Request(base + "/video/m", headers={"Range": "bytes=5000-"}))
        assert e.value.code == 416
        with pytest.raises(urllib.error.HTTPError) as e:
            get(base + "/video/u0")
        assert e.value.code == 404
    finally:
        srv.shutdown()


def test_reload_library(tmp_path):
    fresh = series(5)
    app = App(series(2), lambda u: None, tmp_path / "s.json", reload=lambda: fresh)
    assert app.get("summary", {})["count"] == 2
    assert app.reload() == {"count": 5}
    assert app.get("summary", {})["count"] == 5 and "u4" in app.by_uuid


def test_uncandidate_and_candidates_tab_items(tmp_path):
    removed = []
    ps = series(3)
    base, srv = serve(App(ps, lambda u: None, tmp_path / "s.json", list_album=lambda: ["u1", "elsewhere"],
                          remove_from_album=lambda uuids: removed.append(uuids) or len(uuids)))
    try:
        c = json.loads(get(base + "/api/candidates")[1])
        assert [i["uuid"] for i in c["items"]] == ["u1"]  # 후보 탭 타일용 (라이브러리에 없는 건 개수만)
        assert post(base + "/api/uncandidate", {"uuids": ["u1"]}) == (200, {"ok": True, "removed": 1})
        assert removed == [["u1"]]
        assert post(base + "/api/uncandidate", {"uuids": "u1"})[0] == 400
    finally:
        srv.shutdown()


def test_unmark_restores_group_and_album(tmp_path):
    removed, state = [], tmp_path / "s.json"
    app = App(series(4), lambda u: None, state, remove_from_album=lambda uuids: removed.append(uuids) or len(uuids))
    app.mark("u0", ["u1", "u2"])
    assert app.get("groups", {}) == []
    assert app.unmark("u0") == {"ok": True, "removed": 2}
    assert removed == [["u1", "u2"]] and len(app.get("groups", {})) == 1  # 다시 정리 대상
    assert json.loads(state.read_text()) == []  # 재시작해도 되돌린 상태
    with pytest.raises(ValueError):
        app.unmark("u0")  # 이미 되돌림
    app.mark("u0", [])  # 전부 남기기도 되돌릴 수 있음 (앨범 작업 없음)
    assert app.unmark("u0") == {"ok": True, "removed": 0} and removed == [["u1", "u2"]]


def browse_photos():
    d = lambda days: T0 + timedelta(days=days)  # noqa: E731
    return [Photo(uuid="new", date=d(9), size=5),
            Photo(uuid="shot", date=d(8), size=1, is_screenshot=True),
            Photo(uuid="bad2", date=d(7), size=1, failure=-0.5),
            Photo(uuid="bad1", date=d(6), size=1, failure=-0.2),
            Photo(uuid="ok", date=d(5), size=1, failure=-0.01),
            Photo(uuid="extra", date=d(4), size=1, burst_extra=True),
            Photo(uuid="vid", date=d(3), size=900, is_movie=True, failure=-0.9),  # 영상은 실패작 판정 제외
            Photo(uuid="old", date=d(0), size=2)]


def test_browse_filters_sort_and_paging(tmp_path):
    base, srv = serve(App(browse_photos(), lambda u: None, tmp_path / "s.json"))
    try:
        f = json.loads(get(base + "/api/filters")[1])
        assert {k: v["count"] for k, v in f.items()} == {"all": 8, "old": 8, "screenshot": 1, "fail": 2, "burst": 1, "movie": 1}
        assert f["movie"]["size"] == 900
        uu = lambda q: [i["uuid"] for i in json.loads(get(base + "/api/all?" + q)[1])["items"]]  # noqa: E731
        assert uu("filter=all&limit=3") == ["new", "shot", "bad2"]  # 최신순
        assert uu("filter=all&offset=3&limit=2") == ["bad1", "ok"]  # 이어서
        assert uu("filter=old&limit=2") == ["old", "vid"]
        assert uu("filter=fail") == ["bad2", "bad1"]  # 실패작에 가까운 순
        assert uu("filter=screenshot") == ["shot"] and uu("filter=burst") == ["extra"]
        r = json.loads(get(base + "/api/all?filter=fail&limit=1&ids=1")[1])
        assert (r["total"], r["size"], r["uuids"]) == (2, 2, ["bad2", "bad1"])  # 필터 전체 선택용
        with pytest.raises(urllib.error.HTTPError) as e:
            get(base + "/api/all?filter=nope")
        assert e.value.code == 404
    finally:
        srv.shutdown()


def test_add_candidates_in_chunks(tmp_path):
    calls = []
    ps = [Photo(uuid=f"p{i}", date=T0 + timedelta(seconds=i), size=1) for i in range(1200)]
    app = App(ps, calls.append, tmp_path / "s.json")
    assert app.add_candidates([p.uuid for p in ps])["added"] == 1200
    assert [len(c) for c in calls] == [500, 500, 200]  # osascript 인자·시간 한계 → 나눠서


def test_videos_list_sorted_with_frames(tmp_path):
    lib = tmp_path / "lib/resources/derivatives"
    ps = []
    for u, size in [("small", 10), ("big", 900), ("mid", 300)]:
        thumb = lib / f"masters/{u[0]}/{u}_4_5005_c.jpeg"
        thumb.parent.mkdir(parents=True, exist_ok=True)
        thumb.write_bytes(b"T")
        ps.append(Photo(uuid=u, date=T0, size=size, is_movie=True, thumb=str(thumb)))
    cvt = lib / "cvt/b/big"
    cvt.mkdir(parents=True)
    for i in range(8):
        (cvt / f"big_cvt_t000{i}.jpeg").write_bytes(b"F")
    app = App(ps + series(2), lambda u: None, tmp_path / "s.json")
    r = app.get("videos", {"limit": ["2"]})
    assert (r["total"], r["size"]) == (3, 1210)
    assert [(i["uuid"], i["frames"]) for i in r["items"]] == [("big", 8), ("mid", 0)]  # 큰 순, 장면 미리보기 수
    assert [i["uuid"] for i in app.get("videos", {"offset": ["2"]})["items"]] == ["small"]


def test_delete_reports_and_accumulates_freed_bytes(tmp_path):
    stats = tmp_path / "stats.json"
    mk = lambda: App(series(4), lambda u: None, tmp_path / "s.json", stats_file=stats,  # noqa: E731
                     list_album=lambda: ["u0", "u1"], delete=lambda uuids: uuids)
    app = mk()
    assert app.get("summary", {})["deleted_bytes"] == 0
    assert app.delete_candidates() == {"deleted": 2, "bytes": 20}
    assert mk().get("summary", {})["deleted_bytes"] == 20  # 재시작해도 누적 유지
