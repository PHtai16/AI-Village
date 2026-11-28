---
trigger: always_on
description: Bộ nhớ dự án AI Village cập nhật trạng thái và ranh giới kỹ thuật sau khi hoàn tất PHASE 10
---

# Bộ nhớ dự án AI Village sau PHASE 10

## Trạng thái tiến độ và kiểm chứng
- Dự án đã hoàn tất từ Chặng A đến Chặng F cùng toàn bộ `PHASE 10` gồm `P10.01` đến `P10.05` thuộc Chặng G ở mức `OFFLINE_INTEGRATION_VERIFIED`.
- Chuỗi commit triển khai `PHASE 10`: `258ac10` cho `P10.01`, `3d5f5bb` cho `P10.02`, `f53ca1f` cho `P10.03`, `dd42ac9` cho `P10.04` và `df8ea49` cho `P10.05`.
- Bộ kiểm thử chuyên biệt của `PHASE 10` tại `tests/skills/` cùng `test_p10_01_process_retrospectives.py` và `test_p10_02_pattern_detection.py` đạt `19 passed in 19.58s` trên commit `df8ea49`.
- Trọng tâm kế tiếp: `PHASE 11 — Interrupts & Resources` từ `P11.01` đến `P11.05` và nghiệm thu `Gate G`.

## Bản đồ thành phần hiện có trên đĩa
- `agents/`: có `agents/master/agent.yaml`, `agents/roles/it_artifact_worker/agent.yaml` đều để `tools.allowed: []` và `skills.allowed: []`, cùng 9 manifest phòng ban tại `agents/departments/*/department.yaml`.
- `core/`: gồm `contracts/`, `tasks/`, `events/`, `governance/`, `runtime/`, `agents/`, `orchestration/`, `tools/`, `departments/` và `skills/`. Không import `storage`, `sqlite3`, `tools`, `api` hay `ui`; `core/skills/` triển khai Phương án C và `ApprovedSkillCatalogPort` phục vụ HR; `core/optimization/` chưa tạo vì thuộc `PHASE 11`.
- `engines/`: có `AntigravityEngineAdapter` đã kiểm chứng live trên `agy.exe` và `GoogleGeminiAdapter` mới kiểm chứng unit/contract; chưa có adapter OpenAI, Anthropic hay Ollama.
- `tools/` và `sandbox/`: có 17 công cụ chuẩn đăng ký qua `tools/registry_bootstrap.py`, `GovernedMcpBridge` và `sandbox/Dockerfile`.
- `storage/`: SQLite migrations `0001` đến `0015` gồm `0013_process_retrospectives.sql`, `0014_pattern_improvements.sql` và `0015_skills_lifecycle_and_benchmarks.sql`; `SQLiteRepositoryStore` gắn sẵn `.process` và `.skills` cùng `ArtifactVaultService` và `EvidenceAndKnowledgeService`.
- `api/`, `skills/`, `ui/`: `api/artifacts.py` mới có 2 endpoint đọc cho Gate E; `skills/schemas/skill_manifest.schema.json` đã có mặt, còn `skills/candidates/` và `skills/approved/` giữ trống theo baseline Chặng A; `ui/` vẫn là scaffold React và Vite từ Chặng A.

## Nợ kỹ thuật và điểm lưu ý khi tiếp tục PHASE 11
- `HrDepartmentService`, `QaDepartmentService`, `VaultDepartmentService`, `ProcessDepartmentService` và `SkillLifecycleService` đều đã lưu đọc SQLite bền vững; `ResourcesDepartmentService`, `ServiceUxDepartmentService`, phần `_inbox` và `_dedup_state` của `ReceptionDepartmentService` cùng `_proposals` cũ của `P09.07` vẫn giữ dữ liệu miền trong bộ nhớ kèm `EventJournal` chờ nối bảng bền vững ở `PHASE 11`.
- Quy tắc mặc định `worker.allow_scoped_execution` trong `core/governance/evaluator.py` chưa cấp `connection.read`; nhiều tệp trong `core/` và `engines/providers/gemini.py` đang import `redact_secrets` từ `engines.antigravity.diagnostics`.
- Khi thêm mô-đun mới trong `core/`, tệp `.py` hoặc `SKILL.md` mẫu vào `skills/`, hoặc đánh dấu `[x]` cho `P11.01+`, phải cập nhật allowlist tương ứng ở `tests/stage_a/test_stage_a_baseline.py` và `tests/standards/test_standards_consistency.py`.

## Kỷ luật vận hành cục bộ
- Đọc tài liệu theo thứ tự `docs/architecture/SYSTEM_BLUEPRINT.md`, `docs/standards/`, `docs/PROGRESS.md`, rồi `AGENTS.md`.
- Shell mặc định là Windows PowerShell: nối lệnh bằng `;`, không dùng `&&`.
- Chỉ ghi nhận hoàn tất sau khi chạy `python -m pytest` và `git diff --check` sau chỉnh sửa cuối, sau đó commit chuẩn Conventional Commits và push lên `origin/main`.
