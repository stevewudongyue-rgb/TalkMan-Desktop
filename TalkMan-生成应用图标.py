"""离线生成应用图标。仅构建素材时需要 Pillow，运行程序无需 Pillow。"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
DEST = ROOT / 'TalkMan-assets' / 'app'


def draw_mark(size, *, tile=True, ink='#f5f5f5'):
    scale = 8
    image = Image.new('RGBA', (size * scale, size * scale))
    d = ImageDraw.Draw(image)
    unit = size * scale / 100
    box = lambda *v: tuple(round(x * unit) for x in v)
    if tile:
        d.rounded_rectangle(box(4, 4, 96, 96), radius=round(21 * unit), fill='#111111')
        d.rounded_rectangle(box(4, 4, 96, 96), radius=round(21 * unit), outline='#505050', width=max(1, round(unit)))
    # 小图保留同一个麦克风轮廓；不把细小的光标装饰硬缩进去。
    small = size <= 24
    cx = 50 if small else 43
    line = round((6 if small else 5) * unit)
    d.rounded_rectangle(box(cx-10, 22, cx+10, 55), radius=round(10 * unit), outline=ink, width=line)
    d.arc(box(cx-19, 30, cx+19, 67), 0, 180, fill=ink, width=line)
    d.line(box(cx-19, 42, cx-19, 49), fill=ink, width=line)
    d.line(box(cx+19, 42, cx+19, 49), fill=ink, width=line)
    d.line(box(cx, 65, cx, 77), fill=ink, width=line)
    d.line(box(cx-10, 77, cx+10, 77), fill=ink, width=line)
    if not small:
        d.line(box(75, 26, 75, 73), fill=ink, width=line)
        d.line(box(69, 26, 81, 26), fill=ink, width=line)
        d.line(box(69, 73, 81, 73), fill=ink, width=line)
    return image.resize((size, size), Image.Resampling.LANCZOS)


def main():
    DEST.mkdir(parents=True, exist_ok=True)
    sizes = (16, 20, 24, 32, 40, 48, 64, 128, 256, 512, 1024)
    images = {s: draw_mark(s) for s in sizes}
    for size, image in images.items():
        image.save(DEST / f'TalkMan-{size}.png')
    images[256].save(DEST / 'TalkMan.ico', sizes=[(s, s) for s in sizes if s <= 256],
                     append_images=[images[s] for s in sizes if s < 256])
    images[1024].save(DEST / 'TalkMan.icns', append_images=[images[s] for s in sizes if 16 <= s < 1024])
    # 菜单栏使用透明单色模板，系统可按背景自动反色。
    for size in (18, 36):
        draw_mark(18 if size == 18 else 24, tile=False, ink='#000000').resize((size, size), Image.Resampling.LANCZOS).save(DEST / f'TalkMan-menu-{size}.png')
    (DEST / 'TalkMan.svg').write_text('''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><title>Talk Man：语音与文字光标</title><rect x="4" y="4" width="92" height="92" rx="21" fill="#111" stroke="#505050"/><g fill="none" stroke="#f5f5f5" stroke-width="5"><rect x="33" y="22" width="20" height="33" rx="10"/><path d="M24 42v7a19 18 0 0 0 38 0v-7M43 65v12m-10 0h20M75 26v47m-6-47h12m-12 47h12"/></g></svg>''', encoding='utf-8')
    # 在不同背景及小尺寸上核对识别度，避免只验收大图。
    sheet = Image.new('RGB', (900, 380), '#eeeeee')
    canvas = ImageDraw.Draw(sheet)
    font = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 18) if Path('C:/Windows/Fonts/msyh.ttc').exists() else ImageFont.load_default()
    for row, bg in enumerate(('#eeeeee', '#111111')):
        canvas.rectangle((0, row*190, 900, (row+1)*190), fill=bg)
        canvas.text((24, row*190+20), 'Talk Man · 应用图标 / 系统小图标', font=font, fill='#111111' if row == 0 else '#eeeeee')
        for x, size in zip((24, 210, 310, 390, 460, 520, 580), (128, 64, 48, 32, 24, 20, 16)):
            sheet.paste(images[size], (x, row*190+54), images[size])
        menu = draw_mark(24, tile=False, ink='#111111' if row == 0 else '#ffffff')
        sheet.paste(menu, (675, row*190+54), menu)
    sheet.save(ROOT.parent / 'TalkMan-应用图标预览.png')


if __name__ == '__main__':
    main()
