"""兼容入口：包已迁移到 app.modules.data_loop.evaluation。"""
import sys as _sys
import app.modules.data_loop.evaluation as _new
_sys.modules[__name__] = _new
