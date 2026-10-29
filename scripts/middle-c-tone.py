#!/usr/bin/env python3
"""Synthesize an exact Middle C (C4 = 261.626 Hz) piano-ish note. Code-owns the pitch."""
import sys, numpy as np, wave

SR = 44100
F0 = 261.6256  # Middle C / C4, exact

def piano_note(f0=F0, dur=1.6):
    n = int(SR*dur); t = np.arange(n)/SR
    # additive: fundamental + decaying harmonics (slightly inharmonic), each its own decay
    partials = [(1, 1.00, 1.5), (2, 0.55, 1.1), (3, 0.33, 0.9),
                (4, 0.18, 0.7), (5, 0.10, 0.6), (6, 0.06, 0.5)]
    sig = np.zeros(n)
    for k, amp, tau in partials:
        inharm = 1 + 0.0008*k*k        # slight stretch -> piano timbre
        sig += amp*np.sin(2*np.pi*f0*k*inharm*t)*np.exp(-t/tau)
    # percussive hammer attack (few ms of brighter noise-ish transient)
    att = int(SR*0.006); sig[:att] += 0.25*np.sin(2*np.pi*f0*8*t[:att])*np.linspace(1,0,att)
    # fast attack envelope to avoid click
    a = int(SR*0.004); sig[:a] *= np.linspace(0,1,a)
    sig = sig/np.max(np.abs(sig))*0.9
    return sig

note = piano_note()
pcm = (note*32767).astype(np.int16)
out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/middle-c.wav"
w = wave.open(out,"wb"); w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
w.writeframes(pcm.tobytes()); w.close()
# verify pitch
Y=np.abs(np.fft.rfft(note)); f=np.fft.rfftfreq(len(note),1/SR)
print(f"MIDC_OK {out} dur={len(note)/SR:.2f}s peakHz={f[np.argmax(Y)]:.1f} (target {F0:.1f})")
