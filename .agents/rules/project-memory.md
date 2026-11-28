---
trigger: always_on
description: Bộ nhớ dự án AI Village cập nhật trạng thái và ranh giới kỹ thuật sau khi hoàn tất P11.03
---

# Bộ nhớ dự án AI Village sau P11.03

## Trạng thái tiến độ và kiểm chứng
- Dự án đã hoàn tất từ Chặng A đến Chặng F, toàn bộ `PHASE 10` từ `P10.01` đến `P10.05`, cùng `P11.01`, `P11.02` và `P11.03` thuộc `PHASE 11` ở mức `OFFLINE_INTEGRATION_VERIFIED`.
- Commit mã nguồn và kiểm thử cho `PHASE 11`: `22accc0` cho `P11.01`, `33291b7` cho `P11.02` và `9065d87` cho `P11.03`, lưu báo cáo tương ứng tại `docs/reviews/`.
- Trọng tâm kế tiếp: `P11.04`, `P11.05` trong `PHASE 11` và nghiệm thu `Gate G`.

## Bản đồ thành phần hiện có trên đĩa
- `agents/`: có `agents/master/agent.yaml` và `agents/roles/it_artifact_worker/agent.yaml` giữ `tools.allowed: []` và `skills.allowed: []`, cùng 9 manifest phòng ban tại `agents/departments/*/department.yaml`.
- `core/`: gồm `contracts/`, `tasks/`, `events/`, `governance/`, `runtime/`, `agents/`, `orchestration/`, `tools/`, `departments/`, `skills/` và `optimization/`. Khối `core/optimization/` mới thêm `scheduler_models.py` cùng `ExecutionSchedulerService` tại `scheduler.py` giữ thuần AST.
- `engines/`: có `AntigravityEngineAdapter` đã kiểm chứng live trên `agy.exe` và `GoogleGeminiAdapter` mới kiểm chứng unit và contract; chưa có adapter OpenAI, Anthropic hay Ollama.
- `tools/` và `sandbox/`: có 17 công cụ chuẩn đăng ký qua `tools/registry_bootstrap.py`, `GovernedMcpBridge` và `sandbox/Dockerfile`.
- `storage/`: SQLite migrations `0001` đến `0018` gồm `0016_reception_conversations_and_intake.sql`, `0017_interrupts_routing_and_dedup.sql` và `0018_scheduler_queue_leases_and_schedules.sql`. `SQLiteRepositoryStore` đã gắn `.reception`, `.interrupts` và `.scheduler`.
- `api/`, `skills/`, `ui/`: `api/artifacts.py` mới có 2 endpoint đọc cho Gate E; `skills/schemas/skill_manifest.schema.json` đã có mặt, còn `skills/candidates/` và `skills/approved/` giữ trống theo baseline Chặng A; `ui/` vẫn là scaffold React và Vite từ Chặng A.

## Nợ kỹ thuật và điểm lưu ý khi tiếp tục PHASE 11
- `ReceptionDepartmentService`, `InterruptRoutingService` và `ExecutionSchedulerService` đã lưu SQLite bền vững; `ResourcesDepartmentService` chuẩn bị mở rộng sang `P11.04` qua bảng sổ cái tài nguyên, cảnh báo ngân sách và quota; dọn dẹp sau Task và handoff Process nằm ở `P11.05`.
- `ExecutionSchedulerService` cấp phát slot theo `quota_bucket_key` dùng chung khi chưa có bằng chứng độc lập quota, tách `RESERVED` với `ACTIVE`, chặn xoay key né giới hạn và hỗ trợ lease TTL cùng `scheduled_tasks`.
- Giữ ranh giới thuần AST cho `core/` ở mọi bước `P11.04` và `P11.05`, đồng thời không sửa các file migration đã phát hành `0001` đến `0018`.

## Kỷ luật vận hành cục bộ
- Đọc tài liệu theo thứ tự `docs/architecture/SYSTEM_BLUEPRINT.md`, `docs/standards/`, `docs/PROGRESS.md`, rồi `AGENTS.md`.
- Shell mặc định là Windows PowerShell: nối lệnh bằng `;`, không dùng `&&`.
- Chỉ ghi nhận hoàn tất sau khi chạy `python -m pytest` và `git diff --check` sau chỉnh sửa cuối, sau đó commit chuẩn Conventional Commits và push lên `origin/main`.
