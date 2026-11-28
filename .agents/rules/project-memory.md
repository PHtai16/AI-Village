---
trigger: always_on
description: Bộ nhớ dự án AI Village cập nhật trạng thái và ranh giới kỹ thuật sau khi hoàn tất P11.02
---

# Bộ nhớ dự án AI Village sau P11.02

## Trạng thái tiến độ và kiểm chứng
- Dự án đã hoàn tất từ Chặng A đến Chặng F, toàn bộ `PHASE 10` gồm `P10.01` đến `P10.05`, cùng `P11.01` và `P11.02` thuộc `PHASE 11` ở mức `OFFLINE_INTEGRATION_VERIFIED`.
- Commit triển khai `PHASE 10`: `258ac10` cho `P10.01`, `3d5f5bb` cho `P10.02`, `f53ca1f` cho `P10.03`, `dd42ac9` cho `P10.04` và `df8ea49` cho `P10.05`.
- Commit mã nguồn và kiểm thử cho `PHASE 11`: `22accc0` cho `P11.01` và `33291b7` cho `P11.02`, ghi nhận tại `docs/reviews/PHASE_11_P11_01_AUDIT.md` và `docs/reviews/PHASE_11_P11_02_AUDIT.md`.
- Trọng tâm kế tiếp: `P11.03` đến `P11.05` trong `PHASE 11` và nghiệm thu `Gate G`.

## Bản đồ thành phần hiện có trên đĩa
- `agents/`: có `agents/master/agent.yaml` và `agents/roles/it_artifact_worker/agent.yaml` giữ `tools.allowed: []` và `skills.allowed: []`, cùng 9 manifest phòng ban tại `agents/departments/*/department.yaml`.
- `core/`: gồm `contracts/`, `tasks/`, `events/`, `governance/`, `runtime/`, `agents/`, `orchestration/`, `tools/`, `departments/` và `skills/`. Khối `core/departments/` đã bổ sung `interrupt_models.py` cùng `InterruptRoutingService` tại `interrupts.py` giữ thuần AST; `core/optimization/` sẽ được mở khóa cho `P11.03` ở bước tiếp theo.
- `engines/`: có `AntigravityEngineAdapter` đã kiểm chứng live trên `agy.exe` và `GoogleGeminiAdapter` mới kiểm chứng unit và contract; chưa có adapter OpenAI, Anthropic hay Ollama.
- `tools/` và `sandbox/`: có 17 công cụ chuẩn đăng ký qua `tools/registry_bootstrap.py`, `GovernedMcpBridge` và `sandbox/Dockerfile`.
- `storage/`: SQLite migrations `0001` đến `0017` gồm `0016_reception_conversations_and_intake.sql` và `0017_interrupts_routing_and_dedup.sql`. `SQLiteRepositoryStore` đã gắn `.reception` và `.interrupts` qua `SQLiteReceptionRepository` và `SQLiteInterruptRepository`.
- `api/`, `skills/`, `ui/`: `api/artifacts.py` mới có 2 endpoint đọc cho Gate E; `skills/schemas/skill_manifest.schema.json` đã có mặt, còn `skills/candidates/` và `skills/approved/` giữ trống theo baseline Chặng A; `ui/` vẫn là scaffold React và Vite từ Chặng A.

## Nợ kỹ thuật và điểm lưu ý khi tiếp tục PHASE 11
- `ReceptionDepartmentService` và `InterruptRoutingService` đã lưu SQLite bền vững; `ResourcesDepartmentService` và phần Scheduler đang nằm trong phạm vi `P11.03` và `P11.04`, còn dọn dẹp sau Task và handoff Process nằm ở `P11.05`.
- `InterruptRoutingService` tách biệt năm chiều `classification`, `severity`, `priority`, `urgency`, `impact`, chặn Worker tự nâng quyền `CRITICAL` hay giả danh tác nhân, kiểm tra trạng thái online thật từ bảng `sessions`, và khử trùng lặp qua cả `event_dedup_key` lẫn `incident_key` đang mở.
- Khi tạo `core/optimization/` cho `P11.03`, cần cập nhật `allowed_core_subdirs` trong `tests/stage_a/test_stage_a_baseline.py` và allowlist trong `tests/standards/test_standards_consistency.py`.

## Kỷ luật vận hành cục bộ
- Đọc tài liệu theo thứ tự `docs/architecture/SYSTEM_BLUEPRINT.md`, `docs/standards/`, `docs/PROGRESS.md`, rồi `AGENTS.md`.
- Shell mặc định là Windows PowerShell: nối lệnh bằng `;`, không dùng `&&`.
- Chỉ ghi nhận hoàn tất sau khi chạy `python -m pytest` và `git diff --check` sau chỉnh sửa cuối, sau đó commit chuẩn Conventional Commits và push lên `origin/main`.
