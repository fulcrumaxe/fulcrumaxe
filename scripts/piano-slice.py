#!/usr/bin/env python3
"""Piano Planet 12s vertical slice (0-12s): Welcome + Meet One Quarter Note.
Free 2D pipeline: code-owned satellite puppet + code-composited quarter note + glow +
sparkles + camera push-in. Renders frames; audio is muxed separately."""
import os, math
from PIL import Image, ImageDraw, ImageFilter

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACC = os.environ.get("ACC_DIR", os.path.join(REPO_ROOT, ".autonomous-team", "acceptance-piano"))
OUT = os.environ.get("OUT_DIR", os.path.join(REPO_ROOT, ".render-out", "piano-frames"))
os.makedirs(OUT, exist_ok=True)
# partial re-render support: RENDER_LO/RENDER_HI (frame indices). If unset, full render (clears dir).
RLO = int(os.environ.get("RENDER_LO", "-1")); RHI = int(os.environ.get("RENDER_HI", "-1"))
if RLO < 0:
    for f in os.listdir(OUT): os.remove(os.path.join(OUT, f))

FPS, DUR = 24, 12.0
def ease_out(x): return 1-(1-x)**3
def ease_io(x): return 2*x*x if x<0.5 else 1-(-2*x+2)**2/2
def clamp(x,a=0.0,b=1.0): return max(a,min(b,x))

bg = Image.open(f"{ACC}/mercury-bg.png").convert("RGBA")
W,H = bg.size  # 1024x1024
sat0 = Image.open(f"{ACC}/satellite-cutout.png").convert("RGBA")
SAT_H = int(H*0.52); sat_base = sat0.resize((int(sat0.width*SAT_H/sat0.height), SAT_H), Image.LANCZOS)
note0 = Image.open(f"{ACC}/note-quarter-cut.png").convert("RGBA")
NOTE_H = int(H*0.40); note_base = note0.resize((int(note0.width*NOTE_H/note0.height), NOTE_H), Image.LANCZOS)

# sparkle sprite (4-point star) cached
def make_sparkle(size):
    s = Image.new("RGBA",(size,size),(0,0,0,0)); d=ImageDraw.Draw(s); c=size/2
    for (lx,ly) in [(c,0),(size,c),(c,size),(0,c)]:
        d.polygon([(c,c),( (c+lx)/2, (c+ly)/2 - (ly-c)*0.0 ),(lx,ly)],fill=(255,244,200,255))
    d.polygon([(c,0),(c+size*0.09,c),(c,size),(c-size*0.09,c)],fill=(255,250,225,255))
    d.polygon([(0,c),(c,c-size*0.09),(size,c),(c,c+size*0.09)],fill=(255,250,225,255))
    d.ellipse([c-size*0.10,c-size*0.10,c+size*0.10,c+size*0.10],fill=(255,255,255,255))
    return s
SP = make_sparkle(70)
# sparkle anchors relative to satellite center (dx,dy as fraction of SAT_H), phase
SPARKS = [(-0.62,-0.40,0.0),(0.66,-0.30,0.7),(-0.70,0.25,1.4),(0.72,0.30,2.1),(0.05,-0.66,2.8),(-0.30,0.60,3.5)]

note_cx, note_cy = int(W*0.66), int(H*0.52)

frames = int(DUR*FPS)
lo = RLO if RLO >= 0 else 0
hi = RHI if RHI >= 0 else frames
for fi in range(lo, hi):
    t = fi/FPS
    frame = bg.copy()
    dr = ImageDraw.Draw(frame)

    # --- satellite float-in (0-2.2s) then hover; tilt toward note (5-12s) ---
    intro = clamp(t/2.2)
    sat_cx = W//2
    sat_cy = int(H*1.15 + (H*0.40 - H*1.15)*ease_out(intro))  # rises from below to y~0.40
    bob = 8*math.sin(t*2.0)
    tilt = 9*ease_io(clamp((t-5.0)/1.8))  # lean toward the note after it appears
    s = sat_base.rotate(tilt, expand=True, resample=Image.BICUBIC)
    frame.alpha_composite(s, (sat_cx - s.width//2, int(sat_cy+bob) - s.height//2))

    # --- sparkles around satellite (twinkle) ---
    for (dx,dy,ph) in SPARKS:
        tw = max(0.0, math.sin(t*3.0+ph))
        if tw <= 0.02: continue
        sz = int(70*(0.5+0.5*tw))
        sp = SP.resize((sz,sz)).copy()
        sp.putalpha(sp.getchannel("A").point(lambda a: int(a*tw)))
        x = sat_cx + int(dx*SAT_H) - sz//2
        y = int(sat_cy+bob) + int(dy*SAT_H) - sz//2
        frame.alpha_composite(sp,(x,y))

    # --- quarter note appears (5-6.2s): fade + scale-in, soft pulsing glow ---
    if t >= 5.0:
        ap = clamp((t-5.0)/1.2)
        sc = 0.7 + 0.3*ease_out(ap)
        nw,nh = int(note_base.width*sc), int(note_base.height*sc)
        glowpulse = 0.45 + 0.25*math.sin((t-5.0)*2.2)
        # glow halo
        gl = Image.new("RGBA",(int(nh*1.6),int(nh*1.6)),(0,0,0,0))
        ImageDraw.Draw(gl).ellipse([0,0,gl.width,gl.height],fill=(255,205,90,int(150*ap*glowpulse)))
        gl = gl.filter(ImageFilter.GaussianBlur(nh*0.18))
        frame.alpha_composite(gl,(note_cx-gl.width//2, note_cy-gl.height//2))
        nn = note_base.resize((nw,nh), Image.LANCZOS).copy()
        nn.putalpha(nn.getchannel("A").point(lambda a: int(a*ap)))
        frame.alpha_composite(nn,(note_cx-nw//2, note_cy-nh//2))

    # --- camera push-in (5-12s): zoom 1.0 -> 1.12, centered toward the note ---
    z = 1.0 + 0.12*ease_io(clamp((t-5.0)/7.0))
    if z > 1.001:
        cw,ch = int(W/z), int(H/z)
        # bias the crop center slightly toward the note
        ccx = int(W*0.5 + (note_cx-W*0.5)*0.25); ccy = int(H*0.5 + (note_cy-H*0.5)*0.25)
        l = clamp(ccx-cw//2,0,W-cw); top = clamp(ccy-ch//2,0,H-ch)
        frame = frame.crop((int(l),int(top),int(l)+cw,int(top)+ch)).resize((W,H), Image.LANCZOS)

    frame.convert("RGB").save(f"{OUT}/p-{fi:04d}.png", compress_level=1)

print(f"PIANO_DONE frames={frames} {W}x{H}")
