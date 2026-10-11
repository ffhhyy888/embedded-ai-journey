"""pytest 根配置 —— 让 tests/ 下的用例能 import 到 tools/ 里的模块。

为什么需要它
    pytest 只把**测试文件所在目录**加进 sys.path（本例是 tests/），
    而骨架的工具在**兄弟目录** tools/ 下。没有这一步，`import project_config`
    在收集阶段就会 ImportError —— 表现为"测试文件挂了"，
    而不是"被测功能挂了"，容易把人带偏。

    tools/preflight.py 里也是同样的做法（sys.path.insert(0, ROOT/tools)），
    这里保持一致。

注意
    拷进具体工程后，tools/ 与 tests/ 仍是兄弟目录，本文件无需修改。
"""
import os
import sys

_TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")

if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)
