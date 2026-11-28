---
trigger: always_on
description: Bộ nhớ dự án AI Village cập nhật trạng thái và ranh giới kỹ thuật sau khi hoàn tất P11.05
---

# Bộ nhớ dự án AI Village sau P11.05

## Trạng thái tiến độ và kiểm chứng
- Dự án đã hoàn tất từ Chặng A đến Chặng F, toàn bộ `PHASE 10` từ `P10.01` đến `P10.05`, cùng `P11.01` đến `P11.05` thuộc `PHASE 11` ở mức `OFFLINE_INTEGRATION_VERIFIED`.
- Commit mã nguồn và kiểm thử cho `PHASE 11`: `22accc0` cho `P11.01`, `33291b7` cho `P11.02`, `9065d87` cho `P11.03`, `aba62d0` cho `P11.04` và `30aec5e` cho `P11.05`, lưu báo cáo tương ứng tại `docs/reviews/`.
- Trọng tâm kế tiếp: nghiệm thu đầu-cuối `Gate G` cho toàn bộ `PHASE 11`.

## Bản đồ thành phần hiện có trên đĩa
- `agents/`: có `agents/master/agent.yaml` và `agents/roles/it_artifact_worker/agent.yaml` giữ `tools.allowed: []` và `skills.allowed: []`, cùng 9 manifest phòng ban tại `agents/departments/*/department.yaml`.
- `core/`: gồm `contracts/`, `tasks/`, `events/`, `governance/`, `runtime/`, `agents/`, `orchestration/`, `tools/`, `departments/`, `skills/` và `optimization/`. Khối `core/optimization/` đã có đầy đủ `scheduler_models.py`, `scheduler.py`, `cleanup_models.py` và `cleanup.py` giữ thuần AST.
- `engines/`: có `AntigravityEngineAdapter` đã kiểm chứng live trên `agy.exe` và `GoogleGeminiAdapter` mới kiểm chứng unit và contract; chưa có adapter OpenAI, Anthropic hay Ollama.
- `tools/` và `sandbox/`: có 17 công cụ chuẩn đăng ký qua `tools/registry_bootstrap.py`, `GovernedMcpBridge` và `sandbox/Dockerfile`.
- `storage/`: SQLite migrations `0001` đến `0020` gồm `0020_post_task_cleanup_and_handoffs.sql`. `SQLiteRepositoryStore` đã gắn `.reception`, `.interrupts`, `.scheduler`, `.resources` và `.cleanup`.
- `api/`, `skills/`, `ui/`: `api/artifacts.py` mới có 2 endpoint đọc cho Gate E; `skills/schemas/skill_manifest.schema.json` đã có mặt, còn `skills/candidates/` và `skills/approved/` giữ trống theo baseline Chặng A; `ui/` vẫn là scaffold React và Vite từ Chặng A.

## Nợ kỹ thuật và điểm lưu ý khi tiếp tục CHẶNG G / CHẶNG H
- `PostTaskCleanupService` chỉ xóa trong `managed_temp_root`, chặn đường dẫn thoát hoặc symlink, giữ nguyên toàn bộ Vault artifact, evidence và dữ liệu dùng chung giữa các Task, thu hồi lease còn sót và gửi handoff nối sang `ProcessDepartmentService`.
- Nghiệm thu `Gate G` cần kiểm chứng xuyên suốt kịch bản người dùng giao Task B khi Task A đang chạy, ưu tiên và cô lập hội thoại, phân loại ngắt và cảnh báo ngân sách/quota, dọn dẹp sau hoàn thành cùng replay Event Journal sau khởi động lại.

## Kỷ luật vận hành cục bộ
- Đọc tài liệu theo thứ tự `docs/architecture/SYSTEM_BLUEPRINT.md`, `docs/standards/`, `docs/PROGRESS.md`, rồi `AGENTS.md`.
- Shell mặc định là Windows PowerShell: nối lệnh bằng `;`, không dùng `&&`.
- Chỉ ghi nhận hoàn tất sau khi chạy `python -m pytest` và `git diff --check` sau chỉnh sửa cuối, sau đó commit chuẩn Conventional Commits và push lên `origin/main`.
