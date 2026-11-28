---
trigger: always_on
description: Bộ nhớ dự án AI Village cập nhật trạng thái và ranh giới kỹ thuật sau P10.01
---

# Bộ nhớ dự án AI Village sau P10.01

## Trạng thái tiến độ và kiểm chứng
- Dự án đã hoàn tất từ Chặng A đến Chặng F cùng mục đầu tiên của Chặng G là `P10.01` thuộc `PHASE 10` ở mức `OFFLINE_INTEGRATION_VERIFIED`.
- Mốc commit mã nguồn và kiểm thử `P10.01`: `258ac10` với thông điệp `feat(process): implement persistent task retrospectives`, tiếp nối các mốc Chặng F gồm `4d302c7`, `04e3e73`, `9f31d92` và `ea6907f`.
- Kết quả `python -m pytest tests -v -rs` tại `258ac10` đạt `304 passed, 25 skipped in 84.38s`; `25` ca bỏ qua gồm `22` ca cần `AI_VILLAGE_RUN_LIVE=1`, `2` ca cần gói `crewai` và `1` ca Docker sandbox live do Docker Desktop tắt trên host.
- Trọng tâm kế tiếp: `P10.02` đến `P10.05` trong `PHASE 10` và tiếp theo là `PHASE 11`.

## Bản đồ thành phần hiện có trên đĩa
- `agents/`: có `agents/master/agent.yaml`, `agents/roles/it_artifact_worker/agent.yaml` đều để `tools.allowed: []` và `skills.allowed: []`, cùng 9 manifest phòng ban tại `agents/departments/*/department.yaml`.
- `core/`: gồm `contracts/`, `tasks/`, `events/`, `governance/`, `runtime/`, `agents/`, `orchestration/`, `tools/` và `departments/`. Không import `storage`, `sqlite3`, `tools`, `api` hay `ui`; `core/departments/process_models.py` tách rõ hợp đồng miền `P10.01`; `core/optimization/` chưa tạo vì thuộc `PHASE 11`.
- `engines/`: có `AntigravityEngineAdapter` đã kiểm chứng live trên `agy.exe` và `GoogleGeminiAdapter` mới kiểm chứng unit/contract; chưa có adapter OpenAI, Anthropic hay Ollama.
- `tools/` và `sandbox/`: có 17 công cụ chuẩn đăng ký qua `tools/registry_bootstrap.py`, `GovernedMcpBridge` và `sandbox/Dockerfile`.
- `storage/`: SQLite migrations `0001` đến `0013` với `0013_process_retrospectives.sql` cho hồi cứu bất biến theo revision, `SQLiteRepositoryStore` gắn sẵn `.process` từ `SQLiteProcessRepository`, `ArtifactVaultService` và `EvidenceAndKnowledgeService`.
- `api/`, `skills/`, `ui/`: `api/artifacts.py` mới có 2 endpoint đọc cho Gate E; `skills/` còn trống chờ `P10.03+`; `ui/` vẫn là scaffold React và Vite từ Chặng A.

## Nợ kỹ thuật và điểm lưu ý khi tiếp tục Chặng G
- `ActivityTimelineProjection` trong `core/events/projections.py` đã đọc cả `tool_name` và `tool_id` từ `redacted_payload`, giúp khớp với `ToolRuntime._finalize`.
- `HrDepartmentService`, `QaDepartmentService`, `VaultDepartmentService` và `ProcessDepartmentService` qua `SQLiteProcessRepository` đã lưu đọc SQLite bền vững; `ResourcesDepartmentService`, `ServiceUxDepartmentService` cùng `_inbox` và `_dedup_state` của `ReceptionDepartmentService`, cũng như `_proposals` của `ProcessDepartmentService`, vẫn giữ dữ liệu miền trong bộ nhớ kèm `EventJournal` chờ nối bảng bền vững ở `P10.02+` và `PHASE 11`.
- Quy tắc mặc định `worker.allow_scoped_execution` trong `core/governance/evaluator.py` chưa cấp `connection.read`; nhiều tệp trong `core/` và `engines/providers/gemini.py` đang import `redact_secrets` từ `engines.antigravity.diagnostics`.
- Khi thêm mô-đun mới trong `core/`, tệp `.py` trong `skills/` hoặc đánh dấu `[x]` cho `P10.02+`, phải cập nhật allowlist tương ứng ở `tests/stage_a/test_stage_a_baseline.py` và `tests/standards/test_standards_consistency.py`.

## Kỷ luật vận hành cục bộ
- Đọc tài liệu theo thứ tự `docs/architecture/SYSTEM_BLUEPRINT.md`, `docs/standards/`, `docs/PROGRESS.md`, rồi `AGENTS.md`.
- Shell mặc định là Windows PowerShell: nối lệnh bằng `;`, không dùng `&&`.
- Chỉ ghi nhận hoàn tất sau khi chạy `python -m pytest` và `git diff --check` sau chỉnh sửa cuối, sau đó commit chuẩn Conventional Commits và push lên `origin/main`.
