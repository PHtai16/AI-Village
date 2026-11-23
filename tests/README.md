# Thư mục `tests/`: Hệ thống Kiểm thử Tự động

Thư mục `tests/` lưu trữ toàn bộ bộ kiểm thử tự động của AI Village, bao gồm kiểm thử tính nhất quán đặc tả kỹ thuật, kiểm thử cấu trúc Chặng A, unit test, contract test và integration test cho các phase sau.

## 1. Lệnh Chạy Kiểm thử Thống nhất

Từ thư mục gốc của repository (`C:\DoCho\AI-Village`), chạy toàn bộ bộ kiểm thử bằng một lệnh:

```powershell
python -m pytest tests -v
```

Hoặc chạy riêng từng nhóm kiểm thử hiện có:

```powershell
python -m pytest tests/standards/test_standards_consistency.py -v
python -m pytest tests/stage_a/test_stage_a_baseline.py -v
```

Để kiểm tra build giao diện `ui/` tối thiểu:

```powershell
cd ui
npm run build
```

## 2. Phạm vi Kiểm thử Hiện tại (Chặng A)

- [tests/standards/test_standards_consistency.py](standards/test_standards_consistency.py): Kiểm chứng sự tồn tại của tài liệu chuẩn PHASE 01B, cân bằng thẻ code Markdown, không rò rỉ bí mật, phân tích YAML của 3 template và 10 ví dụ nhúng trong 5 tài liệu Specification.
- [tests/stage_a/test_stage_a_baseline.py](stage_a/test_stage_a_baseline.py): Kiểm chứng cấu trúc 14 thư mục gốc, tài liệu PHASE 00 và README các thư mục PHASE 01, cấu hình `.env.example` và `.gitignore`, kiểm tra liên kết tài liệu, kiểm tra không có mã nghiệp vụ sai tầng, không rò rỉ secret và kiểm tra cấu trúc scaffold của `ui/`.

## 3. Quy tắc Kiểm thử

- Không viết kiểm thử giả định cho các thành phần Runtime chưa triển khai (`engines/antigravity/`, `core/`, `tools/`, `storage/`).
- Không đánh dấu đạt bằng lệnh in chuỗi giả; mọi bài kiểm thử phải đọc và xác minh trực tiếp dữ liệu trên hệ thống tệp.
- Khi có sự cố kiểm thử thất bại, sửa nguyên nhân gốc trong phạm vi phase hiện hành và chạy lại toàn bộ lệnh kiểm thử để ghi nhận bằng chứng mới.
