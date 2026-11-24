# BÁO CÁO NGHIỆM THU PHASE 03 / P03.03: AGENT RUNTIME ABSTRACTION & CUSTOM ORCHESTRATOR INTEGRATION

- **Giai đoạn:** PHASE 03 / P03.03
- **Mục tiêu:** Khóa hợp đồng `AgentRuntime` độc lập framework và tích hợp Custom Orchestrator theo quyết định `ADR-0002`
- **Trạng thái cuối:** **PASS**
- **HEAD tham chiếu lúc triển khai:** `516d34cc0b9cbf4accaed37c3eb85f527f60dd53`
- **Engine chính:** Antigravity CLI thông qua `EngineRegistry`

---

## 1. Runtime Architecture

Kiến trúc thực thi của AI Village sau P03.03 tách biệt lớp giao tiếp ứng dụng khỏi cài đặt điều phối cụ thể và giữ nguyên lớp adapter phía dưới:

`Application / Core Caller -> AgentRuntime Protocol -> CustomAgentRuntime -> P03.01 Custom Orchestrator -> EngineRegistry -> Antigravity CLI`

Gói mới tại `core/runtime/` gồm 4 tệp tin:

- [__init__.py](file:///C:/DoCho/AI-Village/core/runtime/__init__.py): xuất công khai 14 thành phần của hợp đồng và runtime mặc định.
- [contract.py](file:///C:/DoCho/AI-Village/core/runtime/contract.py): khai báo `AgentRuntime` Protocol và toàn bộ Pydantic v2 models chuẩn hóa đầu vào, sự kiện, lỗi, telemetry và kết quả.
- [application.py](file:///C:/DoCho/AI-Village/core/runtime/application.py): lớp gọi ở tầng ứng dụng gồm `execute_agent_workflow` và `AgentWorkflowRunner`, chỉ import `core.runtime.contract`.
- [custom_runtime.py](file:///C:/DoCho/AI-Village/core/runtime/custom_runtime.py): cài đặt `CustomAgentRuntime` kết hợp `EngineRegistry` với hàm `run_master_worker_spike` của P03.01 mà không sửa mã nguồn P03.01.

Ranh giới phụ thuộc được khóa bằng kiểm tra AST tự động: không có bất kỳ lệnh import `crewai` hay tham chiếu tới `CustomAgentRuntime`, `core.orchestration` hoặc `engines.registry` bên trong `contract.py` và `application.py`.

---

## 2. AgentRuntime Interface

Hợp đồng tại `core/runtime/contract.py` định nghĩa `@runtime_checkable class AgentRuntime(Protocol)` với đúng hai phương thức công khai:

```python
@runtime_checkable
class AgentRuntime(Protocol):
    def get_runtime_info(self) -> AgentRuntimeInfo: ...
    def execute(
        self,
        request: AgentRuntimeRequest,
        *,
        on_event: Callable[[AgentRuntimeEvent], None] | None = None,
    ) -> AgentRuntimeResult: ...
```

Thiết kế giữ bề mặt giao tiếp tối giản, không bổ sung hàm `cancel()` rỗng khi chưa có cơ chế hủy quy trình đa bước ở tầng điều phối. Bất kỳ cài đặt nào thỏa chữ ký trên đều vượt qua kiểm tra `isinstance(runtime, AgentRuntime)` tại runtime.

---

## 3. Request/Result Contract

Toàn bộ schema trong [contract.py](file:///C:/DoCho/AI-Village/core/runtime/contract.py) sử dụng Pydantic v2 với `ConfigDict(extra="forbid")`:

| Nhóm dữ liệu | Model | Vai trò chính |
| :--- | :--- | :--- |
| Thông tin năng lực | `AgentRuntimeInfo` | Khai báo `runtime_id`, `runtime_kind`, `version`, cờ streaming, QA và giới hạn hủy tiến trình |
| Phạm vi thư mục | `WorkspaceScope` | Chỉ định `allowed_root_path`, workspace điều phối/worker, `permitted_read_paths`, `permitted_write_paths` |
| Định tuyến AI | `ExecutionRouting` | Chỉ định rõ `provider_id`, `connection_id`, `model_id` mà không tự fallback |
| Tiêu chí QA | `ArtifactAcceptanceSpec` | Khai báo `artifact_relpath`, `expected_content`, `required_substrings` mang tính khái quát |
| Yêu cầu thực thi | `AgentRuntimeRequest` | Chứa `invocation_id`, `user_objective`, `timeout_seconds` và `metadata` đã lọc secret |
| Sự kiện chuẩn | `AgentRuntimeEvent` | Gắn nhãn `sequence`, `stage`, `actor_id`, `actor_role`, `logical_session_id`, `provider_id`, `provider_conversation_id` |
| Tham chiếu artifact | `AgentRuntimeArtifactRef` | Ghi `artifact_relpath`, `artifact_abs_path`, `exists_on_disk`, `sha256`, `size_bytes`, `qa_status` |
| Telemetry | `AgentRuntimeTelemetry` | Ghi `total_duration_ms`, `provider_duration_ms`, `engine_call_count`, token counts và `telemetry_status` |
| Lỗi chuẩn hóa | `AgentRuntimeError` | Chạy `redact_secrets` lên `message` trước khi trả về cho tầng gọi |
| Kết quả thực thi | `AgentRuntimeResult` | Tách biệt `technical_execution_succeeded` khỏi `business_verified` và đính kèm `qa_verification` |

Không có trường giả lập `task_id` hay `run_id` của Phase 04 trong hợp đồng; thay vào đó hợp đồng sử dụng `invocation_id` cho từng lượt gọi runtime và `correlation_id` từ bộ điều phối.

---

## 4. CustomAgentRuntime Implementation

`CustomAgentRuntime` trong [custom_runtime.py](file:///C:/DoCho/AI-Village/core/runtime/custom_runtime.py) nhận `registry: EngineRegistry`, `roster: dict[str, SpikeAgentSpec] | None`, `allow_temp_workspace: bool = False` và `runtime_id: str = "custom.master_worker_v1"`.

Phương thức `get_runtime_info()` trả về `runtime_kind="custom_orchestrator"`, `version="0.3.3"`, `supports_streaming_events=True`, `supports_independent_qa=True` và `supports_workflow_cancellation=False`.

Khi chạy `execute(request, on_event=...)`, adapter chuyển đổi tham số từ `WorkspaceScope`, `ExecutionRouting` và `ArtifactAcceptanceSpec` thành lời gọi tới `core.orchestration.run_master_worker_spike`. Mỗi `AttributedSpikeEvent` được chuyển sang `AgentRuntimeEvent` qua `_map_spike_event`, kết quả kiểm tra đĩa từ `spike.qa_verification` được đóng gói thành `AgentRuntimeArtifactRef`, và chỉ số tiêu thụ từ `engine_results` được tổng hợp bằng `_aggregate_runtime_telemetry`. Nếu bất kỳ `EngineResult` nào thiếu token thật từ provider, các trường token được giữ nguyên giá trị `None` với `telemetry_status="UNAVAILABLE_FROM_PROVIDER"`.

Mã nguồn của P03.01 tại `core/orchestration/spike_models.py` và `core/orchestration/spike_coordinator.py` giữ nguyên trạng thái gốc, không bị chỉnh sửa.

---

## 5. Dependency Injection Evidence

Tầng gọi ứng dụng trong [application.py](file:///C:/DoCho/AI-Village/core/runtime/application.py) cung cấp `execute_agent_workflow` và lớp `AgentWorkflowRunner`. Cả hai nhận bất kỳ đối tượng nào thỏa `AgentRuntime` Protocol, kiểm tra bằng `_require_runtime_protocol` và chỉ gọi `get_runtime_info()` với `execute(...)`.

Hai lớp bảo vệ được thi hành trực tiếp tại ranh giới ứng dụng:

1. Nếu `runtime.execute(...)` ném ngoại lệ chưa bắt, `execute_agent_workflow` chặn lại, đưa chuỗi lỗi qua `AgentRuntimeError` đã lọc secret và trả về `status="FAILED"` với `technical_execution_succeeded=False`, `business_verified=False`.
2. Hàm `_enforce_qa_invariant` từ chối mọi kết quả mang `status="DELIVERED"` nếu thiếu `qa_verification` đạt `VERIFIED_PASS` hoặc thiếu `business_verified=True`, ép trạng thái về `FAILED` với `error_code="QA_INVARIANT_VIOLATION"`.

Nhờ cơ chế injection này, cùng một `AgentWorkflowRunner` có thể chạy `_FakeAgentRuntime` trong unit test offline hoặc chạy `CustomAgentRuntime` nối Antigravity CLI thật mà không sửa một dòng nào trong caller.

---

## 6. Fake Runtime Unit Tests

Tệp kiểm thử [test_agent_runtime_contract_unit.py](file:///C:/DoCho/AI-Village/tests/core/test_agent_runtime_contract_unit.py) gồm 5 ca kiểm thử độc lập, xác minh cả fake adapter lẫn đường đi nối `CustomAgentRuntime` dùng fake engine:

1. `test_boundary_architecture_no_crewai_or_concrete_coupling_in_contract_and_caller`: phân tích AST xác nhận `contract.py` và `application.py` không import `crewai`, `CustomAgentRuntime`, `core.orchestration` hay `engines.registry`; đồng thời xác nhận chữ ký `run_master_worker_spike` nhận được danh sách `denied_paths` và `deny_shell_commands=True`.
2. `test_fake_runtime_swappable_in_application_caller`: thay thế hai đối tượng `_FakeAgentRuntime` và `CustomAgentRuntime` vào cùng caller `AgentWorkflowRunner`, xác nhận kết quả chuẩn hóa, danh sách `observed_tool_names` và không rò dữ liệu giữa các lần gọi.
3. `test_custom_runtime_through_caller_mapping_events_sessions_and_sha256`: chạy đủ luồng `master_plan -> worker_execute -> master_synthesize` qua `AgentWorkflowRunner` và `CustomAgentRuntime` với fake engine, kiểm tra ánh xạ `orchestrator_session_id`, `worker_session_id`, danh sách `AgentRuntimeEvent` và `sha256` khớp trên đĩa.
4. `test_qa_verified_fail_and_unhandled_exception_boundaries`: kiểm tra cả trường hợp nội dung sai dẫn đến `REJECTED_BY_QA` với `technical_execution_succeeded=True` nhưng `business_verified=False`, trường hợp giả mạo `DELIVERED` bị `_enforce_qa_invariant` chặn, và ngoại lệ chứa secret bị lọc thành `[REDACTED_SECRET]`.
5. `test_request_validation_workspace_isolation_no_fallback_and_none_telemetry`: kiểm tra từ chối secret trong `metadata`, từ chối thư mục ngoài vùng cho phép hoặc đường dẫn temp khi `allow_temp_workspace=False`, không fallback khi thiếu provider `unknown_provider`, và bảo toàn token `None` khi thiếu usage thật.

Toàn bộ 5 unit tests đạt trạng thái PASS.

---

## 7. Real Antigravity Live Smoke

Bài kiểm thử live tại [test_agent_runtime_live.py](file:///C:/DoCho/AI-Village/tests/integration/test_agent_runtime_live.py) chạy hàm `test_live_agent_runtime_custom_abstraction_smoke` với biến môi trường `AI_VILLAGE_RUN_LIVE=1` thông qua `AgentWorkflowRunner(CustomAgentRuntime(registry=build_default_engine_registry()))`.

Các chỉ số đo thực tế từ nhật ký chạy thật:

- **Lệnh thực thi:** `$env:AI_VILLAGE_RUN_LIVE="1"; python -m pytest tests/integration/test_agent_runtime_live.py -v -rs --log-cli-level=INFO`
- **Kết quả Pytest:** `1 passed in 127.53s`
- **Runtime ID & Invocation ID:** `custom.master_worker_v1` và `inv-03e9d5039fc8`
- **Correlation ID:** `corr-006ceff3bee0`
- **Trạng thái cuối:** `DELIVERED`
- **Lệnh CLI thực tế qua Antigravity Adapter:**
  - `master_plan` mang `req-d4ea9d9e8f6d`: `C:\Users\taine\.gemini\bin\agy.exe --sandbox --mode plan --output-format stream-json --print-timeout=120s --settings <redacted_rules> --print=<1690_chars>` tại `workspace\p03_03_runtime_6wpqa9pz\master_ws`
  - `worker_execute` mang `req-c43b43218120`: `C:\Users\taine\.gemini\bin\agy.exe --sandbox --mode accept-edits --output-format stream-json --print-timeout=120s --settings <redacted_rules> --print=<1596_chars>` tại `workspace\p03_03_runtime_6wpqa9pz\worker_ws`
  - `master_synthesize` mang `req-c5b4d1ceea56`: `C:\Users\taine\.gemini\bin\agy.exe --sandbox --mode plan --output-format stream-json --print-timeout=120s --settings <redacted_rules> --print=<2621_chars>` tại `workspace\p03_03_runtime_6wpqa9pz\master_ws`
- **Telemetry đo từ `provider_cli`:**
  - `telemetry_status`: `VERIFIED_MEASURED`, `telemetry_source`: `provider_cli`
  - `engine_call_count`: `3`
  - `provider_duration_ms`: `122216`, `total_duration_ms`: `126958`
  - `input_tokens`: `56490`, `output_tokens`: `6136`, `thinking_tokens`: `4670`, `cached_tokens`: `87379`, `total_tokens`: `62626`
  - `estimated_cost_usd`: `null`, `actual_cost_usd`: `null`

---

## 8. Master/Worker Handoff Evidence

Luồng cộng tác `Master -> Worker -> QA -> Master` hoạt động đầy đủ qua giao diện `AgentRuntime`:

- **Danh sách actor thực thi:** `["core.master", "it.artifact_worker"]`
- **Phiên logic tách biệt:** `orchestrator_session_id = "sess-master-d35ea89b58"`, `worker_session_id = "sess-worker-c779bb03f3"`
- **Ba `provider_conversation_id` phân biệt theo lượt gọi CLI:**
  1. `15d1cad1-93dd-41d3-839e-b8bf4daa81cc` cho bước `master_plan`
  2. `c436da79-8b8a-4867-95db-507e048f5730` cho bước `worker_execute`
  3. `d3a24712-7d2e-4e90-9806-4b4dfa8f4dd0` cho bước `master_synthesize`

Dữ liệu nhận diện cho thấy Master giữ chế độ `--mode plan` và không tự tạo artifact thay Worker, trong khi Worker nhận chỉ dẫn handoff tại thư mục làm việc riêng với chế độ `--mode accept-edits`.

---

## 9. Tool Execution Evidence

Trong lượt `worker_execute`, Worker thực thi công cụ thật trên hệ thống thông qua Antigravity CLI. Giá trị `observed_tool_names` thu được từ stream JSON trong bài chạy live là `["read_file", "write_file"]`.

Không có giả lập kết quả ghi tệp bằng Python từ bên ngoài Engine trong bước Worker; mọi tệp đầu ra đều được sinh bởi quá trình gọi công cụ thực tế của Worker CLI.

---

## 10. Artifact SHA-256 Verification

Bước kiểm chứng độc lập trên đĩa kiểm tra sự tồn tại, kích thước, nội dung và mã băm SHA-256 của artifact do Worker tạo trong `worker_ws`:

- **Trạng thái QA:** `VERIFIED_PASS`
- **Toàn vẹn nguồn:** `qa_source_integrity_verified = true`
- **SHA-256 kỳ vọng:** `26d6cbc3dcb476f5e68a435edc2406ea841b05c378d4cc67a490fc8593f48149`
- **SHA-256 đọc thực tế từ đĩa:** `26d6cbc3dcb476f5e68a435edc2406ea841b05c378d4cc67a490fc8593f48149`

Hai giá trị SHA-256 trùng khớp từng ký tự, đồng thời `application.py` xác nhận thỏa điều kiện `_enforce_qa_invariant` trước khi cho phép `status = "DELIVERED"`.

---

## 11. Streaming/Event Attribution

Trong bài chạy live qua `CustomAgentRuntime.execute`, tổng cộng `233` đối tượng `AgentRuntimeEvent` được chuẩn hóa và chuyển tiếp qua callback `on_event` lẫn trường `result.events`.

Mỗi sự kiện giữ đầy đủ 4 chiều định danh theo yêu cầu:

- `actor_id` và `actor_role` phân biệt `"orchestrator"`, `"worker"`, `"qa_verifier"` hoặc `"runtime"`.
- `logical_session_id` tách bạch phiên Master khỏi phiên Worker.
- `provider_id` và `provider_conversation_id` gắn từng sự kiện với đúng tiến trình Antigravity CLI tương ứng.
- `sequence` đánh số tăng dần từ `1` trên toàn vòng đời một `invocation_id`.

---

## 12. Security and Permission Boundaries

Các ranh giới bảo mật được giữ nguyên vẹn xuyên suốt tầng `AgentRuntime`:

- Worker chỉ có quyền truy cập vùng được cấp trong `WorkspaceScope`; mọi thư mục nằm ngoài `allowed_root_path` hoặc nằm trong thư mục temp hệ thống đều bị từ chối khi `allow_temp_workspace=False`.
- Cờ bảo vệ `deny_shell_commands=True` và danh sách `denied_paths` tiếp tục được truyền từ `CustomAgentRuntime` xuống `run_master_worker_spike` rồi tới `EngineRequest`.
- Sau khi bài test live kết thúc, thư mục `workspace/` được dọn dẹp sạch chỉ còn `.gitkeep`, thư mục `agents/` vẫn chỉ gồm `.gitkeep` và `README.md`, không xuất hiện database dư tại thư mục gốc, và không phát hiện secret lộ trong diff.

---

## 13. Runtime/Engine Responsibility Separation

Ranh giới trách nhiệm giữa các tầng được duy trì rõ ràng:

- **Application Caller tại `core/runtime/application.py`:** kiểm tra tuân thủ Protocol, kích hoạt thực thi, xử lý ngoại lệ chưa bắt và cưỡng chế QA invariant.
- **AgentRuntime Contract & Adapter tại `core/runtime/`:** định nghĩa dữ liệu trao đổi chung, chuyển đổi sự kiện đa nhân vật và tổng hợp telemetry cấp workflow. Không giữ logic phân tích stream-json hay quản lý process con.
- **Engine Layer tại `engines/`:** giữ nguyên trách nhiệm dựng cờ CLI `--sandbox`, `--mode`, parse `stream-json`, `EngineRegistry`, `ConnectionResolver` và `AntigravityProcessManager`. Không bị chỉnh sửa trong P03.03.

---

## 14. Cancellation Boundary

Khả năng hủy tiến trình hiện có ở mức `Engine` đơn lẻ thông qua `AntigravityProcessManager` và `Win32JobGuard` đã kiểm chứng tại PHASE 02.

Tuy nhiên, hủy quy trình điều phối nhiều bước mức Task hoặc Workflow chưa có trong phạm vi P03.03. Hợp đồng chọn minh bạch bằng cách khai báo `supports_workflow_cancellation = False` kèm `cancellation_boundary_note` trong `AgentRuntimeInfo`, không thêm phương thức `cancel()` rỗng vào `AgentRuntime` Protocol để tránh tạo cảm giác sai lệch rằng việc hủy đa tác vụ đã hoạt động. Năng lực này thuộc phạm vi `PHASE 04`.

---

## 15. Regression Tests

Kiểm tra hồi quy toàn bộ bộ kiểm thử offline cho PHASE 00, 01, 01B, 02 và 03 đều đạt yêu cầu mà không phá vỡ bài kiểm thử hiện hữu:

- **Suite offline mặc định sau chỉnh sửa:** `python -m pytest tests -v -rs` trả về `85 passed, 24 skipped` với 0 failure và 0 error. Danh sách `24 skipped` gồm `22` bài kiểm thử live yêu cầu `AI_VILLAGE_RUN_LIVE=1` và `2` bài đánh giá CrewAI bỏ qua do `crewai` chỉ cài đặt trong `.runtime/crewai-bench-venv`.
- **Suite đánh giá CrewAI cô lập tại `.runtime/crewai-bench-venv`:** giữ nguyên không thay đổi kể từ P03.02; Python toàn cục vẫn xác nhận `global_crewai: False`.
- **Chuẩn Stage A & Standards Consistency:** `tests/stage_a/test_stage_a_baseline.py` chỉ cho phép thêm `core/runtime/*.py` trong `core/`, còn `tests/standards/test_standards_consistency.py` giữ chốt chặn không đánh dấu DONE vượt quá `P03.03`.

---

## 16. Các giới hạn chuyển sang PHASE 04

Một số thành phần chưa thuộc phạm vi P03.03 và được chuyển tiếp nguyên trạng sang `PHASE 04` cùng `Gate C`:

1. **Task State Machine & Workflow Cancellation:** máy trạng thái tác vụ bền vững, cơ chế tạm dừng, tiếp tục và hủy toàn bộ workflow nhiều bước.
2. **SQLite Event Journal:** lưu trữ bền vững luồng `AgentRuntimeEvent` xuống cơ sở dữ liệu phục vụ truy vết và phát lại.
3. **Agent Registry & `agents/*.yaml`:** nạp hồ sơ nhân vật AI thực tế từ thư mục `agents/` thay cho cấu hình spike mặc định.
4. **Gate C & Mission Control UI:** kiểm chứng tổng hợp toàn bộ PHASE 03 tại Gate C trước khi xây dựng giao diện 2D hoặc 3D.

---

## 17. Verdict

**PASS.** Toàn bộ tiêu chí hoàn thành của `PHASE 03 / P03.03` đạt yêu cầu với bằng chứng thực thi thật:

- Hợp đồng `AgentRuntime` và caller `AgentWorkflowRunner` hoàn toàn độc lập với CrewAI và các cài đặt runtime cụ thể.
- `CustomAgentRuntime` tái sử dụng trực tiếp bộ điều phối Custom Orchestrator và `EngineRegistry` đã kiểm chứng.
- 5 unit tests mới cho fake runtime lẫn ranh giới bảo mật đạt PASS, toàn suite offline đạt `85 passed, 24 skipped`, và 1 bài live smoke qua Antigravity CLI hoàn thành `DELIVERED` với artifact khớp SHA-256 trên đĩa.
- Sẵn sàng bàn giao để nghiệm thu Gate C hoặc khởi động `PHASE 04` khi có lệnh triển khai.
