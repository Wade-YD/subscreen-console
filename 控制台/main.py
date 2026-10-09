# -*- coding: utf-8 -*-
"""副屏控制台 (pywebview): 触摸开关 + 亮度/色彩调节
引擎自动选择: 目标屏 DDC/CI 可用 → 硬件级 (亮度/对比度); 否则 → 伽马 LUT 软件级 (亮度/RGB 增益)
状态持久化: HKCU\\Software\\副屏控制台 (注册表, 不落盘)
"""
import ctypes
import json
import os
import sys
import threading
import time
from ctypes import wintypes as wt

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _project_file(*parts):
    """项目内文件路径: 规范化 + 越界校验, 防路径穿越"""
    root = os.path.realpath(HERE)
    p = os.path.realpath(os.path.join(root, *parts))
    if p != root and not p.startswith(root + os.sep):
        raise ValueError('path escapes project dir: %s' % (parts,))
    return p


UI_FILE = _project_file('ui', 'index.html')

import display
import touchctl
import webview

CFG = {'touch_hwid': 'VID_1A86&PID_E5E3', 'gamma_watchdog_sec': 10}

kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
_adv = ctypes.WinDLL('advapi32', use_last_error=True)
_REG_PATH = r'Software\副屏控制台'
_KEY_WRITE = 0x20006
_KEY_READ = 0x20019
_REG_BINARY = 3

_adv.RegOpenKeyExW.argtypes = [ctypes.c_void_p, wt.LPCWSTR, wt.DWORD, wt.DWORD,
                               ctypes.POINTER(ctypes.c_void_p)]
_adv.RegCreateKeyExW.argtypes = [ctypes.c_void_p, wt.LPCWSTR, wt.DWORD, wt.LPCWSTR,
                                 wt.DWORD, wt.DWORD, ctypes.c_void_p,
                                 ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
_adv.RegSetValueExW.argtypes = [ctypes.c_void_p, wt.LPCWSTR, wt.DWORD, wt.DWORD,
                                ctypes.c_char_p, wt.DWORD]
_adv.RegQueryValueExW.argtypes = [ctypes.c_void_p, wt.LPCWSTR, ctypes.c_void_p,
                                  ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(wt.DWORD)]
_adv.RegCloseKey.argtypes = [ctypes.c_void_p]


def load_state():
    h = ctypes.c_void_p()
    if _adv.RegOpenKeyExW(0x80000001, _REG_PATH, 0, _KEY_READ, ctypes.byref(h)) != 0:
        return None
    buf = ctypes.create_string_buffer(4096)
    got = wt.DWORD(4096)
    rc = _adv.RegQueryValueExW(h, 'state', None, None, buf, ctypes.byref(got))
    _adv.RegCloseKey(h)
    if rc != 0 or not got.value:
        return None
    try:
        return json.loads(buf.raw[:got.value].decode('utf-8'))
    except Exception:
        return None


def save_state(d):
    h = ctypes.c_void_p()
    if _adv.RegCreateKeyExW(0x80000001, _REG_PATH, 0, None, 0, _KEY_WRITE,
                            None, ctypes.byref(h), None) != 0:
        return
    data = json.dumps(d).encode('utf-8')
    _adv.RegSetValueExW(h, 'state', 0, _REG_BINARY, data, len(data))
    _adv.RegCloseKey(h)


def _another_instance_running():
    kernel32.CreateMutexW(None, False, 'SubScreenConsole')
    return ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS


_DDC_MAP = [('brightness', display.VCP_BRIGHTNESS), ('contrast', display.VCP_CONTRAST),
            ('r', display.VCP_R_GAIN), ('g', display.VCP_G_GAIN), ('b', display.VCP_B_GAIN)]


class Api:
    def __init__(self):
        self.lock = threading.Lock()
        self.displays = display.enumerate_displays()
        self.target = display.pick_target(self.displays)
        self.hdc = None
        self.ddc_arr = None
        self.ddc_pm = None
        self.ddc_max = {}
        self.engine = 'gamma'
        self.values = {'brightness': 100, 'contrast': 100, 'r': 100, 'g': 100, 'b': 100}
        self._init_engine()
        threading.Thread(target=self._watchdog, daemon=True).start()

    # ---------- 引擎初始化 ----------
    def _init_engine(self):
        with self.lock:
            arr, _name = display.get_physical_monitor(self.target['hmon'])
            handle = arr[0].hPhysicalMonitor if arr else None
            if handle and display.ddc_read(handle, display.VCP_BRIGHTNESS) is not None:
                self.engine = 'ddc'
                self.ddc_arr, self.ddc_pm = arr, handle
                for key, code in _DDC_MAP:
                    r = display.ddc_read(handle, code)
                    if r:
                        self.ddc_max[key] = r[1]
                        self.values[key] = round(r[0] / r[1] * 100)
                return
            display.destroy_physical_monitor(arr)
            self.engine = 'gamma'
            self.hdc = display.create_dc(self.target['device'])
            self._load()
            self._apply_gamma()

    # ---------- 状态持久化 (注册表) ----------
    def _load(self):
        d = load_state() or {}
        for k in ('brightness', 'r', 'g', 'b'):
            if k in d:
                self.values[k] = max(0, min(100, int(d[k])))

    def _save(self):
        save_state(self.values)

    # ---------- 应用 ----------
    def _apply_gamma(self):
        display.set_gamma(self.hdc, display.make_ramp(
            self.values['brightness'], self.values['r'], self.values['g'], self.values['b']))

    def _apply_ddc(self):
        for key, code in _DDC_MAP:
            mx = self.ddc_max.get(key)
            if mx:
                display.ddc_write(self.ddc_pm, code, round(self.values[key] / 100 * mx))

    def _watchdog(self):
        """伽马模式: 睡眠唤醒/切分辨率后系统会重置 LUT, 周期比对并补涂"""
        while True:
            time.sleep(CFG['gamma_watchdog_sec'])
            try:
                if self.engine == 'gamma' and self.hdc:
                    with self.lock:
                        if display.get_gamma(self.hdc) != display.make_ramp(
                                self.values['brightness'], self.values['r'],
                                self.values['g'], self.values['b']):
                            self._apply_gamma()
            except Exception:
                pass

    # ---------- JS API ----------
    def get_state(self):
        with self.lock:
            snap = {'engine': self.engine, 'values': dict(self.values),
                    'monitor': {'device': self.target['device'], 'rect': self.target['rect']}}
        snap['touch'] = touchctl.find_touch_devices(CFG['touch_hwid'])
        return snap

    def set_value(self, key, val):
        if key not in ('brightness', 'contrast', 'r', 'g', 'b'):
            return {'ok': False}
        if key == 'contrast' and self.engine != 'ddc':
            return {'ok': False}
        with self.lock:
            self.values[key] = max(0, min(100, int(val)))
            if self.engine == 'ddc':
                self._apply_ddc()
            else:
                self._apply_gamma()
        self._save()
        return {'ok': True}

    def set_values(self, vals):
        clean = {k: max(0, min(100, int(v))) for k, v in vals.items()
                 if k in ('brightness', 'r', 'g', 'b')}
        with self.lock:
            self.values.update(clean)
            if self.engine == 'ddc':
                self._apply_ddc()
            else:
                self._apply_gamma()
        self._save()
        return {'ok': True, 'values': dict(self.values)}

    def toggle_touch(self, instance_id, enable):
        ok, msg = touchctl.toggle(instance_id, enable)
        return {'ok': ok, 'msg': msg, 'touch': touchctl.find_touch_devices(CFG['touch_hwid'])}

    def reset_all(self):
        with self.lock:
            self.values = {'brightness': 100, 'contrast': 100, 'r': 100, 'g': 100, 'b': 100}
            if self.engine == 'ddc':
                self._apply_ddc()
            else:
                self._apply_gamma()
        self._save()
        return {'ok': True}


def main():
    if _another_instance_running():
        sys.exit(0)
    api = Api()
    l, t, r, b = api.target['rect']
    w, h = 430, 780
    x = max(l, l + ((r - l) - w) // 2)
    y = max(t, t + ((b - t) - h) // 2)
    webview.create_window('副屏控制台', UI_FILE,
                          js_api=api, width=w, height=h, x=int(x), y=int(y),
                          background_color='#1E1E2E', min_size=(380, 680))
    webview.start()


if __name__ == '__main__':
    main()
