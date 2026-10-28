# -*- coding: utf-8 -*-
"""Cashflow Risk Agent —— 常驻服务包。

能力：数据读取汇总 / 定时巡检告警 / 周报推送 / 前端对话 / 决策卡片。
约束：最小权限（DB 仅 SELECT+INSERT）、配置与实现分离、关键路径留痕。
"""
__version__ = "1.0.0"
