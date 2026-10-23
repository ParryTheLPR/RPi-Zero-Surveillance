#!/usr/bin/env bash
# 在树莓派 Zero W（Raspberry Pi OS Lite 32-bit, Debian Trixie）上执行：
#   chmod +x install.sh && ./install.sh
set -euo pipefail

echo "==> 1/5 更新软件源"
sudo apt-get update -y

echo "==> 2/5 安装系统依赖（libcamera / picamera2 / OpenCV / ffmpeg）"
sudo apt-get install -y \
    python3-picamera2 \
    python3-opencv \
    python3-numpy \
    python3-yaml \
    ffmpeg \
    python3-spidev \
    python3-smbus2 || true

echo "==> 3/5 安装 pysmb（SMB 上传；apt 没有则走 pip）"
if ! python3 -c "import smb" 2>/dev/null; then
    sudo apt-get install -y python3-pip || true
    pip3 install --break-system-packages pysmb || pip3 install pysmb
fi

echo "==> 4/5 启用 CSI 摄像头接口"
# 现代 libcamera 通常自动识别 OV5647；若启动报“no camera”，
# 在 /boot/firmware/config.txt（老系统为 /boot/config.txt）末尾手动加：
#   dtoverlay=ov5647
sudo raspi-config nonint do_camera 0 || true

echo "==> 5/5 校验依赖导入"
python3 - <<'PY'
import importlib
for m in ("picamera2", "cv2", "numpy", "yaml", "smb"):
    try:
        importlib.import_module(m)
        print(f"[OK] {m}")
    except Exception as e:
        print(f"[缺] {m}: {e}")
PY

echo
echo "==> 6/6 安装 systemd 开机自启（可选，自动启用并立即启动）"
PROJ_DIR="$(cd "$(dirname "$0")" && pwd)"
sudo cp "$PROJ_DIR/surveillance.service" /etc/systemd/system/surveillance.service
sudo sed -i "s#/home/pi/rpi-zero-surveillance#$PROJ_DIR#g" /etc/systemd/system/surveillance.service
sudo systemctl daemon-reload
sudo systemctl enable surveillance
# 环境变量文件（若已存在不覆盖）
if [ ! -f /etc/surveillance.env ]; then
    sudo tee /etc/surveillance.env >/dev/null <<'ENV'
MAIL_PASSWORD=
SMB_PASSWORD=
WEB_PASSWORD=
ENV
    sudo chmod 600 /etc/surveillance.env
    echo "已生成 /etc/surveillance.env，请填入 MAIL_PASSWORD / SMB_PASSWORD / WEB_PASSWORD"
fi
sudo systemctl restart surveillance

echo
echo "完成。"
echo "  - 试运行：python3 -m monitor.main --config config.yaml"
echo "  - 状态/日志：systemctl status surveillance ; journalctl -u surveillance -f"
echo "  - 网页：http://<树莓派IP>:8080 （账号 admin，密码取 WEB_PASSWORD）"
echo "  - 改完 config.yaml 后：sudo systemctl restart surveillance"
