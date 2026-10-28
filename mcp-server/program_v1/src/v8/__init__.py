# -*- coding: utf-8 -*-
"""v8 三层风控模型包（纯 Python 标准库实现，零外部依赖）。

第一层 数据质量门禁 → Q ｜ 第二层 改进 DEA-DA → D ｜ 第三层 Benford-随机森林 → P
融合判定 FUSE(Q, D, P) → 红/黄/绿灯
"""
