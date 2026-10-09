# 副屏控制台 (Windows Subscreen Console)

给外接 / 便携显示器做的轻量控制台：**触摸开关 + 亮度调节 + 色彩调节**。pywebview 界面，Windows API 全部经 Python 标准库 ctypes 直调，除 pywebview 外零第三方依赖。

> **ARZOPA 便携屏实测适配**——触摸开关 + 伽马调光全链路在其上跑通（详见下文「实测案例」）。其他品牌的触摸屏、支持 DDC/CI 的显示器同样适用，见「适配你自己的屏」。

起因：便携屏普遍没有官方的触摸开关；亮度/色彩想走 DDC/CI 时，很多廉价便携屏固件根本不响应。本工具对触摸用「禁用/启用 HID 数字化器」思路，对亮度/色彩用「伽马 LUT」兜底，并在屏幕支持 DDC/CI 时自动升级为硬件级。

## 功能

| 功能 | 原理 | 权限 |
|---|---|---|
| 触摸开关（按屏） | `pnputil` 禁用/启用对应触摸数字化器 HID 设备 | 每次切换弹一次 UAC |
| 亮度 | DDC/CI (VCP 0x10) 或软件伽马 LUT，启动时自动选择 | 免管理员 |
| 对比度 | DDC/CI (VCP 0x12)，仅 DDC 可用时出现 | 免管理员 |
| 色彩（R/G/B 增益 + 预设） | DDC/CI (0x16/0x18/0x1A) 或伽马通道增益 | 免管理员 |

## 快速开始

```bat
pip install pywebview
控制台\启动控制台.bat
```

- 要求：Windows 10 2004+ / 11、Python 3.8+、WebView2 运行时（Win11 自带）
- 窗口会自动开在副屏上
- 零依赖备选：根目录 `TouchPanel.ps1`（v1 触摸开关面板，WPF）不需要 Python，双击 `启动面板.bat` 即用

## 适配你自己的屏

1. **触摸**：设备管理器 → 人体学输入设备 → "符合 HID 标准的触摸屏" → 详细信息 → 硬件 ID，把 VID/PID 填进 `控制台/main.py` 顶部的 `CFG['touch_hwid']`
2. **亮度/色彩**：无需配置，启动时自动探测目标屏的 DDC/CI 能力：可用 → 硬件级（亮度/对比度）；不可用 → 伽马 LUT 软件级（亮度/RGB 增益）
3. **目标屏**：默认取非主屏；单屏环境退回主屏（`display.pick_target`）

触摸候选的过滤很严格：设备硬件 ID 必须含数字化器用法页（`UP:000D`）才入选，避免误控同芯片上的厂商自定义接口——实测某沁恒芯片的屏会同时枚举出 MI_00 触摸 + MI_01 厂商通道两个设备。

## 实测案例（某 ARZOPA 便携屏，DP/USB-C 连接，固件 V6.385）

- **完全不支持 DDC/CI**：OSD 恢复出厂后重测，所有 VCP 码（含能力串）仍返回错误 31——廉价便携屏的常见坑
- 触摸芯片为沁恒 (VID_1A86)：禁用 MI_00 即关触摸；关闭状态同口重启保持、换 USB 口自动恢复
- 伽马方案工作良好：写入读回验证通过，睡眠唤醒后面板每 10 秒自动补涂

## 工作原理 & 注意

- **触摸**：Windows 没有按屏关触摸的设置项，但每块触摸屏是一个独立 HID 设备，禁用即关。关闭状态绑定"设备实例"：换 USB 口后系统视为新设备，触摸会自动恢复
- **伽马模式**：调节的是显示 LUT（夜间模式同原理），立即生效但属软件调光——拉很低有轻微对比损失，且不能超过 100%（无法"增亮"）；睡眠唤醒/切分辨率后系统会重置 LUT，面板 watchdog 每 10 秒比对补涂
- **DDC 模式**：调节直达显示器，与物理按键等效
- 状态持久化在注册表 `HKCU\Software\副屏控制台`，不写任何文件；关闭面板后当前伽马保持生效，想完全还原就开面板点"恢复默认"
- 面板打不开时的手动兜底：设备管理器 → 人体学输入设备 → 触摸屏 → 右键 启用/禁用

## 免 UAC（可选，针对 v1 触摸面板）

注册一个以最高权限运行的计划任务，之后 `schtasks /Run` 触发即可不再弹 UAC：

```cmd
schtasks /Create /TN TouchToggle /TR "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File <项目目录>\TouchPanel.ps1" /SC ONCE /ST 23:59 /RL HIGHEST /F
schtasks /Run /TN TouchToggle
```

## 目录结构

```
├─ TouchPanel.ps1        v1 触摸面板（WPF，零依赖，保留备用）
├─ 启动面板.bat           v1 启动器（弹 UAC）
├─ 免UAC启动.bat          v1 免 UAC 启动（需先注册上面的计划任务）
└─ 控制台/               v2 主程序（pywebview）
   ├─ main.py            入口 + js_api + 注册表持久化 + DDC/伽马引擎选择
   ├─ display.py         显示器枚举 / 伽马 LUT / DDC/CI（ctypes 直调）
   ├─ touchctl.py        触摸设备枚举（cfgmgr32）与切换（runas + pnputil）
   ├─ ui/index.html      界面
   └─ 启动控制台.bat      启动器
```

## License

[MIT](LICENSE)
