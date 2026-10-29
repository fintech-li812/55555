# -*- coding: utf-8 -*-
"""v8 风控算法开关配置（四级覆盖，默认全部回退到原有逻辑）。

为什么需要这一层
----------------
算法侧的几项改动（默认算法版本、证据不足是否放行绿灯、Benford 适用性声明、
Q 是否参与融合判别）都带业务口径含义：同一个数值，在不同口径下会落到不同灯色。
把它们写死在代码里，等于把业务裁决固化成技术细节，事后无法回退也无法灰度。

本模块把这些项统一成**可被配置驱动**的开关，并明确约定：
每个开关的默认值都必须是「改造前的原有行为」，
因此不配置任何东西时，系统表现与加入这些开关之前逐位一致。

四级覆盖（优先级从高到低）
--------------------------
1. **请求参数**：``GET /api/v8/assess?algo=v1&evidence=loose``、POST body、
   ``engine.assess(...)`` 的关键字参数。用于灰度、A/B 与人工复核。
2. **环境变量**：``CF_V8_*``。用于容器 / systemd 部署，不改文件。
3. **配置文件**：``deploy/algorithm.config.json``（可用 ``CF_V8_CONFIG`` 改路径）。
   用于版本化的基线口径，随代码进仓库。
4. **内置默认**：本模块的 ``DEFAULTS``。全部等于改造前行为。

一致性规则
----------
* 配置文件缺失、损坏、含未知键 → 只告警并回落默认值，**绝不让服务起不来**；
* 环境变量取值非法 → 记 WARNING 并回落下一级，不抛异常；
* 请求参数非法 → 与算法参数错误同级处理，返回 400（调用方缺陷，须修调用方）。

用法
----
::

    from v8 import config
    s = config.resolve(algo="v1", evidence="loose")   # 请求级覆盖
    engine_kwargs = s.engine_kwargs()

    config.snapshot()      # 供 /api/v8/config 展示当前生效口径
    config.reload()        # 改完配置文件后热加载，无需重启进程
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading

from .algorithm.validation import ValidationError

LOGGER = logging.getLogger("v8.config")

_SRC = os.path.dirname(os.path.abspath(__file__))


def _find_root() -> str:
    """向上定位仓库根，标志是同时含 ``deploy`` 与 ``web`` 两个目录。

    刻意**不数层级**（``dirname`` 套娃）：本文件在 ``src/v8/`` 下，
    而同级的 ``v8_server.py`` 在 ``src/`` 下，两者到根的层数差一层，
    数层级必然有一处写错，且搬动文件时会静默指错目录。
    """
    path = _SRC
    for _ in range(8):
        if os.path.isdir(os.path.join(path, "deploy")) and \
                os.path.isdir(os.path.join(path, "web")):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    # 兜底：找不到标志目录时退回三层上（历史位置），至少不会指到磁盘根
    return os.path.dirname(os.path.dirname(os.path.dirname(_SRC)))


_ROOT = _find_root()

# ---------------------------------------------------------------- 内置默认
# 每一项都刻意等于「加入开关之前的原有行为」，见下注释。
DEFAULTS = {
    # 默认算法仍是基线 v8：切到 v1 会让 Q 尺度变化并在默认配置下取消绿灯，
    # 属于业务口径变更，必须由业务显式确认后再配。
    "algorithm": {"default": "v8", "allowed": ["v8", "ra-next-0.1", "v1"]},
    # strict = 证据不足不得放行绿灯（改造后的新行为，是本轮默认）；
    # loose  = 恢复三档判别，复核标记仍在。
    "evidence": {"mode": "strict"},
    # Benford 适用性三态：None 未声明（默认）/ True 已论证 / False 不适用。
    # 默认「未声明」→ Q 数值照常进融合表，证据等级记为「不足」。
    "quality": {"benford_applicable": None, "gate_in_fusion": True},
    # 30 次分层 bootstrap 看 D 稳不稳，实测 1~2 秒，默认关闭以免拖慢接口。
    "stability": {"enabled": False},
    # 池级模型缓存是性能优化主体，默认开启；关掉即回到每请求全量重算。
    "cache": {"enabled": True, "max": 64},
    # 上界 60 是硬约束：池级模型构建随规模三次增长（n=100 实测 40 秒），
    # 再高就是 DoS 入口。60 刚好满足「P 层证据充分」所需规模，超出不划算。
    "pool": {"size_per_group": 20},
    "forest": {"trees": 60},
}

# 环境变量映射：CF_V8_<NAME> → (section, key, 解析函数)
_ENV_MAP = {
    "CF_V8_ALGO": ("algorithm", "default", str),
    "CF_V8_EVIDENCE": ("evidence", "mode", str),
    "CF_V8_BENFORD": ("quality", "benford_applicable", str),
    "CF_V8_Q_IN_FUSION": ("quality", "gate_in_fusion", str),
    "CF_V8_STABILITY": ("stability", "enabled", str),
    "CF_V8_CACHE": ("cache", "enabled", str),
    "CF_V8_CACHE_MAX": ("cache", "max", int),
    "CF_V8_POOL_N": ("pool", "size_per_group", int),
    "CF_V8_TREES": ("forest", "trees", int),
}

_TRUE = {"1", "true", "yes", "on", "y", "t"}
_FALSE = {"0", "false", "no", "off", "n", "f", ""}


def _as_bool(text):
    """把环境变量文本转成三态布尔；无法识别返回 None，由调用方决定回落。

    ``""``（空串）视为「未配置」而不是 False——这样"声明了但留空"与
    "声明为假"不会混为一谈。
    """
    t = str(text).strip().lower()
    if t in _TRUE:
        return True
    if t in _FALSE:
        return False
    return None


def _as_tri(text):
    """Benford 适用性三态解析。

    auto/留空 → None（未声明）；1/true → True；0/false → False。
    无法识别返回哨兵 ``_INVALID``，让调用方明确知道"配错了"而不是"没配"。
    """
    t = str(text).strip().lower()
    if t in ("", "auto", "none", "null", "unset"):
        return None
    b = _as_bool(t)
    return b


_INVALID = object()


class Settings:
    """一次评估的生效口径。构造后只读。

    请求级覆盖通过 ``overrides`` 传入，``None`` 表示"这一项没指定"，
    从而与"显式指定为默认值"区分开——否则调用方无法只覆盖其中一项。
    """

    __slots__ = ("algo", "benford_applicable", "require_full_evidence_for_green",
                 "q_in_fusion", "stability", "cache_enabled", "cache_max",
                 "pool_n", "trees", "sources")

    def __init__(self, algo, benford_applicable, require_full_evidence_for_green,
                 q_in_fusion, stability, cache_enabled, cache_max, pool_n, trees,
                 sources=None):
        self.algo = algo
        self.benford_applicable = benford_applicable
        self.require_full_evidence_for_green = require_full_evidence_for_green
        self.q_in_fusion = q_in_fusion
        self.stability = stability
        self.cache_enabled = cache_enabled
        self.cache_max = cache_max
        self.pool_n = pool_n
        self.trees = trees
        self.sources = sources or {}

    def engine_kwargs(self):
        """转成 engine.assess 的关键字参数（不含行业/场景/金额等业务参数）。"""
        return {
            "algo": self.algo,
            "benford_applicable": self.benford_applicable,
            "require_full_evidence_for_green": self.require_full_evidence_for_green,
            "q_in_fusion": self.q_in_fusion,
            "with_stability": self.stability,
            "n_per_group": self.pool_n,
            "n_trees": self.trees,
        }

    def to_dict(self):
        return {
            "算法版本": self.algo,
            "证据模式": "strict" if self.require_full_evidence_for_green else "loose",
            "Benford适用性": (None if self.benford_applicable is None
                          else bool(self.benford_applicable)),
            "Q参与融合判别": bool(self.q_in_fusion),
            "稳定性bootstrap": bool(self.stability),
            "池级缓存": bool(self.cache_enabled),
            "缓存上限": self.cache_max,
            "每组样本量": self.pool_n,
            "森林树数": self.trees,
        }


# ---------------------------------------------------------------- 配置加载

_LOCK = threading.Lock()
_FILE_CACHE = {"path": None, "stamp": None, "data": None}


def config_path():
    """配置文件路径；默认 ``<仓库根>/deploy/algorithm.config.json``。"""
    return os.environ.get("CF_V8_CONFIG") or os.path.join(
        _ROOT, "deploy", "algorithm.config.json")


def _load_file():
    """读取配置文件。任何异常都回落到空字典，不让服务起不来。

    结果按 (路径, mtime) 缓存，避免每个请求都去读盘。
    """
    path = config_path()
    try:
        st = os.stat(path)
    except OSError:
        with _LOCK:
            if _FILE_CACHE["data"] is not None and _FILE_CACHE["path"] == path:
                return _FILE_CACHE["data"]
        LOGGER.info("配置文件 %s 不存在，全部开关取默认值（即改造前行为）", path)
        return {}

    # 缓存键用**内容哈希**而不是 mtime：
    # 部分文件系统 mtime 粒度是秒级，且改动常常大小不变（"v8"→"v1"），
    # 只比 (mtime, size) 会在同一秒内漏检改动，热加载形同虚设。
    try:
        with open(path, "rb") as fh:
            raw_bytes = fh.read()
    except OSError as e:                                    # pragma: no cover
        LOGGER.warning("配置文件 %s 读取失败（%s），全部开关回落默认值",
                       path, type(e).__name__)
        return {}
    digest = hashlib.sha256(raw_bytes).hexdigest()
    with _LOCK:
        cached = _FILE_CACHE
        if cached["path"] == path and cached["stamp"] == digest:
            return cached["data"] or {}

    data = {}
    try:
        raw = json.loads(raw_bytes.decode("utf-8"))
        if isinstance(raw, dict):
            data = raw
            # 拼错的键一旦静默忽略，运维会以为配上了其实没有——必须说出来
            for sec in list(data):
                if sec.startswith("_"):
                    continue
                if sec not in DEFAULTS:
                    LOGGER.warning("配置文件含未知段 %r，已忽略（有效段：%s）",
                                   sec, "、".join(sorted(DEFAULTS)))
                    continue
                if not isinstance(data[sec], dict):
                    LOGGER.warning("配置文件段 %s 不是对象，已忽略", sec)
                    continue
                for k in data[sec]:
                    if k not in DEFAULTS[sec]:
                        LOGGER.warning("配置文件含未知项 %s.%s，已忽略", sec, k)
        else:
            LOGGER.warning("配置文件 %s 顶层不是对象，已忽略", path)
    except (ValueError, UnicodeDecodeError) as e:
        LOGGER.warning("配置文件 %s 解析失败（%s），全部开关回落默认值",
                       path, type(e).__name__)

    with _LOCK:
        _FILE_CACHE.update(path=path, stamp=digest, data=data)
    return data


def reload():
    """清空配置文件缓存，下次读取会重新解析。用于改配置后热加载。"""
    with _LOCK:
        _FILE_CACHE.update(path=None, stamp=None, data=None)
    LOGGER.info("配置已热加载：%s", config_path())
    return _load_file()


def _file_value(section, key):
    data = _load_file()
    sec = data.get(section)
    if isinstance(sec, dict) and key in sec:
        v = sec[key]
        if key == "benford_applicable" and v is not None and not isinstance(v, bool):
            LOGGER.warning("quality.benford_applicable=%r 不是 true/false/null，"
                           "已忽略", v)
            return None
        return v
    return None


def _raw(section, key):
    """取环境变量 > 配置文件 > 内置默认 的值，并返回取值来源。

    环境变量优先于配置文件：随代码进仓库的基线口径保持稳定，
    运维仍可用环境变量临时改口径（例如应急关掉某个开关）而不用改文件、不用发版。
    """
    env_name = None
    for name, (sec, k, _) in _ENV_MAP.items():
        if sec == section and k == key:
            env_name = name
            break
    env_val = os.environ.get(env_name) if env_name else None
    if env_val is not None and str(env_val).strip() != "":
        return env_val, "环境变量 " + env_name
    v = _file_value(section, key)
    if v is not None:
        return v, "配置文件 %s.%s" % (section, key)
    return DEFAULTS[section][key], "内置默认"


def _pick(override, section, key, caster):
    """四级取值：请求参数 > 环境变量 > 配置文件 > 内置默认。"""
    if override is not None:
        if caster is int:
            # 请求参数类型不对是调用方缺陷：静默回落会让调用方以为
            # 自己在评 20 条样本，实际传的是 "abc"。必须 400。
            try:
                return int(override), "请求参数"
            except (TypeError, ValueError):
                raise ValidationError("%s 必须是整数，收到 %r" % (key, override),
                                      field=key)
        return caster(override), "请求参数"
    raw, src = _raw(section, key)
    if caster is int:
        # 来源不同，处理不同：
        #   - 环境变量 / 配置文件配错 → 告警后回落默认（运维输入，退化即可）；
        #   - 请求参数类型不对 → 抛 ValidationError，让上层返回 400。
        #     调用方把 "abc" 传进来是调用方的缺陷，静默回落成 20 会让它
        #     以为自己真的在评 20 条样本。
        try:
            return int(raw), src
        except (TypeError, ValueError):
            if src == "请求参数":
                raise ValidationError("%s 必须是整数，收到 %r"
                                      % (key, raw), field=key)
            LOGGER.warning("%s=%r 不是整数，回落内置默认 %s",
                           section + "." + key, raw, DEFAULTS[section][key])
            return DEFAULTS[section][key], "内置默认（取值非法）"
    return raw, src


def resolve(**overrides):
    """解析出本次评估的生效口径。

    overrides 支持：``algo`` / ``evidence``（"strict"|"loose"）/ ``benford``
    （True|False|"auto"）/ ``q_in_fusion`` / ``stability`` / ``cache`` /
    ``pool_n`` / ``trees``。传 ``None`` 表示"未指定"，走下面三级。

    非法值一律回落下一级而不是抛异常：配置是运维输入，
    配错最多退化成默认行为，不该让整个评估接口 500。
    """
    sources = {}

    # ---- 算法版本：必须落在允许列表内，否则回落默认（不严防会走到 KeyError）
    algo, src = _pick(overrides.get("algo"), "algorithm", "default", str)
    allowed = DEFAULTS["algorithm"]["allowed"]
    if algo not in allowed:
        LOGGER.warning("算法版本 %r 不在允许列表 %s 内，回落 %s",
                       algo, allowed, DEFAULTS["algorithm"]["default"])
        algo, src = DEFAULTS["algorithm"]["default"], "内置默认（取值非法）"
    sources["算法版本"] = src

    # ---- 证据模式：strict / loose，其他一律按 strict（更保守的一侧）
    ev, src = _pick(overrides.get("evidence"), "evidence", "mode", str)
    if isinstance(ev, bool):                       # True → strict
        ev = "strict" if ev else "loose"
    ev = str(ev).strip().lower()
    if ev not in ("strict", "loose"):
        LOGGER.warning("证据模式 %r 非法（只接受 strict/loose），回落 strict", ev)
        ev, src = "strict", "内置默认（取值非法）"
    sources["证据模式"] = src
    strict = ev == "strict"

    # ---- Benford 适用性三态
    ben, src = _pick(overrides.get("benford"), "quality", "benford_applicable", str)
    if isinstance(ben, bool):
        benford = ben
    else:
        t = str(ben).strip().lower() if ben is not None else ""
        if t in ("", "auto", "none", "null", "unset"):
            benford = None
        elif t in _TRUE:
            benford = True
        elif t in _FALSE:
            benford = False
        else:
            LOGGER.warning("Benford 适用性 %r 非法（只接受 1/0/auto），回落 auto", ben)
            benford, src = None, "内置默认（取值非法）"
    sources["Benford适用性"] = src

    # ---- Q 是否参与融合判别
    qif, src = _pick(overrides.get("q_in_fusion"), "quality", "gate_in_fusion", str)
    if isinstance(qif, bool):
        q_in_fusion = qif
    else:
        b = _as_bool(qif)
        if b is None:
            LOGGER.warning("q_in_fusion=%r 非法（只接受 1/0），回落 1", qif)
            q_in_fusion, src = True, "内置默认（取值非法）"
        else:
            q_in_fusion = b
    sources["Q参与融合判别"] = src

    # ---- 稳定性 / 缓存 / 池大小 / 树数
    stab, src = _pick(overrides.get("stability"), "stability", "enabled", str)
    stability = stab if isinstance(stab, bool) else bool(_as_bool(stab))
    sources["稳定性bootstrap"] = src

    cache, src = _pick(overrides.get("cache"), "cache", "enabled", str)
    cache_enabled = cache if isinstance(cache, bool) else bool(_as_bool(cache))
    sources["池级缓存"] = src

    cache_max, _ = _pick(None, "cache", "max", int)
    pool_n, _ = _pick(overrides.get("pool_n"), "pool", "size_per_group", int)
    trees, _ = _pick(overrides.get("trees"), "forest", "trees", int)

    # 数值型开关还要过取值范围，超范围回落默认并告警
    bounds = {"每组样本量": (pool_n, 2, 60, "pool.size_per_group"),
              "森林树数": (trees, 1, 500, "forest.trees"),
              "缓存上限": (cache_max, 1, 512, "cache.max")}
    fixed = {}
    for label, (v, lo, hi, path) in bounds.items():
        if not (lo <= v <= hi):
            d = DEFAULTS[path.split(".")[0]][path.split(".")[1]]
            LOGGER.warning("%s=%r 超出 [%s,%s]，回落 %s", path, v, lo, hi, d)
            fixed[label] = d
        else:
            fixed[label] = v
    pool_n = fixed["每组样本量"]
    trees = fixed["森林树数"]
    cache_max = fixed["缓存上限"]

    return Settings(algo=algo, benford_applicable=benford,
                    require_full_evidence_for_green=strict,
                    q_in_fusion=q_in_fusion, stability=stability,
                    cache_enabled=cache_enabled, cache_max=cache_max,
                    pool_n=pool_n, trees=trees, sources=sources)


def apply_runtime(settings):
    """把缓存类开关落到算法包上（缓存开关影响进程级状态，需显式应用）。

    只动缓存：其他开关都是纯函数参数，随 ``engine_kwargs()`` 传下去即可。
    """
    from . import algorithm as algs
    algs.set_cache_enabled(settings.cache_enabled)
    algs.set_cache_max(settings.cache_max)
    return settings


def snapshot():
    """当前生效口径 + 来源，供 ``/api/v8/config`` 展示与上线核验。"""
    s = resolve()
    return {
        "配置版本": "v8-switches-v1",
        "配置文件": config_path(),
        "配置文件存在": os.path.exists(config_path()),
        "生效口径": s.to_dict(),
        "取值来源": s.sources,
        "内置默认": {k: dict(v) for k, v in DEFAULTS.items()},
        "环境变量映射": sorted(_ENV_MAP.keys()),
    }
