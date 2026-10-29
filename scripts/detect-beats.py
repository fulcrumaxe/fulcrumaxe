#!/usr/bin/env python3
"""Detect tempo (BPM) + beat/onset times from an audio file (librosa)."""
import sys, json
import numpy as np
import librosa

path = sys.argv[1]
y, sr = librosa.load(path, sr=22050, mono=True)
dur = librosa.get_duration(y=y, sr=sr)

# Tempo + beats
tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr, units="frames")
beat_times = librosa.frames_to_time(beat_frames, sr=sr)

# Onsets (percussive hits) — often better for "pops" than the beat grid
onset_frames = librosa.onset.onset_detect(y=y, sr=sr, backtrack=True)
onset_times = librosa.frames_to_time(onset_frames, sr=sr)

# Onset strength envelope peaks with strength, to rank the strongest hits
oenv = librosa.onset.onset_strength(y=y, sr=sr)
oenv_times = librosa.times_like(oenv, sr=sr)
peaks = librosa.util.peak_pick(oenv, pre_max=3, post_max=3, pre_avg=5,
                               post_avg=5, delta=0.2, wait=10)
peak_times = oenv_times[peaks]
peak_strengths = oenv[peaks]
ranked = sorted(zip(peak_times.tolist(), peak_strengths.tolist()),
                key=lambda t: -t[1])

out = {
    "duration": round(float(dur), 3),
    "bpm": round(float(np.atleast_1d(tempo)[0]), 2),
    "beat_times": [round(float(t), 3) for t in beat_times],
    "onset_times": [round(float(t), 3) for t in onset_times],
    "top_onset_peaks": [[round(t, 3), round(s, 3)] for t, s in ranked[:12]],
}
print(json.dumps(out, indent=2))
