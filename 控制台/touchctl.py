# -*- coding: utf-8 -*-
"""触摸数字化器控制: 状态读取 (cfgmgr32, 免管理员) / 切换 (ShellExecute runas + pnputil, 单次 UAC)"""

import ctypes
import re
import time
from ctypes import wintypes as wt

cfg = ctypes.WinDLL('cfgmgr32')
shell32 = ctypes.WinDLL('shell32')

CM_DRP_HARDWAREID = 0x2
DN_HAS_PROBLEM = 0x400
CM_PROB_DISABLED = 22
_DIGITIZER_HW = 'UP:000D_U:000'  # 用法页 000D (Digitizers)


def _id_list():
    size = wt.ULONG()
    if cfg.CM_Get_Device_ID_List_SizeW(ctypes.byref(size), 'HID', 0) != 0 or size.value == 0:
        return []
    buf = ctypes.create_unicode_buffer(size.value)
    if cfg.CM_Get_Device_ID_ListW('HID', buf, size.value, 0) != 0:
        return []
    return [s for s in ctypes.wstring_at(buf, size.value).split('\x00') if s]


def _devinst(device_id):
    di = wt.ULONG()
    if cfg.CM_Locate_DevNodeW(ctypes.byref(di), device_id, 0) != 0:
        return None
    return di.value


def _status(devinst):
    st, pb = wt.ULONG(), wt.ULONG()
    if cfg.CM_Get_DevNode_Status(ctypes.byref(st), ctypes.byref(pb), devinst, 0) != 0:
        return None, None
    return st.value, pb.value


def _hardware_ids(devinst):
    # 超配缓冲区 + 全量 NUL 切分, 规避 W 版长度单位(字符/字节)的文档歧义
    buf = ctypes.create_unicode_buffer(4096)
    got = wt.ULONG(len(buf))
    if cfg.CM_Get_DevNode_Registry_PropertyW(devinst, CM_DRP_HARDWAREID,
                                             None, buf, ctypes.byref(got), 0) != 0:
        return []
    return [s for s in ctypes.wstring_at(buf, len(buf)).split('\x00') if s]


def find_touch_devices(hwid_pattern):
    """→ [{'id', 'enabled', 'problem'}]
    只收数字化器接口 (硬件 ID 含 UP:000D 用法页), 排除同芯片的厂商自定义接口 (如 MI_01);
    hwid_pattern (VID/PID) 命中者优先, 其余数字化器兜底。
    problem: 0=正常 22=已禁用 其他=设备自身异常"""
    primary, fallback = [], []
    for did in _id_list():
        if not did.startswith('HID\\VID_'):
            continue
        di = _devinst(did)
        if di is None:
            continue
        st, pb = _status(di)
        if st is None:
            continue
        problem = pb if (st & DN_HAS_PROBLEM) else 0
        if not any(_DIGITIZER_HW in s for s in _hardware_ids(di)):
            continue
        entry = {'id': did, 'enabled': problem != CM_PROB_DISABLED, 'problem': problem}
        if re.search(hwid_pattern, did):
            primary.append(entry)
        else:
            fallback.append(entry)
    return primary + fallback


def toggle(instance_id, enable):
    """切换触摸设备, 每次弹一次 UAC; 返回 (是否成功, 说明)"""
    op = '/enable-device' if enable else '/disable-device'
    rc = shell32.ShellExecuteW(None, 'runas', 'pnputil.exe',
                               '%s "%s"' % (op, instance_id), None, 0)
    if rc <= 32:
        return False, '提权被取消或失败 (code=%d)' % rc
    time.sleep(0.8)
    return True, '已%s触摸设备' % ('启用' if enable else '禁用')
