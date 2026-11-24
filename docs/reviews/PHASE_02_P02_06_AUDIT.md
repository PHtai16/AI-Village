# Báo cáo Kiểm chứng và Nghiệm thu: PHASE 02 / P02.06

- **Tiểu phase:** `P02.06: Provider contract`
- **Ngày kiểm chứng:** 2026-11-24
- **Kết luận:** **`BLOCKED`**

## 1. Engine Contract Architecture

Hợp đồng thực thi trung lập với nhà cung cấp được đặt hoàn toàn trong tầng `engines/` nhằm tách biệt mã gọi ở tầng trên khỏi giao thức riêng của Antigravity CLI hay từng AI Model API, không thay đổi bất kỳ tệp nào trong `core/`:

- Tầng hợp đồng trừu tượng nằm tại [engines/contract.py](../../engines/contract.py), tách biệt chuẩn dữ liệu vào/ra khỏi mã triển khai từng nhà cung cấp.
- Tầng định danh kết nối đa khóa nằm tại [engines/connections.py](../../engines/connections.py), tách biệt `connection_id`, `project_ref` và `credential_ref` khỏi tên mô hình lẫn ngữ cảnh Agent.
- Tầng đăng ký và phân giải hợp đồng nằm tại [engines/registry.py](../../engines/registry.py), ánh xạ `EngineRequest` sang đúng `EngineAdapter` và `EngineConnectionRef` theo nguyên tắc Fail-Closed mà không tự ý chuyển nhà cung cấp thay thế ngoài chính sách.
- Tầng triển khai nhà cung cấp nằm tại [engines/providers/](../../engines/providers/__init__.py), giữ nguyên toàn bộ runtime `P02.01` đến `P02.05` trong `engines/antigravity/` và cô lập bộ chuyển đổi Google Gemini REST API ở một module độc lập.

## 2. Các Interface và Models Đã Triển Khai

Trạng thái `IMPLEMENTED` và `UNIT_VERIFIED` cho toàn bộ các mô hình Pydantic v2 với `ConfigDict(extra="forbid")` cùng lớp cơ sở trừu tượng:

- Trong [engines/contract.py](../../engines/contract.py): `EngineAdapter`, `EngineRequest`, `EngineResult`, `EngineEvent`, `EngineTelemetry`, `EngineCapabilities`, `EngineSessionReference`, `EngineToolCallProposal` và `EngineError`, cùng các kiểu phân loại `CapabilityStatus`, `CapabilityName`, `EnginePlatformKind`, `EngineExecutionStatus`, `EngineEventSource`, `EngineEventType`, `EngineTelemetryStatus`, `EngineTelemetrySource`, `EngineErrorCode` và `EngineErrorSource`.
- Trong [engines/connections.py](../../engines/connections.py): `EngineConnectionRef`, `ProviderConnectionGroup`, `CredentialResolver`, `EnvCredentialResolver` và `ConnectionRegistry`.
- Trong [engines/registry.py](../../engines/registry.py): `EngineRegistry`.
- Cổng xuất công khai tại [engines/__init__.py](../../engines/__init__.py) và [engines/providers/__init__.py](../../engines/providers/__init__.py).

## 3. Antigravity Adapter Compatibility

- Lớp bọc [engines/providers/antigravity.py](../../engines/providers/antigravity.py) cung cấp `AntigravityEngineAdapter` tái sử dụng trực tiếp `AntigravityAdapter.discover()`, `probe()`, `handshake()` và `stream_task()` mà không viết lại cơ chế CLI, sandbox, session hay process lifecycle đã nghiệm thu ở `P02.01` đến `P02.05`.
- Phân loại `platform_kind = "agent_execution_platform"`, giữ nguyên ánh xạ `telemetry_source = "provider_cli"` và tách biệt sự kiện từ `provider` với `adapter`.
- Trạng thái xác minh: `IMPLEMENTED`, `UNIT_VERIFIED` và `LIVE_VERIFIED` cho luồng gọi qua `EngineRegistry` trên `agy.exe` `1.5.15`.

## 4. Alternative API Provider Thực Tế

- Đã triển khai bộ chuyển đổi REST thuần Python `GoogleGeminiAdapter` tại [engines/providers/gemini.py](../../engines/providers/gemini.py) sử dụng thư viện `requests` gọi `POST {base_url}/v1beta/models/{model_id}:generateContent`, truyền khóa xác thực qua header `x-goog-api-key` ở ranh giới backend thay vì tham số URL.
- Kiểm tra tính sẵn sàng của môi trường host bằng các lệnh đọc chỉ đọc an toàn: tệp `.env` không tồn tại, không có bất kỳ biến khóa API nào trong tiến trình hay Windows Registry cấp User và Machine cho Gemini, OpenAI, Anthropic, Groq, OpenRouter, DeepSeek hay Ollama, `ollama` không có trong PATH và các cổng cục bộ `11434`, `1234`, `8000` đều không hoạt động.
- Do thiếu khóa API hợp lệ được người vận hành cấp phép, không thực hiện đăng ký tài khoản, không trích xuất thông tin từ trình duyệt hay tệp ngoài quyền và không giả lập `agy.exe` thành Provider thứ hai.
- Trạng thái xác minh của `GoogleGeminiAdapter`: `IMPLEMENTED` và `UNIT_VERIFIED` qua mock HTTP transport; `LIVE_VERIFIED` cho kiểm tra chặn capability cục bộ; **`BLOCKED`** cho lời gọi mạng live thật tới máy chủ Gemini.

## 5. Live Provider Response Evidence

- **`LIVE_VERIFIED` từ Test A trên Antigravity qua hợp đồng chung:**
  - Bộ kiểm thử `test_live_antigravity_through_common_contract` trong `tests/integration/test_engine_provider_contract_live.py` đạt `PASSED`.
  - Lượt chạy thu thập dữ liệu thật `req-ef60c6fc90e0` ghi nhận `provider_id: "antigravity"`, `adapter_id: "antigravity_cli"`, `platform_kind: "agent_execution_platform"`, `model_id: null`, `status: "SUCCEEDED"`, `business_verification_status: "UNVERIFIED"`, `response_text: "AI_VILLAGE_P02_06_EV_OK\n"`, `duration_ms: 16036`, `conversation_id: "6b1d0603-406b-4264-bc1a-33eafe68fcc2"` và `events_count: 2`.
- **`BLOCKED` cho Test B và Test C trên Provider API thứ hai:**
  - Hai bài `test_live_alternative_api_provider_gemini` và `test_live_provider_switching_without_core_changes` trả về trạng thái `SKIPPED` với lý do `BLOCKED: No authorized Google Gemini credential (GEMINI_API_KEY / GOOGLE_API_KEY) is configured in environment`.
  - Chưa ghi nhận phản hồi HTTP 200 live từ máy chủ Model API độc lập.

## 6. Common Request và Result Contract

- `EngineRequest` hỗ trợ thống nhất `request_id`, `provider_id`, `adapter_id`, `connection_id`, `model_id`, `prompt`, `context`, `workspace_path` tùy chọn, `timeout_seconds`, `capabilities_required`, `execution_mode`, `data_locality`, `response_schema`, `tool_declarations`, `session_reference` và `metadata` đã kiểm tra sạch bí mật.
- `EngineResult` tách biệt rõ trạng thái thực thi kỹ thuật `status` gồm `SUCCEEDED`, `FAILED`, `TIMED_OUT`, `CANCELLED`, `BLOCKED` khỏi `business_verification_status = "UNVERIFIED"`, đồng thời phân tách `tool_call_proposals` với `executed: False` khỏi `observed_tool_names`.

## 7. Capability Awareness

- `EngineCapabilities` phân tách rõ hai loại nền tảng `agent_execution_platform` và `model_api`. Một bộ chuyển đổi khai báo `platform_kind == "model_api"` bị chặn ngay tại lớp model validator nếu đặt `native_tool_execution` hoặc `workspace_access` thành `SUPPORTED`.
- Mười năng lực chuẩn hóa gồm `text_generation`, `structured_output`, `streaming`, `tool_calling_proposal`, `native_tool_execution`, `workspace_access`, `session_continuation`, `cancellation`, `usage_telemetry` và `model_discovery` với bốn trạng thái `SUPPORTED`, `UNSUPPORTED`, `UNKNOWN` và `NOT_VERIFIED`.
- **`LIVE_VERIFIED` từ Test D:** khi gửi yêu cầu `native_tool_execution` và `workspace_access` ở chế độ `accept-edits` vào `gemini_primary`, `validate_request_capabilities()` chặn ngay trước khi đụng khóa API hay mạng, trả về `status: "BLOCKED"`, `error_code: "TOOL_UNSUPPORTED_BY_PROVIDER"`, `error_source: "capability"`, `observed_tool_names: []` và `tmp_files_created: []`. Yêu cầu có `data_locality: "local_only"` gửi sang dịch vụ cloud hay `cli_managed` bị chặn bằng `DATA_BOUNDARY_VIOLATION`.

## 8. Multi-Connection và BYOK Design

- `EngineConnectionRef` và `ProviderConnectionGroup` biểu diễn nhiều kết nối độc lập cho cùng một `provider_id`, phân biệt sáu khái niệm gồm Provider, `account_ref`, `project_ref`, `connection_id`, `credential_ref` và `default_model_id`.
- `ConnectionRegistry.has_shared_project_scope()` nhận diện hai kết nối dùng chung `project_ref` và `ConnectionRegistry.has_independent_quota_proof()` luôn trả `False` khi chưa có bằng chứng billing API, ngăn chặn giả định nhầm rằng hai API key có hai hạn mức riêng biệt.
- `EnvCredentialResolver` đóng vai trò cơ chế giải mã tạm thời ở ranh giới backend cho biến môi trường hợp lệ; Vault bền vững chưa được triển khai trong `P02.06`.
- **`LIVE_VERIFIED` từ Test E:** nhóm `gemini_primary` và `gemini_secondary` cùng trỏ `google_project_a` xác nhận `shared_project_scope: true`, `independent_quota_proof: false`, gọi vào kết nối tắt `gemini_secondary` bị chặn Fail-Closed bằng `CONNECTION_DISABLED`, và `to_safe_summary()` chỉ trả `credential_ref_summary: "ref:secr***"`.

## 9. Telemetry Normalization

- `EngineTelemetry` giữ `null` cho mọi chỉ số không được nhà cung cấp trả về và cấm biến `null` thành `0`, không suy đoán quota còn lại và đặt `estimated_cost_usd: null`, `actual_cost_usd: null` khi thiếu bảng giá xác minh.
- **`LIVE_VERIFIED` trên Antigravity trong lượt chạy `req-ef60c6fc90e0`:** `telemetry_status: "VERIFIED_MEASURED"`, `telemetry_source: "provider_cli"`, `duration_ms: 16036`, `input_tokens: 5977`, `output_tokens: 63`, `total_tokens: 6040`, `thinking_tokens: 47`, `cached_tokens: 16384`, `provider_duration_seconds: 8.9073027`.
- **`UNIT_VERIFIED` trên Google Gemini:** trích xuất đúng các trường số nguyên từ `usageMetadata` sang `provider_reported_usage` với `telemetry_source: "provider_api"`, hoặc đặt `telemetry_status: "UNAVAILABLE_FROM_PROVIDER"` và `telemetry_source: "unavailable"` khi thiếu `usageMetadata`.

## 10. Error Handling

- `EngineError` chuẩn hóa 18 mã lỗi gồm `AUTH_ERROR`, `CREDENTIAL_UNAVAILABLE`, `RATE_LIMITED`, `QUOTA_EXCEEDED`, `INVALID_MODEL`, `UNSUPPORTED_CAPABILITY`, `TOOL_UNSUPPORTED_BY_PROVIDER`, `CONNECTION_NOT_FOUND`, `CONNECTION_DISABLED`, `DATA_BOUNDARY_VIOLATION`, `NETWORK_ERROR`, `TIMED_OUT`, `INVALID_RESPONSE`, `PROVIDER_ERROR`, `CANCELLED`, `EXECUTION_BLOCKED`, `UNSUPPORTED_ARGUMENT` và `PROCESS_LAUNCH_ERROR`, kèm `error_source`, `retryable`, `retry_after_seconds`, `provider_id` và `connection_id`.
- Trạng thái xác minh: `IMPLEMENTED` và `UNIT_VERIFIED` cho HTTP `401`/`403` -> `AUTH_ERROR`, `429` rate limit kèm `Retry-After` -> `RATE_LIMITED`, `429` quota/billing -> `QUOTA_EXCEEDED`, `404`/model sai -> `INVALID_MODEL`, timeout -> `TIMED_OUT`, lỗi mạng -> `NETWORK_ERROR` và lỗi thiếu kết nối hoặc sai cấu hình; không có vòng lặp tự retry hay tự chuyển key né hạn mức.

## 11. Security Validation

- Khóa API thô không xuất hiện trong `engines/`, `tests/` hoặc `docs/`; trình kiểm tra `EngineRequest` từ chối mọi khóa nhạy cảm trong `metadata` và `EngineConnectionRef` chặn cả khóa thô lẫn tham số truy vấn `?key=`/`token=`/`secret=` trong `endpoint_url`.
- `GoogleGeminiAdapter` truyền bí mật qua HTTP header `x-goog-api-key`, áp dụng `_scrub_secret()` và `redact_secrets()` cho mọi thông điệp lỗi, nhật ký `logging` và sự kiện. `EnvCredentialResolver.__repr__()` chỉ in danh sách tên tham chiếu.
- Kết thúc mọi lượt kiểm thử live, thư mục `workspace/` được thu hồi sạch chỉ giữ `.gitkeep`.

## 12. Unit Tests

- Bộ unit test mới [test_engine_provider_contract_unit.py](../../tests/engines/test_engine_provider_contract_unit.py) gồm 5 hàm kiểm thử bao phủ đầy đủ 20 kịch bản từ Mục XI trong đặc tả `P02.06`, hoàn toàn `UNIT_VERIFIED` với trạng thái `PASSED`.
- Khi chạy mặc định `python -m pytest tests -v` sau lần chỉnh sửa cuối cùng, toàn bộ bộ kiểm thử đạt **`49 passed, 18 skipped`** với exit code `0`, toàn bộ 18 bài skip là nhóm kiểm thử live opt-in.

## 13. Live Integration Tests

- Lệnh `$env:AI_VILLAGE_RUN_LIVE="1"; python -m pytest tests/integration/test_engine_provider_contract_live.py -v -rs` đạt **`3 passed, 2 skipped in 17.15s`** với exit code `0`:
  - `test_live_antigravity_through_common_contract`: `PASSED` và `LIVE_VERIFIED`
  - `test_live_unsupported_capability_fail_closed`: `PASSED` và `LIVE_VERIFIED`
  - `test_live_multi_connection_metadata_isolation`: `PASSED` và `LIVE_VERIFIED`
  - `test_live_alternative_api_provider_gemini`: `SKIPPED` với trạng thái `BLOCKED` do chưa có `GEMINI_API_KEY` hay `GOOGLE_API_KEY` hợp lệ
  - `test_live_provider_switching_without_core_changes`: `SKIPPED` với trạng thái `BLOCKED` do thiếu khóa API của Provider thứ hai

## 14. Regression Results

- Toàn bộ các bài unit test của `P02.01` đến `P02.05` trong `tests/engines/`, 5 bài kiểm tra cấu trúc tại [test_stage_a_baseline.py](../../tests/stage_a/test_stage_a_baseline.py) sau khi cập nhật cho phép thư mục `engines/providers/` và 8 bài nhất quán đặc tả tại [test_standards_consistency.py](../../tests/standards/test_standards_consistency.py) đều giữ trạng thái `PASSED` và nằm trong tổng số `49 passed`.
- Không sửa đổi bất kỳ tệp nào trong `core/`.

## 15. Những Giới Hạn Chưa Triển Khai

- Chưa có chứng thực live qua HTTP tới Google Gemini API hay một Model API độc lập thứ hai do máy trạm kiểm thử chưa được cấu hình khóa API hợp lệ nào.
- Chưa triển khai streaming SSE của Gemini API nên năng lực `streaming` của `GoogleGeminiAdapter` giữ trung thực ở trạng thái `NOT_VERIFIED`, đồng thời `model_discovery` trên cả hai adapter giữ ở trạng thái `NOT_VERIFIED`.
- Chưa triển khai Credential Vault bền vững, Provider Router đa tầng hay Tool Runtime thực thi thay cho LLM API vì các thành phần đó thuộc `PHASE 04` đến `PHASE 07`.

## 16. Verdict

- **Kết luận cuối cùng:** **`BLOCKED`**.
- Căn cứ theo Mục VI và Mục XIV của yêu cầu nhiệm vụ, `P02.06` chưa thể đánh dấu `DONE` và chưa thể mở khóa kiểm toán `Gate B` khi thiếu phản hồi live thật từ một API Provider độc lập với Antigravity.
- Điều kiện để người vận hành gỡ chặn và hoàn tất nghiệm thu `P02.06`: cấu hình tối thiểu một khóa API hợp lệ như `GEMINI_API_KEY` trong biến môi trường hoặc `.env` cục bộ ngoài Git, hoặc khởi chạy một endpoint suy luận cục bộ được phép sử dụng, rồi chạy lại `$env:AI_VILLAGE_RUN_LIVE="1"; python -m pytest tests/integration/test_engine_provider_contract_live.py -v` để xác minh Test B và Test C.
