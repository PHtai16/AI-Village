# Báo cáo Kiểm toán PHASE 03 / P03.02: Đánh giá CrewAI Flow và Crew với Custom Orchestrator

- **Phạm vi kiểm toán:** Đối chứng ba phương án điều phối trên nền `Antigravity CLI` tại `P03.02`
- **Commit tham chiếu đầu vào:** `e5239ab7e36e11148ba725543e09e172a72540ba`
- **Ngày kiểm toán:** `2026-11-24`
- **Kết luận cuối cùng:** **`PASS`** và lựa chọn **`custom_orchestrator`** tại `docs/decisions/ADR-0002-orchestration-architecture.md`

## 1. Phiên bản CrewAI và môi trường thử nghiệm cô lập

Thư viện `crewai` không tồn tại trong Python toàn cục của máy chủ. Nhóm kiểm chứng tạo môi trường ảo riêng tại `.runtime/crewai-bench-venv` trên `Python 3.13.13` bằng `uv 0.13.7` sau khi được người vận hành phê duyệt và giữ nguyên `pyproject.toml` không thêm dependency production nào.

| Thông số môi trường | Python toàn cục | Môi trường benchmark `.runtime/crewai-bench-venv` |
| :--- | :--- | :--- |
| Phiên bản Python | `3.13.13` | `3.13.13` |
| Cài đặt `crewai` | `False` | `crewai==1.15.45`, `crewai-core==1.15.45`, `crewai-cli==1.15.45` |
| Tổng số gói cài đặt | Nền tối giản của repo | `124` gói gồm `pytest==9.1.1` và `lancedb`, `qdrant-client`, `pyarrow`, `openai`, `grpcio`, `tokenizers`, `opentelemetry-*` |
| Dung lượng đĩa venv | Không phát sinh thêm | `556.62 MB` |
| Thời gian import nguội | `core.orchestration`: `1018.5 ms` | `core.orchestration`: `1628.6 ms`; `crewai`: `12963.0 ms`; `crewai.flow.flow`: `15266.7 ms` |

Trong lần chạy đầu tiên trước khi có tệp `.pyc` cho `crewai.flow.flow`, thời gian import đo được lên tới `27410.0 ms`, còn trạng thái `.pyc` ấm vẫn mất `15266.7 ms`. Theo kiểm tra log thực thi thật của `crewai_core.settings`, quá trình import vẫn đọc cấu hình người dùng tại `C:\Users\taine\.config\crewai\settings.json`.

## 2. Mốc nền Phương án A: Custom Orchestrator

Bộ điều phối nội bộ ở `P03.01` nằm hoàn toàn trong bốn tệp Python gồm `core/__init__.py` với `3` dòng, `core/orchestration/__init__.py` với `37` dòng, `core/orchestration/spike_models.py` với `347` dòng và `core/orchestration/spike_coordinator.py` với `1091` dòng, tổng cộng `1478` dòng mã nguồn. Giao diện gọi `run_master_worker_spike()` dùng thuần `pydantic` và thư viện chuẩn Python, nối trực tiếp vào `EngineRegistry.execute()`.

Lượt benchmark gốc `corr-59b284583d7f` ở `P03.01` đạt `DELIVERED` trong `105618 ms` với `3` lượt gọi `agy.exe` tốn `101980 ms` thời gian provider, `5989` ký tự prompt, `82211` tổng tokens thực đo chưa tính `64089` cached tokens, `211` sự kiện gán nhãn, và `verify_engine_artifact()` xác nhận SHA-256 `c50b77fd0ab5f284d208122b3c98620d85382870405238f92e8e6efb1d871d0f` với `VERIFIED_PASS`. Bọc ngoài không sửa đổi tại `tests/benchmarks/p03_02/custom_harness.py` đo tách bạch thêm trên fake adapter rằng khi `Worker` trả `telemetry_status == "UNAVAILABLE_FROM_PROVIDER"`, bộ tổng hợp giữ nguyên `input_tokens_total = None` và `telemetry_complete = False`.

## 3. Kết quả khảo sát và thực nghiệm Phương án B: CrewAI Flow

Nguyên mẫu `P0302CrewAIFlow` trong `tests/benchmarks/p03_02/crewai_flow_prototype.py` triển khai đúng sáu bước từ `master_planning`, `validate_master_plan`, `worker_execution`, `qa_verification`, `master_synthesis` đến `final_handoff` bằng `@start`, `@router`, `@listen` và `SQLiteFlowPersistence` trỏ vào tệp SQLite cô lập theo phiên chạy. Mọi lượt suy luận đều đi thẳng qua `_execute_stage()` vào `EngineRegistry.execute()` mà không dùng agent loop của CrewAI.

Những phát hiện thực tế rút ra khi chạy `crewai==1.15.45` trên `Flow`:

- `Flow.__init__(**kwargs)` bỏ qua tham số từ điển trạng thái truyền qua keyword của lớp con nếu không gán trực tiếp lên `self.state` sau `super().__init__(persistence=...)`, gây lỗi rỗng `correlation_id` ở lượt chạy unit đầu tiên cho tới khi sửa lại trình tự khởi tạo.
- `SQLiteFlowPersistence()` mặc định lưu tệp vào `C:\Users\taine\AppData\Local\CrewAI\AI-Village\flow_states.db` theo `db_storage_path()` nếu không truyền tường minh `db_path`.
- Trên phiên bản `1.15.45`, lớp `Flow` không cung cấp phương thức `.cancel()`, `.abort()` hay `.stop()`; luồng chạy bọc mỗi bước đồng bộ vào `asyncio.to_thread()`, phù hợp với các báo cáo thực tế trên GitHub về khó khăn khi ngắt giữa chừng tiến trình con.
- Lượt benchmark live `corr-flow-a75da5b33a` đạt `DELIVERED` và `VERIFIED_PASS` trong `112066 ms` gồm `108302 ms` thời gian `agy.exe`, `7 ms` xác minh QA và `3757 ms` overhead của Flow, phát sinh `213` sự kiện Engine cùng `24` sự kiện trên `crewai_event_bus`.

## 4. Kết quả khảo sát và thực nghiệm Phương án C: CrewAI Crew

Nguyên mẫu trong `tests/benchmarks/p03_02/crewai_crew_prototype.py` nối `Agent` và `Task` của `CrewAI` với `AntigravityEngineBaseLLM` kế thừa `crewai.BaseLLM`. Khảo sát mã nguồn cài đặt thực tế cho thấy nhiều điểm khác biệt lớn so với tài liệu trực tuyến:

- Trong `crewai==1.15.45`, `BaseLLM` kế thừa từ `pydantic.BaseModel` và `abc.ABC`, đòi hỏi mọi thuộc tính trạng thái nội bộ của bridge phải dùng `PrivateAttr` tên bắt đầu bằng dấu gạch dưới.
- `BaseLLM` tự động bọc phương thức `call()` bằng `run_with_rate_limit_retry` để tự thử lại tối đa `3` lần khi chuỗi lỗi khớp từ khóa `429` hoặc `rate limit`, nên bridge phải chủ động gói lỗi thành `EngineExecutionHaltedError` không chứa từ khóa đó để tránh CrewAI tự động thử lại ngầm.
- `BaseLLM` không định nghĩa sẵn `supports_function_calling()`, nên `Agent._supports_native_tool_calling()` kiểm tra động bằng `hasattr`. Nguyên mẫu phải cài đặt tường minh trả về `False` nhằm ngăn CrewAI dựng vòng lặp ReAct hoặc tool calling chồng lên vòng lặp công cụ nội sinh của `agy.exe`.
- Một `Crew` tuần tự gồm nhiều `Task` đẩy trực tiếp văn bản thô từ `Task 1` sang `Task 2`, không có chốt chặn parse schema hay kiểm soát `WorkerHandoff` trước khi `Worker` chạy trừ khi phá vỡ `Crew` thành các `Crew` đơn nhiệm vụ có mã Python chặn giữa các chặng.
- Bật `Task.guardrail` kèm `guardrail_max_retries=1` làm tăng số lượt gọi `EngineRegistry` lên `2` cho cùng một tác vụ như chứng minh ở unit test, đồng thời `crewai/task.py` từ chối hàm guardrail viết trong tệp có `from __future__ import annotations` vì so khớp `sig.return_annotation` theo kiểu runtime chưa giải chuỗi PEP 563.
- Trong suốt lượt chạy live và unit của Crew, `crewai==1.15.45` phát ra lần lượt `39` và `91` `DeprecationWarning` ngay từ mã nội bộ của thư viện như `function_calling_llm`, `allow_code_execution` và `multimodal`.

## 5. Khả năng tương thích tích hợp với Antigravity

Phương án A tích hợp tự nhiên qua `EngineRegistry` với phân tách sạch `agent_execution_platform` và `model_api`. Phương án B tích hợp được vì thực chất gạt bỏ hoàn toàn tầng LLM của CrewAI để gọi thẳng `EngineRegistry`. Phương án C buộc phải ép một nền tảng thực thi Agent hoàn chỉnh vào chiếc áo `BaseLLM` vốn chỉ thiết kế cho API sinh văn bản, dẫn tới phình prompt `+23.0%`, mất khả năng quan sát công cụ trên event bus của CrewAI và buộc vô hiệu hóa công cụ CrewAI để tránh double tool execution.

## 6. Phương pháp benchmark công bằng

Cả ba phương án sử dụng cùng tác vụ đọc `worker_ws\package.json` chứa `verification_marker` ngẫu nhiên bí mật không xuất hiện trong `user_request` hay `master_ws`, tạo `worker_ws\proof.txt`, giữ nguyên `package.json`, chạy cùng `agy.exe` `1.5.15`, cùng cờ `--sandbox`, cùng danh sách chặn `denied_paths`, cùng cờ `deny_shell_commands=True`, cùng `timeout_seconds=120` và cùng hàm QA `verify_engine_artifact()`. Thời lượng được tách rõ thành bốn phần: `provider_duration_ms` của `agy.exe`, `verification_duration_ms` của QA trên đĩa, `framework_overhead_ms` phần điều phối Python cộng thư viện, và `extra_reasoning_calls` vượt quá mốc `3` lượt chuẩn. Các lượt đo live chạy một lần có giới hạn trên mỗi phương án mới nên không ngoại suy làm trung bình thống kê dài hạn.

## 7. Bằng chứng thực thi live chi tiết

| Chỉ số thực nghiệm | Phương án A `P03.01` | Phương án B `CrewAI Flow` | Phương án C `CrewAI Crew` |
| :--- | :--- | :--- | :--- |
| Định danh lượt chạy | `corr-59b284583d7f` | `corr-flow-a75da5b33a` | `corr-crew-74f3dd05f4` |
| Trạng thái cuối | `DELIVERED` | `DELIVERED` | `DELIVERED` |
| QA SHA-256 trên đĩa | `VERIFIED_PASS` | `VERIFIED_PASS` | `VERIFIED_PASS` |
| Hàm băm `proof.txt` khớp `100%` | `c50b77fd0ab5f284d208122b3c98620d85382870405238f92e8e6efb1d871d0f` | `cacf2fa609bea680bda1e91fbe55cf993d2eb7d2d14c5b6c9159ef3b1cf6073e` | `49ebf0b1a644b2a01a8004e46085d1790847fa39d076095aaccb015a42ff3060` |
| Công cụ thực tế quan sát được | `["read_file", "write_file"]` | `["read_file", "write_file"]` | `["read_file", "write_file"]` |
| Số lượt gọi `agy.exe` | `3` | `3` | `3` |
| Tổng thời gian chạy | `105618 ms` | `112066 ms` | `115466 ms` |
| Thời gian `agy.exe` | `101980 ms` | `108302 ms` | `111573 ms` |
| Overhead khung cộng QA | `3638 ms` | `3764 ms` gồm `7 ms` QA | `3893 ms` gồm `2 ms` QA |
| Tổng ký tự prompt gửi CLI | `5989` | `6032` | `7369` |
| Sự kiện AI Village `AttributedSpikeEvent` | `211` | `213` | `232` |
| Sự kiện Framework `crewai_event_bus` | `0` | `24` | `18` trong đó `0` sự kiện Tool |
| Input tokens thực đo | `77806` | `57439` | `37926` |
| Output tokens thực đo | `4405` | `4862` | `5606` |
| Cached tokens | `64089` | `84354` | `104705` |
| Tổng tokens báo cáo chính | `82211` | `62301` | `43532` |

## 8. Kiểm chứng cách ly phiên

Cả ba phương án đều tách biệt hoàn toàn cặp `master_session_id` và `worker_session_id` khỏi ba `provider_conversation_id` do `agy.exe` cấp phát và truyền `denied_paths` đối xứng giữa `master_ws` với `worker_ws`. Tuy nhiên cơ chế cách ly này hoàn toàn do mã của AI Village kiểm soát tại `_preflight_validate`, `_snapshot_workspace_tree` và `AntigravityEngineBaseLLM`; CrewAI không tự hiểu khái niệm `provider_conversation_id`, `workspace_path` hay `denied_paths` và có xu hướng đọc cấu hình người dùng toàn cục hoặc ghi DB vào `%LOCALAPPDATA%` nếu không bị chặn bằng biến môi trường cô lập.

## 9. Kiểm chứng hủy tiến trình và timeout

Kiểm thử `test_section_xi_05_worker_cancellation_via_process_manager_and_spike` chứng minh nhánh Custom nối sẵn `AntigravityProcessManager.request_cancel` cùng `Win32JobGuard` và chặn `VERIFIED_FAIL` khi `EngineResult.status == "CANCELLED"` hoặc `TIMED_OUT`. Trái lại, kiểm tra phản xạ trực tiếp trên `crewai==1.15.45` xác nhận cả `Flow` lẫn `Crew` không có phương thức `.cancel()`, `.abort()` hay `.stop()`, và tài liệu lỗi cộng đồng trên GitHub ghi nhận thêm lỗi `Ctrl+C` bỏ rơi luồng nền trên Windows khi chạy Flow bất đồng bộ.

## 10. Kiểm chứng bàn giao và kiểm soát phạm vi

Trong Phương án A và B, `WorkerHandoff` chỉ được tạo sau khi `MasterPlan` vượt qua `parse_strict_json_object` và `validate_master_plan_policy`. Ở Phương án C, kiến trúc gộp nhiều `Task` trong cùng một `Crew` không cung cấp sẵn chốt kiểm soát đường dẫn động giữa hai `Task` nối tiếp, buộc phải tách thành ba `Crew` một tác vụ riêng biệt qua mã Python bên ngoài mới giữ được ranh giới bàn giao an toàn như `P03.01`.

## 11. Khả năng quan sát sự kiện và streaming

Cả ba phương án thu được trên `210` `AttributedSpikeEvent` từ `AntigravityEngineAdapter` khi chạy live vì tất cả đều đi qua callback `on_event` của `EngineRegistry`. Điểm khác biệt rõ nằm ở `crewai_event_bus`: `Flow` phát `24` sự kiện vòng đời phương thức, còn `Crew` chỉ phát `18` sự kiện tổng quan và ghi nhận đúng `0` sự kiện `ToolUsage*` do mọi lần gọi `read_file` và `write_file` diễn ra bên trong `agy.exe` chứ không đi qua lớp tool của CrewAI. Dữ liệu gửi lên `Mission Control` trong mọi trường hợp vẫn phải dựa vào `EngineEvent` và Event Journal của AI Village chứ không thể dựa vào bus của CrewAI.

## 12. Phục hồi sau lỗi và lưu bền vững trạng thái

`Custom Orchestrator` hiện chỉ giữ bản ghi từng phần `MasterWorkerSpikeResult` trong bộ nhớ tiến trình và chưa có lưu bền vững qua SQLite của `PHASE 04`. `CrewAI Flow` có `SQLiteFlowPersistence` lưu lại snapshot từ điển trạng thái `P0302FlowState` qua mỗi bước như chứng minh tại `test_option_b_crewai_flow_failure_qa_and_persistence_reload`. Tuy vậy bảng SQLite này chỉ giữ cục JSON của state Flow, không lưu append-only Event Journal đã che bí mật, không khôi phục cây tiến trình hay đồng bộ trạng thái `provider_conversation_id` của CLI, nên hệ thống vẫn phải xây kho lưu trữ `PHASE 04` bất kể chọn phương án nào.

## 13. Phân tích Overhead, Token và độ phình Prompt

CrewAI `Agent` và `Task` ngay cả khi đặt `use_system_prompt=False` vẫn chèn mẫu chỉ dẫn `Task:` và `This is the expected criteria for your final answer:` quanh nội dung gốc, làm tổng số ký tự prompt gửi sang `agy.exe` ở Phương án C tăng từ `5989` lên `7369` ký tự, tức tăng `1380` ký tự hoặc `23.0%` trên một tác vụ nhỏ gồm ba lượt. Lượt chạy `BaseLLM` cũng sinh `3` cảnh báo suy đoán token `1 token = 4 ký tự` từ `crewai/llms/base_llm.py:468`, còn `CrewOutput.token_usage` mặc định trả toàn số `0` khi thiếu telemetry trừ phi bridge tự gọi phương thức nội bộ `_track_token_usage_internal`.

| Thành phần độ trễ | Phương án A Custom | Phương án B CrewAI Flow | Phương án C CrewAI Crew |
| :--- | :--- | :--- | :--- |
| Import nguội môi trường | `1018.5 ms` global | `15266.7 ms` trong venv | `12963.0 ms` trong venv |
| Thời gian gọi `agy.exe` | `101980 ms` | `108302 ms` | `111573 ms` |
| Overhead khung và QA | `3638 ms` | `3764 ms` | `3893 ms` |

## 14. Ranh giới bảo mật và phân quyền

Không phương án CrewAI nào có cơ chế Permission Engine bảy tầng, canonical workspace path check hay ngăn chặn `Master` ghi đè thư mục `Worker`. Toàn bộ tính năng bảo vệ trong bài thử nghiệm ở cả ba nhánh đều do mã Python xác định của AI Village thực thi gồm `validate_session_workspace`, `resolve_within_workspace`, `validate_master_plan_policy`, `--settings` deny rules và `snapshot_file_hashes`.

## 15. Kết quả kiểm thử Unit và Live

| Bộ kiểm thử | Môi trường Python | Kết quả | Thời gian | Ghi chú |
| :--- | :--- | :--- | :--- | :--- |
| `python -m pytest tests -v` | Toàn cục `3.13.13` | `80 passed, 23 skipped` | `5.24s` | Gồm `23` unit `P03.01`, `4` unit `P03.02` Custom, `53` bài nền; bỏ qua `CrewAI` và `live` |
| `.runtime/crewai-bench-venv/Scripts/python.exe -m pytest tests/benchmarks/p03_02/test_crewai_evaluation_unit.py -v` | Venv cô lập `crewai==1.15.45` | `4 passed, 91 warnings` | `12.15s` | Phủ happy path, persistence reload, prompt expansion, `UsageMetrics` `0` và guardrail retry |
| `$env:AI_VILLAGE_RUN_LIVE="1"; ... pytest tests/integration/test_p03_02_crewai_benchmark_live.py -v -rs` | Venv cô lập với `agy.exe` thật | `2 passed, 39 warnings` | `237.80s` | Chạy `1` lượt Flow thật và `1` lượt Crew thật qua `EngineRegistry` |

## 16. Ma trận quyết định bắt buộc

Bảng đối chiếu toàn bộ `19` tiêu chí đánh giá sử dụng chính xác sáu nhãn trạng thái chuẩn gồm `LIVE_VERIFIED`, `UNIT_VERIFIED`, `DOC_SUPPORTED`, `NOT_VERIFIED`, `UNSUPPORTED` và `BLOCKED`:

| Tiêu chí | Custom | CrewAI Flow | CrewAI Crew |
| :--- | :--- | :--- | :--- |
| Antigravity integration | `LIVE_VERIFIED`: Gọi trực tiếp qua `EngineRegistry` giữ nguyên mô hình agent platform | `LIVE_VERIFIED`: Chạy được khi gọi thẳng `EngineRegistry` bên ngoài LLM layer | `LIVE_VERIFIED`: Chạy được qua bridge `BaseLLM` nhưng phải tắt tool native của CrewAI và chia nhỏ từng Crew |
| Engine Registry compatibility | `LIVE_VERIFIED`: Khớp toàn bộ `EngineRequest` và `EngineResult` | `LIVE_VERIFIED`: Phải tự viết glue code gọi `EngineRegistry` trong bước Flow | `LIVE_VERIFIED`: Mất cấu trúc `EngineResult` trực tiếp ở kết quả `Crew`, buộc dùng kênh phụ |
| Master/Worker handoff | `LIVE_VERIFIED`: `WorkerHandoff` kiểm tra sau `MasterPlan` nghiêm ngặt | `LIVE_VERIFIED`: Giữ được khi tái sử dụng hàm validate của AI Village | `UNIT_VERIFIED`: Multi-task Crew truyền text thô thiếu cổng policy, chỉ an toàn khi tách Crew đơn lẻ |
| Session isolation | `LIVE_VERIFIED`: Đã chứng minh hai phiên và ba `conversation_id` tách biệt | `LIVE_VERIFIED`: Dựa hoàn toàn vào tham số truyền xuống từ AI Village, bản thân Flow không quản lý CLI session | `LIVE_VERIFIED`: Cần bọc `BaseLLM` riêng cho từng Agent mới tránh rò ngữ cảnh |
| Workspace permissions | `LIVE_VERIFIED`: Áp dụng `--sandbox`, `denied_paths`, snapshot trước và sau | `LIVE_VERIFIED`: Dựa hoàn toàn vào lớp wrapper của AI Village, Flow đọc ghi global config nếu không khóa env | `UNIT_VERIFIED`: CrewAI Agent không có kiểm tra đường dẫn thư mục trước gọi tool nếu mở công cụ riêng |
| Native tool events | `LIVE_VERIFIED`: Thu đủ `read_file` và `write_file` từ provider stream | `LIVE_VERIFIED`: Nhận nguyên vẹn qua callback `EngineRegistry` | `UNSUPPORTED`: `ToolUsage*` trong Crew ghi nhận `0` sự kiện khi Antigravity tự chạy công cụ |
| Cancellation | `UNIT_VERIFIED`: Kết hợp `AntigravityProcessManager.request_cancel` và `Win32JobGuard` | `UNSUPPORTED`: Không có API `.cancel()` hay `.abort()`, tài liệu lỗi ghi nhận kẹt luồng khi Ctrl+C | `UNSUPPORTED`: Không có API `.cancel()` hay `.abort()` trên lớp `Crew` |
| Timeout | `LIVE_VERIFIED`: `agy.exe` và `ProcessLifecycle` cưỡng chế đúng hạn mức | `UNIT_VERIFIED`: Không có timeout toàn Flow thống nhất ở lớp wrapper của benchmark, phụ thuộc `EngineRequest.timeout_seconds` từng lượt | `DOC_SUPPORTED`: Chỉ thấy `max_execution_time` ở schema `Agent`, không cưỡng chế cụm tiến trình con ngoài Engine |
| Recovery/resume | `UNIT_VERIFIED`: Giữ state lỗi trong RAM, bền vững qua restart chờ `PHASE 04` | `UNIT_VERIFIED`: `SQLiteFlowPersistence` lưu và đọc lại được state JSON nhưng không phục hồi Event Journal hay CLI process | `NOT_VERIFIED`: Tính năng checkpoint của Crew chưa đủ bảo chứng an toàn cho replay công cụ |
| Event streaming | `LIVE_VERIFIED`: `211` `AttributedSpikeEvent` từ `EngineEvent` | `LIVE_VERIFIED`: `213` sự kiện Engine cộng `24` sự kiện Flow | `LIVE_VERIFIED`: `232` sự kiện Engine cộng `18` sự kiện Crew thiếu chi tiết công cụ |
| Task/Agent/Run correlation | `LIVE_VERIFIED`: Gắn `correlation_id`, `agent_id`, `stage`, `logical_session_id` | `LIVE_VERIFIED`: Có `flow_id` nhưng vẫn phải mang vỏ `AttributedSpikeEvent` của AI Village | `UNIT_VERIFIED`: `Crew` tự sinh UUID nội bộ tách rời `WorkerHandoff` của AI Village |
| Provider-neutral routing | `LIVE_VERIFIED`: Toàn bộ định tuyến do `EngineRegistry` đảm nhiệm | `LIVE_VERIFIED`: Toàn bộ định tuyến vẫn do `EngineRegistry` đảm nhiệm | `UNIT_VERIFIED`: Bọc tên giả `antigravity/default` qua `BaseLLM` gây cảnh báo ước lượng token sai bản chất |
| Quota/cost observability | `LIVE_VERIFIED`: Giữ `VERIFIED_MEASURED` hoặc `None` trung thực | `LIVE_VERIFIED`: Bảo toàn qua danh sách `EngineResult` giữ bên ngoài Flow | `UNIT_VERIFIED`: `CrewOutput.token_usage` ép về `0` khi thiếu usage từ provider và tự cảnh báo `len/4` |
| Error propagation | `LIVE_VERIFIED`: Truyền `OrchestrationError` có cấu trúc, không retry ngầm | `UNIT_VERIFIED`: Phải dùng nhánh `@router` riêng thay vì exception nếu muốn giữ state sau bước lỗi | `UNIT_VERIFIED`: `BaseLLM` tự động cài `run_with_rate_limit_retry` buộc bridge né từ khóa lỗi |
| QA independence | `LIVE_VERIFIED`: `verify_engine_artifact()` chạy sau Worker và chặn `DELIVERED` | `LIVE_VERIFIED`: Giữ nguyên được do gọi `verify_engine_artifact()` trực tiếp trong bước `@listen` | `UNIT_VERIFIED`: Phải tự chặn sau khi Crew kết thúc vì `Task` thành công không biết SHA-256 trên đĩa |
| Dependency complexity | `LIVE_VERIFIED`: `0` gói framework ngoài `pydantic` và stdlib | `LIVE_VERIFIED`: `124` gói chiếm `556.62 MB`, import chậm `15.3s` | `LIVE_VERIFIED`: `124` gói chiếm `556.62 MB`, import chậm `13.0s` và `91` deprecation warnings |
| Maintainability | `LIVE_VERIFIED`: `4` tệp Python rõ ràng, không ràng buộc breaking changes thư viện ngoài | `UNIT_VERIFIED`: Phụ thuộc hành vi nội bộ Pydantic của `Flow.__init__` và API hay thay đổi | `UNIT_VERIFIED`: Ràng buộc PEP 563 ở `Task.guardrail` và hành vi ẩn của agent executor |
| Extension to many Workers | `UNIT_VERIFIED`: Sẵn sàng mở rộng sang danh sách `AgentSpec` và state machine `PHASE 04`–`06` | `DOC_SUPPORTED`: Tài liệu hỗ trợ nhánh song song `or_`/`and_` nhưng vẫn thiếu `PermissionManager` | `DOC_SUPPORTED`: `Process.hierarchical` đòi hỏi `manager_llm` tạo thêm vòng lặp LLM tốn độ trễ và token |
| Mission Control integration | `LIVE_VERIFIED`: `AttributedSpikeEvent` ánh xạ thẳng sang Event Journal và Timeline | `UNIT_VERIFIED`: Bus toàn cục `crewai_event_bus` không tách run sẵn theo hợp đồng AI Village | `UNIT_VERIFIED`: Thiếu sự kiện `ToolUsage*` cho công cụ Antigravity, làm Timeline bị mù thao tác tệp nếu dựa vào CrewAI |

Ghi chú phân biệt nguồn dữ liệu: các mục đánh dấu `DOC_SUPPORTED` như khả năng `or_`/`and_` song song của Flow, `max_execution_time` hoặc `Process.hierarchical` chỉ mới đọc trong tài liệu hoặc schema phản xạ của CrewAI chứ chưa chạy kiểm chứng trên `agy.exe`; mọi kết luận về chi phí, prompt bloat, thiếu API hủy, cảnh báo suy giảm, lỗi PEP 563 và thiếu `ToolUsage*` đều đo trực tiếp trên runtime thật.

## 17. Khuyến nghị quyết định kiến trúc ADR

Chốt **`custom_orchestrator`** trong `docs/decisions/ADR-0002-orchestration-architecture.md`. Lựa chọn này dựa trực tiếp trên ba sự kiện thực chứng:

1. `Antigravity CLI` vốn là nền tảng tác nhân có sẵn vòng lặp và công cụ nội sinh nên dùng `CrewAI Crew` tạo thêm lớp bọc gây phình prompt `+23.0%`, mù sự kiện `ToolUsage*` ở phía framework, xung đột retry ẩn trên `BaseLLM` và buộc xé nhỏ thành nhiều `Crew` đơn nhiệm vụ mới giữ được cổng fail-closed.
2. `CrewAI Flow` chỉ chạy đạt vì tự gạt bỏ lớp tác nhân CrewAI để gọi thẳng `EngineRegistry` của AI Village, trong khi lại áp đặt `124` dependency nặng `556.62 MB`, nạp nguội `15.3s`, thiếu API hủy tác vụ và không thay thế được Event Journal hay Permission Engine của `PHASE 04`.
3. `Custom Orchestrator` gọn `1478` dòng đạt đầy đủ mục tiêu với chi phí phụ thuộc bằng không và tương thích thẳng với `AgentRuntime` ở `P03.03`.

## 18. Những điểm chưa kiểm chứng và giới hạn

- Chưa chạy kiểm chứng live đa Worker song song hay `Process.hierarchical` trên `agy.exe`, do phạm vi `P03.02` chỉ đối chứng trên tác vụ chuẩn một Master và một Worker để tiết kiệm quota.
- Chưa cố ý ngắt tiến trình `agy.exe` đang chạy bên trong `Flow` live hoặc ép lỗi tài khoản `AUTH_ERROR`/`QUOTA_EXCEEDED` trên live; phần hủy tiến trình và phục hồi lỗi được chứng minh qua kiểm tra mã nguồn CrewAI `1.15.45` cộng unit test điều khiển.
- Chưa đo số lần lặp thống kê lớn trên cả ba phương án nên chênh lệch thời gian tổng `105.6s` so với `112.1s` và `115.5s` phản ánh đo đơn lẻ cộng overhead đo được chứ không thay thế benchmark thống kê diện rộng.

## 19. Kết luận nghiệm thu `P03.02`

**Verdict: `PASS`**. `P03.02` đáp ứng toàn bộ tiêu chí Definition of Done ở Mục XV, bảo toàn Antigravity CLI làm Engine chính, không để sót tệp trong `workspace/` hay DB rác ở thư mục gốc, giữ nguyên `agents/` và ban hành quyết định chính thức `custom_orchestrator` tại `docs/decisions/ADR-0002-orchestration-architecture.md` để mở đường cho `P03.03`.
