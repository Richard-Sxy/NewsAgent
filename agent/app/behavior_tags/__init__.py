"""兼容入口：包已迁移到 app.modules.behavior_tags，旧子模块路径经本代理继续可用。"""
import sys as _sys
from app.modules import behavior_tags as _new
_sys.modules[__name__] = _new
