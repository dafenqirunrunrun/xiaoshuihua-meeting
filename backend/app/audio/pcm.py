import struct


def apply_pcm16_gain(data: bytes, gain: float) -> bytes:
    """Apply linear gain to little-endian signed PCM16 with saturation."""
    if gain == 1.0:
        return data
    if len(data) % 2 != 0:
        raise ValueError("PCM16 data length must be even")

    samples = struct.unpack(f"<{len(data) // 2}h", data)
    amplified = []
    for sample in samples:
        value = int(round(sample * gain))
        amplified.append(max(-32768, min(32767, value)))
    return struct.pack(f"<{len(amplified)}h", *amplified)