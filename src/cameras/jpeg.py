"""Standard-library JPEG header checks for go2rtc frames."""


def jpeg_size(data):
    """Reject oversized JPEG dimensions before allocating a decoded image."""
    if not data.startswith(b"\xff\xd8"):
        raise RuntimeError("go2rtc did not return JPEG data")
    cursor = 2
    while cursor + 4 <= len(data):
        if data[cursor] != 255:
            break
        while cursor < len(data) and data[cursor] == 255:
            cursor += 1
        if cursor >= len(data):
            break
        marker = data[cursor]
        cursor += 1
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            continue
        if marker in (0xD9, 0xDA) or cursor + 2 > len(data):
            break
        size = int.from_bytes(data[cursor : cursor + 2], "big")
        if size < 2 or cursor + size > len(data):
            break
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if size < 8:
                break
            height = int.from_bytes(data[cursor + 3 : cursor + 5], "big")
            width = int.from_bytes(data[cursor + 5 : cursor + 7], "big")
            if not width or not height or width * height > 16_000_000:
                raise RuntimeError("go2rtc JPEG dimensions exceed the frame limit")
            return width, height
        cursor += size
    raise RuntimeError("go2rtc returned an invalid JPEG header")
