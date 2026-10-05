"""합성 라이브러리로 UI 확인: uv run python tests/demo.py → http://localhost:8799"""
import random
import tempfile
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

from PIL import Image, ImageDraw

from photo_tidy.analyze import Photo
from photo_tidy.library import add_hashes
from photo_tidy.server import App, make_handler

tmp = Path(tempfile.mkdtemp())
rnd = random.Random(1)


def scene(name, sky, ground, x):
    img = Image.new("RGB", (360, 360), sky)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 220, 360, 360], fill=ground)
    d.ellipse([x, 120, x + 50, 170], fill="#f2c9a0")  # 얼굴
    d.rectangle([x - 10, 170, x + 60, 300], fill="#264653")  # 몸
    path = tmp / f"{name}.jpg"
    img.save(path)
    return str(path)


photos = []


def add(uuid, date, lat, lon, city, thumb=None, **kw):
    photos.append(Photo(uuid=uuid, date=date, size=rnd.randint(2, 6) * 1_000_000, lat=lat, lon=lon,
                        city=city, country="대한민국", score=rnd.random(), thumb=thumb, **kw))


t = datetime(2026, 3, 1, 10)
for i in range(30):  # 집(서울) 일상
    add(f"home{i}", t + timedelta(days=i), 37.5665, 126.978, "서울", scene(f"home{i}", "#9bb", "#777", rnd.randint(20, 280)))
jeju = datetime(2026, 7, 10, 15)
for i in range(14):  # 협재 포즈 연속
    add(f"jeju{i}", jeju + timedelta(seconds=20 * i), 33.394, 126.239, "제주시",
        scene(f"jeju{i}", "#4aa3df", "#e9d8a6", 140 + rnd.randint(-12, 12)), favorite=(i == 5))
for i in range(6):  # 제주 다른 날
    add(f"jejud{i}", jeju + timedelta(days=1, hours=i), 33.25, 126.56, "서귀포시",
        scene(f"jejud{i}", "#2a9d8f", "#3d5a40", rnd.randint(20, 280)))
busan = datetime(2026, 9, 2, 18)
for i in range(8):  # 광안리 노을
    add(f"busan{i}", busan + timedelta(seconds=30 * i), 35.153, 129.118, "부산",
        scene(f"busan{i}", "#f4a261", "#264653", 60 + rnd.randint(-10, 10)))
add("movie0", jeju + timedelta(hours=3), 33.394, 126.239, "제주시", is_movie=True)
photos[-1].size = 480_000_000

for p in photos:
    p.preview = p.thumb  # 데모는 썸네일 = 크게 보기
add_hashes(photos, tmp / "hashes.json")
album: list[str] = []  # 가짜 Photos 앨범


def fake_delete(uuids):
    gone = list(uuids)
    album.clear()
    return gone


app = App(photos, album.extend, tmp / "state.json", settings_file=tmp / "settings.json",
          list_album=lambda: list(album), delete=fake_delete,
          remove_from_album=lambda uuids: len([album.remove(u) for u in uuids if u in album]))
print(f"묶음 {len(app.groups)}개, 여행 {len(app.trips)}개 → http://localhost:8799")
ThreadingHTTPServer(("127.0.0.1", 8799), make_handler(app)).serve_forever()
