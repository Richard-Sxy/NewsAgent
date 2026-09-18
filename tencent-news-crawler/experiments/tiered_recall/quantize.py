"""分层精度：fp32 / fp16 / int8 / binary 的量化与还原。

向量约定为 L2 归一化，使用内积度量；量化后仍还原为 fp32 供 hnswlib 建索引，
但按目标精度记账存储体积，用召回损失反映“越冷越近似”。
"""

from __future__ import annotations

import numpy as np

FP32, FP16, INT8, BINARY = "fp32", "fp16", "int8", "binary"
PRECISIONS = (FP32, FP16, INT8, BINARY)


def roundtrip(v: np.ndarray, precision: str) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32)
    if precision == FP32:
        return v.copy()
    if precision == FP16:
        return v.astype(np.float16).astype(np.float32)
    if precision == INT8:
        scale = np.abs(v).max(axis=1, keepdims=True)
        scale[scale == 0] = 1.0
        codes = np.clip(np.round(v / scale * 127.0), -127, 127)
        return (codes / 127.0 * scale).astype(np.float32)
    if precision == BINARY:
        return (np.sign(v) / np.sqrt(v.shape[1])).astype(np.float32)
    raise ValueError(f"unknown precision: {precision}")


def storage_bytes(n: int, dim: int, precision: str) -> int:
    if precision == FP32:
        return n * dim * 4
    if precision == FP16:
        return n * dim * 2
    if precision == INT8:
        return n * dim + n * 4
    if precision == BINARY:
        return n * ((dim + 7) // 8)
    raise ValueError(f"unknown precision: {precision}")
