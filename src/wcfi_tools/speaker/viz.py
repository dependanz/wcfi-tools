"""Per-clip visualization data for the annotator: a mel-spectrogram + where the target voice speaks.

No matplotlib — the spectrogram is a small normalized numpy array the browser renders on a canvas.
"target scores" slide a window across the clip, embed each, and score cosine similarity to the
cluster's centroid, so the UI can mark the region where *this* voice is talking (useful when a clip
accidentally contains two people).
"""

from __future__ import annotations

import numpy as np

SR = 16000


def _hz2mel(f: np.ndarray) -> np.ndarray:
    return 2595.0 * np.log10(1.0 + f / 700.0)


def _mel2hz(m: np.ndarray) -> np.ndarray:
    return 700.0 * (10.0 ** (m / 2595.0) - 1.0)


def _mel_filterbank(sr: int, n_fft: int, n_mels: int, fmin: float = 40.0, fmax: float | None = None) -> np.ndarray:
    fmax = fmax or sr / 2
    pts = _mel2hz(np.linspace(_hz2mel(np.array(fmin)), _hz2mel(np.array(fmax)), n_mels + 2))
    bins = np.clip(np.floor((n_fft + 1) * pts / sr).astype(int), 0, n_fft // 2)
    fb = np.zeros((n_mels, n_fft // 2 + 1), dtype=np.float32)
    for m in range(1, n_mels + 1):
        left, center, right = bins[m - 1], bins[m], bins[m + 1]
        if center > left:
            fb[m - 1, left:center] = (np.arange(left, center) - left) / (center - left)
        if right > center:
            fb[m - 1, center:right] = (right - np.arange(center, right)) / (right - center)
    return fb


def mel_spectrogram(samples: np.ndarray, n_fft: int = 1024, hop: int = 128, n_mels: int = 96, n_frames: int = 256) -> list:
    x = np.asarray(samples, dtype=np.float32)
    if len(x) < n_fft:
        x = np.pad(x, (0, n_fft - len(x)))
    win = np.hanning(n_fft).astype(np.float32)
    power = np.stack([np.abs(np.fft.rfft(x[s : s + n_fft] * win)) ** 2 for s in range(0, len(x) - n_fft + 1, hop)])
    mel = np.log(power @ _mel_filterbank(SR, n_fft, n_mels).T + 1e-6)  # (T, n_mels)
    if mel.shape[0] > n_frames:  # downsample time
        ti = np.linspace(0, mel.shape[0], n_frames + 1).astype(int)
        mel = np.stack([mel[ti[i] : max(ti[i] + 1, ti[i + 1])].mean(0) for i in range(n_frames)], axis=0)
    rng = float(mel.max() - mel.min())
    mel = (mel - mel.min()) / (rng if rng else 1.0)
    return mel.round(3).tolist()  # (frames, n_mels), 0..1


def target_scores(samples: np.ndarray, centroid: np.ndarray, embedder, *, win_sec=1.0, hop_sec=0.2) -> list:
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
        "spec": mel_spectrogram(samples),
        "scores": target_scores(samples, centroid, embedder),
        "dur": round(len(samples) / SR, 2),
    }
