# WebRTC Raspberry Pi 4 (Debian 12, Bookworm)

WebRTC thuần Debian
- libcamera (thông qua GStreamer `libcamerasrc`) để lấy dữ liệu từ camera module V2
- GStreamer `webrtcbin` để đẩy video H.264 qua WebRTC cho Chrome
- aiohttp để phục vụ trang web và WebSocket signaling

Truy cập: `http://<ip-raspberry-pi>:8082` để xem stream.

**Trên RPI2 WICOM lab nên sử dụng địa chỉ IP của RPI2 kết nối với mạng, địa chỉ hotspot do RPI2 phát không ổn định thường xuyên bị mất kết nối hoặc không kết nối được.**

## Yêu cầu

- Raspberry Pi 4
- Debian GNU/Linux 12 (bookworm)
- Camera module V2
- Chrome/Edge làm client

## Cài đặt

```bash
# Clone repo
git clone git@github.com:phuoc-SET/wicomlab_webrtc_rpi.git
cd wicomlab_webrtc_rpi

# Cài dependency
chmod +x scripts/install_deps.sh
./scripts/install_deps.sh
```

Kiểm tra plugin quan trọng:
```bash
gst-inspect-1.0 libcamerasrc
gst-inspect-1.0 webrtcbin
```

## Chạy

```bash
# Chạy server
python3 server.py --host 0.0.0.0 --port 8082 --width 1280 --height 720 --fps 30 --bitrate 2500000 --force-sw
```

- `--force-sw`: bắt buộc để lấy dữ liệu từ camera 
- `--host`: mặc định `0.0.0.0`
- `--port`: mặc định `8082`
- `--width/--height`: độ phân giải (mặc định 1280x720)
- `--fps`: khung hình/giây (mặc định 30)
- `--bitrate`: bit rate video (bps), mặc định 2.5 Mbps

Trên máy client (Chrome), mở `http://<ip-raspberry-pi>:8082`, nhấn "Kết nối".

## Tự khởi động với systemd

Sửa đường dẫn trong file service cho phù hợp (WorkingDirectory/ExecStart), rồi:

```bash
sudo cp systemd/webrtc-camera.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable webrtc-camera
sudo systemctl start webrtc-camera
sudo systemctl status webrtc-camera
```
