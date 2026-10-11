# -*- coding: utf-8 -*-
"""日出日落时刻估算 (NOAA 简化算法, 纯标准库) — 供夜间自动暖色判断"""
import datetime
import math

DEFAULT_LAT = 39.9042   # 默认: 北京
DEFAULT_LON = 116.4074


def sun_times(lat, lon, date, zenith=90.833):
    """→ (日出分钟, 日落分钟) 当地时间 0..1439; 极昼/极夜等无解时返回 (None, None)"""
    n = date.timetuple().tm_yday
    g = 2 * math.pi / 365.0 * (n - 1 + 0.5)  # 取当天正午的"分数年"
    eq = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                   - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    dec = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
           - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
           - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    la = math.radians(lat)
    cos_ha = (math.cos(math.radians(zenith)) / (math.cos(la) * math.cos(dec))
              - math.tan(la) * math.tan(dec))
    if not -1.0 <= cos_ha <= 1.0:
        return None, None
    ha = math.degrees(math.acos(cos_ha))
    off = datetime.datetime.combine(date, datetime.time(12, 0))
    off = off.astimezone().utcoffset().total_seconds() / 60.0  # 当地时区偏移(分钟)
    rise = int(round(720 - 4 * (lon + ha) - eq + off)) % 1440
    setm = int(round(720 - 4 * (lon - ha) - eq + off)) % 1440
    return rise, setm
