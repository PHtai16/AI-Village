# Thư mục `tests/`: Hệ thống Kiểm thử Tự động

Thư mục `tests/` lưu trữ toàn bộ bộ kiểm thử tự động của AI Village, bao gồm kiểm thử tính nhất quán đặc tả kỹ thuật, kiểm thử cấu trúc Chặng A, unit test, contract test và integration test cho các phase sau.

## 1. Lệnh Chạy Kiểm thử Thống nhất

Từ thư mục gốc của repository (`C:\DoCho\AI-Village`), chạy toàn bộ bộ kiểm thử bằng một lệnh:

```powershell
python -m pytest tests -v
```

Chạy kiểm thử live handshake `P02.01`, tool execution `P02.02`, event streaming `P02.03`, session isolation `P02.04`, process lifecycle `P02.05`, provider contract `P02.06`, `Gate B` End-to-End và `P03.01` Master -> Worker Spike với CLI/Provider thật (không chạy mặc định trong unit test):

```powershell
$env:AI_VILLAGE_RUN_LIVE="1"; python -m pytest tests/integration/test_antigravity_handshake_live.py -v
$env:AI_VILLAGE_RUN_LIVE="1"; python -m pytest tests/integration/test_antigravity_tool_execution_live.py -v
$env:AI_VILLAGE_RUN_LIVE="1"; python -m pytest tests/integration/test_antigravity_streaming_live.py -v
$env:AI_VILLAGE_RUN_LIVE="1"; python -m pytest tests/integration/test_antigravity_session_isolation_live.py -v
$env:AI_VILLAGE_RUN_LIVE="1"; python -m pytest tests/integration/test_antigravity_lifecycle_live.py -v
$env:AI_VILLAGE_RUN_LIVE="1"; python -m pytest tests/integration/test_engine_provider_contract_live.py -v
$env:AI_VILLAGE_RUN_LIVE="1"; python -m pytest tests/integration/test_gate_b_end_to_end_live.py -v
$env:AI_VILLAGE_RUN_LIVE="1"; python -m pytest tests/integration/test_master_worker_spike_live.py -v -rs --log-cli-level=INFO
$env:AI_VILLAGE_RUN_LIVE="1"; $env:CREWAI_DISABLE_TELEMETRY="true"; $env:OTEL_SDK_DISABLED="true"; .runtime/crewai-bench-venv/Scripts/python.exe -m pytest tests/integration/test_p03_02_crewai_benchmark_live.py -v -rs --log-cli-level=INFO
$env:AI_VILLAGE_RUN_LIVE="1"; python -m pytest tests/integration/test_agent_runtime_live.py -v -rs --log-cli-level=INFO
```

Hoặc chạy riêng từng nhóm kiểm thử hiện có:

```powershell
python -m pytest tests/standards/test_standards_consistency.py -v
python -m pytest tests/stage_a/test_stage_a_baseline.py -v
python -m pytest tests/engines/test_antigravity_adapter_unit.py -v
python -m pytest tests/engines/test_antigravity_tool_execution_unit.py -v
python -m pytest tests/engines/test_antigravity_streaming_unit.py -v
python -m pytest tests/engines/test_antigravity_session_isolation_unit.py -v
python -m pytest tests/engines/test_antigravity_lifecycle_unit.py -v
python -m pytest tests/engines/test_engine_provider_contract_unit.py -v
python -m pytest tests/engines/test_gate_b_verification_unit.py -v
python -m pytest tests/core/test_master_worker_spike_unit.py -v
python -m pytest tests/core/test_p03_02_custom_error_recovery_unit.py -v
python -m pytest tests/core/test_agent_runtime_contract_unit.py -v
python -m pytest tests/core/test_p04_01_core_contracts_unit.py -v
python -m pytest tests/core/test_p04_02_task_state_machine_unit.py -v
python -m pytest tests/core/test_p04_03_event_journal_unit.py -v
python -m pytest tests/core/test_p04_06_workflow_lifecycle_unit.py -v
python -m pytest tests/core/test_p04_07_governance_unit.py -v
python -m pytest tests/integration/test_p04_06_workflow_lifecycle.py -v
python -m pytest tests/integration/test_p04_07_governance_persistence.py -v
python -m pytest tests/integration/test_phase_04_gate_c_acceptance.py -v
python -m pytest tests/agents/test_p05_01_agent_manifest.py -v
python -m pytest tests/agents/test_p05_02_agent_registry.py -v
python -m pytest tests/agents/test_p05_07_agent_recruitment.py -v
python -m pytest tests/agents/test_p09_01_hr_department.py -v
python -m pytest tests/departments/test_p09_02_04_departments.py tests/departments/test_p09_05_07_departments.py tests/departments/test_p09_08_09_departments.py tests/departments/test_gate_f_end_to_end.py -v
python -m pytest tests/orchestration/test_p05_03_master_planning.py -v
python -m pytest tests/orchestration/test_p05_04_dynamic_team_builder.py -v
python -m pytest tests/orchestration/test_p05_03_04_integration_acceptance.py -v
python -m pytest tests/orchestration/test_p05_05_worker_session_lifecycle.py -v
python -m pytest tests/orchestration/test_p05_06_worker_handoff.py -v
python -m pytest tests/orchestration/test_p05_05_06_integration_acceptance.py -v
python -m pytest tests/orchestration/test_p06_01_04_meetings.py -v
python -m pytest tests/orchestration/test_p06_05_06_delivery_and_gate_d.py -v
python -m pytest tests/storage/test_p04_04_sqlite_migrations.py -v
python -m pytest tests/storage/test_p04_05_persistent_repositories.py -v
python -m pytest tests/storage/test_p04_05_persistence_acceptance.py -v
python -m pytest tests/storage/test_p08_01_02_artifact_vault.py tests/storage/test_p08_03_04_evidence_and_knowledge.py tests/storage/test_p08_05_backup_trash_and_bulk.py tests/storage/test_gate_e_end_to_end.py -v
python -m pytest tests/integration/test_stage_e_cross_stage_sync.py -v
python -m pytest tests/tools -v
$env:AI_VILLAGE_RUN_LIVE="1"; python -m pytest tests/tools/test_code_and_mcp_tools.py -k live -v
$env:CREWAI_DISABLE_TELEMETRY="true"; $env:OTEL_SDK_DISABLED="true"; .runtime/crewai-bench-venv/Scripts/python.exe -m pytest tests/benchmarks/p03_02/test_crewai_evaluation_unit.py -v
```

Để kiểm tra build giao diện `ui/` tối thiểu:

```powershell
cd ui
npm run build
```

## 2. Phạm vi Kiểm thử Hiện tại (Chặng A)

- [tests/standards/test_standards_consistency.py](standards/test_standards_consistency.py): Kiểm chứng sự tồn tại của tài liệu chuẩn PHASE 01B, cân bằng thẻ code Markdown, không rò rỉ bí mật, phân tích YAML của 3 template và 10 ví dụ nhúng trong 5 tài liệu Specification.
- [tests/stage_a/test_stage_a_baseline.py](stage_a/test_stage_a_baseline.py): Kiểm chứng cấu trúc 14 thư mục gốc, tài liệu PHASE 00 và README các thư mục PHASE 01, cấu hình `.env.example` và `.gitignore`, kiểm tra liên kết tài liệu, kiểm tra không có mã nghiệp vụ sai tầng, không rò rỉ secret và kiểm tra cấu trúc scaffold của `ui/`.

## 3. Quy tắc Kiểm thử

- Không viết kiểm thử giả định cho các thành phần Runtime chưa triển khai (`engines/antigravity/`, `core/`, `tools/`, `storage/`).
- Không đánh dấu đạt bằng lệnh in chuỗi giả; mọi bài kiểm thử phải đọc và xác minh trực tiếp dữ liệu trên hệ thống tệp.
- Khi có sự cố kiểm thử thất bại, sửa nguyên nhân gốc trong phạm vi phase hiện hành và chạy lại toàn bộ lệnh kiểm thử để ghi nhận bằng chứng mới.
