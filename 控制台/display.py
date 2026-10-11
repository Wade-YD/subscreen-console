# -*- coding: utf-8 -*-
"""显示器枚举 + 亮度/色彩控制: 伽马 LUT (主) 与 DDC/CI (可用时自动升级)"""

import ctypes
import math
import time
from ctypes import wintypes as wt

user32 = ctypes.WinDLL('user32', use_last_error=True)
gdi32 = ctypes.WinDLL('gdi32', use_last_error=True)
dxva2 = ctypes.WinDLL('dxva2', use_last_error=True)


class _RECT(ctypes.Structure):
    _fields_ = [('left', wt.LONG), ('top', wt.LONG), ('right', wt.LONG), ('bottom', wt.LONG)]


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [('cbSize', wt.DWORD),
                ('rcMonitor', _RECT), ('rcWork', _RECT),
                ('dwFlags', wt.DWORD),
                ('szDevice', wt.WCHAR * 32)]


_MONITORENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HMONITOR, wt.HDC, ctypes.POINTER(_RECT), wt.LPARAM)


class _PHYSICAL_MONITOR(ctypes.Structure):
    _fields_ = [('hPhysicalMonitor', wt.HANDLE),
                ('szPhysicalMonitorDescription', wt.WCHAR * 128)]


dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR.argtypes = [wt.HMONITOR, ctypes.POINTER(wt.DWORD)]
dxva2.GetPhysicalMonitorsFromHMONITOR.argtypes = [wt.HMONITOR, wt.DWORD, ctypes.POINTER(_PHYSICAL_MONITOR)]
dxva2.DestroyPhysicalMonitors.argtypes = [wt.DWORD, ctypes.POINTER(_PHYSICAL_MONITOR)]
dxva2.GetVCPFeatureAndVCPFeatureReply.argtypes = [wt.HANDLE, wt.BYTE, ctypes.POINTER(wt.DWORD),
                                                  ctypes.POINTER(wt.DWORD), ctypes.POINTER(wt.DWORD)]
dxva2.SetVCPFeature.argtypes = [wt.HANDLE, wt.BYTE, wt.DWORD]


def enumerate_displays():
    r"""→ [{'hmon': int, 'device': '\\\\.\\DISPLAY2', 'primary': bool, 'rect': (l, t, r, b)}]"""
    out = []

    def cb(hmon, hdc, lprc, lparam):
        mi = _MONITORINFOEXW()
        mi.cbSize = ctypes.sizeof(_MONITORINFOEXW)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            rc = mi.rcMonitor
            out.append({'hmon': hmon, 'device': mi.szDevice,
                        'primary': bool(mi.dwFlags & 1),
                        'rect': (rc.left, rc.top, rc.right, rc.bottom)})
        return True

    user32.EnumDisplayMonitors(None, None, _MONITORENUMPROC(cb), 0)
    return out


def pick_target(monitors):
    """控制对象 = 非主屏 (副屏); 只有一块屏时退回主屏"""
    cands = [m for m in monitors if not m['primary']]
    return cands[0] if cands else monitors[0]


# ---------- 伽马 LUT (软件级亮度/色彩) ----------

def create_dc(device):
    return gdi32.CreateDCW(None, device, None, None) or None


def delete_dc(hdc):
    if hdc:
        gdi32.DeleteDC(hdc)


def get_gamma(hdc):
    buf = (wt.WORD * 768)()
    if not gdi32.GetDeviceGammaRamp(hdc, buf):
        return None
    return list(buf)


def set_gamma(hdc, ramp):
    return bool(gdi32.SetDeviceGammaRamp(hdc, (wt.WORD * 768)(*ramp)))


def get_kelvin_gain(kelvin):
    """色温 (K, 2000..6500) → 归一化 RGB 增益 (最大通道=1); Tanner Helland 近似"""
    k = max(2000.0, min(6500.0, float(kelvin)))
    if k >= 6500.0:
        return (1.0, 1.0, 1.0)
    t = k / 100.0
    # 红
    if t <= 66.0:
        r = 255.0
    else:
        r = min(255.0, max(0.0, 329.698727446 * (t - 60.0) ** -0.1332047592))
    # 绿
    if t <= 66.0:
        g = 99.4708025861 * math.log(t) - 161.1195681661
    else:
        g = 288.1221695283 * (t - 60.0) ** -0.0755148492
    g = min(255.0, max(0.0, g))
    # 蓝
    if t >= 66.0:
        b = 255.0
    elif t <= 19.0:
        b = 0.0
    else:
        b = min(255.0, max(0.0, 138.5177312231 * math.log(t - 10.0) - 305.0447927307))
    m = max(r, g, b)
    return (r / m, g / m, b / m)


def make_ramp(brightness, kelvin=6500, mode='power'):
    """亮度 (1..100) 与色温 (2000..6500K) → 768 项 LUT

    mode='linear' (需 GdiIcmGammaRange 已在本会话生效): e=1, g=亮度/100 ——
          白位=设定值 (背光语义), 黑位不动, 灰阶等比缩放 → 不动对比度
    mode='power' (未解禁/键未生效会话的回退): 幂曲线保"白位恒满 65535" ——
          GDI 对 ramp 有每点偏离 identity 上限 (~±32768) 的校验: 白位降到半程
          以下即触限被拒, 只能压中间调, 代价是暗部压缩的对比感损失;
          f_c < 0.501 逐通道抬底 (不超过 g)
    out(i) = round(65535 * f_c * (i/255)^e), f_c = g * gain_c
    """
    br = max(1, min(100, int(brightness)))
    if mode == 'linear':
        e = 1.0
        g = br / 100.0
        floor = 0.0
    else:
        e = min(100.0 / br, 4.0)
        g = 1.0
        floor = 0.501
    kr, kg, kb = get_kelvin_gain(kelvin)
    ramp = []
    for gain in (kr, kg, kb):
        f = min(g, max(g * gain, floor))
        ramp.extend(min(65535, max(0, round(65535.0 * f * (i / 255.0) ** e))) for i in range(256))
    return ramp


# ---------- DDC/CI (硬件级, 屏支持时使用) ----------

VCP_BRIGHTNESS = 0x10
VCP_CONTRAST = 0x12
VCP_R_GAIN = 0x16
VCP_G_GAIN = 0x18
VCP_B_GAIN = 0x1A


def get_physical_monitor(hmon):
    """→ (物理显示器数组[保活], 名称) 或 (None, None)"""
    n = wt.DWORD()
    if not dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(hmon, ctypes.byref(n)) or n.value < 1:
        return None, None
    arr = (_PHYSICAL_MONITOR * n.value)()
    if not dxva2.GetPhysicalMonitorsFromHMONITOR(hmon, n.value, arr):
        return None, None
    return arr, arr[0].szPhysicalMonitorDescription


def destroy_physical_monitor(arr):
    if arr:
        dxva2.DestroyPhysicalMonitors(len(arr), arr)


def ddc_read(handle, code):
    """→ (当前值, 最大值) 或 None (3 次重试)"""
    cur, mx, vt = wt.DWORD(), wt.DWORD(), wt.DWORD()
    for _ in range(3):
        if dxva2.GetVCPFeatureAndVCPFeatureReply(handle, wt.BYTE(code),
                                                 ctypes.byref(vt), ctypes.byref(cur), ctypes.byref(mx)):
            return cur.value, mx.value
        time.sleep(0.15)
    return None


def ddc_write(handle, code, value):
    for _ in range(3):
        if dxva2.SetVCPFeature(handle, wt.BYTE(code), wt.DWORD(int(value))):
            return True
        time.sleep(0.15)
    return False
