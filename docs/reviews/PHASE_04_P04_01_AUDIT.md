# BÁO CÁO NGHIỆM THU PHASE 04 / P04.01: CORE CONTRACTS & FOUNDATION DATA MODELS

- **Giai đoạn:** PHASE 04 / P04.01
- **Mục tiêu:** Thiết lập bộ hợp đồng dữ liệu nền tảng tại `core/contracts/` cho Task Runtime, Event Journal, SQLite repositories, Permission Manager và Mission Control
- **Trạng thái cuối:** **PASS**
- **Hình thức kiểm chứng:** Offline unit tests và AST boundary validation, chưa chạy live `agy.exe` do thuần hợp đồng dữ liệu
- **HEAD tham chiếu lúc bắt đầu:** `d7c1600e31832c4fea22883bd7d16b9a257c9c30`

---

## 1. Danh sách Core Contracts

Gói mới tại `core/contracts/` gồm `10` mô-đun liên kết theo đồ thị có hướng không chu trình với [__init__.py](file:///C:/DoCho/AI-Village/core/contracts/__init__.py):

| Mô-đun | Thành phần chính | Vai trò |
| :--- | :--- | :--- |
| [identifiers.py](file:///C:/DoCho/AI-Village/core/contracts/identifiers.py) | `TaskId`, `RunId`, `SessionId`, `ArtifactId`, `EventId`, `ChecklistItemId`, `NotificationId`, `ProjectId`, `EvidenceId`, `AgentId`, `DepartmentId`, `Sha256Hex`, `SemVer`, các hàm `new_*_id()` và validator an toàn | Chuẩn hóa định danh, kiểm tra timezone, thứ tự thời gian, đường dẫn tương đối và lọc bí mật |
| [references.py](file:///C:/DoCho/AI-Village/core/contracts/references.py) | `ConnectionRef`, `ProjectRef`, `ChecklistItem`, `NotificationRef` | Tham chiếu kết nối an toàn, phân nhóm dự án, bước công việc có chứng cứ và tham chiếu thông báo |
| [agent.py](file:///C:/DoCho/AI-Village/core/contracts/agent.py) | `AgentIdentity`, alias `Agent`, `AgentRuntimeStateRef`, `DEFAULT_DEPARTMENTS` | Định danh tĩnh tối thiểu của Agent, ngoại lệ Master và trạng thái runtime tách biệt |
| [session.py](file:///C:/DoCho/AI-Village/core/contracts/session.py) | `Session`, `SessionStatus` | Phiên làm việc logic UUID v4 của AI Village, tách biệt `provider_conversation_id` và hỗ trợ `legacy_session_ref` |
| [artifact.py](file:///C:/DoCho/AI-Village/core/contracts/artifact.py) | `Artifact`, `ArtifactVerificationStatus` | Siêu dữ liệu sản phẩm và băm SHA-256 tách khỏi kết quả xác minh QA |
| [run.py](file:///C:/DoCho/AI-Village/core/contracts/run.py) | `Run`, `RunStatus`, `RunTelemetryRef`, `RunErrorRef` | Lượt thực thi độc lập của một Task, mang telemetry trung thực và mã lỗi đã lọc secret |
| [task.py](file:///C:/DoCho/AI-Village/core/contracts/task.py) | `Task`, `TaskStatus`, `TaskPriority` | Mục tiêu công việc cấp người dùng không chứa logic chuyển trạng thái |
| [event.py](file:///C:/DoCho/AI-Village/core/contracts/event.py) | `Event`, `EventType`, `EventSource`, `sanitize_event_payload` | Hợp đồng sự kiện chuẩn hóa cho Event Journal ở `P04.03` |
| [compat.py](file:///C:/DoCho/AI-Village/core/contracts/compat.py) | `connection_ref_from_engine`, `connection_ref_to_execution_routing`, `run_telemetry_from_runtime`, `core_event_from_runtime_event` | Bộ chuyển đổi chỉ đọc sang `AgentRuntime` và `EngineConnectionRef` |

Toàn bộ public models đều dùng Pydantic v2 với `ConfigDict(extra="forbid")`.

---

## 2. Entity relationships

Các thực thể liên kết thông qua mã định danh chuẩn mà không nhúng vòng lặp đối tượng:

- `Task.project_id` liên kết tùy chọn tới `ProjectRef.project_id` mang nghĩa gom nhóm.
- `Task` giữ tham chiếu tới `active_run_id`, `assigned_agent_ids`, `checklist_item_ids` và `artifact_ids`.
- `Run.task_id` liên kết ngược về một `Task`, cho phép nhiều `Run` phục vụ một `Task`.
- `Run.session_ids` liên kết tới một hoặc nhiều `Session.session_id`, còn `Session.agent_id` gắn với `AgentIdentity.agent_id`.
- `ChecklistItem.task_id` gắn về `Task.task_id` và tùy chọn `assigned_agent_id`.
- `Artifact` mang bộ khóa `task_id`, `run_id` và `owner_agent_id`.
- `Event` giữ các tham chiếu tùy chọn `task_id`, `run_id`, `session_id`, `actor_id`, `provider_id`, `connection_id`, `model_id`, `artifact_id`, `evidence_id` và `correlation_id`, yêu cầu có ít nhất một neo định danh hợp lệ.
- `NotificationRef` nối tới `event_id` cùng tùy chọn `task_id` và `run_id`.

---

## 3. Identifier conventions

Hệ thống tách biệt từng miền ID bằng tiền tố và biểu thức chính quy riêng tại [identifiers.py](file:///C:/DoCho/AI-Village/core/contracts/identifiers.py):

- **Domain IDs có tiền tố riêng:** `task_*`, `run_*`, `art_*`, `evt_*`, `chk_*`, `ntf_*`, `prj_*`, `evd_*` ngăn ngừa nhầm lẫn giữa các trường khi truyền qua hàm hoặc JSON.
- **Logical Session ID:** `Session.session_id` bắt buộc chuẩn UUID v4 dạng chữ thường khớp mục `3.2` của [AGENT_SPEC.md](file:///C:/DoCho/AI-Village/docs/standards/AGENT_SPEC.md). Các mã `sess-*` từng chạy tại `P03.01` đến `P03.03` không bị sửa ngược trong spike cũ mà được giữ riêng tại `Session.legacy_session_ref`.
- **Agent và Department:** `AgentId` tuân thủ `^[a-z0-9_]+\.[a-z0-9_]+$`; `DepartmentId`, `provider_id` và `connection_id` tuân thủ `snake_case`.
- **Mã băm và phiên bản:** `Sha256Hex` chuẩn hóa về 64 ký tự hex viết thường; `SemVer` kiểm tra đúng cú pháp `MAJOR.MINOR.PATCH`.

---

## 4. Task/Run separation

Hợp đồng tách biệt [Task](file:///C:/DoCho/AI-Village/core/contracts/task.py) khỏi [Run](file:///C:/DoCho/AI-Village/core/contracts/run.py):

- `Task` mô tả mục tiêu công việc của người dùng, lưu `status` thuộc 9 trạng thái vòng đời `QUEUED`, `ASSIGNED`, `RUNNING`, `WAITING_INPUT`, `WAITING_APPROVAL`, `BLOCKED`, `COMPLETED`, `FAILED`, `CANCELLED`. Trạng thái kết thúc `COMPLETED`, `FAILED`, `CANCELLED` cấm giữ `active_run_id`.
- `Run` mô tả từng lượt thực thi cụ thể, lưu `status` thuộc `PENDING`, `RUNNING`, `WAITING_INPUT`, `WAITING_APPROVAL`, `BLOCKED`, `SUCCEEDED`, `FAILED`, `CANCELLED`, `TIMED_OUT`. Khi retry phát sinh lượt `Run` thứ hai trên cùng `task_id`, trạng thái `Run` hoàn toàn độc lập với `Task.status`.
- Không triển khai hàm chuyển trạng thái của máy trạng thái hay retry engine trong `P04.01`; phần đó thuộc `P04.02` và `P04.06`.

---

## 5. Agent/Session separation

Hợp đồng tách biệt danh tính tĩnh khỏi trạng thái động và phiên thực thi:

- `AgentIdentity` tại [agent.py](file:///C:/DoCho/AI-Village/core/contracts/agent.py) đại diện cho danh tính nhân sự AI, áp dụng ràng buộc hai chiều của ngoại lệ Master trong [AGENT_SPEC.md](file:///C:/DoCho/AI-Village/docs/standards/AGENT_SPEC.md): chỉ `agent_id="core.master"` kết hợp `department="orchestration"` mới được dùng `system_role="orchestrator"`. Thuộc tính `department` từ chối tên các nhà cung cấp như `antigravity`, `google_gemini`, `openai`, `anthropic`, `ollama`, `openai_compatible`.
- `AgentRuntimeStateRef` tách riêng `session_id`, `current_task_id`, `current_run_id`, `status`, `active_connection` và `last_active_at` khỏi danh tính tĩnh, chưa đọc file YAML manifest của Phase 05.
- `Session` tại [session.py](file:///C:/DoCho/AI-Village/core/contracts/session.py) khóa kiểm tra `provider_conversation_id != session_id` và cấm `invocation_id` mang tiền tố `task_`, `run_`, `art_`, `evt_`. Trạng thái `INITIALIZED` cấm gán sẵn `started_at` hay `finished_at`.

---

## 6. Artifact/QA separation

Hợp đồng [Artifact](file:///C:/DoCho/AI-Village/core/contracts/artifact.py) chỉ giữ siêu dữ liệu và tham chiếu kiểm chứng:

- Khởi tạo `Artifact` hoàn toàn thuần túy trong bộ nhớ, không truy cập hệ thống tệp để kiểm tra sự tồn tại của tệp thật.
- Đường dẫn `relative_path` qua `validate_safe_relative_path` từ chối wildcard `*`/`?`, đường dẫn tuyệt đối và đoạn `..`/`.`.
- Truyền một chuỗi `sha256` hợp lệ luôn giữ nguyên `verification_status="UNVERIFIED"`. Hệ thống cấm gán `verification_status="VERIFIED_PASS"` nếu thiếu cả `evidence_reference` lẫn `sha256` đã ghi nhận.

---

## 7. Event references

Hợp đồng [Event](file:///C:/DoCho/AI-Village/core/contracts/event.py) chuẩn hóa nhật ký sự kiện cho `P04.03`:

- Trường `event_id` dùng định dạng `evt_*` và `timestamp` bắt buộc có múi giờ, không tự đặt giá trị mặc định lúc khởi tạo.
- Các trường `provider_id`, `connection_id`, `model_id`, `artifact_id`, `evidence_id` và `correlation_id` là tùy chọn để hỗ trợ cả sự kiện nội bộ lẫn sự kiện gọi AI thật. Mô hình chỉ yêu cầu tồn tại ít nhất một neo quy chiếu trong nhóm `task_id`, `run_id`, `session_id`, `actor_id`, `artifact_id`, `correlation_id`.
- Trường `redacted_payload` kiểm tra đệ quy toàn bộ từ điển và danh sách để chặn fail-closed mọi khóa nhạy cảm hoặc chuỗi khớp định dạng khóa API. Hàm `sanitize_event_payload` hỗ trợ lọc trước các payload thô từ log bên ngoài.

---

## 8. ConnectionRef boundaries

`ConnectionRef` trong [references.py](file:///C:/DoCho/AI-Village/core/contracts/references.py) đóng vai trò tham chiếu gọn nhẹ tại tầng Core mà không sao chép [EngineConnectionRef](file:///C:/DoCho/AI-Village/engines/connections.py) hoặc dựng thêm `CredentialResolver`:

- Chỉ giữ `provider_id`, `connection_id`, tùy chọn `model_id`, `project_ref` và `credential_ref`.
- `credential_ref` chỉ chấp nhận định danh tham chiếu mờ như `env:VAR_NAME` hoặc `ref:scope/name`, từ chối toàn bộ tiền tố khóa thô, khoảng trắng hoặc dấu `=`.
- Phương thức `to_safe_summary()` che giá trị `credential_ref` trước khi đưa ra log hoặc API.
- Khi chuyển đổi qua `connection_ref_from_engine` tại [compat.py](file:///C:/DoCho/AI-Village/core/contracts/compat.py), tham số `include_credential_ref` mặc định đặt `False`.

---

## 9. Project grouping semantics

`ProjectRef` trong [references.py](file:///C:/DoCho/AI-Village/core/contracts/references.py) và trường `Task.project_id` chỉ phục vụ phân nhóm tổ chức công việc. Do `ConfigDict(extra="forbid")` chặn thêm trường lạ vào `ProjectRef` và không định nghĩa runtime, hàng đợi hay state machine riêng cho Project, kiến trúc giữ nguyên một lộ trình thực thi duy nhất `Task -> Run`.

---

## 10. Checklist và Notification references

- `ChecklistItem` theo dõi từng bước dự kiến của một `Task` thông qua `order`, `assigned_agent_id`, `status` và `evidence_reference`, loại bỏ hoàn toàn trường phần trăm tiến độ giả. Một bước chỉ được phép giữ `status="COMPLETED"` khi đồng thời có `evidence_reference` và `completed_at`.
- `NotificationRef` lưu `notification_id`, `event_id`, `severity`, `notification_type`, `created_at` bắt buộc có múi giờ cùng `task_id` hoặc `run_id` tùy chọn, chưa dựng cơ chế phân phối, Action Inbox hay khử trùng lặp thông báo.

---

## 11. Validation rules

Các ràng buộc kiểm chứng chính được thi hành đồng nhất trên toàn gói:

1. **Múi giờ tuyệt đối:** `validate_aware_datetime` từ chối mọi `datetime` dạng naive chưa gắn `tzinfo`.
2. **Thứ tự thời gian:** `validate_chronological` từ chối `updated_at < created_at`, `finished_at < started_at`, `completed_at < created_at`, hoặc ghi thời điểm kết thúc khi chưa có thời điểm bắt đầu.
3. **Không bịa đặt giá trị:** mọi `started_at`, `finished_at`, `updated_at`, `completed_at`, `input_tokens`, `output_tokens`, `total_tokens`, `estimated_cost_usd` và `actual_cost_usd` mặc định là `None`; `RunTelemetryRef` cấm chọn `telemetry_status="VERIFIED_MEASURED"` cùng `telemetry_source="unavailable"`.
4. **Danh sách ID duy nhất:** `validate_unique_id_list` loại bỏ ID trùng lặp trong `assigned_agent_ids`, `checklist_item_ids`, `artifact_ids`, `session_ids` và `capability_ids`.
5. **Từ chối trường ngoài hợp đồng:** tất cả model đặt `extra="forbid"`.

---

## 12. JSON serialization

Toàn bộ 11 model dữ liệu nền gồm `Task`, `Run`, `AgentIdentity`, `AgentRuntimeStateRef`, `Session`, `Artifact`, `Event`, `ConnectionRef`, `ChecklistItem`, `NotificationRef` và `ProjectRef` đã qua kiểm thử `model_dump_json()` và `model_validate_json()`, bảo toàn chính xác kiểu dữ liệu, cấu trúc lồng nhau và giá trị `None` cho các trường nullable mà không ép sang `0` hoặc chuỗi rỗng.

---

## 13. AgentRuntime compatibility

Hợp đồng [contract.py](file:///C:/DoCho/AI-Village/core/runtime/contract.py) của `PHASE 03 / P03.03` được bảo toàn nguyên vẹn, không sửa một dòng nào trong `core/runtime/` hay `core/orchestration/`. Mô-đun [compat.py](file:///C:/DoCho/AI-Village/core/contracts/compat.py) cung cấp 4 hàm ánh xạ thuần túy giúp nối dữ liệu sang tầng Core sau này:

- `connection_ref_from_engine` và `connection_ref_to_execution_routing` nối `EngineConnectionRef`, `ConnectionRef` và `ExecutionRouting`.
- `run_telemetry_from_runtime` chuyển `AgentRuntimeTelemetry` sang `RunTelemetryRef`, giữ nguyên giá trị `None`.
- `core_event_from_runtime_event` chuyển `AgentRuntimeEvent` sang `Event`, yêu cầu có timestamp thực từ caller hoặc `EngineEvent.timestamp` và tự bảo toàn mã phiên `sess-*` cũ tại `redacted_payload["legacy_session_ref"]` nếu chưa dùng UUID v4.

---

## 14. Security review

Quét bảo mật và ranh giới phụ thuộc trên `core/contracts/` xác nhận:

- Không có khóa API, token hay thông tin xác thực trong mã nguồn, bài test hay tài liệu.
- `reject_secret_text`, `ensure_not_secret`, `_is_sensitive_key`, `validate_safe_metadata` và `validate_safe_payload` từ chối khóa bí mật trong mọi trường text, `metadata`, `credential_ref` và `redacted_payload` lồng sâu, vẫn cho phép các khóa thống kê sử dụng như `input_tokens` và `total_tokens`.
- Kiểm tra AST tự động xác nhận `core/contracts/` không import `crewai`, `ui`, `storage`, `api`, `sqlite3`, `sqlalchemy`, `httpx`, `urllib`, `subprocess`, adapter tiến trình con của `engines/antigravity/` hoặc lớp thực thi `CustomAgentRuntime`.
- `test_stage_a_baseline.py` mở allowlist chính xác thêm `core/contracts` mà không nới quyền cho `storage/`, `api/`, `agents/`, `tools/`, `skills/` hoặc `sandbox/`.

**Các giới hạn chưa thể kiểm chứng tại P04.01:** tính toàn vẹn khóa ngoại trong SQLite, transaction đa bảng, replay Event Journal từ đĩa và quyền hạn thực tế của Permission Manager vẫn cần được kiểm thử ở `P04.02` đến `P04.07`.

---

## 15. Unit tests

Tệp kiểm thử [test_p04_01_core_contracts_unit.py](file:///C:/DoCho/AI-Village/tests/core/test_p04_01_core_contracts_unit.py) gồm `17` ca kiểm thử ngoại tuyến bao phủ đủ 24 hạng mục yêu cầu:

- Bộ test mới của P04.01 đạt `17 passed` với fixture Python thuần, không gọi mạng và không tiêu tốn token `agy.exe`.
- Bộ kiểm thử tổng hợp sau khi cập nhật tài liệu được ghi nhận ở lần chạy cuối cùng trong mục `Verification` trước bước commit.
- Không có bài test nào đánh dấu hay giả lập trạng thái `LIVE_VERIFIED` cho P04.01.

---

## 16. Những phần cố ý chưa triển khai

Tuân thủ kỷ luật phạm vi của `P04.01`, các thành phần sau được giữ nguyên ở trạng thái kế hoạch:

1. Máy trạng thái và quy tắc chuyển tiếp `QUEUED -> RUNNING -> COMPLETED/FAILED/CANCELLED/BLOCKED` thuộc `P04.02`.
2. Event Journal bền vững và cơ chế Event Replay thuộc `P04.03`.
3. SQLite migrations tại `storage/db/` và Repository layer tại `storage/repositories/` thuộc `P04.04` và `P04.05`.
4. Workflow cancellation, retry có kiểm soát và Permission Manager thuộc `P04.06` và `P04.07`.
5. Agent YAML manifests thuộc `PHASE 05`, tầng `api/`, Mission Control UI 2D và 3D.

---

## 17. Verdict

**PASS.** Giai đoạn `PHASE 04 / P04.01` hoàn thành đầy đủ bộ hợp đồng dữ liệu nền tảng tại `core/contracts/` với kiểm chứng offline đầy đủ, tương thích hoàn toàn với `AgentRuntime` của `PHASE 03` và giữ nguyên mọi ranh giới an ninh trước khi chuyển sang `P04.02`.
