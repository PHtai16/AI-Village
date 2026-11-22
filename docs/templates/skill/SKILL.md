---
spec_version: "1.0.0"
id: "example_skill_template"
name: "Template Skill Workflow"
version: "0.1.0"
description: "Mẫu hướng dẫn quy trình kỹ năng chuẩn hóa theo SKILL_SPEC.md. Chưa kiểm chứng và chưa kích hoạt."
category: "general_workflow"
author: "human_expert"
maintainer: "process"
status: "DRAFT"
requirements:
  tools:
    - "tools.files.read"
  dependencies: []
  min_engine_tier: "flash"
---

# Mẫu Kỹ năng: Template Skill Workflow

> **Lưu ý:** Đây là mẫu thiết kế kỹ năng ở trạng thái `DRAFT`, chưa trải qua kiểm thử độc lập và chưa được kích hoạt trong `skills/approved/`.

## 1. Mục đích và Phạm vi

Mô tả rõ bài toán nghiệp vụ mà kỹ năng này giải quyết và các loại đầu vào được hỗ trợ.

## 2. Điều kiện kích hoạt (Triggers)

- Nhiệm vụ yêu cầu xử lý loại công việc tương ứng với mô tả kỹ năng.
- Agent sở hữu đầy đủ công cụ liệt kê trong `requirements.tools`.

## 3. Điều kiện cấm kích hoạt (Anti-triggers)

- Nhiệm vụ có thể xử lý trực tiếp chỉ với một thao tác đọc đơn lẻ không cần quy trình nhiều bước.
- Môi trường thiếu công cụ bắt buộc hoặc vượt phạm vi quyền hạn cho phép.

## 4. Đầu vào cam kết (Inputs)

- `input_path`: Đường dẫn tệp hợp lệ nằm trong `workspace/{task_id}/inputs/`.

## 5. Đầu ra cam kết (Outputs)

- `output_report`: Tệp kết quả định dạng Markdown hoặc dữ liệu có cấu trúc nằm trong `workspace/{task_id}/outputs/`.

## 6. Quy trình thực thi từng bước (Execution Steps)

1. Kiểm tra sự tồn tại và tính hợp lệ của dữ liệu đầu vào bằng công cụ được cấp phép.
2. Thực hiện phân tích hoặc biến đổi dữ liệu theo từng bước xác định.
3. Tạo tệp kết quả tại đường dẫn đầu ra quy định trong workspace nhiệm vụ.
4. Kiểm tra lại định dạng và tính nhất quán của kết quả vừa tạo.

## 7. Ranh giới an toàn và Quyền hạn (Permissions)

- Kỹ năng không tự cấp thêm quyền; mọi thao tác phải tuân thủ giới hạn trong Agent Manifest và Permission Manager.
- Không đọc tệp bí mật (`.env`, khóa cá nhân) và không ghi tệp ra ngoài `workspace/{task_id}/` hoặc `output/{task_id}/`.

## 8. Tiêu chí hoàn thành (Definition of Done)

- Tệp đầu ra tồn tại, không rỗng và tuân thủ cấu trúc cam kết ở Mục 5.
- Không có lỗi thực thi chưa được xử lý.

## 9. Yêu cầu bằng chứng kiểm chứng (Evidence)

- Đường dẫn tệp đầu ra thực tế kèm mã băm SHA-256.
- Log gọi công cụ đã được lọc thông tin nhạy cảm (`redacted`).

## 10. Xử lý sự cố và Khôi phục (Error Handling)

- Nếu thiếu tệp đầu vào hoặc dữ liệu sai định dạng: dừng quy trình, giữ nguyên trạng thái an toàn và báo lỗi rõ ràng về Master hoặc QA, không tự bịa đặt dữ liệu thay thế.
