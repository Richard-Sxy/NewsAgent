"""兼容入口：包已迁移到 simulation.warehouse_simulation。"""
import sys as _sys
import simulation.warehouse_simulation as _new
_sys.modules[__name__] = _new
