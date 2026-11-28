---
trigger: always_on
description: Bộ nhớ dự án AI Village cập nhật trạng thái và ranh giới kỹ thuật sau khi hoàn tất P11.04
---

# Bộ nhớ dự án AI Village sau P11.04

## Trạng thái tiến độ và kiểm chứng
- Dự án đã hoàn tất từ Chặng A đến Chặng F, toàn bộ `PHASE 10` từ `P10.01` đến `P10.05`, cùng `P11.01`, `P11.02`, `P11.03` và `P11.04` thuộc `PHASE 11` ở mức `OFFLINE_INTEGRATION_VERIFIED`.
- Commit mã nguồn và kiểm thử cho `PHASE 11`: `22accc0` cho `P11.01`, `33291b7` cho `P11.02`, `9065d87` cho `P11.03` và `aba62d0` cho `P11.04`, lưu báo cáo tương ứng tại `docs/reviews/`.
- Trọng tâm kế tiếp: `P11.05` trong `PHASE 11` và nghiệm thu `Gate G`.

## Bản đồ thành phần hiện có trên đĩa
- `agents/`: có `agents/master/agent.yaml` và `agents/roles/it_artifact_worker/agent.yaml` giữ `tools.allowed: []` và `skills.allowed: []`, cùng 9 manifest phòng ban tại `agents/departments/*/department.yaml`.
- `core/`: gồm `contracts/`, `tasks/`, `events/`, `governance/`, `runtime/`, `agents/`, `orchestration/`, `tools/`, `departments/`, `skills/` và `optimization/`. Khối `core/departments/` đã thêm `resources_models.py` cùng `resources_ledger.py` nối vào `ResourcesDepartmentService` giữ thuần AST.
- `engines/`: có `AntigravityEngineAdapter` đã kiểm chứng live trên `agy.exe` và `GoogleGeminiAdapter` mới kiểm chứng unit và contract; chưa có adapter OpenAI, Anthropic hay Ollama.
- `tools/` và `sandbox/`: có 17 công cụ chuẩn đăng ký qua `tools/registry_bootstrap.py`, `GovernedMcpBridge` và `sandbox/Dockerfile`.
- `storage/`: SQLite migrations `0001` đến `0019` gồm `0019_resource_ledger_and_alerts.sql`. `SQLiteRepositoryStore` đã gắn `.reception`, `.interrupts`, `.scheduler` và `.resources`.
- `api/`, `skills/`, `ui/`: `api/artifacts.py` mới có 2 endpoint đọc cho Gate E; `skills/schemas/skill_manifest.schema.json` đã có mặt, còn `skills/candidates/` và `skills/approved/` giữ trống theo baseline Chặng A; `ui/` vẫn là scaffold React và Vite từ Chặng A.

## Nợ kỹ thuật và điểm lưu ý khi tiếp tục PHASE 11
- `ResourcesDepartmentService` giữ nguyên tương thích ngược `P09.02` qua `ledger_repository=None` tùy chọn, bảo toàn `None` cho số liệu thiếu từ Provider, tách `actual_cost_usd` với `estimated_cost_usd`, gộp cảnh báo quota theo `quota_bucket_key` dùng chung, khử trùng lặp cảnh báo và không tự hủy Task/Run hay xoay key.
- Bước kế tiếp `P11.05` sẽ triển khai dọn dẹp an toàn sau Task trong `managed_temp_root`, bảo vệ artifact/evidence/file dùng chung, kiểm kê sau tác vụ và handoff sang `ProcessDepartmentService` kèm migration `0020`.
- Giữ ranh giới thuần AST cho `core/` và không chỉnh sửa các file migration `0001` đến `0019` đã phát hành.

## Kỷ luật vận hành cục bộ
- Đọc tài liệu theo thứ tự `docs/architecture/SYSTEM_BLUEPRINT.md`, `docs/standards/`, `docs/PROGRESS.md`, rồi `AGENTS.md`.
- Shell mặc định là Windows PowerShell: nối lệnh bằng `;`, không dùng `&&`.
- Chỉ ghi nhận hoàn tất sau khi chạy `python -m pytest` và `git diff --check` sau chỉnh sửa cuối, sau đó commit chuẩn Conventional Commits và push lên `origin/main`.
