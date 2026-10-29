#!/usr/bin/env python3
"""Galaxy the Bear teaches Middle C — full 60s, 1080p (1920x1080), free 2D pipeline.
Moat split: bear = model-made cutout puppet; the teaching keyboard, the exact Middle C
key highlight, the star-pointer tap, and the 261.626 Hz tone are ALL code-owned."""
import os, math, sys
from PIL import Image, ImageDraw, ImageFilter
sys.path.insert(0, os.path.dirname(__file__))
from kbd import draw_keyboard

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACC = os.environ.get("ACC_DIR", os.path.join(REPO_ROOT, ".autonomous-team", "acceptance-galaxy"))
OUT = os.environ.get("OUT_DIR", os.path.join(REPO_ROOT, ".render-out", "c-frames"))
os.makedirs(OUT, exist_ok=True)
RLO = int(os.environ.get("RENDER_LO", "-1")); RHI = int(os.environ.get("RENDER_HI", "-1"))
if RLO < 0:
    for f in os.listdir(OUT): os.remove(os.path.join(OUT, f))

W, H = 1920, 1080
FPS, DUR = 24, 60.0
def cl(x, a=0.0, b=1.0): return max(a, min(b, x))
def eo(x): return 1-(1-x)**3
def eio(x): return 2*x*x if x < 0.5 else 1-(-2*x+2)**2/2
def env(t, b, rise=0.12, fall=0.6):
    if t < b-rise or t > b+fall: return 0.0
    if t <= b: return 1-(1-(t-(b-rise))/rise)**3
    return (1-(t-b)/fall)**3

# ---- static base: sky + code-drawn glowing stage ----
sky = Image.open(f"{ACC}/sky-1080.png").convert("RGBA").resize((W, H), Image.LANCZOS)
BASE = sky.copy(); d = ImageDraw.Draw(BASE)
# glowing stage platform (layered ellipses)
scx, scy, srx, sry = 960, 945, 840, 140
glowlayer = Image.new("RGBA", (W, H), (0, 0, 0, 0)); gd = ImageDraw.Draw(glowlayer)
gd.ellipse([scx-srx-40, scy-sry-40, scx+srx+40, scy+sry+40], fill=(120, 90, 220, 90))
glowlayer = glowlayer.filter(ImageFilter.GaussianBlur(40)); BASE.alpha_composite(glowlayer)
d.ellipse([scx-srx, scy-sry, scx+srx, scy+sry], fill=(46, 34, 84, 255), outline=(150, 120, 255, 255), width=6)
d.ellipse([scx-srx+10, scy-sry+8, scx+srx-10, scy-sry+60], fill=(86, 64, 150, 130))

# ---- code-owned teaching keyboard (cache base + highlighted) ----
KW, NW, MIDC = 70, 15, 7
KBD = draw_keyboard(n_white=NW, kw=KW, kh=300, midc_white=MIDC, highlight=None, label="Middle C")
KBD_HI = draw_keyboard(n_white=NW, kw=KW, kh=300, midc_white=MIDC, highlight=MIDC, press=1.0, label="Middle C", glow=0.0)
KBW, KBH = KBD.size
KBX, KBY = (W-KBW)//2 + 120, 656         # shifted right; bear sits at left
MIDC_CX = KBX + 16 + MIDC*KW + KW//2     # absolute x of Middle C key center
KEY_TOP = KBY + 16; KEY_CY = KEY_TOP + int(300*0.5)

# ---- bear cutout puppet ----
bear0 = Image.open(f"{ACC}/bear-cutout.png").convert("RGBA")
BEAR_H = 600; BEAR = bear0.resize((int(bear0.width*BEAR_H/bear0.height), BEAR_H), Image.LANCZOS)
BEAR_CX, BEAR_BOTTOM = 430, 1000

# ---- glow disc + sparkle + star pointer ----
_g = Image.new("RGBA", (400, 400), (0, 0, 0, 0)); ImageDraw.Draw(_g).ellipse([80, 80, 320, 320], fill=(255, 235, 150, 255))
GLOW = _g.filter(ImageFilter.GaussianBlur(60))
def make_sparkle(s):
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0)); dd = ImageDraw.Draw(im); c = s/2
    dd.polygon([(c, 0), (c+s*0.08, c), (c, s), (c-s*0.08, c)], fill=(255, 250, 225, 255))
    dd.polygon([(0, c), (c, c-s*0.08), (s, c), (c, c+s*0.08)], fill=(255, 250, 225, 255))
    dd.ellipse([c-s*0.1, c-s*0.1, c+s*0.1, c+s*0.1], fill=(255, 255, 255, 255))
    return im
SP = make_sparkle(60)
def make_star(s, col=(255, 90, 200)):
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0)); dd = ImageDraw.Draw(im); c = s/2
    pts = []
    for k in range(10):
        ang = -math.pi/2 + k*math.pi/5; r = c*(0.95 if k % 2 == 0 else 0.42)
        pts.append((c+r*math.cos(ang), c+r*math.sin(ang)))
    dd.polygon(pts, fill=col, outline=(255, 220, 255));
    dd.ellipse([c-6, c-6, c+6, c+6], fill=(255, 255, 255))
    return im
STAR = make_star(72)
SAT_SPARKS = [(-0.55, -0.45, 0.0), (0.5, -0.5, 1.1), (-0.6, 0.1, 2.2), (0.58, 0.05, 3.0), (0.0, -0.66, 3.8)]

# ---- timeline ----
PRESS_TIMES = [27.85, 31.36, 31.85, 32.20, 32.61, 40.55, 49.8]   # tones land here (synced to VO)
def press_amt(t): return max((env(t, b) for b in PRESS_TIMES), default=0.0)
def attention(t):
    g = 0.0
    if 17.5 <= t < 23.0: g = 0.45+0.45*math.sin((t-17.5)*3.0)        # reveal "there it is"
    if 43.5 <= t < 49.5: g = max(g, 0.35+0.35*math.sin((t-43.5)*3.5))  # "your turn" invite
    return g
def pointer_visible(t): return t >= 16.0   # star pointer appears at the find-Middle-C section

frames = int(DUR*FPS)
lo = RLO if RLO >= 0 else 0
hi = RHI if RHI >= 0 else frames
for fi in range(lo, hi):
    t = fi/FPS
    frame = BASE.copy()
    pa = press_amt(t); att = attention(t); keyglow = max(pa, att)

    # bear: slide-in (0-1.5), idle bob+sway, lean toward keyboard on press, celebrate bounce
    intro = cl(t/1.5)
    by = int(BEAR_BOTTOM + (1-eo(intro))*420)
    bob = 7*math.sin(t*2.0)
    sway = 3*math.sin(t*0.8) - 6*pa            # lean right toward the keyboard when pressing
    cele = 0.0
    if t >= 56.9:
        p = cl((t-56.9)/3.1); cele = abs(math.sin(p*math.pi*3))*(1-p)*90
    b = BEAR.rotate(sway, expand=True, resample=Image.BICUBIC)
    frame.alpha_composite(b, (BEAR_CX-b.width//2, int(by+bob-cele)-b.height))

    # bear sparkles
    for (dx, dy, ph) in SAT_SPARKS:
        tw = max(0.0, math.sin(t*3.0+ph))
        if tw <= 0.05: continue
        sz = int(60*(0.5+0.5*tw)); sp = SP.resize((sz, sz)).copy()
        sp.putalpha(sp.getchannel("A").point(lambda a: int(a*tw)))
        frame.alpha_composite(sp, (int(BEAR_CX+dx*BEAR_H-sz/2), int(by+bob-BEAR_H*0.5+dy*BEAR_H-sz/2)))

    # keyboard (cached base) + highlight overlay on press/attention
    frame.alpha_composite(KBD, (KBX, KBY))
    if keyglow > 0.01:
        # glow halo behind/over the Middle C key
        gs = int(360*(0.7+0.5*keyglow)); g = GLOW.resize((gs, gs)).copy()
        g.putalpha(g.getchannel("A").point(lambda a: int(a*cl(keyglow))))
        frame.alpha_composite(g, (MIDC_CX-gs//2, KEY_CY-gs//2))
        hi_img = KBD_HI.copy()
        hi_img.putalpha(hi_img.getchannel("A").point(lambda a: int(a*cl(keyglow))))
        frame.alpha_composite(hi_img, (KBX, KBY))

    # star pointer: bobs above Middle C, dips down to tap on press
    if pointer_visible(t):
        appear = cl((t-16.0)/0.5)
        base_y = KEY_TOP - 90 + 12*math.sin(t*3.0)
        tap_y = base_y + pa*150
        st = STAR.copy()
        if appear < 1.0:
            st = st.copy(); st.putalpha(st.getchannel("A").point(lambda a: int(a*appear)))
        # little tail/handle implied by rotation
        st = st.rotate(-20, expand=True, resample=Image.BICUBIC)
        frame.alpha_composite(st, (MIDC_CX-st.width//2, int(tap_y)-st.height//2))
        # tap flash ring
        if pa > 0.25:
            r = int(40+60*pa); ring = Image.new("RGBA", (r*2, r*2), (0, 0, 0, 0))
            ImageDraw.Draw(ring).ellipse([4, 4, r*2-4, r*2-4], outline=(255, 240, 180, int(220*(1-pa))), width=8)
            frame.alpha_composite(ring, (MIDC_CX-r, KEY_CY-r))

    # celebration sparkle burst
    if t >= 56.9:
        for k in range(6):
            ph = k*1.0; tw = max(0.0, math.sin(t*5+ph)); sz = int(70*tw)
            if sz < 8: continue
            ang = k*1.05
            frame.alpha_composite(SP.resize((sz, sz)),
                                  (int(960+math.cos(ang)*260-sz/2), int(520+math.sin(ang)*200-sz/2)))

    # fade out 58.5-60
    if t >= 58.5:
        f = cl((t-58.5)/1.5)
        frame.alpha_composite(Image.new("RGBA", (W, H), (0, 0, 0, int(255*f))))

    frame.convert("RGB").save(f"{OUT}/c-{fi:04d}.png", compress_level=1)

print(f"C_DONE frames={frames} {W}x{H} midc_cx={MIDC_CX}")
