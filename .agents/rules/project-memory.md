---
trigger: always_on
description: Bộ nhớ dự án AI Village cập nhật trạng thái và ranh giới kỹ thuật sau khi hoàn tất Chặng F
---

# Bộ nhớ dự án AI Village sau Chặng F

## Trạng thái tiến độ và kiểm chứng
- Dự án đã hoàn tất từ Chặng A đến Chặng F gồm `PHASE 00` đến `PHASE 09` cùng `Gate A` đến `Gate F` ở mức `OFFLINE_INTEGRATION_VERIFIED`.
- Mốc commit gần nhất trước đợt đồng bộ bộ nhớ: `4d302c7` và `04e3e73` cho `P09.01`, `9f31d92` và `ea6907f` cho `P09.02` đến `P09.09` cùng `Gate F`.
- Kết quả `python -m pytest tests -v -rs` tại `ea6907f` đạt `296 passed, 25 skipped`; `25` ca bỏ qua gồm `22` ca `AI_VILLAGE_RUN_LIVE=1`, `2` ca `crewai` và `1` ca Docker sandbox live do Docker Desktop trên host tắt.
- Trọng tâm kế tiếp: Chặng G gồm `PHASE 10` và `PHASE 11`.

## Bản đồ thành phần hiện có trên đĩa
- `agents/`: có `agents/master/agent.yaml`, `agents/roles/it_artifact_worker/agent.yaml` đều để `tools.allowed: []` và `skills.allowed: []`, cùng 9 manifest phòng ban tại `agents/departments/*/department.yaml`.
- `core/`: gồm `contracts/`, `tasks/`, `events/`, `governance/`, `runtime/`, `agents/`, `orchestration/`, `tools/` và `departments/`. Không import `storage`, `sqlite3`, `tools`, `api` hay `ui`; `core/optimization/` chưa tạo vì thuộc `PHASE 11`.
- `engines/`: có `AntigravityEngineAdapter` đã kiểm chứng live trên `agy.exe` và `GoogleGeminiAdapter` mới kiểm chứng unit/contract; chưa có adapter OpenAI, Anthropic hay Ollama.
- `tools/` và `sandbox/`: có 17 công cụ chuẩn đăng ký qua `tools/registry_bootstrap.py`, `GovernedMcpBridge` và `sandbox/Dockerfile`.
- `storage/`: SQLite migrations `0001` đến `0012`, `SQLiteRepositoryStore`, `ArtifactVaultService` và `EvidenceAndKnowledgeService`.
- `api/`, `skills/`, `ui/`: `api/artifacts.py` mới có 2 endpoint đọc-chỉ cho Gate E; `skills/` còn trống chờ `PHASE 10`; `ui/` vẫn là scaffold React + Vite từ Chặng A.

## Nợ kỹ thuật và điểm lưu ý khi mở Chặng G
- `ToolRuntime._finalize` tại `core/tools/runtime.py` phát sự kiện `tool.call_completed` với khóa `"tool_id"`, nhưng `core/events/projections.py` mới đọc `payload.get("tool_name")` và cần bổ sung `"tool_id"` để `P10.01` và timeline không khuyết tên công cụ.
- `HrDepartmentService`, `QaDepartmentService` và `VaultDepartmentService` lưu đọc trực tiếp SQLite hoặc Vault; các lớp `ResourcesDepartmentService`, `ProcessDepartmentService`, `ServiceUxDepartmentService`, cùng `_inbox` và `_dedup_state` của `ReceptionDepartmentService` đang giữ dữ liệu miền trong bộ nhớ kèm `EventJournal`, cần nối bảng bền vững ở `PHASE 10` và `PHASE 11`.
- Quy tắc mặc định `worker.allow_scoped_execution` trong `core/governance/evaluator.py` chưa cấp `connection.read`; gần 20 tệp trong `core/` và `engines/providers/gemini.py` đang import `redact_secrets` từ `engines.antigravity.diagnostics`.
- Khi thêm mô-đun mới trong `core/`, tệp `.py` trong `skills/` hoặc đánh dấu `[x]` cho `P10+`, phải cập nhật allowlist tương ứng ở `tests/stage_a/test_stage_a_baseline.py` và `tests/standards/test_standards_consistency.py`.

## Kỷ luật vận hành cục bộ
- Đọc tài liệu theo thứ tự `docs/architecture/SYSTEM_BLUEPRINT.md`, `docs/standards/`, `docs/PROGRESS.md`, rồi `AGENTS.md`.
- Shell mặc định là Windows PowerShell: nối lệnh bằng `;`, không dùng `&&`.
- Chỉ ghi nhận hoàn tất sau khi chạy `python -m pytest` và `git diff --check` sau chỉnh sửa cuối, sau đó commit chuẩn Conventional Commits và push lên `origin/main`.
