"""输电网单线退出风险筛查 —— 应用包。

计算模式为大量小矩阵运算（100×100 量级），多线程 BLAS 在此规模下
没有收益，反而可能引入每调用线程调度抖动；默认限定单线程 BLAS
（显式设置过环境变量的场景尊重用户配置）。
"""
import os

for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_var, "1")
