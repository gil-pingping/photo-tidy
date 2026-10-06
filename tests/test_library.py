import json
from datetime import datetime

from PIL import Image, ImageDraw

from photo_tidy.analyze import DEFAULT, Photo
from photo_tidy import library
from photo_tidy.library import add_hashes


def scene(path, shift=0, flip=False):
    img = Image.new("RGB", (256, 256), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([40 + shift, 60, 140 + shift, 200], fill="black")  # 사람 자리
    d.ellipse([170, 20, 240, 90], fill="gray")
    if flip:
        img = img.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    img.save(path)
    return str(path)


def P(uuid, thumb, **kw):
    return Photo(uuid=uuid, date=datetime(2026, 1, 1), size=1, thumb=thumb, **kw)


def test_hashes_similar_close_different_far_and_cached(tmp_path):
    a = P("a", scene(tmp_path / "a.jpg"))
    b = P("b", scene(tmp_path / "b.jpg", shift=8))
    c = P("c", scene(tmp_path / "c.jpg", flip=True))
    cache = tmp_path / "h.json"
    add_hashes([a, b, c], cache)
    assert (a.hash ^ b.hash).bit_count() <= DEFAULT.hash_max < (a.hash ^ c.hash).bit_count()
    assert set(json.loads(cache.read_text())) == {"a", "b", "c"}
    (tmp_path / "a.jpg").unlink()  # 캐시 있으면 파일 다시 안 읽음
    a2 = P("a", str(tmp_path / "a.jpg"))
    add_hashes([a2], cache)
    assert a2.hash == a.hash


def test_truncated_cache_is_recomputed(tmp_path):
    cache = tmp_path / "h.json"
    cache.write_text('{"a": "ff')  # 중단으로 잘린 파일
    a = P("a", scene(tmp_path / "a.jpg"))
    add_hashes([a], cache)
    assert a.hash is not None and json.loads(cache.read_text()) == {"a": f"{a.hash:016x}"}


def test_unreadable_thumb_and_skips(tmp_path):
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not an image")
    photos = [P("bad", str(bad)), P("none", None), P("mov", scene(tmp_path / "m.jpg"), is_movie=True)]
    add_hashes(photos, tmp_path / "h.json")
    assert [p.hash for p in photos] == [None, None, None]


def test_add_to_album_passes_uuids_and_surfaces_errors():
    from subprocess import CompletedProcess

    import pytest

    from photo_tidy.library import ALBUM, add_to_album

    def no_helper(*a):
        raise RuntimeError("헬퍼 없음")

    helped = []
    add_to_album(["A"], run=None, helper=lambda *a: helped.append(a))  # 기본: PhotoKit 헬퍼
    assert helped == [("album", ["A"], ALBUM)]
    calls = []  # 헬퍼 실패 → AppleScript
    add_to_album(["A", "B"], run=lambda cmd, **kw: calls.append(cmd) or CompletedProcess(cmd, 0, "", ""), helper=no_helper)
    assert calls[0][0] == "osascript" and calls[0][3:] == [ALBUM, "A", "B"]
    with pytest.raises(RuntimeError, match="-1743"):
        add_to_album(["A"], run=lambda cmd, **kw: CompletedProcess(cmd, 1, "", "Not authorized (-1743)"), helper=no_helper)


def test_preview_queue_order_cap_file_and_single_helper(tmp_path, monkeypatch):
    import os
    import time
    from pathlib import Path

    from photo_tidy import library

    monkeypatch.setattr(library, "PREVIEW_DIR", tmp_path)
    (tmp_path / "HAVE.jpg").write_bytes(b"x")  # 이미 받음 → 대기열에 안 넣음
    launches = []
    qfile = tmp_path / "q.txt"
    q = library.PreviewQueue(cap=4, launch=lambda: launches.append(1), qfile=qfile)
    read = lambda: qfile.read_text().split()  # noqa: E731 — 헬퍼가 읽는 대기열 파일
    assert q.push(["a", "b", "HAVE"]) == 2 and read() == ["a", "b"]
    assert q.push(["c", "d", "b"]) == 3 and read() == ["c", "d", "b", "a"]  # 나중 요청이 맨 앞, 중복 없음
    q.push(["e"])
    assert read() == ["e", "c", "d", "b"]  # 상한 4 → 가장 오래된 'a' 버림
    (tmp_path / "c.jpg").write_bytes(b"x")  # 헬퍼가 받아 둠
    q.forget(["d"])  # 다 본 사진
    assert read() == ["e", "b"]
    assert launches == [1]  # 방금 띄운 헬퍼는 뜨는 중으로 보고 다시 안 띄움
    alive = Path(f"{qfile}.alive")
    q.launched_at = 0
    alive.write_bytes(b"")  # 하트비트 살아 있음
    q.push(["f"])
    assert launches == [1]
    old = time.time() - 60
    os.utime(alive, (old, old))  # 하트비트 끊김 = 헬퍼 종료
    q.push(["g"])
    assert launches == [1, 1]


def test_video_jobs_start_once_and_report(tmp_path, monkeypatch):
    from photo_tidy import library

    monkeypatch.setattr(library, "VIDEO_DIR", tmp_path)
    launched = []
    jobs = library.VideoJobs(launch=launched.append)
    assert jobs.status("v") == "none" and launched == []  # 상태 조회는 실행 안 함
    assert jobs.start("v") == "working" and jobs.start("v") == "working" and launched == ["v"]  # 진행 중 중복 실행 X
    (tmp_path / "v.mp4.err").write_text("iCloud 받기 실패")
    assert jobs.start("v") == "failed: iCloud 받기 실패"
    (tmp_path / "v.mp4.err").unlink()
    (tmp_path / "v.mp4").write_bytes(b"x")
    assert jobs.start("v") == "ready"


def test_big_helper_log_is_reset_before_launch(tmp_path, monkeypatch):
    log = tmp_path / "helper.log"
    log.write_bytes(b"x" * (library.LOG_MAX + 1))
    library.trim_log(log)
    assert not log.exists()  # 헬퍼가 꺼져 있을 때만 호출 → 지워도 안전
    log.write_text("small")
    library.trim_log(log)
    assert log.read_text() == "small"
    library.trim_log(tmp_path / "none.log")  # 없으면 무시


def test_set_dates_sends_uuid_and_epoch_in_chunks():
    from datetime import timedelta, timezone
    calls = []

    def fake(mode, lines):
        calls.append((mode, lines))
        return {"updated": len(lines)}

    d = datetime(2018, 7, 4, 11, 47, 21, tzinfo=timezone(timedelta(hours=9)))
    assert library.set_dates({f"u{i}": d for i in range(600)}, helper=fake) == 600
    assert [(m, len(ls)) for m, ls in calls] == [("setdate", 500), ("setdate", 100)]
    assert calls[0][1][0] == f"u0\t{d.timestamp()}"  # 절대 시각(UTC 초) → 시간대 혼동 없음
    assert library.set_dates({}, helper=fake) == 0
