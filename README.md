[English Ver](README.en.md)

# 树莓派 Zero W + OV5647 CSI 摄像头监控程序

面向 **Raspberry Pi Zero W / Raspberry Pi OS Lite 32-bit（Debian Trixie，无桌面）** 的轻量监控服务，基于现代 **libcamera / picamera2** 硬件编码。

## 功能

- **CSI OV5647 采集**：主通道录像 + 低分辨率通道移动检测，硬件 H.264 编码 MP4。
- **移动标记**：帧差 + 轮廓检测，在事件快照 JPEG 上画移动框、时间戳；录像本体保持干净（零 W 性能考虑）。
- **两种录制模式**（`recorder.mode` 可切）：
  - `motion`：移动触发分段录像，静止 `post_motion_seconds` 秒后收尾；
  - `continuous`：7×24 连续录像，每 `segment_seconds` 秒切分。
- **SMB 备份上传**：后台线程 + 持久化队列，断网/重启后自动补传；视频按日期分目录，快照单独目录。
- **电池亏电邮件**：可插拔后端（命令 / MCP3008 分压 / INA219），分级低电压/严重低压，冷却防刷屏。
- **内置网页面板**：Basic Auth 登录、MJPEG 实时预览、运行状态、分级热重载配置（安全项即时生效，结构项提示重启）。
- **开机自启**：systemd 单元 `Restart=always`，崩溃自动拉起，安装脚本一键 enable。

## 目录结构

```
rpi-zero-surveillance/
├── config.yaml            # 实际生效配置（网页保存会整份重写，不含注释）
├── config.example.yaml    # 带逐行注释的说明模板，供对照查阅/复制
├── requirements.txt
├── install.sh             # 树莓派一键装依赖
├── surveillance.service    # systemd 开机自启单元
├── README.md
└── monitor/
    ├── main.py            # 主循环编排
    ├── camera.py         # picamera2 封装/硬件录像
    ├── motion.py         # 帧差检测 + 快照标注
    ├── recorder.py       # 录制状态机/快照/本地清理
    ├── smb_uploader.py   # SMB 队列上传
    ├── battery.py        # 电池监测后端
    ├── mailer.py         # SMTP 发信
    └── config.py         # 配置加载
```

## 安装（在树莓派上）

```bash
# 把整个 rpi-zero-surveillance 目录拷到树莓派，例如 /home/pi/
cd /home/pi/rpi-zero-surveillance
chmod +x install.sh
./install.sh
```

摄像头接线：OV5647 排线对准 Zero W 的 CSI 口（金属面朝 PCB 背面/HDMI 侧，按主板丝印）。若 `libcamera` 识别不到，在 `/boot/firmware/config.txt` 末尾加 `dtoverlay=ov5647` 后重启。

## 配置（必改项）

各项含义、取值范围和调法见 `config.example.yaml`（逐行注释模板，程序不读取它）；实际改 `config.yaml` 或直接在网页表单里改。

1. **SMB**：`smb.host / share / username / remote_dir`；密码放环境变量 `SMB_PASSWORD`。
2. **邮件**：`mail.smtp_host / port / username / recipients`；465 用 `use_ssl: true`，587 用 `use_starttls: true`；密码放环境变量 `MAIL_PASSWORD`。
3. **电池**：`battery.backend` 选 `command / mcp3008 / ina219`，填对应阈值与参数。
4. **录制模式**：`recorder.mode: motion` 或 `continuous`。

> 网页“保存配置”会整份重写 `config.yaml`（不保留注释），所以注释统一放在 `config.example.yaml`。

环境变量建议写进 `/etc/surveillance.env`（`chmod 600`）：

```bash
MAIL_PASSWORD="你的邮箱授权码"
SMB_PASSWORD="SMB密码"
WEB_PASSWORD="网页登录密码"
```

## 试运行

```bash
export MAIL_PASSWORD=xxx SMB_PASSWORD=xxx WEB_PASSWORD=xxx
python3 -m monitor.main --config config.yaml
```

看到 `监控已启动` 即正常；移动时 `recordings/` 出视频、`snapshots/` 出带框快照。

文件命名：
- 视频：`Video{开始时间YYYYMMDD-HHMMSS}.mp4`，例 `Video20261023-140644.mp4`
- 快照：`{YYYYMMDD-HHMMSS}_{毫秒}_snap.jpg`，例 `20261023-140645_066_snap.jpg`

## 本机 dev 模拟模式（无树莓派也能看网页）

在 PC 上用合成移动方块代替摄像头，点验网页预览/检测/状态/配置；不产生真实录像、不上传 SMB、不读电池：

```bash
python3 -m monitor.main --dev --config config.yaml
# 浏览器打开 http://127.0.0.1:8080，账号 admin，密码 admin（dev 默认）
```

dev 模式自动关闭 SMB 上传与电池监测；仅用于界面演示，真实采集/录像请在树莓派上不带 `--dev` 运行。

## 网页面板

启动后浏览器打开 `http://<树莓派IP>:8080`，用 `web.username`（默认 admin）和环境变量 `WEB_PASSWORD` 登录。

- **预览**：MJPEG 实时流（`/stream`），分辨率/帧率/质量在页面“预览流”区调整并即时生效。
- **状态**：顶部实时显示录制中/空闲、录制模式、电池电压、SMB 待传队列、邮件开关、运行时长（每 2 秒刷新）。
- **配置**：表单按移动检测/录制/电池/邮件/SMB/预览分组保存。
  - **即时生效（热重载）**：灵敏度、面积、确认帧数、静止续录秒数、分段时长、本地保留、电池阈值/轮询/冷却、SMTP 账号/收件人、SMB 重试/快照开关、预览参数。
  - **需重启服务**：摄像头分辨率/帧率/码率、录制模式、SMB 主机/共享/账号/目录、电池后端、Web 端口——保存写入文件，`sudo systemctl restart surveillance` 后生效；页面顶部也有“一键重启服务”按钮。
- **保存与备份**：点“保存配置”会把整份配置写回 `config.yaml`；写盘前自动把当前文件备份为 `config-bak-YYYYMMDD-HHMMSS_毫秒.yaml`，同目录只保留最新 3 份，更早的自动删除。

> 网页按钮走 `sudo -n systemctl restart surveillance`，需配免密 sudo：
> `sudo visudo -f /etc/sudoers.d/surveillance` 写入
> `pi ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart surveillance`
> 未配置时按钮回退为进程自重启（效果等同，不经 systemd）。

## 开机自启（systemd）

`install.sh` 第 6 步已自动完成安装并 `enable`；手动方式：

```bash
sudo cp surveillance.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now surveillance
journalctl -u surveillance -f      # 看日志
```

服务 `Restart=always`，崩溃或断电恢复后自动拉起；改完 `config.yaml` 执行 `sudo systemctl restart surveillance`。

日志写在 `logs/surveillance.log`，按天滚动：每天午夜新建一个文件（旧文件带日期后缀 `surveillance.log.YYYYMMDD`），自动保留最近 14 天。

## 电池后端说明

| backend | 适用 | 关键参数 |
|---|---|---|
| `command`（默认） | 已有脚本/驱动能输出电压 | `battery.command` 打印一个浮点电压(V) |
| `mcp3008` | SPI ADC + 分压电阻 | `divider_ratio` 按实际分压比换算 |
| `ina219` | I2C 电流电压传感器/UPS HAT | `i2c_bus / address` |

阈值按电池串数自行换算：单节锂电 3.5V 提醒、3.3V 紧急；多串乘串数。新增硬件只需在 `battery.py` 实现 `read_voltage()`。

## 调参建议（误报/漏报）

- 光影、树叶晃动误报：调高 `motion.sensitivity`，增大 `min_area`，提高 `confirm_frames`。
- 小目标漏检：调低 `sensitivity`，减小 `min_area`。
- Zero W 卡顿：降低 `camera.main_width/height` 或 `fps`，缩小 `lores`。

## 常见问题

- **`Failed to find camera`**：检查排线、`raspi-config` 已启用摄像头、必要时加 `dtoverlay=ov5647`。
- **SMB 连不上**：确认共享允许访客/账号、防火墙 445、`remote_dir` 路径；队列在 `upload_queue.json`，恢复网络后自动补传。
- **邮件发不出**：多数邮箱需用“授权码”而非登录密码；确认 SSL/STARTTLS 与端口匹配。
