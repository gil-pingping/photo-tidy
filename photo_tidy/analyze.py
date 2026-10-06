from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta

TRIP_GAP = timedelta(days=1)


@dataclass(frozen=True)
class Params:
    """UI ⚙️ 패널에서 조절하는 보정 노브."""
    # phash 64비트 해밍 거리. 실제 라이브러리(2만여 장) 경계 쌍 육안 보정: ≤22 같은 촬영, 24부터 오탐.
    hash_max: int = 22
    gap_min: float = 5  # 연속 촬영 간격(분)
    near_m: float = 100  # 같은 장소 거리(m), 둘 다 GPS 있을 때만
    min_group: int = 3
    trip_km: float = 50
    min_trip: int = 5
    home_days: int = 30  # 서로 다른 날 이만큼 찍은 ~10km 지역 = 집(여러 곳 가능)


DEFAULT = Params()


@dataclass
class Photo:
    uuid: str
    date: datetime
    size: int
    lat: float | None = None
    lon: float | None = None
    is_movie: bool = False
    is_screenshot: bool = False
    favorite: bool = False
    score: float = 0.0
    city: str | None = None
    country: str | None = None
    thumb: str | None = None
    preview: str | None = None  # 크게 보기용 (가장 큰 로컬 미리보기)
    hash: int | None = None
    face: float | None = None  # 얼굴 점수 0~1 (얼굴 없으면 None)
    eyes_closed: bool = False
    smile: bool = False
    has_original: bool = False  # 원본이 Mac에 있음 (영상 재생 준비가 몇 초면 됨)
    failure: float = 0.0  # Apple 실패작 점수 (음수일수록 어둡·흐림·실수로 찍힌 사진)
    burst_extra: bool = False  # iPhone 연사에서 대표가 아닌 나머지
    duration: float = 0.0  # 영상 길이(초)
    filename: str = ""  # 원본 파일명 (날짜 복원 단서)
    added: datetime | None = None  # 라이브러리에 들어온 시각
    has_exif: bool = False  # 카메라 정보 있음


def has_gps(p: Photo) -> bool:
    return p.lat is not None and p.lon is not None


def km(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    dlat, dlon = math.radians(b_lat - a_lat), math.radians(b_lon - a_lon)
    h = math.sin(dlat / 2) ** 2 + math.cos(math.radians(a_lat)) * math.cos(math.radians(b_lat)) * math.sin(dlon / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def _similar(a: Photo, b: Photo, pr: Params) -> bool:
    if b.date - a.date > timedelta(minutes=pr.gap_min):
        return False
    if has_gps(a) and has_gps(b) and km(a.lat, a.lon, b.lat, b.lon) * 1000 > pr.near_m:
        return False
    return (a.hash ^ b.hash).bit_count() <= pr.hash_max


def similar_groups(photos: list[Photo], pr: Params = DEFAULT) -> list[list[Photo]]:
    """날짜순으로 직전 사진과 비슷하면 이어 붙인다 (포즈가 조금씩 바뀌는 연속 촬영)."""
    cands = sorted((p for p in photos if not p.is_movie and not p.is_screenshot and p.hash is not None),
                   key=lambda p: p.date)
    groups, cur = [], []
    for p in cands:
        if cur and _similar(cur[-1], p, pr):
            cur.append(p)
            continue
        if len(cur) >= pr.min_group:
            groups.append(cur)
        cur = [p]
    if len(cur) >= pr.min_group:
        groups.append(cur)
    return groups


# 사람이 나온 묶음에서 얼굴 점수 비중. 나머지는 Apple 미적 점수(score.overall).
# overall은 "잘 찍힌 사진"이지 표정·눈 감음을 거의 안 봄 → 인물 연사에선 얼굴 쪽을 더 믿는다.
FACE_W = 0.65


def face_score(faces: list[tuple[float, int, int]]) -> tuple[float | None, bool, bool]:
    """faces: (quality, eye_state, has_smile) — Photos DB 값.
    quality -1=미계산(대개 배경의 작은 얼굴), eye_state 0=미상 1=감음 2=뜸 (실 라이브러리 분포로 확인).
    반환: (얼굴 평균 점수, 누군가 눈 감음, 누군가 웃음)."""
    if not faces:
        return None, False, False
    valid = [f for f in faces if f[0] >= 0] or faces

    def one(q: float, eye: int, smile: int) -> float:
        quality = min(q / 0.6, 1.0) if q >= 0 else 0.5
        # 눈 감음은 사실상 탈락 사유 → 눈 비중 크게
        return 0.5 * quality + 0.4 * {1: 0.0, 2: 1.0}.get(eye, 0.5) + 0.1 * bool(smile)

    return (round(sum(one(*f) for f in valid) / len(valid), 6),
            any(f[1] == 1 for f in valid), any(bool(f[2]) for f in valid))


def rank(p: Photo, people: bool) -> float:
    if not people:
        return p.score
    return (1 - FACE_W) * p.score + FACE_W * (p.face if p.face is not None else 0.0)


def best(group: list[Photo]) -> Photo:
    people = any(p.face is not None for p in group)
    return max(group, key=lambda p: (p.favorite, rank(p, people)))


def homes(photos: list[Photo], pr: Params = DEFAULT) -> list[tuple[float, float]]:
    """장수가 아니라 '찍은 날 수'로 판정 → 여행지에서 수백 장 찍어도 집이 되지 않음."""
    days: dict[tuple, set] = {}
    for p in photos:
        if has_gps(p):
            days.setdefault((round(p.lat, 1), round(p.lon, 1)), set()).add(p.date.date())
    found = [c for c, d in days.items() if len(d) >= pr.home_days]
    return found or ([max(days, key=lambda c: len(days[c]))] if days else [])


def trips(photos: list[Photo], pr: Params = DEFAULT) -> list[dict]:
    hs = homes(photos, pr)
    if not hs:
        return []
    ordered = sorted(photos, key=lambda p: p.date)
    dates = [p.date for p in ordered]

    def away(p: Photo) -> bool:
        return has_gps(p) and all(km(*h, p.lat, p.lon) >= pr.trip_km for h in hs)

    spans: list[list[datetime]] = []
    for p in filter(away, ordered):
        if spans and p.date - spans[-1][1] <= TRIP_GAP:
            spans[-1][1] = p.date
        else:
            spans.append([p.date, p.date])
    out = []
    for start, end in spans:
        window = ordered[bisect_left(dates, start):bisect_right(dates, end)]
        members = [p for p in window if not has_gps(p) or away(p)]
        if len(members) < pr.min_trip:
            continue
        cities = [c for c, _ in Counter(p.city for p in members if p.city).most_common(3)]
        out.append({"name": " · ".join(cities) or "여행", "start": start, "end": end, "photos": members})
    return out


def places(photos: list[Photo]) -> list[dict]:
    agg: dict[tuple, dict] = {}
    for p in photos:
        key = (p.country or "위치 정보 없음", p.city or "")
        a = agg.setdefault(key, {"country": key[0], "city": key[1], "count": 0, "size": 0,
                                 "lat": None, "lon": None, "uuids": []})
        a["count"] += 1
        a["size"] += p.size
        a["uuids"].append(p.uuid)
        if a["lat"] is None and has_gps(p):
            a["lat"], a["lon"] = p.lat, p.lon
    return sorted(agg.values(), key=lambda a: -a["count"])


def size_by_year(photos: list[Photo]) -> dict[int, int]:
    out: Counter = Counter()
    for p in photos:
        out[p.date.year] += p.size
    return dict(sorted(out.items()))
