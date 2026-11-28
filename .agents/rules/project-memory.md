---
trigger: always_on
description: Bộ nhớ dự án AI Village cập nhật trạng thái và ranh giới kỹ thuật sau khi hoàn tất CHẶNG G / PHASE 11 và nghiệm thu Gate G
---

# Bộ nhớ dự án AI Village sau PHASE 11 (Gate G PASS)

## Trạng thái tiến độ và kiểm chứng
- Dự án đã hoàn tất từ Chặng A đến Chặng G (`PHASE 10` và toàn bộ `PHASE 11` gồm `P11.01`–`P11.05` cùng `Gate G: PASS`) ở mức `OFFLINE_E2E_VERIFIED`.
- Các commit mã nguồn chính của `PHASE 11`: `22accc0` cho `P11.01`, `33291b7` cho `P11.02`, `9065d87` cho `P11.03`, `aba62d0` cho `P11.04`, `30aec5e` cho `P11.05` và `a2bcac6` cho nghiệm thu `Gate G`, báo cáo tổng hợp tại `docs/reviews/PHASE_11_GATE_G_AUDIT.md`.
- Trọng tâm kế tiếp: đã chính thức đăng ký **PHASE 11.5 — APPLICATION INTEGRATION & RUNTIME READINESS** (`PLANNED`, đặc tả tại `docs/PHASE_11_5_APPLICATION_INTEGRATION_RUNTIME_READINESS.md`) làm pha tích hợp backend bắt buộc (bắt đầu từ `11.5-A`) trước khi mở khóa **CHẶNG H / PHASE 12 — API & Realtime**.

## Bản đồ thành phần hiện có trên đĩa
- `agents/`: có `agents/master/agent.yaml` và `agents/roles/it_artifact_worker/agent.yaml` giữ `tools.allowed: []` và `skills.allowed: []`, cùng 9 manifest phòng ban tại `agents/departments/*/department.yaml`.
- `core/`: gồm `contracts/`, `tasks/`, `events/`, `governance/`, `runtime/`, `agents/`, `orchestration/`, `tools/`, `departments/`, `skills/` và `optimization/`, giữ thuần AST 100%.
- `engines/`: có `AntigravityEngineAdapter` đã kiểm chứng live trên `agy.exe` và `GoogleGeminiAdapter` mới kiểm chứng unit và contract; chưa có adapter OpenAI, Anthropic hay Ollama.
- `tools/` và `sandbox/`: có 17 công cụ chuẩn đăng ký qua `tools/registry_bootstrap.py`, `GovernedMcpBridge` và `sandbox/Dockerfile`.
- `storage/`: SQLite migrations `0001` đến `0020`. `SQLiteRepositoryStore` đã gắn đủ `.reception`, `.interrupts`, `.scheduler`, `.resources`, `.cleanup`, `.process`, `.skills`, và `recover_task` đã hỗ trợ đối soát tái sử dụng artifact chéo Task từ `artifact_task_references`.
- `api/`, `skills/`, `ui/`: `api/artifacts.py` mới có 2 endpoint đọc cho Gate E sẵn sàng mở rộng tại Phase 12; `skills/schemas/skill_manifest.schema.json` đã có mặt, còn `skills/candidates/` và `skills/approved/` giữ trống theo baseline Chặng A; `ui/` vẫn là scaffold React và Vite từ Chặng A.

## Điểm lưu ý khi triển khai CHẶNG H (PHASE 12–13)
- API và Realtime ở Phase 12 cần đọc từ cùng nguồn `SQLiteRepositoryStore`, tuyệt đối không tạo logic điều phối hay tính toán trùng lặp trong `api/`, không lộ `credential_ref`, secret hay quyền tool thô ra HTTP/SSE/browser, và giữ nguyên giá trị `null` khi Provider chưa cấp số liệu usage/chi phí.
- Giao diện 2D ở Phase 13 chỉ mở khóa triển khai sau khi Phase 12 hoàn tất DTO đọc, stream event journal và policy kiểm soát quyền.

## Kỷ luật vận hành cục bộ
- Đọc tài liệu theo thứ tự `docs/architecture/SYSTEM_BLUEPRINT.md`, `docs/standards/`, `docs/PROGRESS.md`, rồi `AGENTS.md`.
- Shell mặc định là Windows PowerShell: nối lệnh bằng `;`, không dùng `&&`.
- Chỉ ghi nhận hoàn tất sau khi chạy `python -m pytest` và `git diff --check` sau chỉnh sửa cuối, sau đó commit chuẩn Conventional Commits và push lên `origin/main`.
