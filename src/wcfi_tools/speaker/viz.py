"""Per-clip visualization data for the annotator: a spectrogram + where the target voice speaks.

No matplotlib — the spectrogram is a small normalized numpy array the browser renders on a canvas.
"target scores" slide a window across the clip, embed each, and score cosine similarity to the
cluster's centroid, so the UI can highlight the region where *this* voice is talking (useful when a
clip accidentally contains two people).
"""

from __future__ import annotations

import numpy as np

SR = 16000


def spectrogram(samples: np.ndarray, n_fft: int = 512, hop: int = 256, n_bins: int = 48, n_frames: int = 160) -> list:
    x = np.asarray(samples, dtype=np.float32)
    if len(x) < n_fft:
        x = np.pad(x, (0, n_fft - len(x)))
    win = np.hanning(n_fft).astype(np.float32)
    starts = range(0, len(x) - n_fft + 1, hop)
    mag = np.stack([np.abs(np.fft.rfft(x[s : s + n_fft] * win)) for s in starts])  # (T, F)
    mag = np.log1p(mag)
    fi = np.linspace(0, mag.shape[1], n_bins + 1).astype(int)
    mag = np.stack([mag[:, fi[i] : max(fi[i] + 1, fi[i + 1])].mean(1) for i in range(n_bins)], axis=1)
    if mag.shape[0] > n_frames:
        ti = np.linspace(0, mag.shape[0], n_frames + 1).astype(int)
        mag = np.stack([mag[ti[i] : max(ti[i] + 1, ti[i + 1])].mean(0) for i in range(n_frames)], axis=0)
    rng = float(mag.max() - mag.min())
    mag = (mag - mag.min()) / (rng if rng else 1.0)
    return mag.round(3).tolist()  # (frames, bins), 0..1


def target_scores(samples: np.ndarray, centroid: np.ndarray, embedder, *, win_sec=1.5, hop_sec=0.25) -> list:
    x = np.asarray(samples, dtype=np.float32)
    win = int(win_sec * SR)
    hop = int(hop_sec * SR)
    dur = len(x) / SR
    if len(x) <= win:
        e = embedder.embed(x)
        return [{"t0": 0.0, "t1": round(dur, 2), "score": round(float(e @ centroid), 3)}]
    out = []
    for s in range(0, len(x) - win + 1, hop):
        e = embedder.embed(x[s : s + win])
        out.append({"t0": round(s / SR, 2), "t1": round((s + win) / SR, 2), "score": round(float(e @ centroid), 3)})
    return out


def clip_viz(samples: np.ndarray, centroid: np.ndarray, embedder) -> dict:
    return {
        "spec": spectrogram(samples),
        "scores": target_scores(samples, centroid, embedder),
        "dur": round(len(samples) / SR, 2),
    }
