# -*- coding: utf-8 -*-
"""一次性转换：把 UTF-8 无 BOM 的中文 bat 转成 GBK(ANSI)，并补 chcp 936。

原因：cmd 默认按活动代码页 936(GBK) 逐字节读取 bat 文件，
UTF-8 编码的中文字符（如 ORG_PROPERTY_SHORT=住武物业）会被
误读成乱码，进入环境变量后写库即产生 浣忔\\ue11f鐗╀笟 之类损坏值。
启动系统.bat 已是 GBK+chcp 936 的正确做法，这里统一成同样约定。
"""
import io
import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGETS = ("启动_调整版.bat", "启动_试用版.bat")
CHCP = "@echo off\r\nchcp 936 >nul\r\n"


def main():
    for name in TARGETS:
        p = os.path.join(BASE, name)
        with io.open(p, "r", encoding="utf-8") as f:
            text = f.read()
        # 已有 chcp 行的跳过
        lines = text.splitlines()
        if not any("chcp 936" in ln for ln in lines):
            # 拆掉旧的 @echo off，统一用 CHCP 头
            body = "\r\n".join(
                ln for ln in lines if ln.strip() and not ln.strip().startswith("@echo off"))
            text = CHCP + body + "\r\n"
        with io.open(p, "w", encoding="gbk", newline="") as f:
            f.write(text)
        # 回读验证
        with io.open(p, "r", encoding="gbk") as f:
            back = f.read()
        print("OK  %s  -> GBK(%d chars) chcp936=%s 首行=%r" %
              (name, len(back), "chcp 936" in back, back.splitlines()[1]))


if __name__ == "__main__":
    main()