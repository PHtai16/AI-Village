# Thư mục `docs/`: Hệ thống Tài liệu Kiến trúc, Tiêu chuẩn và Tiến độ

Thư mục `docs/` là nơi lưu trữ toàn bộ tài liệu kiến trúc, đặc tả hợp đồng dữ liệu, mẫu cấu hình, hồ sơ quyết định kỹ thuật (ADR), báo cáo kiểm toán và nhật ký tiến độ của AI Village.

## 1. Phân định Vai trò các Tài liệu Nền tảng

| Vai trò | Đường dẫn | Quy định sử dụng |
| :--- | :--- | :--- |
| **Single Source of Truth (Kiến trúc)** | [docs/architecture/SYSTEM_BLUEPRINT.md](architecture/SYSTEM_BLUEPRINT.md) | Tài liệu chuẩn gốc về tầm nhìn, ranh giới 12 thư mục kỹ thuật, Master Agent, 9 phòng ban mặc định và 9 nguyên tắc bất biến. |
| **Theo dõi Tiến độ (Roadmap & Log)** | [docs/PROGRESS.md](PROGRESS.md) | Nguồn chuẩn duy nhất quản lý tiến độ theo từng tiểu phase, tiêu chí nghiệm thu, trạng thái Gate và nhật ký commit SHA thực tế. |
| **Tiêu chuẩn Kỹ thuật (Schemas & Specs)** | [docs/standards/](standards/) | Hợp đồng dữ liệu chi tiết cho 5 trụ cột: [AGENT_SPEC.md](standards/AGENT_SPEC.md), [DEPARTMENT_SPEC.md](standards/DEPARTMENT_SPEC.md), [SKILL_SPEC.md](standards/SKILL_SPEC.md), [RULE_SPEC.md](standards/RULE_SPEC.md), [TOOL_SPEC.md](standards/TOOL_SPEC.md). |
| **Kiến trúc Nền tảng Phase 00** | [docs/architecture/](architecture/) | Bao gồm [vision.md](architecture/vision.md), [boundaries.md](architecture/boundaries.md) và [reuse_inventory.md](architecture/reuse_inventory.md). |
| **Hồ sơ Quyết định Thiết kế (ADR)** | [docs/decisions/](decisions/) | Lưu trữ các quyết định kiến trúc và lựa chọn công nghệ có bối cảnh, lý do và tiêu chí kiểm chứng, khởi đầu với [ADR-0001-temporary-technology-stack.md](decisions/ADR-0001-temporary-technology-stack.md). |
| **Bộ Mẫu Cấu hình (Templates)** | [docs/templates/](templates/) | Các mẫu tĩnh chuẩn hóa cho Agent ([agent.yaml](templates/agent/agent.yaml)), Department ([department.yaml](templates/department/department.yaml)) và Skill ([SKILL.md](templates/skill/SKILL.md)). |
| **Báo cáo Kiểm toán & Nghiệm thu** | [docs/reviews/](reviews/) | Lưu giữ biên bản rà soát chéo và kết luận Gate, bao gồm [PHASE_01B_AUDIT.md](reviews/PHASE_01B_AUDIT.md). |
| **Tài liệu Vận hành (Operations)** | `docs/operations/` (tạo khi bước vào các phase vận hành sau) | Hướng dẫn chạy cục bộ, sao lưu, khôi phục và xử lý sự cố ở Phase 17. |

## 2. Quy trình Cập nhật Tài liệu

1. Mọi thay đổi kiến trúc phải tuân thủ thứ tự ưu tiên tham chiếu trong [AGENTS.md](../AGENTS.md): `SYSTEM_BLUEPRINT.md` -> `docs/standards/` -> `docs/PROGRESS.md` -> `AGENTS.md`.
2. Không sao chép lặp lại nội dung chi tiết của Blueprint sang nhiều tệp tài liệu khác; các tài liệu vệ tinh chỉ tham chiếu và hệ thống hóa theo đúng phạm vi được giao.
3. Khi phát hiện mâu thuẫn giữa Blueprint và Specification, tuyệt đối không tự ý sửa ngầm; phải ghi nhận rõ vị trí xung đột và đề xuất hướng xử lý để người vận hành phê duyệt.
4. Mọi cập nhật vào `docs/standards/` hoặc `docs/templates/` phải vượt qua bộ kiểm thử tự động tại [tests/standards/test_standards_consistency.py](../tests/standards/test_standards_consistency.py).
