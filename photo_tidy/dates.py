"""촬영 날짜 복원 — 클라우드(네이버 MYBOX 등)에서 받아 날짜가 '가져온 날'로 몰린 사진.

1) 파일명에 촬영 시각이 있으면 그대로 (20170819_161549 · Screenshot_2017-08-19-16-15-49 · KakaoTalk_Photo_2024-09-20-13-18-38).
   단 그 시각이 가져온 시각과 같으면(MYBOX 파일명의 '받은 시각') 단서가 아님.
2) MYBOX 파일명 '<파일번호>_<받은시각 17자리>': 파일번호는 올린 순서 → 번호 앞뒤의 날짜 아는 사진 사이를 선형 보간.
   아래쪽 경계 = 최근 4개 중 가장 늦은 날짜, 위쪽 경계 = 그보다 이르지 않은 첫 3개 중 가장 이른 날짜
   (옛 사진을 늦게 올린 앵커를 건너뜀). 실측 3,374장: 하루 이내 90%, 연속 촬영분을 통째로 숨긴 엄격 검증에서 31일 이내 92%.
3) 등급 = 창 폭: day ≤3일, month ≤45일, season ≤120일, 나머지 year. 파일명 시각은 exact.
적용은 PhotoKit(library.set_dates). 원래 날짜는 JSON에 백업하고 undo로 복원.
"""
from __future__ import annotations

import bisect
import csv
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

TIERS = ("exact", "day", "month", "season", "year", "unknown")
_FN_DATE = re.compile(r"(?<!\d)(19\d\d|20\d\d)[-_.]?(\d\d)[-_.]?(\d\d)(?:[-_ T.]?(\d\d)[-_.:]?(\d\d)[-_.:]?(\d\d))?")
_NAVER = re.compile(r"^(\d{8,})_\d{17}\.")


@dataclass
class Item:
    uuid: str
    filename: str
    date: datetime  # 사진 앱이 보여주는 날짜 (시간대 포함)
    added: datetime  # 라이브러리에 들어온 시각
    has_exif: bool = False  # 카메라 정보 있음 = 그 날짜는 진짜 촬영 시각
    screenshot: bool = False  # 스크린샷은 캡처 시각이 곧 날짜


@dataclass
class Estimate:
    date: datetime | None
    lo: datetime | None
    hi: datetime | None
    tier: str
    method: str


def filename_date(name: str) -> datetime | None:
    """파일명 속 날짜(+시각). 시간대 없는 값 — 호출 쪽에서 사진의 시간대를 붙임."""
    for m in _FN_DATE.finditer(name):
        y, mo, d, h, mi, s = (int(x) if x else 0 for x in m.groups())
        try:
            return datetime(y, mo, d, h, mi, s)
        except ValueError:
            continue
    return None


def naver_id(name: str) -> int | None:
    m = _NAVER.match(name)
    return int(m.group(1)) if m else None


def is_stuck(it: Item) -> bool:
    """날짜가 '가져온 시각'이고 카메라 정보가 없음 → 촬영 날짜를 잃은 사진."""
    return abs(it.date - it.added) < timedelta(days=1) and not it.has_exif and not it.screenshot


def tier(width: timedelta) -> str:
    days = width.total_seconds() / 86400
    return "day" if days <= 3 else "month" if days <= 45 else "season" if days <= 120 else "year"


def _interpolate(x: int, lst: list[tuple[int, datetime]]) -> Estimate | None:
    ids = [a[0] for a in lst]
    j = bisect.bisect(ids, x)
    below, above = lst[max(0, j - 4):j], lst[j:j + 60]
    if not below:
        return None
    lo_a = max(below, key=lambda a: a[1])
    consistent = [a for a in above if a[1] >= lo_a[1]]  # 더 이른 날짜 = 옛 사진을 늦게 올림 → 경계 아님
    if not consistent:
        return None
    hi_a = min(consistent[:3], key=lambda a: a[1])
    f = (x - lo_a[0]) / (hi_a[0] - lo_a[0]) if hi_a[0] != lo_a[0] else 0.5
    pt = lo_a[1] + (hi_a[1] - lo_a[1]) * min(max(f, 0.0), 1.0)
    return Estimate(pt, lo_a[1], hi_a[1], tier(hi_a[1] - lo_a[1]), "sequence")


def estimate(items: list[Item]) -> dict[str, Estimate]:
    """날짜를 잃은 사진마다 추정. 날짜 있는 사진은 결과에 없음."""
    stuck = [it for it in items if is_stuck(it)]
    anchors: dict[int, list[tuple[int, datetime]]] = defaultdict(list)
    for it in items:
        nid = naver_id(it.filename)
        if nid is not None and not is_stuck(it):
            anchors[len(str(nid))].append((nid, it.date))  # 자릿수가 다르면 다른 번호 체계
    out: dict[str, Estimate] = {}
    for it in stuck:
        fd = filename_date(it.filename) if naver_id(it.filename) is None else None  # MYBOX 이름의 17자리는 받은 시각
        if fd is None:
            continue
        fd = fd.replace(tzinfo=it.date.tzinfo)
        if abs(fd - it.added) >= timedelta(days=1):
            out[it.uuid] = Estimate(fd, fd, fd, "exact", "filename")
            nid = naver_id(it.filename)
            if nid is not None:
                anchors[len(str(nid))].append((nid, fd))
    for lst in anchors.values():
        lst.sort()
    for it in stuck:
        if it.uuid in out:
            continue
        nid = naver_id(it.filename)
        est = _interpolate(nid, anchors[len(str(nid))]) if nid is not None else None
        out[it.uuid] = est or Estimate(None, None, None, "unknown", "none")
    return out


def items_from(photos) -> list[Item]:
    return [Item(p.uuid, p.filename, p.date, p.added or p.date, p.has_exif, p.is_screenshot) for p in photos]


def report(est: dict[str, Estimate], items: list[Item], csv_path: Path) -> str:
    by = {it.uuid: it for it in items}
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["uuid", "filename", "current", "estimate", "lo", "hi", "tier", "method"])
        for u, e in sorted(est.items(), key=lambda kv: TIERS.index(kv[1].tier)):
            it = by[u]
            w.writerow([u, it.filename, it.date.isoformat(), *(x.isoformat() if x else "" for x in (e.date, e.lo, e.hi)), e.tier, e.method])
    c = Counter(e.tier for e in est.values())
    lines = [f"날짜가 '가져온 날'로 된 사진 {len(est)}장 — 복원 가능 {len(est) - c['unknown']}장, 단서 없음 {c['unknown']}장 (저장·받은 이미지 등)"]
    lines += [f"  {t:8} {c[t]:5}장" for t in TIERS if c[t] and t != "unknown"]
    lines.append(f"→ 사진별 계획: {csv_path}")
    return "\n".join(lines)


def apply(items: list[Item], est: dict[str, Estimate], tiers: set[str], backup: Path, set_dates) -> int:
    """등급이 tiers에 드는 사진의 날짜를 바꿈. 원래 날짜는 backup에 (처음 값만) 보관."""
    orig = json.loads(backup.read_text()) if backup.exists() else {}
    todo = {}
    for it in items:
        e = est.get(it.uuid)
        if e and e.tier in tiers and e.date is not None:
            todo[it.uuid] = e.date
            orig.setdefault(it.uuid, it.date.isoformat())
    if not todo:
        return 0
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(json.dumps(orig))
    return set_dates(todo)


def undo(backup: Path, set_dates) -> int:
    orig = json.loads(backup.read_text()) if backup.exists() else {}
    return set_dates({u: datetime.fromisoformat(s) for u, s in orig.items()}) if orig else 0
