---
trigger: always_on
description: Bộ nhớ dự án AI Village cập nhật trạng thái và ranh giới kỹ thuật sau khi hoàn tất P11.01
---

# Bộ nhớ dự án AI Village sau P11.01

## Trạng thái tiến độ và kiểm chứng
- Dự án đã hoàn tất từ Chặng A đến Chặng F, toàn bộ `PHASE 10` gồm `P10.01` đến `P10.05`, và hạng mục `P11.01` thuộc `PHASE 11` ở mức `OFFLINE_INTEGRATION_VERIFIED`.
- Commit triển khai `PHASE 10`: `258ac10` cho `P10.01`, `3d5f5bb` cho `P10.02`, `f53ca1f` cho `P10.03`, `dd42ac9` cho `P10.04` và `df8ea49` cho `P10.05`.
- Commit mã nguồn và kiểm thử cho `P11.01`: `22accc0` ghi nhận đầy đủ tại `docs/reviews/PHASE_11_P11_01_AUDIT.md`.
- Trọng tâm kế tiếp: `P11.02` đến `P11.05` trong `PHASE 11` và nghiệm thu `Gate G`.

## Bản đồ thành phần hiện có trên đĩa
- `agents/`: có `agents/master/agent.yaml` và `agents/roles/it_artifact_worker/agent.yaml` giữ `tools.allowed: []` và `skills.allowed: []`, cùng 9 manifest phòng ban tại `agents/departments/*/department.yaml`.
- `core/`: gồm `contracts/`, `tasks/`, `events/`, `governance/`, `runtime/`, `agents/`, `orchestration/`, `tools/`, `departments/` và `skills/`. Khối `core/departments/` đã có thêm `reception_models.py` cùng `ReceptionDepartmentService` xử lý tiếp nhận đồng thời theo ngữ cảnh hội thoại và giữ thuần AST không import `sqlite3`, `storage`, `tools`, `api` hay `ui`; `core/optimization/` chưa tạo vì dành cho các bước tài nguyên sau của `PHASE 11`.
- `engines/`: có `AntigravityEngineAdapter` đã kiểm chứng live trên `agy.exe` và `GoogleGeminiAdapter` mới kiểm chứng unit và contract; chưa có adapter OpenAI, Anthropic hay Ollama.
- `tools/` và `sandbox/`: có 17 công cụ chuẩn đăng ký qua `tools/registry_bootstrap.py`, `GovernedMcpBridge` và `sandbox/Dockerfile`.
- `storage/`: SQLite migrations `0001` đến `0016` gồm `0016_reception_conversations_and_intake.sql` quản lý `conversations`, `intake_requests`, `conversation_task_links`, `conversation_turns`, `task_refinements`, `action_inbox_items` và `reception_dedup_state`. `SQLiteRepositoryStore` đã gắn `.reception` qua `SQLiteReceptionRepository`, cùng `.process`, `.skills`, `ArtifactVaultService` và `EvidenceAndKnowledgeService`.
- `api/`, `skills/`, `ui/`: `api/artifacts.py` mới có 2 endpoint đọc cho Gate E; `skills/schemas/skill_manifest.schema.json` đã có mặt, còn `skills/candidates/` và `skills/approved/` giữ trống theo baseline Chặng A; `ui/` vẫn là scaffold React và Vite từ Chặng A.

## Nợ kỹ thuật và điểm lưu ý khi tiếp tục PHASE 11
- `HrDepartmentService`, `QaDepartmentService`, `VaultDepartmentService`, `ProcessDepartmentService`, `SkillLifecycleService` và `ReceptionDepartmentService` đều đã nối lưu trữ SQLite bền vững. `ResourcesDepartmentService`, `ServiceUxDepartmentService` và danh sách `_proposals` cũ của `P09.07` vẫn giữ trạng thái miền trong bộ nhớ kèm `EventJournal` chờ nối bảng bền vững ở các tiểu phase tiếp theo của `PHASE 11`.
- `ReceptionDepartmentService` đã có idempotency key phạm vi DB, khóa phiên bản lạc quan `record_version`, trigger chống sửa xóa cho `conversation_turns` và `task_refinements`, và sửa lỗi băm khử trùng lặp của `triage_incoming_signal` đối với tín hiệu `QUEUED` và `APPENDED`.
- Khi thêm mô-đun mới trong `core/`, tệp `.py` hoặc `SKILL.md` mẫu vào `skills/`, hoặc đánh dấu `[x]` cho `P11.02+`, phải cập nhật allowlist tương ứng ở `tests/stage_a/test_stage_a_baseline.py` và `tests/standards/test_standards_consistency.py`.

## Kỷ luật vận hành cục bộ
- Đọc tài liệu theo thứ tự `docs/architecture/SYSTEM_BLUEPRINT.md`, `docs/standards/`, `docs/PROGRESS.md`, rồi `AGENTS.md`.
- Shell mặc định là Windows PowerShell: nối lệnh bằng `;`, không dùng `&&`.
- Chỉ ghi nhận hoàn tất sau khi chạy `python -m pytest` và `git diff --check` sau chỉnh sửa cuối, sau đó commit chuẩn Conventional Commits và push lên `origin/main`.
