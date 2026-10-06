import json
from datetime import datetime, timedelta, timezone

from photo_tidy import dates
from photo_tidy.dates import Estimate, Item

KST = timezone(timedelta(hours=9))
T = lambda s: datetime.fromisoformat(s).replace(tzinfo=KST)  # noqa: E731


def item(uuid, name, date, added=None, exif=False, screenshot=False):
    return Item(uuid=uuid, filename=name, date=T(date), added=T(added or date), has_exif=exif, screenshot=screenshot)


def test_filename_date_patterns():  # 시간대 없는 값 — estimate()가 사진의 시간대를 붙임
    assert dates.filename_date("20170819_161549.jpg") == datetime(2017, 8, 19, 16, 15, 49)  # 삼성·안드로이드 기본
    assert dates.filename_date("IMG_20170819_161549.jpg") == datetime(2017, 8, 19, 16, 15, 49)
    assert dates.filename_date("Screenshot_2017-08-19-16-15-49.png") == datetime(2017, 8, 19, 16, 15, 49)
    assert dates.filename_date("KakaoTalk_Photo_2024-09-20-13-18-38 002.jpeg") == datetime(2024, 9, 20, 13, 18, 38)
    assert dates.filename_date("3472391391334402604_20241110103020667.jpg") == datetime(2024, 11, 10, 10, 30, 20)  # MYBOX: 받은 시각
    assert dates.filename_date("IMG_1404.HEIC") is None
    assert dates.filename_date("12345678_99999999999999999.jpg") is None  # 말이 안 되는 날짜


def test_naver_id():
    assert dates.naver_id("3472391391334402604_20241110103020667.jpg") == 3472391391334402604
    assert dates.naver_id("IMG_1404.HEIC") is None


def test_stuck_means_date_equals_import_and_no_exif():
    assert dates.is_stuck(item("a", "x.jpg", "2024-11-10T10:00", "2024-11-10T10:05"))
    assert not dates.is_stuck(item("b", "x.jpg", "2017-08-19T16:15", "2024-11-10T10:05"))  # 진짜 날짜 있음
    assert not dates.is_stuck(item("c", "x.jpg", "2024-11-10T10:00", "2024-11-10T10:05", exif=True))  # 카메라로 그날 찍음
    assert not dates.is_stuck(item("d", "IMG_0001.PNG", "2024-11-10T10:00", "2024-11-10T10:05", screenshot=True))  # 스크린샷 = 그 시각이 맞음


def test_filename_date_is_used_unless_it_is_the_import_time():
    items = [item("a", "20170819_161549.jpg", "2024-11-10T10:00"),  # 파일명의 촬영 시각
             item("b", "99_20241110100000000.jpg", "2024-11-10T10:00"),  # 파일명 시각 = 가져온 시각 → 단서 아님
             item("c", "34723702741600433_20241110103044865.jpg", "2025-08-25T10:00")]  # MYBOX 이름: 17자리는 받은 시각 (나중에 다시 가져와도)
    est = dates.estimate(items)
    assert est["a"].date == T("2017-08-19T16:15:49") and est["a"].tier == "exact"
    assert est["b"].tier == "unknown" and est["b"].date is None
    assert est["c"].tier == "unknown"


def naver(uuid, nid, date=None):
    stuck = date is None
    return item(uuid, f"{nid:019d}_20241110103000000.jpg", "2024-11-10T10:30" if stuck else date, "2024-11-10T10:30", exif=not stuck)


def test_sequence_interpolation_skips_late_upload_anchor():
    items = [naver("g1", 1000, "2018-01-01T12:00"), naver("g2", 1100, "2018-01-11T12:00"),
             naver("s1", 1050),  # g1·g2 사이 한가운데 → 01-06, 창 10일
             naver("late", 1120, "2017-06-01T12:00"),  # 옛 사진을 늦게 올림 → 위쪽 경계로 쓰면 안 됨
             naver("g3", 1200, "2018-01-13T12:00"),
             naver("s2", 1110),  # g2(01-11)와 g3(01-13) 사이 → 창 2일
             naver("s3", 1300)]  # 위쪽에 날짜 아는 사진 없음
    est = dates.estimate(items)
    assert est["s1"].date == T("2018-01-06T12:00") and est["s1"].tier == "month"
    assert (est["s1"].lo, est["s1"].hi) == (T("2018-01-01T12:00"), T("2018-01-11T12:00"))
    assert est["s2"].tier == "day" and (est["s2"].lo, est["s2"].hi) == (T("2018-01-11T12:00"), T("2018-01-13T12:00"))
    assert est["s3"].tier == "unknown"
    assert "g1" not in est  # 날짜 있는 사진은 손대지 않음


def test_report_separates_recoverable_from_no_clue(tmp_path):
    est = {"a": Estimate(T("2018-01-06T12:00"), None, None, "month", "sequence"), "b": Estimate(None, None, None, "unknown", "none")}
    items = [item("a", "x.jpg", "2024-11-10T10:00"), item("b", "IMG_0002.JPG", "2024-11-10T10:00")]
    text = dates.report(est, items, tmp_path / "plan.csv")
    assert "복원 가능 1장" in text and "단서 없음 1장" in text
    assert (tmp_path / "plan.csv").read_text().splitlines()[0] == "uuid,filename,current,estimate,lo,hi,tier,method"


def test_tiers_by_window_width():
    assert dates.tier(timedelta(days=2)) == "day"
    assert dates.tier(timedelta(days=45)) == "month"
    assert dates.tier(timedelta(days=46)) == "season"
    assert dates.tier(timedelta(days=121)) == "year"


def test_apply_backs_up_originals_and_undo_restores(tmp_path):
    calls = []

    def fake_set(d, helper=None):
        calls.append(dict(d))
        return len(d)

    backup = tmp_path / "backup.json"
    items = [item("a", "x.jpg", "2024-11-10T10:00"), item("b", "y.jpg", "2024-11-10T10:01")]
    est = {"a": Estimate(T("2018-01-06T12:00"), None, None, "month", "seq"),
           "b": Estimate(T("2016-01-01T12:00"), None, None, "year", "seq")}
    assert dates.apply(items, est, tiers={"day", "month"}, backup=backup, set_dates=fake_set) == 1  # year는 제외
    assert calls[-1] == {"a": T("2018-01-06T12:00")}
    assert json.loads(backup.read_text()) == {"a": "2024-11-10T10:00:00+09:00"}
    dates.apply(items, est, tiers={"day", "month"}, backup=backup, set_dates=fake_set)  # 다시 적용해도 원본 백업은 그대로
    assert json.loads(backup.read_text()) == {"a": "2024-11-10T10:00:00+09:00"}
    assert dates.undo(backup, set_dates=fake_set) == 1
    assert calls[-1] == {"a": T("2024-11-10T10:00")}
