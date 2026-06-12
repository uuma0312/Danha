# tools/make_logo_ico.py — danha_logo_final.svg → danha_logo.ico
#
# 외부 SVG 렌더러(cairosvg/inkscape) 없이 Pillow만으로 변환한다.
# 로고는 단순한 cubic bezier 패스 4개(개나리 꽃잎)라 직접 샘플링해서
# 폴리곤으로 그려도 충분히 정확함. 배경은 아이콘답게 투명 처리.
#
# 사용: python tools/make_logo_ico.py
from PIL import Image, ImageDraw

# danha_logo_final.svg의 패스 (M 시작점 + C 세그먼트들), fill 색
PETALS = [
    # (시작점, [C 세그먼트 (c1, c2, end)], fill)
    ((330, 315), [
        ((310, 285), (265, 235), (245, 175)),
        ((232, 135), (248, 105), (262, 98)),
        ((272, 93), (285, 100), (295, 118)),
        ((315, 152), (320, 210), (330, 315)),
    ], "#FFD700"),
    ((348, 325), [
        ((375, 300), (430, 270), (490, 268)),
        ((528, 267), (548, 283), (552, 298)),
        ((555, 310), (548, 328), (530, 338)),
        ((498, 354), (440, 345), (348, 325)),
    ], "#FFE030"),
    ((328, 348), [
        ((308, 375), (272, 418), (248, 465)),
        ((230, 500), (235, 528), (248, 538)),
        ((258, 546), (275, 542), (290, 528)),
        ((318, 502), (332, 455), (328, 348)),
    ], "#FFCF00"),
    ((352, 350), [
        ((372, 378), (405, 415), (428, 448)),
        ((445, 472), (442, 495), (430, 504)),
        ((420, 511), (405, 508), (392, 494)),
        ((368, 465), (352, 410), (352, 350)),
    ], "#FFD820"),
]
STROKE = "#C8A000"
STROKE_W_SVG = 2.0  # SVG 좌표계 기준 stroke-width

SAMPLES = 80  # cubic 세그먼트당 샘플 수


def cubic(p0, c1, c2, p1, t):
    mt = 1.0 - t
    x = mt**3 * p0[0] + 3 * mt**2 * t * c1[0] + 3 * mt * t**2 * c2[0] + t**3 * p1[0]
    y = mt**3 * p0[1] + 3 * mt**2 * t * c1[1] + 3 * mt * t**2 * c2[1] + t**3 * p1[1]
    return (x, y)


def petal_points(start, segs):
    pts = [start]
    cur = start
    for c1, c2, end in segs:
        for i in range(1, SAMPLES + 1):
            pts.append(cubic(cur, c1, c2, end, i / SAMPLES))
        cur = end
    return pts


def main():
    polys = [(petal_points(s, segs), fill) for s, segs, fill in PETALS]

    # 콘텐츠 bbox → 정사각 캔버스에 중앙 배치 (여백 6%)
    xs = [p[0] for poly, _ in polys for p in poly]
    ys = [p[1] for poly, _ in polys for p in poly]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    w, h = maxx - minx, maxy - miny
    side = max(w, h) * 1.12  # 양쪽 6% 여백
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2

    CANVAS = 1024
    SS = 4  # 슈퍼샘플링 배수
    big = CANVAS * SS
    scale = big / side

    def tx(p):
        return ((p[0] - cx) * scale + big / 2, (p[1] - cy) * scale + big / 2)

    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    sw = max(1, round(STROKE_W_SVG * scale))
    for poly, fill in polys:
        tp = [tx(p) for p in poly]
        d.polygon(tp, fill=fill)
        d.line(tp + [tp[0]], fill=STROKE, width=sw, joint="curve")

    img = img.resize((CANVAS, CANVAS), Image.LANCZOS)
    img.save(
        "danha_logo.ico",
        sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)],
    )
    # 미리보기/문서용 PNG도 하나
    img.resize((256, 256), Image.LANCZOS).save("danha_logo_256.png")
    print("danha_logo.ico / danha_logo_256.png 생성 완료")


if __name__ == "__main__":
    main()
