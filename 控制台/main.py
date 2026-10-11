# -*- coding: utf-8 -*-
"""副屏控制台 (pywebview): 触摸开关 + 亮度/色温调节 + 托盘常驻 + 夜间自动暖色
引擎自动选择: 目标屏 DDC/CI 可用 → 硬件级 (亮度/对比度/色温增益); 否则 → 伽马 LUT 软件级 (亮度/色温)
状态持久化: HKCU\\Software\\副屏控制台 (注册表, 不落盘)
"""
import ctypes
import datetime
import json
import os
import subprocess
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
import sunset
import webview

CFG = {'touch_hwid': 'VID_1A86&PID_E5E3', 'gamma_watchdog_sec': 10}

kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
user32 = ctypes.WinDLL('user32', use_last_error=True)
shell32 = ctypes.WinDLL('shell32', use_last_error=True)
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

user32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
user32.UnregisterHotKey.argtypes = [wt.HWND, ctypes.c_int]
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]

_RUN_KEY = r'HKCU\Software\Microsoft\Windows\CurrentVersion\Run'
_RUN_VALUE = 'SubScreenConsole'
_ICM_REG_ADD = (r'add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ICM"'
                r' /v GdiIcmGammaRange /t REG_DWORD /d 1 /f')
_BR_MIN_LOCKED = 20    # 未解禁亮度下限: 实测 GDI 两条约束 (单通道峰值≥32768 且陡曲线不允许峰值缩水), b≤19 均被拒, b=20 可过
_BR_MIN_UNLOCKED = 1   # HKLM GdiIcmGammaRange=1 解禁后的下限
_KELVIN_ANCHORS = [6500, 5000, 4500, 4000, 3400, 2700]  # 热键 ←→ 步进锚点
_NIGHT_KELVIN = 3400
_HK_MOD = 0x0002 | 0x0001  # MOD_CONTROL | MOD_ALT
_HOTKEYS = [(1, 0x26), (2, 0x28), (3, 0x25), (4, 0x27), (5, 0x4D), (6, 0x51)]  # ↑ ↓ ← → M Q
_WM_HOTKEY = 0x0312
_CREATE_NO_WINDOW = 0x08000000


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


def _autostart_enabled():
    try:
        p = subprocess.run(['reg', 'query', _RUN_KEY, '/v', _RUN_VALUE],
                           capture_output=True, creationflags=_CREATE_NO_WINDOW)
        return p.returncode == 0
    except Exception:
        return False


_DDC_MAP = [('brightness', display.VCP_BRIGHTNESS), ('contrast', display.VCP_CONTRAST),
            ('r', display.VCP_R_GAIN), ('g', display.VCP_G_GAIN), ('b', display.VCP_B_GAIN)]


class Api:
    def __init__(self):
        self.lock = threading.Lock()
        self.displays = display.enumerate_displays()
        saved = (load_state() or {}).get('device')
        self.target = (next((m for m in self.displays if m['device'] == saved), None)
                       or display.pick_target(self.displays))
        self.hdc = None
        self.ddc_arr = None
        self.ddc_pm = None
        self.ddc_max = {}
        self.engine = 'gamma'
        self.values = {'brightness': 100, 'contrast': 100, 'r': 100, 'g': 100, 'b': 100,
                       'kelvin': 6500, 'night_mode': False, 'night_kelvin': _NIGHT_KELVIN,
                       'lat': sunset.DEFAULT_LAT, 'lon': sunset.DEFAULT_LON}
        self.apply_ok = True
        self._gdi_unlocked = bool(display.is_gdi_unlocked())
        self._night_applied = None
        self._hidden = False
        self._exiting = False
        self._tray_ok = False
        self._tray_icon = None
        self._window = None
        self._load()  # 先恢复持久化值 (DDC 模式下硬件读数稍后覆盖亮度/增益, 色温与夜间设置保留)
        self._init_engine()
        threading.Thread(target=self._watchdog, daemon=True).start()
        threading.Thread(target=self._hotkey_loop, daemon=True).start()
        threading.Thread(target=self._night_loop, daemon=True).start()

    # ---------- 引擎 (以下 _ 开头方法除特殊注明外均须持有 self.lock) ----------
    def _init_engine(self):
        """探测引擎 (DDC 优先, 失败回退伽马) 并应用当前值; 换屏重建时复用"""
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
        self._apply_gamma()

    def _release_engine(self):
        """销毁当前引擎句柄 (DC / 物理显示器), 换屏前调用"""
        display.delete_dc(self.hdc)
        self.hdc = None
        display.destroy_physical_monitor(self.ddc_arr)
        self.ddc_arr = None
        self.ddc_pm = None
        self.ddc_max = {}

    # ---------- 状态持久化 (注册表) ----------
    def _load(self):
        d = load_state() or {}
        for k in ('brightness', 'r', 'g', 'b', 'contrast'):
            if k in d:
                self.values[k] = max(0, min(100, int(d[k])))
        if 'kelvin' in d:
            self.values['kelvin'] = max(2000, min(6500, int(d['kelvin'])))
        self.values['night_mode'] = bool(d.get('night_mode', False))
        if 'night_kelvin' in d:
            self.values['night_kelvin'] = max(2000, min(6500, int(d['night_kelvin'])))
        try:
            if 'lat' in d:
                self.values['lat'] = float(d['lat'])
            if 'lon' in d:
                self.values['lon'] = float(d['lon'])
        except (TypeError, ValueError):
            pass

    def _save(self):
        d = dict(self.values)
        d['device'] = self.target['device']
        save_state(d)

    # ---------- 应用 ----------
    def _br_min(self):
        """亮度下限由 GDI 解禁状态决定 (伽马引擎的硬件约束, 统一应用以保持 UI 一致)"""
        return _BR_MIN_UNLOCKED if self._gdi_unlocked else _BR_MIN_LOCKED

    def _apply_gamma(self):
        """伽马写入; 记录系统是否接受 (apply_ok 供 UI 提示) → bool"""
        self.apply_ok = bool(display.set_gamma(
            self.hdc, display.make_ramp(self.values['brightness'], self.values['kelvin'])))
        return self.apply_ok

    def _apply_ddc(self):
        # 色温 → RGB 硬件增益 (get_kelvin_gain 已按最大通道归一化到 1, ×100 后 ≤100)
        rg, gg, bg = display.get_kelvin_gain(self.values.get('kelvin', 6500))
        self.values['r'] = round(rg * 100)
        self.values['g'] = round(gg * 100)
        self.values['b'] = round(bg * 100)
        for key, code in _DDC_MAP:
            mx = self.ddc_max.get(key)
            if mx:
                display.ddc_write(self.ddc_pm, code, round(self.values[key] / 100 * mx))

    def _apply_current(self):
        """把当前 values 应用到引擎 → bool"""
        if self.engine == 'ddc':
            self._apply_ddc()
            self.apply_ok = True
            return True
        return self._apply_gamma()

    def _watchdog(self):
        """每 10s: 目标屏被拔 → 换屏重建; 伽马模式 LUT 被系统重置 (睡眠唤醒/切分辨率) → 补涂"""
        while True:
            time.sleep(CFG['gamma_watchdog_sec'])
            rebuilt = False
            try:
                with self.lock:
                    mons = display.enumerate_displays()
                    if mons and not any(m['device'] == self.target['device'] for m in mons):
                        self._release_engine()
                        self.displays = mons
                        self.target = display.pick_target(mons)
                        self._init_engine()
                        self._save()
                        rebuilt = True
                    elif self.engine == 'gamma' and self.hdc:
                        if display.get_gamma(self.hdc) != display.make_ramp(
                                self.values['brightness'], self.values['kelvin']):
                            self._apply_gamma()
            except Exception:
                pass
            if rebuilt:
                self._sync_ui()

    # ---------- 全局热键 (独立线程, daemon) ----------
    def _hotkey_loop(self):
        for hid, vk in _HOTKEYS:
            if not user32.RegisterHotKey(None, hid, _HK_MOD, vk):
                for h2, _v in _HOTKEYS:  # 任一注册失败 (被占用) → 全部放弃, 热键禁用
                    user32.UnregisterHotKey(None, h2)
                return
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == _WM_HOTKEY:
                try:
                    self._on_hotkey(msg.wParam)
                except Exception:
                    pass

    def _on_hotkey(self, hid):
        if hid in (1, 2):  # ↑ / ↓ 亮度 ±10
            step = 10 if hid == 1 else -10
            with self.lock:
                self.values['brightness'] = max(self._br_min(),
                                                min(100, self.values['brightness'] + step))
                self._apply_current()
            self._save()
            self._sync_ui()
        elif hid in (3, 4):  # ← 更暖 / → 更冷 (锚点间移动)
            moved = False
            with self.lock:
                k = int(self.values['kelvin'])
                t = (next((a for a in _KELVIN_ANCHORS if a > k), None) if hid == 4
                     else next((a for a in reversed(_KELVIN_ANCHORS) if a < k), None))
                if t:
                    self.values['kelvin'] = t
                    self._apply_current()
                    moved = True
            if moved:
                self._save()
                self._sync_ui()
        elif hid == 5:  # M 触摸开关 (走 UAC)
            devs = touchctl.find_touch_devices(CFG['touch_hwid'])
            if devs:
                touchctl.toggle(devs[0]['id'], not devs[0]['enabled'])
            self._sync_ui()
        elif hid == 6:  # Q 面板显隐
            self._toggle_window()

    def _sync_ui(self):
        try:
            w = webview.windows[0]
            if w:
                w.evaluate_js('window.__sync && window.__sync()')
        except Exception:
            pass  # 窗口未就绪 / 已销毁

    def _toggle_window(self):
        try:
            w = webview.windows[0]
            if self._hidden:
                w.show()
                self._hidden = False
            else:
                w.hide()
                self._hidden = True
        except Exception:
            pass

    # ---------- 夜间自动暖色 (独立线程, daemon) ----------
    def _night_loop(self):
        while True:
            time.sleep(60)
            try:
                self._night_check()
            except Exception:
                pass

    def _night_target(self):
        """→ 当前应处的色温 (夜=night_kelvin / 昼=6500); 无日落数据 → None"""
        now = datetime.datetime.now()
        rise, setm = sunset.sun_times(self.values.get('lat', sunset.DEFAULT_LAT),
                                      self.values.get('lon', sunset.DEFAULT_LON), now.date())
        if rise is None or setm is None or setm <= rise:
            return None
        cur = now.hour * 60 + now.minute
        night = cur >= setm or cur < rise  # 日落后 → 日出前
        return int(self.values.get('night_kelvin', _NIGHT_KELVIN)) if night else 6500

    def _night_check(self):
        if not self.values.get('night_mode'):
            return
        target = self._night_target()
        if target is None or target == self._night_applied:
            return
        self._fade_kelvin(target)  # 简化: 日落切换视为一次 set, 此后不再覆盖手动调节
        self._night_applied = target

    def _fade_kelvin(self, target):
        """10 步渐变到目标色温 (每步 200ms)"""
        src = int(self.values.get('kelvin', 6500))
        for i in range(1, 11):
            k = int(round(src + (target - src) * i / 10.0))
            with self.lock:
                self.values['kelvin'] = k
                self._apply_current()
            time.sleep(0.2)
        self._save()
        self._sync_ui()

    # ---------- 托盘 + 关窗行为 ----------
    def attach_window(self, window):
        """注册关窗行为 + 启动托盘 (须在 create_window 之后、start 之前调用)"""
        self._window = window

        def on_closing():
            # pywebview 5.x: closing 处理器返回 False 即取消关闭。
            # 方案: 有托盘 → 阻止关闭并隐藏到托盘; 无托盘 (pystray 不可用) 或退出中 → 放行 (关窗即退出)。
            if self._exiting:
                return True
            if self._tray_ok:
                try:
                    window.hide()
                    self._hidden = True
                except Exception:
                    pass
                return False
            return True

        try:
            window.events.closing += on_closing
        except Exception:
            pass
        self._start_tray()

    def _start_tray(self):
        """托盘常驻; pystray/Pillow 不可用时降级: 无托盘, 关窗即退出 (热键 Q 仍可隐藏)"""
        try:
            import pystray
            from PIL import Image, ImageDraw
        except Exception:
            self._tray_ok = False
            return

        def make_icon():
            img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.rounded_rectangle((4, 4, 60, 60), radius=16, fill=(49, 50, 68, 255))
            d.ellipse((18, 12, 46, 40), fill=(250, 179, 135, 255))   # 太阳
            d.ellipse((30, 18, 52, 40), fill=(49, 50, 68, 255))      # 咬出月牙
            d.rectangle((14, 46, 50, 51), fill=(166, 227, 161, 255))  # 底部横条
            return img

        def on_show(icon, item):
            try:
                self._window.show()
                self._hidden = False
            except Exception:
                pass

        def on_quit(icon, item):
            self._exit_app(icon)

        icon = pystray.Icon('SubScreenConsole', make_icon(), '副屏控制台',
                            pystray.Menu(pystray.MenuItem('显示', on_show, default=True),
                                         pystray.MenuItem('退出', on_quit)))
        self._tray_icon = icon
        self._tray_ok = True
        threading.Thread(target=icon.run, daemon=True).start()

    def _exit_app(self, icon=None):
        self._exiting = True
        try:
            ic = icon or self._tray_icon
            if ic:
                ic.stop()
        except Exception:
            pass
        try:
            if self._window:
                self._window.destroy()
        except Exception:
            pass
        os._exit(0)

    # ---------- JS API ----------
    def get_state(self):
        with self.lock:
            self._gdi_unlocked = bool(display.is_gdi_unlocked())
            snap = {'engine': self.engine,
                    'values': dict(self.values),
                    'apply_ok': bool(self.apply_ok),
                    'gdi_unlocked': self._gdi_unlocked,
                    'autostart': _autostart_enabled(),
                    'monitor': {'device': self.target['device'], 'rect': self.target['rect']},
                    'target_device': self.target['device'],
                    'displays': [{'device': m['device'], 'primary': m['primary']}
                                 for m in self.displays]}
        snap['touch'] = touchctl.find_touch_devices(CFG['touch_hwid'])
        return snap

    def set_value(self, key, val):
        if key not in ('brightness', 'contrast', 'kelvin'):
            return {'ok': False}
        if key == 'contrast' and self.engine != 'ddc':
            return {'ok': False}
        with self.lock:
            if key == 'kelvin':
                self.values['kelvin'] = max(2000, min(6500, int(val)))
            elif key == 'brightness':
                self.values['brightness'] = max(self._br_min(), min(100, int(val)))
            else:
                self.values[key] = max(0, min(100, int(val)))
            ok = self._apply_current()
        self._save()
        return {'ok': True, 'apply_ok': bool(ok)}

    def set_values(self, vals):
        clean = {k: max(0, min(100, int(v))) for k, v in vals.items()
                 if k in ('brightness', 'r', 'g', 'b')}
        with self.lock:
            if 'brightness' in clean:
                clean['brightness'] = max(self._br_min(), clean['brightness'])
            self.values.update(clean)
            ok = self._apply_current()
        self._save()
        return {'ok': True, 'values': dict(self.values), 'apply_ok': bool(ok)}

    def select_display(self, device):
        """切换控制目标屏: 销毁旧句柄 → 重建引擎 (重新探测 DDC/伽马)"""
        with self.lock:
            mons = display.enumerate_displays()
            m = next((x for x in mons if x['device'] == device), None)
            if m is None:
                return {'ok': False, 'msg': '未找到该显示器'}
            if m['device'] != self.target['device']:
                self._release_engine()
                self.displays = mons
                self.target = m
                self._init_engine()
                self._save()
        self._sync_ui()
        return {'ok': True, 'state': self.get_state()}

    def unlock_gdi(self):
        """UAC 提权写 HKLM GdiIcmGammaRange=1, 解除伽马峰值约束 (亮度下限 11→1)"""
        if not display.is_gdi_unlocked():
            rc = shell32.ShellExecuteW(None, 'runas', 'reg.exe', _ICM_REG_ADD, None, 0)
            if rc <= 32:
                return {'ok': False, 'unlocked': bool(display.is_gdi_unlocked()),
                        'msg': '提权被取消或失败'}
            time.sleep(0.6)  # ShellExecute 启动即返回, 略等注册表写入后复核
        with self.lock:
            self._gdi_unlocked = bool(display.is_gdi_unlocked())
        return {'ok': self._gdi_unlocked, 'unlocked': self._gdi_unlocked}

    def set_autostart(self, enable):
        if enable:
            pyw = sys.executable
            if pyw.lower().endswith('python.exe'):
                pyw = pyw[:-len('python.exe')] + 'pythonw.exe'
            cmd = '"%s" "%s" --hidden' % (pyw, os.path.abspath(__file__))
            args = ['reg', 'add', _RUN_KEY, '/v', _RUN_VALUE, '/t', 'REG_SZ', '/d', cmd, '/f']
        else:
            args = ['reg', 'delete', _RUN_KEY, '/v', _RUN_VALUE, '/f']
        try:
            p = subprocess.run(args, capture_output=True, creationflags=_CREATE_NO_WINDOW)
            ok = p.returncode == 0
        except Exception:
            ok = False
        return {'ok': ok, 'autostart': _autostart_enabled()}

    def set_night_mode(self, enable):
        with self.lock:
            self.values['night_mode'] = bool(enable)
        self._save()
        if enable:
            self._night_applied = None
            threading.Thread(target=self._night_check, daemon=True).start()  # 立即评估一次
        return {'ok': True, 'night_mode': bool(enable)}

    def toggle_touch(self, instance_id, enable):
        ok, msg = touchctl.toggle(instance_id, enable)
        return {'ok': ok, 'msg': msg, 'touch': touchctl.find_touch_devices(CFG['touch_hwid'])}

    def reset_all(self):
        with self.lock:
            for k, v in (('brightness', 100), ('contrast', 100), ('r', 100), ('g', 100),
                         ('b', 100), ('kelvin', 6500)):
                self.values[k] = v
            self._apply_current()
        self._save()
        return {'ok': True}


def main():
    if _another_instance_running():
        # 秒退会让用户以为启动失败; pythonw 下用 ctypes 弹窗告知
        ctypes.windll.user32.MessageBoxW(
            None, '副屏控制台已在运行中（请看屏幕窗口或系统托盘的月牙图标）。',
            '副屏控制台', 0x40)
        sys.exit(0)
    api = Api()
    l, t, r, b = api.target['rect']
    w, h = 430, 860
    x = max(l, l + ((r - l) - w) // 2)
    y = max(t, t + ((b - t) - h) // 2)
    window = webview.create_window(
        '副屏控制台', UI_FILE, js_api=api, width=w, height=h, x=int(x), y=int(y),
        background_color='#1E1E2E', min_size=(380, 700),
        hidden=('--hidden' in sys.argv))  # 开机自启带 --hidden: 启动即隐藏, 托盘/热键 Q 唤出
    api.attach_window(window)
    webview.start()


if __name__ == '__main__':
    main()
