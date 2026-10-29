#!/usr/bin/env python3
"""Piano Planet — Satellite Teaches Quarter Notes (full 60s). Free 2D pipeline:
code-owned satellite puppet + 4 identical code-composited quarter notes + glow + sparkles
+ camera moves. 8 sections per the script. Audio muxed separately."""
import os, math
from PIL import Image, ImageDraw, ImageFilter

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACC = os.environ.get("ACC_DIR", os.path.join(REPO_ROOT, ".autonomous-team", "acceptance-piano"))
OUT = os.environ.get("OUT_DIR", os.path.join(REPO_ROOT, ".render-out", "full-frames"))
os.makedirs(OUT, exist_ok=True)
# partial re-render support: RENDER_LO/RENDER_HI (frame indices). If unset, full render (clears dir).
RLO=int(os.environ.get("RENDER_LO","-1")); RHI=int(os.environ.get("RENDER_HI","-1"))
if RLO<0:
    for f in os.listdir(OUT): os.remove(os.path.join(OUT, f))

FPS, DUR = 24, 60.0
def eo(x): return 1-(1-x)**3
def eio(x): return 2*x*x if x<0.5 else 1-(-2*x+2)**2/2
def cl(x,a=0.0,b=1.0): return max(a,min(b,x))
def kf(t,ks):
    if t<=ks[0][0]: return ks[0][1]
    if t>=ks[-1][0]: return ks[-1][1]
    for i in range(len(ks)-1):
        t0,v0=ks[i]; t1,v1=ks[i+1]
        if t0<=t<=t1: return v0+(v1-v0)*eio((t-t0)/(t1-t0))
    return ks[-1][1]
def bp(t,b,rise=0.14,fall=0.5):
    if t<b-rise or t>b+fall: return 0.0
    if t<=b: return 1-(1-(t-(b-rise))/rise)**3
    return (1-(t-b)/fall)**3

bg = Image.open(f"{ACC}/mercury-bg.png").convert("RGBA"); W,H = bg.size
sat0 = Image.open(f"{ACC}/satellite-cutout.png").convert("RGBA")
SAT_H=int(H*0.46); SAT=sat0.resize((int(sat0.width*SAT_H/sat0.height),SAT_H),Image.LANCZOS)
note0 = Image.open(f"{ACC}/note-quarter-cut.png").convert("RGBA")
def note_img(h):
    return note0.resize((max(1,int(note0.width*h/note0.height)),h),Image.LANCZOS)
NOTE_BIG=note_img(int(H*0.40)); NOTE_ROW=note_img(int(H*0.26))

# precomputed glow disc (gold, blurred) — scaled per-frame (fast)
_g=Image.new("RGBA",(400,400),(0,0,0,0)); ImageDraw.Draw(_g).ellipse([60,60,340,340],fill=(255,205,95,255))
GLOW=_g.filter(ImageFilter.GaussianBlur(70))
def paste_glow(frame,cx,cy,size,alpha):
    if alpha<=0.01: return
    g=GLOW.resize((size,size)).copy(); g.putalpha(g.getchannel("A").point(lambda a:int(a*cl(alpha))))
    frame.alpha_composite(g,(int(cx-size/2),int(cy-size/2)))

# sparkle
def make_sparkle(s):
    im=Image.new("RGBA",(s,s),(0,0,0,0)); d=ImageDraw.Draw(im); c=s/2
    d.polygon([(c,0),(c+s*0.08,c),(c,s),(c-s*0.08,c)],fill=(255,250,225,255))
    d.polygon([(0,c),(c,c-s*0.08),(s,c),(c,c+s*0.08)],fill=(255,250,225,255))
    d.ellipse([c-s*0.09,c-s*0.09,c+s*0.09,c+s*0.09],fill=(255,255,255,255))
    return im
SP=make_sparkle(64)
SAT_SPARKS=[(-0.62,-0.40,0.0),(0.66,-0.30,0.7),(-0.70,0.25,1.4),(0.72,0.30,2.1),(0.05,-0.66,2.8),(-0.34,0.58,3.5)]

# layout
rowX=[int(W*x) for x in (0.20,0.40,0.60,0.80)]; rowY=int(H*0.62)
cenX,cenY=int(W*0.5),int(H*0.52)
# clap beats locked to the music's 99.4 BPM grid (every-other-beat, kid-clap pace)
clapT=[float(x) for x in os.environ.get("CLAP_T","32.508,33.692,34.9,36.107").split(",")]

# satellite horizontal sweep (section 4) + otherwise centered
satX_kf=[(0,0.5),(20,0.5),(23.5,0.20),(24.8,0.20),(25.6,0.40),(26.8,0.40),
         (27.6,0.60),(28.8,0.60),(29.6,0.80),(30.6,0.80),(31.6,0.5),(60,0.5)]

def note_state(i,t):
    """returns (x,y,scale_img, opacity, rot, glow)"""
    glow=0.0
    # base appearance/position
    if i==0:
        if t<5.0: return None
        ap=cl((t-5.0)/1.2)
        if t<20.0:   # center, big
            x,y,img=cenX,cenY,NOTE_BIG; rot=18*math.sin((t-12)*0.8) if 12<=t<20 else 0
            glow=0.35+0.2*math.sin((t-5)*2.2)
        else:        # move to row pos 0, shrink
            m=cl((t-20.0)/2.0)
            x=int(cenX+(rowX[0]-cenX)*eio(m)); y=int(cenY+(rowY-cenY)*eio(m))
            bigh=NOTE_BIG.height; rowh=NOTE_ROW.height; h=int(bigh+(rowh-bigh)*eio(m))
            img=note_img(h); rot=0; ap=1.0
    else:
        appear=21.0+0.8*i
        if t<appear: return None
        ap=cl((t-appear)/0.8); x,y,img,rot=rowX[i],rowY,NOTE_ROW,0
    # glows: sweep proximity (23-31), claps (30-42), remember (52-54.5), goodbye (56.5-60)
    if 22.5<=t<31.0:
        scx=kf(t,satX_kf)*W
        glow=max(glow, cl(1-abs(scx-(rowX[i] if (i>0 or t>=22) else cenX))/(W*0.085)))
    if 30.0<=t<42.0:
        glow=max(glow, bp(t,clapT[i]))
    if i==1 and 52.0<=t<55.0:
        glow=max(glow, 0.5+0.5*math.sin((t-52)*4))
    if t>=56.3:
        glow=max(glow, 0.4+0.35*math.sin((t-56.3)*6+i))
    return (x,y,img,ap,rot,glow)

def camera(t):
    z=1.0; cxb=0.5; cyb=0.5
    if 5<=t<12: z=1.0+0.12*eio((t-5)/7)
    elif 12<=t<20: z=1.12
    elif 20<=t<24: z=1.12-0.12*eio((t-20)/4)
    elif 42<=t<50:  # slow pan across the row
        z=1.10; cxb=0.30+0.40*((t-42)/8)
    return z,cxb,cyb

frames=int(DUR*FPS)
lo=RLO if RLO>=0 else 0
hi=RHI if RHI>=0 else frames
for fi in range(lo,hi):
    t=fi/FPS
    frame=bg.copy()

    # notes (behind satellite)
    for i in range(4):
        st=note_state(i,t)
        if st is None: continue
        x,y,img,ap,rot,glow=st
        gs=int(img.height*1.5)
        paste_glow(frame,x,y,gs,glow*0.9*ap)
        im=img.rotate(rot,expand=True,resample=Image.BICUBIC) if abs(rot)>0.5 else img
        if ap<1.0:
            im=im.copy(); im.putalpha(im.getchannel("A").point(lambda a:int(a*ap)))
        frame.alpha_composite(im,(int(x-im.width/2),int(y-im.height/2)))

    # satellite
    intro=cl(t/2.2)
    scx=kf(t,satX_kf)*W
    scy=int(H*1.15+(H*0.30-H*1.15)*eo(intro))
    bob=8*math.sin(t*2.0)
    tilt=9*eio(cl((t-5.0)/1.8))*(1 if t<20 else 0)        # tilt at note in sec2
    if t>=56.3: tilt=360*eio(cl((t-56.3)/1.6))            # goodbye spin
    s=SAT.rotate(tilt,expand=True,resample=Image.BICUBIC)
    sx,sy=int(scx-s.width/2),int(scy+bob-s.height/2)
    frame.alpha_composite(s,(sx,sy))

    # sparkles around satellite
    for (dx,dy,ph) in SAT_SPARKS:
        tw=max(0.0,math.sin(t*3.0+ph))
        if tw<=0.04: continue
        sz=int(64*(0.5+0.5*tw)); sp=SP.resize((sz,sz)).copy()
        sp.putalpha(sp.getchannel("A").point(lambda a:int(a*tw)))
        frame.alpha_composite(sp,(int(scx+dx*SAT_H-sz/2),int(scy+bob+dy*SAT_H-sz/2)))
    # extra sparkle burst on note1 in "remember"
    if 52.0<=t<55.0:
        for k in range(5):
            ph=k*1.2; tw=max(0.0,math.sin(t*5+ph))
            sz=int(56*tw)
            if sz<6: continue
            sp=SP.resize((sz,sz))
            ang=k*1.25
            frame.alpha_composite(sp,(int(rowX[1]+math.cos(ang)*70-sz/2),int(rowY+math.sin(ang)*70-sz/2)))

    # camera
    z,cxb,cyb=camera(t)
    if z>1.001:
        cw,ch=int(W/z),int(H/z)
        l=cl(int(W*cxb)-cw//2,0,W-cw); top=cl(int(H*cyb)-ch//2,0,H-ch)
        frame=frame.crop((int(l),int(top),int(l)+cw,int(top)+ch)).resize((W,H),Image.LANCZOS)

    # fade out 58-60
    if t>=58.0:
        f=cl((t-58.0)/2.0)
        black=Image.new("RGBA",(W,H),(0,0,0,int(255*f))); frame.alpha_composite(black)

    frame.convert("RGB").save(f"{OUT}/p-{fi:04d}.png", compress_level=1)

print(f"FULL_DONE frames={frames} {W}x{H}")
