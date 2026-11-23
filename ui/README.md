# Thư mục `ui/`: Khung Giao diện Web Tối thiểu (UI Scaffold)

Thư mục `ui/` chứa ứng dụng web React + Vite ở mức khung khởi tạo tối thiểu phục vụ kiểm chứng nền tảng giao diện cục bộ trong Chặng A.

> **Trạng thái hiện tại:** Chỉ là scaffold khởi đầu chưa kết nối Runtime. Tuyệt đối chưa triển khai giao diện Chat thật, Dashboard 8 phân khu, không gian 3D hay dữ liệu thống kê giả.

## 1. Kiến trúc và Ranh giới

- Giao diện người dùng chỉ chịu trách nhiệm hiển thị thông tin từ API và gửi lệnh điều khiển qua tầng `api/` (sẽ xây dựng ở `PHASE 12`–`13`).
- Không chứa logic điều phối của Master Agent, không truy cập trực tiếp `.runtime/` SQLite và không giữ trạng thái Agent đang làm việc giả tạo khi Runtime chưa kết nối.
- Không gian văn phòng 3D (`PHASE 14`) chỉ được phát triển sau khi Runtime và giao diện 2D hoàn chỉnh đạt Gate H.

## 2. Cách Chạy và Kiểm tra Cục bộ

Từ thư mục `ui/`:

```powershell
cd ui
npm install
npm run dev
```

Kiểm tra build sản phẩm tĩnh:

```powershell
cd ui
npm run build
```

Thư mục `node_modules/` và `dist/` được loại trừ bởi `.gitignore` ở gốc repository.
