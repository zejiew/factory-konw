# -*- coding: utf-8 -*-
"""
代工厂物料台账 —— 启动入口

    双击「启动.bat」或运行  python main.py
"""

import os
import sys
import traceback


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        import ui
        ui.run()
    except Exception:
        msg = traceback.format_exc()
        # 出错时留一份日志到 data/error.log，同时弹窗告知
        try:
            logdir = os.path.join(here, "data")
            os.makedirs(logdir, exist_ok=True)
            with open(os.path.join(logdir, "error.log"), "a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except Exception:
            pass
        try:
            import tkinter.messagebox as mb
            import tkinter as tk
            r = tk.Tk()
            r.withdraw()
            mb.showerror("启动失败",
                         "程序没能启动，错误信息：\n\n%s\n\n详细日志在 data/error.log" % msg[-1200:])
            r.destroy()
        except Exception:
            print(msg)
        raise


if __name__ == "__main__":
    main()
