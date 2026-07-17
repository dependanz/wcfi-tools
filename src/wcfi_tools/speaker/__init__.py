"""Speaker identification (torch-free, ONNX via sherpa-onnx).

Enrollment-based: register named voiceprints once, then match every meeting's voice segments
against them. Robust to long meetings because each segment is matched to a persistent reference,
independent of the recording's length. Models auto-download from non-gated public URLs.
"""
