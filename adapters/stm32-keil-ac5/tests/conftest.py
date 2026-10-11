"""pytest 根配置 —— adapter 用例的 sys.path 引导（**只做这一件事，不含业务逻辑**）。

与 `skeleton/tests/conftest.py` 的**行为等价**：都是把工程的 `tools/` 加进 sys.path。
唯一差别：在平台仓库里，骨架工具（`skeleton/tools`）与适配器工具
（`adapters/<家族>/tools`）分处两个目录，本文件两个都加；拷进具体工程后它们
合并成 `<工程>/tools`，另一个路径不存在会被跳过。

⚠️ 拷进工程后，本文件会与 skeleton 那份**同名覆盖**。无论留下哪一份，
   行为都相同（都是"加入本工程的 tools/"），所以覆盖是安全的 ——
   但改其中一份时，请顺手看一眼另一份，别让两者漂移。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))

# 平台仓库布局：<平台>/adapters/<家族>/tests -> 需要下面两个目录
_CANDIDATES = (
    os.path.join(os.path.dirname(_HERE), "tools"),                                    # <家族>/tools
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(_HERE))),            # <平台>/skeleton/tools
                 "skeleton", "tools"),
)

for _path in _CANDIDATES:
    _path = os.path.abspath(_path)
    if os.path.isdir(_path) and _path not in sys.path:
        sys.path.insert(0, _path)
