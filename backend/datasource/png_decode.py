# -*- coding: utf-8 -*-
"""极简 PNG 解码器 —— 纯标准库(zlib),供固定模板 OCR 通道读像素用。

为什么自己写：Python 标准库没有图片解码(无 PIL)，而「固定模板纯代码识别」通道
要读截图像素做数字模板匹配。PNG 本质是 zlib 压缩 + 逐行滤波，标准库 zlib 即可解，
故手写一个够用的解码器 → 真·零第三方依赖(项目铁律)。JPEG 无法用标准库解，本通道
只支持 PNG(手机截图默认 PNG)，遇 JPEG 明确报错。

支持范围(覆盖绝大多数手机截图)：
  - 8 bit 位深；color type 0(灰度)/2(RGB)/3(调色板)/6(RGBA)
  - filter 0-4(None/Sub/Up/Average/Paeth)；非隔行(interlace=0)
不支持(抛 ValueError 附中文提示)：16bit、隔行、非 PNG。
"""
import struct
import zlib

_SIG = b"\x89PNG\r\n\x1a\n"


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def decode_png(data):
    """PNG 字节 → (width, height, gray_rows)。

    gray_rows: 长度 height 的列表，每行是长度 width 的灰度整数(0-255)列表。
    失败抛 ValueError(附中文原因)。
    """
    if data[:8] != _SIG:
        raise ValueError("不是 PNG 图片(本通道仅支持 PNG 截图，JPEG 请先转 PNG)")
    pos = 8
    width = height = bit_depth = color_type = interlace = None
    palette = None
    idat = bytearray()
    n = len(data)
    while pos + 8 <= n:
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        ctype = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length  # 4 len + 4 type + body + 4 crc
        if ctype == b"IHDR":
            width, height, bit_depth, color_type, _comp, _filt, interlace = \
                struct.unpack(">IIBBBBB", body[:13])
        elif ctype == b"PLTE":
            palette = [tuple(body[i:i + 3]) for i in range(0, len(body), 3)]
        elif ctype == b"IDAT":
            idat += body
        elif ctype == b"IEND":
            break
    if width is None:
        raise ValueError("PNG 缺少 IHDR")
    if bit_depth != 8:
        raise ValueError(f"暂不支持 {bit_depth}bit 位深(仅 8bit)")
    if interlace:
        raise ValueError("暂不支持隔行 PNG")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
    if channels is None:
        raise ValueError(f"暂不支持的 color type={color_type}")

    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    expected = (stride + 1) * height
    if len(raw) < expected:
        raise ValueError("PNG 数据不完整")

    # 逐行反滤波：每行首字节是 filter type，其后 stride 字节为像素数据。
    prev = bytearray(stride)
    gray_rows = []
    off = 0
    for _y in range(height):
        ftype = raw[off]
        off += 1
        line = bytearray(raw[off:off + stride])
        off += stride
        bpp = channels  # 8bit 下每像素字节数 = 通道数
        if ftype == 1:      # Sub
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ftype == 2:    # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:    # Average
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:    # Paeth
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                c = prev[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + _paeth(a, prev[i], c)) & 0xFF
        elif ftype != 0:
            raise ValueError(f"未知的行滤波类型 {ftype}")
        prev = line
        gray_rows.append(_row_to_gray(line, width, channels, color_type, palette))
    return width, height, gray_rows


def _row_to_gray(line, width, channels, color_type, palette):
    """一行原始字节 → 灰度整数列表(luma: 0.299R+0.587G+0.114B)。"""
    out = [0] * width
    if color_type == 0:            # 灰度
        for x in range(width):
            out[x] = line[x]
    elif color_type == 4:          # 灰度 + alpha
        for x in range(width):
            out[x] = line[x * 2]
    elif color_type == 2:          # RGB
        for x in range(width):
            r, g, b = line[x * 3], line[x * 3 + 1], line[x * 3 + 2]
            out[x] = (r * 299 + g * 587 + b * 114) // 1000
    elif color_type == 6:          # RGBA
        for x in range(width):
            r, g, b = line[x * 4], line[x * 4 + 1], line[x * 4 + 2]
            out[x] = (r * 299 + g * 587 + b * 114) // 1000
    elif color_type == 3:          # 调色板
        pal = palette or []
        for x in range(width):
            idx = line[x]
            r, g, b = pal[idx] if idx < len(pal) else (0, 0, 0)
            out[x] = (r * 299 + g * 587 + b * 114) // 1000
    return out


def encode_gray_png(gray_rows):
    """灰度二维列表(0-255) → PNG 字节(8bit 灰度, filter 0)。

    供固定模板通道把裁剪的「基金名区域」编回小图，base64 后交前端确认页显示。
    """
    height = len(gray_rows)
    width = len(gray_rows[0]) if height else 0
    raw = bytearray()
    for row in gray_rows:
        raw.append(0)  # filter None
        raw.extend(int(v) & 0xFF for v in row)

    def _chunk(ctype, body):
        return (struct.pack(">I", len(body)) + ctype + body +
                struct.pack(">I", zlib.crc32(ctype + body) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (_SIG + _chunk(b"IHDR", ihdr) +
            _chunk(b"IDAT", zlib.compress(bytes(raw))) + _chunk(b"IEND", b""))
