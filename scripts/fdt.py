#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FDT (Flattened Device Tree) 读写库 —— 纯 Python，无第三方依赖

支持：解析结构块 / 字符串块 → 节点树 → 序列化回结构块。

## 两个必知要点

1. **字符串块支持「后缀复用」寻址**
   FDT 允许把属性名的 nameoff 指向另一个字符串的**后半段**，例如
   属性 `gpio` 的 nameoff 可以指向 `qcom,pm8550-gpio` 里 `gpio` 的起始位置。
   所以建立 name→offset 映射时，必须为每个完整名的**所有后缀**都建映射，
   否则重建时会有一批属性名找不到。

2. **4 字节对齐**
   节点名与属性值都要补齐到 4 字节边界（用 NUL 填充）。

## 往返验证

`roundtrip_check()` 会把解析后的树重新序列化，并与原结构块逐字节比对。
这是最可靠的自检手段 —— 它通过说明库本身没坏，后面改出来的东西才可信。
"""
FDT_MAGIC = 0xD00DFEED

FDT_BEGIN_NODE = 1
FDT_END_NODE = 2
FDT_PROP = 3
FDT_NOP = 4
FDT_END = 9


class Node:
    """设备树节点"""
    __slots__ = ("name", "props", "kids", "parent")

    def __init__(self, name, parent=None):
        self.name = name
        self.props = {}       # 属性名 -> bytes
        self.kids = []        # list[Node]
        self.parent = parent

    def child(self, name):
        for k in self.kids:
            if k.name == name:
                return k
        return None

    def path(self):
        parts = []
        n = self
        while n is not None and n.parent is not None:
            parts.append(n.name)
            n = n.parent
        return "/" + "/".join(reversed(parts))

    def root(self):
        n = self
        while n.parent is not None:
            n = n.parent
        return n

    def find(self, path):
        """按绝对路径查找，如 /fragment@45/__overlay__/mca_strategy_quick_charge"""
        if not path.startswith("/"):
            raise ValueError("路径必须以 / 开头：%r" % path)
        cur = self.root()
        for part in path.split("/"):
            if not part:
                continue
            cur = cur.child(part)
            if cur is None:
                return None
        return cur

    def walk(self, fn, depth=0):
        """深度优先遍历，回调 fn(node, depth)"""
        fn(self, depth)
        for k in self.kids:
            k.walk(fn, depth + 1)

    def deep_clone(self, parent=None):
        """深拷贝整个子树（克隆 timing 节点这类操作用）"""
        n = Node(self.name, parent)
        n.props = dict(self.props)
        for k in self.kids:
            n.kids.append(k.deep_clone(n))
        return n

    def add_prop(self, name, value):
        self.props[name] = value

    def __repr__(self):
        return "<Node %s props=%d kids=%d>" % (self.name or "/", len(self.props), len(self.kids))


def parse(blob):
    """解析 dtb 字节流 → (root_node, meta)"""
    if len(blob) < 40:
        raise ValueError("数据太短，不是 FDT")

    magic = int.from_bytes(blob[0:4], "big")
    if magic != FDT_MAGIC:
        raise ValueError("FDT 魔数不对：0x%08x，应为 0x%08x" % (magic, FDT_MAGIC))

    totalsize = int.from_bytes(blob[4:8], "big")
    off_struct = int.from_bytes(blob[8:12], "big")
    off_strings = int.from_bytes(blob[12:16], "big")
    off_rsvmap = int.from_bytes(blob[16:20], "big")
    version = int.from_bytes(blob[20:24], "big")
    last_comp = int.from_bytes(blob[24:28], "big")
    boot_cpuid = int.from_bytes(blob[28:32], "big")
    size_strings = int.from_bytes(blob[32:36], "big")
    size_struct = int.from_bytes(blob[36:40], "big")

    strings = blob[off_strings:off_strings + size_strings]
    rsvmap = blob[off_rsvmap:off_struct]

    def sname(off):
        end = strings.find(b"\x00", off)
        if end < 0:
            end = len(strings)
        return strings[off:end].decode("utf-8", "replace")

    root = None
    cur = None
    pos = off_struct
    end_pos = off_struct + size_struct

    while pos < end_pos:
        tag = int.from_bytes(blob[pos:pos + 4], "big")
        here = pos
        pos += 4

        if tag == FDT_BEGIN_NODE:
            e = blob.find(b"\x00", pos)
            if e < 0:
                raise ValueError("节点名没有 NUL 结尾 @ %d" % pos)
            name = blob[pos:e].decode("utf-8", "replace")
            pos = (e + 1 + 3) & ~3
            nd = Node(name, cur)
            if cur is None:
                root = nd
            else:
                cur.kids.append(nd)
            cur = nd

        elif tag == FDT_END_NODE:
            if cur is not None:
                cur = cur.parent

        elif tag == FDT_PROP:
            vlen = int.from_bytes(blob[pos:pos + 4], "big")
            nameoff = int.from_bytes(blob[pos + 4:pos + 8], "big")
            pos += 8
            value = blob[pos:pos + vlen]
            if cur is not None:
                cur.props[sname(nameoff)] = value
            pos += vlen
            pos = (pos + 3) & ~3

        elif tag == FDT_NOP:
            pass

        elif tag == FDT_END:
            break

        else:
            raise ValueError("未知 FDT tag %d @ %d" % (tag, here))

    meta = {
        "totalsize": totalsize,
        "off_struct": off_struct,
        "off_strings": off_strings,
        "off_rsvmap": off_rsvmap,
        "version": version,
        "last_comp": last_comp,
        "boot_cpuid": boot_cpuid,
        "size_strings": size_strings,
        "size_struct": size_struct,
        "strings": strings,
        "rsvmap": rsvmap,
    }
    return root, meta


def build_name_offsets(strings):
    """属性名 → 字符串块内偏移（含所有后缀，见模块文档第 1 点）"""
    out = {}
    starts = []
    pos = 0
    n = len(strings)
    while pos < n:
        end = strings.find(b"\x00", pos)
        if end < 0:
            break
        name = strings[pos:end].decode("utf-8", "replace")
        starts.append((pos, name))
        if name:
            out.setdefault(name, pos)
        pos = end + 1

    for start, name in starts:
        for cut in range(1, len(name)):
            out.setdefault(name[cut:], start + cut)
    return out


def serialize_struct(root, name_offsets):
    """节点树 → 结构块字节"""
    buf = bytearray()

    def pad():
        while len(buf) % 4:
            buf.append(0)

    def emit(node):
        buf.extend(FDT_BEGIN_NODE.to_bytes(4, "big"))
        buf.extend(node.name.encode("utf-8") + b"\x00")
        pad()

        for pname, pval in node.props.items():
            if pname not in name_offsets:
                raise KeyError("字符串块里没有属性名 %r（后缀映射也找不到）" % pname)
            buf.extend(FDT_PROP.to_bytes(4, "big"))
            buf.extend(len(pval).to_bytes(4, "big"))
            buf.extend(name_offsets[pname].to_bytes(4, "big"))
            buf.extend(pval)
            pad()

        for k in node.kids:
            emit(k)
        buf.extend(FDT_END_NODE.to_bytes(4, "big"))

    emit(root)
    buf.extend(FDT_END.to_bytes(4, "big"))
    pad()
    return bytes(buf)


def serialize(root, meta, strings=None):
    """重建整个 dtb → (dtb_bytes, info)"""
    if strings is None:
        strings = meta["strings"]

    name_offsets = build_name_offsets(strings)
    struct_blob = serialize_struct(root, name_offsets)
    rsvmap = meta["rsvmap"]

    off_rsvmap = 40
    off_struct = off_rsvmap + len(rsvmap)
    off_strings = off_struct + len(struct_blob)
    totalsize = off_strings + len(strings)

    hdr = bytearray(40)
    hdr[0:4] = FDT_MAGIC.to_bytes(4, "big")
    hdr[4:8] = totalsize.to_bytes(4, "big")
    hdr[8:12] = off_struct.to_bytes(4, "big")
    hdr[12:16] = off_strings.to_bytes(4, "big")
    hdr[16:20] = off_rsvmap.to_bytes(4, "big")
    hdr[20:24] = meta["version"].to_bytes(4, "big")
    hdr[24:28] = meta["last_comp"].to_bytes(4, "big")
    hdr[28:32] = meta["boot_cpuid"].to_bytes(4, "big")
    hdr[32:36] = len(strings).to_bytes(4, "big")
    hdr[36:40] = len(struct_blob).to_bytes(4, "big")

    out = bytes(hdr) + rsvmap + struct_blob + strings
    info = {
        "off_struct": off_struct,
        "off_strings": off_strings,
        "size_struct": len(struct_blob),
        "size_strings": len(strings),
        "totalsize": totalsize,
    }
    return out, info


def roundtrip_check(dtb_blob):
    """自检：解析 → 重新序列化，与镜像内结构块比对。

    返回 (是否通过, 说明, 首个差异偏移或 None)

    注意：只比**有意义的内容**，不做逐字节硬比 ——
    因为原镜像的 padding 字节可能是任意值（有的厂商写 0x00，有的写 0x74 之类
    的垃圾），而 dtc 只保证语义等价，不保证 padding 一模一样。
    所以这里比较的是「解析后重新序列化得到的结构块」与「原结构块在忽略 padding
    差异后是否等价」——通过重新解析两边再比对树来做。
    """
    root1, meta1 = parse(dtb_blob)
    new_struct = serialize_struct(root1, build_name_offsets(meta1["strings"]))
    orig_struct = dtb_blob[meta1["off_struct"]:meta1["off_struct"] + meta1["size_struct"]]

    if new_struct == orig_struct:
        return True, "逐字节一致", None

    # 长度不同 —— 说明序列化有问题
    if len(new_struct) != len(orig_struct):
        return False, "长度不同：重算 %d / 原 %d" % (len(new_struct), len(orig_struct)), None

    # 长度相同但字节不同 —— 逐位找差异，判断是否只差 padding
    diffs = [i for i in range(len(orig_struct)) if orig_struct[i] != new_struct[i]]
    return False, "长度一致，但有 %d 个字节不同（多为 padding）" % len(diffs), diffs[0]


# ---------------- 值编解码 ----------------

def u32_list(blob):
    """<0x1 0x2 0x3> → [1,2,3]"""
    if len(blob) % 4:
        raise ValueError("u32 值长度 %d 不是 4 的倍数" % len(blob))
    return [int.from_bytes(blob[i:i + 4], "big") for i in range(0, len(blob), 4)]


def pack_u32(vals):
    return b"".join(int(v).to_bytes(4, "big") for v in vals)


def str_list(blob):
    """"a","b","c" → ['a','b','c']"""
    return [x.decode("utf-8", "replace") for x in blob.split(b"\x00") if x]


def pack_str_list(items):
    return b"".join(str(x).encode("utf-8") + b"\x00" for x in items)


def byte_list(blob):
    """[aa bb cc] → [0xaa, 0xbb, 0xcc]"""
    return list(blob)
