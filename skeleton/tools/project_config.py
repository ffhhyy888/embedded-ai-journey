#!/usr/bin/env python3
"""工程级配置加载器 —— **骨架层**，不含任何芯片 / 工具链知识。

为什么需要它
    在此之前，"工程名 / 工程文件路径 / 产物路径 / 阈值 / 串口端口 / 启动标记"
    这些**工程级**参数被写死在各个脚本里（最典型的是 build.py 的 `SDIO_test.uvprojx`），
    导致同一套工具无法驱动第二个工程。本模块把它们收敛到工程根的
    `tools/project.cfg` 一个文件里 —— 换工程只改这一处。

三个来源，各管各的（不要混）
    工程级  tools/project.cfg            换工程要改        -> 本模块
    机器级  环境变量 AUTOLAB_*           换电脑要改        -> os.environ
    代码内  判据逻辑本身                 换工程**不该**改  -> 各工具自己

优先级（全平台统一，由 resolve() 实现，并有单测锁定）
    CLI 参数  >  环境变量  >  tools/project.cfg  >  内置默认

**为什么不静默兜底**
    cfg 缺失或字段缺失时直接报错，不回落到代码里的内置默认值。
    一旦代码里留一份隐式默认，就等于有了两个真相来源：
    改了 cfg 却不生效，而且没有任何迹象 —— 这比直接报错难查得多。
    这与平台"NOT RUN ≠ PASS / 不把故障伪装成正常"的原则一致。

格式
    INI（Python 标准库 configparser），不引入任何第三方依赖。
    · 支持 `;` 与 `#` 行首注释，以及 `key = 值  ; 行尾注释`
    · **值里的 `%` 原样保留**（关闭了插值）
    · 相对路径一律相对**工程根**（即 tools/ 的上一级）解析
    · 路径可以写 `/` 或 `\\`，行为一致（Windows 上手写配置的常见情况）

用法
    from project_config import load, resolve
    cfg = load(TOOLS_DIR)                     # 缺失即抛 ConfigMissing
    axf = cfg.path("build", "axf")            # 绝对路径
    port = resolve(args.port, "AUTOLAB_PORT", cfg.get("serial", "port"), "COM9")
"""

import configparser
import os
from pathlib import Path

CFG_BASENAME = "project.cfg"

FIX_HINT = (
    "修复：把适配器里的模板复制成工程配置，再按本工程填写\n"
    "      cp <平台仓库>/adapters/<芯片家族>/project.cfg.example tools/project.cfg"
)

_UNSET = object()          # 哨兵：区分"没传 default"与"传了 None 当 default"


class ConfigError(Exception):
    """配置相关错误的基类（文件缺失 / 字段缺失 / 值非法）。"""


class ConfigMissing(ConfigError):
    """找不到 tools/project.cfg。"""


class ConfigKeyMissing(ConfigError):
    """cfg 存在，但缺少本次读取所必需的 section/key。"""


class ConfigValueInvalid(ConfigError):
    """cfg 里有这个字段，但值无法按期望的类型解析。"""


class ProjectConfig:
    """一个工程的配置。所有相对路径都相对 `root`（tools/ 的上一级）解析。"""

    def __init__(self, cfg_file, root):
        # ⚠️ 属性名是 cfg_file 而不是 path —— 下面有个 path() 方法，同名会互相遮蔽
        self.cfg_file = Path(cfg_file)
        self.root = Path(root)

        cp = configparser.ConfigParser(
            interpolation=None,               # 值里的 % 原样保留，不当作插值符
            inline_comment_prefixes=(";",),   # 支持 `key = 值  ; 说明`
        )
        with open(self.cfg_file, encoding="utf-8") as f:
            cp.read_file(f)
        self._cp = cp

    # ------------------------------------------------------------ 读取

    def has(self, section, key):
        """字段是否存在（section 不存在也算不存在，不抛异常）。"""
        return self._cp.has_option(section, key)

    def get(self, section, key, default=_UNSET):
        """取字符串。字段缺失时：给了 default 就返回 default，没给就抛 ConfigKeyMissing。"""
        if not self._cp.has_option(section, key):
            if default is not _UNSET:
                return default
            raise ConfigKeyMissing(
                f"{self.cfg_file} 缺少 [{section}] {key}\n{FIX_HINT}")
        return self._cp[section][key].strip()

    def int(self, section, key, default=_UNSET):
        raw = self.get(section, key) if default is _UNSET else self.get(section, key, str(default))
        try:
            return int(raw)
        except (TypeError, ValueError):
            raise ConfigValueInvalid(
                f"{self.cfg_file} 的 [{section}] {key} 不是整数：{raw!r}") from None

    def bool(self, section, key, default=False):
        raw = self.get(section, key, "true" if default else "false").strip().lower()
        if raw in ("1", "true", "yes", "on"):
            return True
        if raw in ("0", "false", "no", "off"):
            return False
        raise ConfigValueInvalid(
            f"{self.cfg_file} 的 [{section}] {key} 不是布尔值：{raw!r}"
            f"（可用 true/false）")

    def list(self, section, key):
        """逗号分隔。字段**缺失**即报错；字段存在但为空 -> 空列表。"""
        raw = self.get(section, key)
        return [x.strip() for x in raw.split(",") if x.strip()]

    def path(self, section, key):
        """解析成**绝对路径**，相对工程根。

        字符串里的 `\\` 统一按路径分隔符处理：Windows 用户手写配置时会写反斜杠，
        行为必须与写正斜杠一致，且跨平台可复现（有单测锁定）。
        """
        raw = self.get(section, key)
        return (self.root / raw.replace("\\", "/")).resolve()

    def __repr__(self):
        return f"<ProjectConfig {self.cfg_file}>"


# ---------------------------------------------------------------- 入口

def load(tools_dir):
    """加载工程配置。

    tools_dir：tools/ 的**路径**（通常传 `Path(__file__).resolve().parent`）。
    找不到 cfg 就抛 ConfigMissing —— 不做任何静默兜底。

    ⚠️ 必须在**函数体内**调用，不要在模块 import 期调用：
       preflight.py 会 `import build`，若 build 在 import 期就要求 cfg，
       那么"工程还没建好配置"会表现为"preflight 自身崩了"，把人带偏。
    """
    tools_dir = Path(tools_dir).resolve()
    cfg_file = tools_dir / CFG_BASENAME
    if not cfg_file.is_file():
        raise ConfigMissing(f"找不到 {cfg_file}\n{FIX_HINT}")
    return ProjectConfig(cfg_file, tools_dir.parent)


def resolve(cli_value, env_name, cfg_value, default=None):
    """四级优先级：CLI > 环境变量 > 配置文件 > 内置默认。

    空串一律视为"没提供"：否则 CLI 传一个空字符串会静默盖掉环境变量，
    这类"看起来配了、其实没生效"的问题很难查。
    """
    if cli_value not in (None, ""):
        return cli_value
    if env_name:
        env_value = os.environ.get(env_name)
        if env_value not in (None, ""):
            return env_value
    if cfg_value not in (None, ""):
        return cfg_value
    return default
