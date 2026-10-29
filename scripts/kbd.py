#!/usr/bin/env python3
"""Code-drawn teaching piano keyboard (PIL). Owns the exact keys so Middle C is
pixel-exact + highlightable. draw_keyboard() returns an RGBA image."""
from PIL import Image, ImageDraw, ImageFont

# black-key pattern within an octave (after white index, mod 7): C D _ F G A _
_BLACK_AFTER = {0, 1, 3, 4, 5}

def _font(sz):
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try: return ImageFont.truetype(p, sz)
        except Exception: pass
    return ImageFont.load_default()

def draw_keyboard(n_white=15, kw=70, kh=300, midc_white=7,
                  highlight=None, press=0.0, label=None, glow=0.0):
    """n_white white keys; midc_white = white-key index that is Middle C.
    highlight = white-key index to light gold; press 0..1 pushes it down;
    glow 0..1 adds a halo; label drawn under the highlighted key."""
    pad = 16; W = n_white*kw + pad*2; H = kh + pad*2 + 48
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # keybed shadow / frame
    d.rounded_rectangle([pad-8, pad-8, W-pad+8, pad+kh+10], 14, fill=(28, 18, 46, 235))
    # white keys
    for i in range(n_white):
        x0 = pad + i*kw; x1 = x0 + kw - 2
        dy = int(press*10) if i == highlight else 0
        top = (255, 236, 150) if i == highlight else (250, 250, 252)
        bot = (245, 205, 70) if i == highlight else (222, 224, 230)
        # simple vertical shade
        for yy in range(kh):
            f = yy/kh
            c = tuple(int(top[k]+(bot[k]-top[k])*f) for k in range(3))
            d.line([(x0, pad+yy+dy), (x1, pad+yy+dy)], fill=c)
        d.rounded_rectangle([x0, pad+dy, x1, pad+kh+dy], 6, outline=(150, 150, 165), width=2)
        if i == midc_white:  # subtle dot marking middle C even when not highlighted
            d.ellipse([x0+kw/2-7, pad+kh-34, x0+kw/2+7, pad+kh-20],
                      fill=(255, 170, 60) if i != highlight else (180, 90, 0))
    # black keys (drawn on top)
    bw = int(kw*0.62); bh = int(kh*0.62)
    for i in range(n_white-1):
        if (i % 7) in _BLACK_AFTER:
            cx = pad + (i+1)*kw
            d.rounded_rectangle([cx-bw//2, pad, cx+bw//2, pad+bh], 5,
                                fill=(24, 22, 34), outline=(0, 0, 0), width=1)
            d.rounded_rectangle([cx-bw//2+3, pad+4, cx+bw//2-3, pad+bh-10], 4, fill=(46, 44, 60))
    # glow halo around highlighted key
    if highlight is not None and glow > 0.01:
        gx = pad + highlight*kw + kw//2
        gl = Image.new("RGBA", (kw*4, kw*4), (0, 0, 0, 0))
        ImageDraw.Draw(gl).ellipse([0, 0, kw*4, kw*4], fill=(255, 240, 180, int(120*glow)))
        gl = gl.filter(__import__("PIL.ImageFilter", fromlist=["ImageFilter"]).GaussianBlur(kw*0.6))
        img.alpha_composite(gl, (gx-kw*2, pad+kh//2-kw*2))
    # label under highlighted (or middle C) key
    if label:
        li = highlight if highlight is not None else midc_white
        lx = pad + li*kw + kw//2
        f = _font(30)
        tb = d.textbbox((0, 0), label, font=f)
        d.rounded_rectangle([lx-(tb[2]-tb[0])//2-10, pad+kh+12, lx+(tb[2]-tb[0])//2+10, pad+kh+50],
                            10, fill=(255, 196, 60, 255))
        d.text((lx-(tb[2]-tb[0])//2, pad+kh+14), label, font=f, fill=(40, 24, 0))
    return img

if __name__ == "__main__":
    import sys
    draw_keyboard(highlight=7, press=0.7, label="Middle C", glow=0.8).save(
        sys.argv[1] if len(sys.argv) > 1 else "/tmp/kbd.png")
    print("KBD_OK")
