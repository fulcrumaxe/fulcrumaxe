#!/usr/bin/env python3
"""Synthesize a realistic hand-clap SFX (no samples needed).
Hand-clap acoustics: broadband transient, energy peak ~1-2.5kHz, ~1ms attack,
fast body decay + short room tail. We stack a few micro-jittered claps so it reads
as a crisp single clap with body (not a flat noise burst)."""
import sys
import numpy as np
from scipy.signal import butter, sosfilt

SR = 44100
rng = np.random.default_rng(7)

def one_clap(dur=0.20, body_tau=0.016, tail_tau=0.075, tail_amt=0.22):
    n = int(SR*dur); t = np.arange(n)/SR
    noise = rng.standard_normal(n)
    env = np.exp(-t/body_tau) + tail_amt*np.exp(-t/tail_tau)  # sharp body + room tail
    sig = noise*env
    # emphasize clap band (~0.5-3.5kHz), roll off rumble + hiss -> warm hand-clap
    sos = butter(3, [500, 3500], btype="band", fs=SR, output="sos")
    sig = sosfilt(sos, sig)
    # tiny "snap" transient at the very front (low gain so it stays a clap, not a snap)
    snap = np.zeros(n); snap[:int(SR*0.002)] = rng.standard_normal(int(SR*0.002))
    sos_hi = butter(2, 2500, btype="high", fs=SR, output="sos")
    sig = sig + 0.3*sosfilt(sos_hi, snap)
    return sig

def group_clap():
    """3 tightly micro-jittered claps -> full but still one clap event."""
    base = int(SR*0.22)
    out = np.zeros(base + int(SR*0.02))
    for k in range(3):
        c = one_clap()
        off = int(SR*rng.uniform(0.0, 0.006))           # <6ms jitter
        g = [1.0, 0.7, 0.55][k]
        out[off:off+len(c)] += g*c
    out = out/np.max(np.abs(out))*0.92
    # gentle fade-out tail to avoid click
    fade = int(SR*0.01); out[-fade:] *= np.linspace(1, 0, fade)
    return out

clap = group_clap()
pcm = (clap*32767).astype(np.int16)
import wave
out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/clap-real.wav"
w = wave.open(out, "wb"); w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
w.writeframes(pcm.tobytes()); w.close()
print(f"CLAP_OK {out} dur={len(clap)/SR:.3f}s peak={np.max(np.abs(clap)):.2f}")
