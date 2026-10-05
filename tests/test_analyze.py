from datetime import datetime, timedelta

from photo_tidy.analyze import Params, Photo, best, face_score, homes, rank, places, similar_groups, size_by_year, trips

T0 = datetime(2026, 7, 1, 12, 0)
SEOUL = (37.5665, 126.9780)
JEJU = (33.4996, 126.5312)


def ph(i, minutes=0, gps=SEOUL, h=0, **kw):
    lat, lon = gps if gps else (None, None)
    return Photo(uuid=f"u{i}", date=T0 + timedelta(minutes=minutes), size=1000, lat=lat, lon=lon, hash=h, **kw)


def test_pose_series_grouped_and_scene_change_splits():
    series = [ph(i, minutes=i, h=1 << i) for i in range(12)]  # 이웃끼리 2비트 차이
    other = ph(99, minutes=13, h=(1 << 64) - 1)  # 완전히 다른 장면
    assert [len(g) for g in similar_groups(series + [other])] == [12]


def test_time_gap_and_distance_split():
    a = [ph(i, minutes=i) for i in range(3)]
    late = [ph(10 + i, minutes=20 + i) for i in range(3)]  # 5분 초과
    far = [ph(20 + i, minutes=23 + i, gps=(37.60, 126.9780)) for i in range(3)]  # ~3.7km
    assert [len(g) for g in similar_groups(a + late + far)] == [3, 3, 3]


def test_no_gps_still_grouped_and_exclusions():
    nogps = [ph(i, minutes=i, gps=None) for i in range(3)]
    movie = ph(50, minutes=3, gps=None, is_movie=True)
    shot = ph(51, minutes=3, gps=None, is_screenshot=True)
    nohash = ph(52, minutes=3, gps=None, h=None)
    groups = similar_groups(nogps + [movie, shot, nohash])
    assert [[p.uuid for p in g] for g in groups] == [["u0", "u1", "u2"]]


def test_pair_is_not_a_group():
    assert similar_groups([ph(0), ph(1, minutes=1)]) == []


def test_best_prefers_favorite_then_score():
    assert best([ph(0, score=0.9), ph(1, score=0.5, favorite=True), ph(2, score=0.7)]).uuid == "u1"
    assert best([ph(0, score=0.2), ph(1, score=0.8)]).uuid == "u1"


def test_home_and_trips():
    at_home = [ph(i, minutes=i * 60 * 24) for i in range(10)]  # 서울 10일
    day = T0 + timedelta(days=20)
    jeju = [Photo(uuid=f"j{i}", date=day + timedelta(hours=i * 6), size=1, lat=JEJU[0], lon=JEJU[1], city="제주시")
            for i in range(6)]
    jeju_nogps = Photo(uuid="jn", date=day + timedelta(hours=7), size=1)
    after = ph(200, minutes=60 * 24 * 30)
    photos = at_home + jeju + [jeju_nogps, after]
    assert homes(photos) == [(37.6, 127.0)]  # 30일 미만 → 가장 많은 날 찍은 곳 하나
    (t,) = trips(photos)
    assert t["name"] == "제주시"
    assert {p.uuid for p in t["photos"]} == {f"j{i}" for i in range(6)} | {"jn"}


def test_two_homes_are_not_trips_but_big_trip_is():
    home_a = [ph(i, minutes=i * 60 * 24, gps=(36.32, 127.42)) for i in range(40)]  # 대전, 40일
    home_b = [ph(100 + i, minutes=(60 + i) * 60 * 24, gps=(35.17, 129.07)) for i in range(40)]  # 부산, 다른 40일
    day = T0 + timedelta(days=120)  # 파리: 사진은 많지만 3일뿐 → 집이 아니라 여행
    paris = [Photo(uuid=f"z{i}", date=day + timedelta(minutes=i * 7), size=1, lat=48.86, lon=2.35, city="Paris")
             for i in range(600)]
    hs = homes(home_a + home_b + paris)
    assert sorted(hs) == [(35.2, 129.1), (36.3, 127.4)]
    (t,) = trips(home_a + home_b + paris)
    assert t["name"] == "Paris" and len(t["photos"]) == 600


def test_short_trip_ignored_and_empty_library():
    photos = [ph(i, minutes=i) for i in range(5)] + [ph(9, minutes=60 * 24 * 3, gps=JEJU)]
    assert trips(photos) == []
    assert homes([]) == [] and trips([]) == [] and similar_groups([]) == []
    assert trips([ph(0, gps=None)]) == []


def test_places_and_size_by_year():
    photos = [ph(0, city="서울", country="대한민국"), ph(1, city="서울", country="대한민국"), ph(2, gps=None)]
    p = places(photos)
    assert (p[0]["country"], p[0]["city"], p[0]["count"], p[0]["size"]) == ("대한민국", "서울", 2, 2000)
    assert p[1]["country"] == "위치 정보 없음" and p[1]["lat"] is None
    assert size_by_year(photos) == {2026: 3000}


def test_params_change_grouping():
    series = [ph(i, minutes=i, h=1 << i) for i in range(6)]  # 이웃끼리 해밍 2
    assert [len(g) for g in similar_groups(series)] == [6]
    assert similar_groups(series, Params(hash_max=1)) == []  # 더 엄격 → 안 묶임
    assert similar_groups(series, Params(gap_min=0.5)) == []  # 1분 간격 > 30초
    assert [len(g) for g in similar_groups(series[:2], Params(min_group=2))] == [2]


def test_face_score():
    assert face_score([]) == (None, False, False)
    open_good, _, _ = face_score([(0.6, 2, 1)])  # 품질 좋음 · 눈 뜸 · 웃음
    closed, eyes_closed, _ = face_score([(0.6, 1, 0)])
    assert open_good == 1.0 and eyes_closed and closed < open_good
    unknown, _, _ = face_score([(-1.0, 0, 0)])  # 미분석 얼굴뿐 → 중간값
    assert 0.3 < unknown < 0.7
    # 품질 -1(배경의 작은 얼굴)은 유효한 얼굴이 있으면 무시
    assert face_score([(0.6, 2, 1), (-1.0, 1, 0)]) == (1.0, False, True)


def test_best_prefers_open_eyes_in_people_groups():
    blink = ph(0, score=0.9)
    blink.face, blink.eyes_closed = face_score([(0.5, 1, 0)])[:2]
    good = ph(1, score=0.3)
    good.face = face_score([(0.5, 2, 0)])[0]
    assert best([blink, good]).uuid == "u1"  # Apple 점수 높아도 눈 감으면 밀림
    assert rank(good, people=False) == 0.3  # 풍경 묶음 → Apple 점수 그대로
