#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dtbo.img 读写库 —— DTBO 表 + 内嵌 dtb + AVB（哈希校验型）

## 镜像布局（高通 Android dtbo 典型）

    [0 .. table_total)          DTBO 表头(32B) + N 个条目(32B each)
                                条目：dtb_size(4) dtb_offset(4) id(4) rev(4) custom(16)
    [.. vbmeta_offset)          各 dtb 内容（通常紧跟表头）
    [vbmeta_offset .. +size)    AVB vbmeta
    [.. footer_offset)          填充
    [footer_offset .. +4096)    AVB footer（1KB 位置在分区末尾 -64B）

## AVB 结构

vbmeta 头 256 字节，后面依次是 authentication_data 与 auxiliary_data（描述符区）。
hash descriptor（tag = 2）标准布局：

    +0    tag                      (u64)
    +8    num_bytes_following      (u64)
    +16   image_size               (u64)
    +24   hash_algorithm           (32 字节，如 "sha256")
    +56   partition_name_len       (u32)
    +60   salt_len                 (u32)
    +64   digest_len               (u32)
    +68   partition_name           (partition_name_len 字节)
    ...   salt                     (salt_len 字节)
    ...   digest                   (digest_len 字节)

**但实际镜像里各厂/各 avbtool 版本会在字段之间插入对齐填充**，
导致硬算偏移会取到错位的数据。本库的做法是：

  1. 按标准布局算一次候选；
  2. 用 `sha256(salt || image[0:image_size])` 实算一遍校验；
  3. 校验不过就把描述符区里所有 32 字节窗口都当 salt 试一遍，
     直到找到一个能让 digest 对上的组合。

这样无论填充怎么变都能拿到正确结果。
"""
import hashlib

DTB_TABLE_MAGIC = 0xD7B7AB1E
AVB_MAGIC = b"AVB0"
AVB_FOOTER_MAGIC = b"AVBf"
AVB_FOOTER_SIZE = 4096
AVB_HEADER_SIZE = 256
AVB_HASH_DESC_TAG = 2
SALT_LEN = 32
DIGEST_LEN = 32


def _u32(b, o):
    return int.from_bytes(b[o:o + 4], "big")


def _u64(b, o):
    return int.from_bytes(b[o:o + 8], "big")


class AvbInfo:
    """AVB 解析结果"""

    def __init__(self):
        self.present = False
        self.algorithm_type = None
        self.auth_size = 0
        self.aux_size = 0
        self.image_size = 0
        self.salt = b""
        self.digest = b""
        self.hash_algorithm = b""
        # 回写时用的绝对偏移（相对 vbmeta 起点）
        self.image_size_off = None
        self.digest_off = None
        self.how = ""          # 解析方式说明，便于排查


class DtboImage:
    """一个 dtbo 分区的完整视图，改完可回写"""

    def __init__(self, blob):
        self.blob = bytearray(blob)
        self._parse_table()
        self._locate_avb()

    # ======================= DTBO 表 =======================
    def _parse_table(self):
        magic = _u32(self.blob, 0)
        if magic != DTB_TABLE_MAGIC:
            raise ValueError("不是 DTBO 表：magic=0x%08x，应为 0x%08x"
                             % (magic, DTB_TABLE_MAGIC))

        self.table_total = _u32(self.blob, 4)
        self.table_hdr_size = _u32(self.blob, 8)
        self.entry_size = _u32(self.blob, 12)
        self.entry_count = _u32(self.blob, 16)

        self.entries = []
        for i in range(self.entry_count):
            o = self.table_hdr_size + i * self.entry_size
            self.entries.append({
                "index": i,
                "size": _u32(self.blob, o),
                "offset": _u32(self.blob, o + 4),
                "id": _u32(self.blob, o + 8),
                "rev": _u32(self.blob, o + 12),
                "custom": bytes(self.blob[o + 16:o + 32]),
            })

    def dtb(self, index=0):
        e = self.entries[index]
        return bytes(self.blob[e["offset"]:e["offset"] + e["size"]])

    @property
    def content_end(self):
        return max(e["offset"] + e["size"] for e in self.entries)

    def set_dtb(self, dtb_blob, index=0):
        """写入第 index 个 dtb，同步更新表头与表项。

        注意：如果新内容会盖住 vbmeta，必须先调 reserve_vbmeta()。
        """
        e = self.entries[index]
        old_span = e["size"]
        e["size"] = len(dtb_blob)

        # 先把这块区域清干净（新内容可能比旧内容短）
        span = max(old_span, len(dtb_blob))
        start = e["offset"]
        self.blob[start:start + span] = b"\x00" * span
        self.blob[start:start + len(dtb_blob)] = dtb_blob

        new_end = max(x["offset"] + x["size"] for x in self.entries)
        self.blob[4:8] = new_end.to_bytes(4, "big")
        self.table_total = new_end

        for x in self.entries:
            o = self.table_hdr_size + x["index"] * self.entry_size
            self.blob[o:o + 4] = x["size"].to_bytes(4, "big")
            self.blob[o + 4:o + 8] = x["offset"].to_bytes(4, "big")

    # ======================= AVB =======================
    def _locate_avb(self):
        self.avb = AvbInfo()

        self.footer_off = len(self.blob) - 64
        if self.blob[self.footer_off:self.footer_off + 4] != AVB_FOOTER_MAGIC:
            found = self.blob.find(AVB_FOOTER_MAGIC)
            if found < 0:
                raise ValueError("找不到 AVB footer")
            self.footer_off = found

        f = self.footer_off
        self.footer_version = _u32(self.blob, f + 4)
        self.avb.image_size = _u64(self.blob, f + 12)
        self.vbmeta_off = _u64(self.blob, f + 20)
        self.vbmeta_size = _u64(self.blob, f + 28)

        if self.blob[self.vbmeta_off:self.vbmeta_off + 4] != AVB_MAGIC:
            return      # 无 vbmeta，当无 AVB 处理

        self.avb.present = True
        vb = self.vbmeta_off
        self.avb.auth_size = _u64(self.blob, vb + 12)
        self.avb.aux_size = _u64(self.blob, vb + 20)
        self.avb.algorithm_type = _u32(self.blob, vb + 28)

        self._parse_descriptors()

    def _payload(self):
        return bytes(self.blob[0:self.avb.image_size])

    def _try_layout(self, desc_pos):
        """按标准布局从 desc_pos 处解析 hash descriptor，并实测校验"""
        try:
            tag = _u64(self.blob, desc_pos)
            if tag != AVB_HASH_DESC_TAG:
                return False
            img_size = _u64(self.blob, desc_pos + 16)
            hash_alg = bytes(self.blob[desc_pos + 24:desc_pos + 56]).rstrip(b"\x00")
            pn_len = _u32(self.blob, desc_pos + 56)
            salt_len = _u32(self.blob, desc_pos + 60)
            dig_len = _u32(self.blob, desc_pos + 64)
            if not (0 < img_size <= len(self.blob)):
                return False
            if not (0 < salt_len <= 64 and 0 < dig_len <= 64):
                return False
            if not (0 <= pn_len <= 64):
                return False

            name_off = desc_pos + 68
            salt_off = name_off + pn_len
            dig_off = salt_off + salt_len

            salt = bytes(self.blob[salt_off:salt_off + salt_len])
            digest = bytes(self.blob[dig_off:dig_off + dig_len])

            payload = bytes(self.blob[0:img_size])
            calc = hashlib.sha256(salt + payload).digest()
            if calc != digest:
                return False

            self.avb.image_size = img_size
            self.avb.hash_algorithm = hash_alg
            self.avb.salt = salt
            self.avb.digest = digest
            self.avb.image_size_off = (desc_pos + 16) - self.vbmeta_off
            self.avb.digest_off = dig_off - self.vbmeta_off
            self.avb.how = "标准布局 (desc@%d)" % (desc_pos - self.vbmeta_off)
            return True
        except Exception:
            return False

    def _probe_layout(self):
        """兜底：把描述符区所有 32 字节窗口当 salt 试，找出能让 digest 自洽的组合"""
        vb = self.vbmeta_off
        desc_start = vb + AVB_HEADER_SIZE + self.avb.auth_size
        desc_end = desc_start + self.avb.aux_size
        if desc_end > vb + self.vbmeta_size:
            desc_end = vb + self.vbmeta_size

        # 优先用 footer 里记的 image_size；不行就试着扫几个候选
        size_cands = [self.avb.image_size]
        for e in self.entries:
            size_cands.append(e["offset"] + e["size"])
        size_cands.append(self.table_total)
        size_cands = [s for s in size_cands if 0 < s <= len(self.blob)]

        for img_size in size_cands:
            payload = bytes(self.blob[0:img_size])
            off = desc_start
            while off + SALT_LEN + DIGEST_LEN <= desc_end:
                salt = bytes(self.blob[off:off + SALT_LEN])
                calc = hashlib.sha256(salt + payload).digest()
                hit = self.blob.find(calc, off + SALT_LEN, desc_end)
                if hit >= 0:
                    self.avb.image_size = img_size
                    self.avb.salt = salt
                    self.avb.digest = calc
                    self.avb.digest_off = hit - vb
                    self.avb.hash_algorithm = b"sha256"
                    # image_size 字段：找描述符里等于 img_size 的 8 字节
                    self.avb.image_size_off = None
                    p = desc_start
                    while p + 8 <= hit:
                        if _u64(self.blob, p) == img_size:
                            self.avb.image_size_off = p - vb
                            break
                        p += 8
                    self.avb.how = "探测 (salt@%d digest@%d img@%s)" % (
                        off - vb, hit - vb,
                        "-" if self.avb.image_size_off is None else self.avb.image_size_off)
                    return True
                off += 4
        return False

    def _parse_descriptors(self):
        vb = self.vbmeta_off
        desc_start = vb + AVB_HEADER_SIZE + self.avb.auth_size

        # 1) 标准布局：从描述符区起点试
        if self._try_layout(desc_start):
            return

        # 2) 描述符区里可能有 padding，扫一遍 tag=2 的位置
        desc_end = desc_start + self.avb.aux_size
        for p in range(desc_start, min(desc_end, vb + self.vbmeta_size) - 8, 8):
            if _u64(self.blob, p) == AVB_HASH_DESC_TAG:
                if self._try_layout(p):
                    return

        # 3) 兜底探测
        if self._probe_layout():
            return

        raise RuntimeError(
            "AVB 描述符解析失败：算法 type=%s，auth=%d，aux=%d —— "
            "可能是未知的 AVB 变体，或镜像本身校验就不对"
            % (self.avb.algorithm_type, self.avb.auth_size, self.avb.aux_size))

    # ---------- 校验 ----------
    @property
    def avb_is_hash_only(self):
        return (self.avb.present
                and self.avb.algorithm_type == 0
                and self.avb.auth_size == 0)

    def avb_calc_digest(self):
        return hashlib.sha256(
            self.avb.salt + bytes(self.blob[0:self.avb.image_size])).digest()

    def avb_verify(self):
        if not self.avb.present:
            return None
        return self.avb_calc_digest() == self.avb.digest

    # ---------- 写回 ----------
    def reserve_vbmeta(self, new_content_end, align=4096):
        """改内容之前调用：若新内容会盖住 vbmeta，先把 vbmeta 搬到安全位置。

        必须在改内容之前调 —— 否则旧 vbmeta 已被新内容覆盖，就拷不出来了。
        返回 (新位置, 是否搬动)
        """
        if not self.avb.present:
            return self.vbmeta_off, False
        if self.vbmeta_off >= new_content_end:
            return self.vbmeta_off, False

        new_off = ((new_content_end + align - 1) // align) * align
        if new_off + self.vbmeta_size > self.footer_off:
            raise RuntimeError(
                "搬 vbmeta 会撞 AVB footer：%d + %d > %d"
                % (new_off, self.vbmeta_size, self.footer_off))

        old_vb = bytes(self.blob[self.vbmeta_off:self.vbmeta_off + self.vbmeta_size])
        self.blob[new_off:new_off + self.vbmeta_size] = old_vb
        self.vbmeta_off = new_off

        f = self.footer_off
        self.blob[f + 20:f + 28] = new_off.to_bytes(8, "big")
        self.blob[f + 28:f + 36] = self.vbmeta_size.to_bytes(8, "big")
        return new_off, True

    def avb_reflash(self):
        """改完内容后调用：清尾巴 → 更新 image_size → 重算 digest → 更新 footer"""
        if not self.avb.present:
            return {"skipped": "镜像无 AVB"}

        info = {}
        content_end = self.content_end
        info["old_image_size"] = self.avb.image_size
        info["new_image_size"] = content_end

        if self.vbmeta_off < content_end:
            raise RuntimeError(
                "内容末端 %d 越过 vbmeta 位置 %d —— 必须先 reserve_vbmeta()"
                % (content_end, self.vbmeta_off))

        # 清掉 [content_end, old_image_size) 的旧残留，否则哈希会算进脏数据
        if content_end < self.avb.image_size:
            self.blob[content_end:self.avb.image_size] = (
                b"\x00" * (self.avb.image_size - content_end))
            info["cleared_tail"] = self.avb.image_size - content_end

        vb = self.vbmeta_off

        # image_size 字段
        if self.avb.image_size_off is not None:
            o = vb + self.avb.image_size_off
            self.blob[o:o + 8] = content_end.to_bytes(8, "big")

        self.avb.image_size = content_end
        self.avb.digest = self.avb_calc_digest()
        info["new_digest"] = self.avb.digest.hex()

        # digest 字段
        o = vb + self.avb.digest_off
        dsz = len(self.avb.digest)
        self.blob[o:o + dsz] = self.avb.digest

        # footer
        f = self.footer_off
        self.blob[f + 12:f + 20] = content_end.to_bytes(8, "big")
        self.blob[f + 20:f + 28] = self.vbmeta_off.to_bytes(8, "big")
        self.blob[f + 28:f + 36] = self.vbmeta_size.to_bytes(8, "big")

        info["vbmeta_off"] = self.vbmeta_off
        info["verified"] = self.avb_verify()
        return info

    def to_bytes(self):
        return bytes(self.blob)
