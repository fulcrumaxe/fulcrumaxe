#!/usr/bin/env python3
"""2D-puppet animation of the real satellite cutout (PIL). Code owns the exact pixels
AND the motion: leans toward each gold note as it pops on beat (L->R), then bounces."""
import os, math
from PIL import Image

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACC = os.environ.get("ACC_DIR", os.path.join(REPO_ROOT, ".autonomous-team", "acceptance-349b"))
OUTDIR = os.environ.get("OUT_DIR", os.path.join(REPO_ROOT, ".render-out", "pframes"))
os.makedirs(OUTDIR, exist_ok=True)
# partial re-render support: RENDER_LO/RENDER_HI (frame indices). If unset, full render (clears dir).
RLO = int(os.environ.get("RENDER_LO", "-1")); RHI = int(os.environ.get("RENDER_HI", "-1"))
if RLO < 0:
    for f in os.listdir(OUTDIR):
        os.remove(os.path.join(OUTDIR, f))

BEATS = [0.673, 1.347, 1.974, 2.647]
FPS, DUR = 24, 4.8

def ease_in_out(x): return 2*x*x if x < 0.5 else 1 - (-2*x+2)**2/2
def kf(t, ks):
    if t <= ks[0][0]: return ks[0][1]
    if t >= ks[-1][0]: return ks[-1][1]
    for i in range(len(ks)-1):
        t0,v0 = ks[i]; t1,v1 = ks[i+1]
        if t0 <= t <= t1:
            return v0 + (v1-v0)*ease_in_out((t-t0)/(t1-t0))
    return ks[-1][1]
def beat_pulse(t, b, rise=0.12, fall=0.42):
    if t < b-rise or t > b+fall: return 0.0
    if t <= b: return 1 - (1-(t-(b-rise))/rise)**3
    return (1-(t-b)/fall)**3

bg = Image.open(f"{ACC}/puppet-bg.png").convert("RGBA")
W, H = bg.size
sat0 = Image.open(f"{ACC}/satellite-cutout.png").convert("RGBA")
SAT_SCALE = 0.82
sat_base = sat0.resize((int(sat0.width*SAT_SCALE), int(sat0.height*SAT_SCALE)), Image.LANCZOS)
sat_cx, sat_cy = W//2, int(H*0.45)

notes = [Image.open(f"{ACC}/note-sprite-{i}.png").convert("RGBA") for i in range(4)]
note_h = int(H*0.12)
notes = [n.resize((int(n.width*note_h/n.height), note_h), Image.LANCZOS) for n in notes]
span = W*0.74; x0 = (W-span)/2 + span/8
def note_cx(i): return int(x0 + (span/4)*i)
row_y = int(H*0.84)

ROT_KF = [(0,0),(BEATS[0],-8),(BEATS[1],-3),(BEATS[2],3),(BEATS[3],8),(3.4,0),(DUR,0)]
frames = int(DUR*FPS)
lo = RLO if RLO >= 0 else 0
hi = RHI if RHI >= 0 else frames
for fi in range(lo, hi):
    t = fi/FPS
    rot = kf(t, ROT_KF)
    dy = 6*math.sin(t*3.2); squash = 1.0
    if t >= 3.4:
        p = max(0.0, min(1.0, (t-3.4)/1.2))
        bounce = abs(math.sin(p*math.pi*2))*(1-p)
        dy = -150*bounce; squash = 1 - 0.07*bounce
    dy += sum(beat_pulse(t,b)*14 for b in BEATS)  # nod dip on each beat

    frame = bg.copy()
    # satellite: vertical squash -> rotate (expand) -> paste centered at pivot
    s = sat_base
    if abs(squash-1) > 1e-3:
        s = s.resize((s.width, max(1,int(s.height*squash))), Image.LANCZOS)
    sr = s.rotate(rot, expand=True, resample=Image.BICUBIC)
    px = sat_cx - sr.width//2
    py = int(sat_cy + dy) - sr.height//2
    frame.alpha_composite(sr, (px, py))

    # notes pop on their beat
    for i, n in enumerate(notes):
        inten = beat_pulse(t, BEATS[i]); sc = 1 + 0.4*inten
        nn = n.resize((max(1,int(n.width*sc)), max(1,int(n.height*sc))), Image.LANCZOS) if sc != 1 else n
        frame.alpha_composite(nn, (note_cx(i)-nn.width//2, row_y-nn.height//2))

    frame.convert("RGB").save(f"{OUTDIR}/p-{fi:04d}.png", compress_level=1)

print(f"PUPPET_DONE frames={frames} {W}x{H}")
