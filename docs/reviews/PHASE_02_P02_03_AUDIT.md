# Báo cáo Kiểm chứng và Nghiệm thu: PHASE 02 / P02.03

- **Tiểu phase:** `P02.03: Streaming events`
- **Ngày kiểm chứng:** 2026-11-24
- **Kết luận:** **`PASS`**

## 1. Environment

- **CLI executable path:** `C:\Users\taine\.gemini\bin\agy.exe`
- **CLI version:** `1.5.15`
- **Streaming options được xác minh:** `--output-format stream-json`, `--print=<prompt>`, `--print-timeout=<N>s`, `--sandbox`, `--mode accept-edits`

## 2. Streaming Contract

Thăm dò trực tiếp trên `agy.exe` phiên bản `1.5.15` xác nhận `--output-format stream-json` tương thích với `--print`, `--sandbox` và `--mode accept-edits`, phát các sự kiện theo chuẩn NDJSON trên `stdout` theo thời gian thực trong khi tiến trình đang chạy và đưa chẩn đoán kỹ thuật ra `stderr`.

- **Định dạng và cấu trúc top-level:**
  - `event == "init"`: chứa `conversation_id` và object `init` gồm `cwd`, `permission_mode`, `tools`. Ánh xạ thành `execution_started`.
  - `event == "step_update"`: chứa object `step_update` với `conversation_id`, `step_index`, `step_type`, `state` cùng trường chuyên biệt theo bước:
    - `step_type == "tool"`: `state == "ACTIVE"` cùng `tool_name` và `tool_info.parameters` ánh xạ thành `tool_call_started`; `state == "DONE"` cùng `duration_seconds` và `tool_info.output` tùy chọn ánh xạ thành `tool_call_completed`; trạng thái `ERROR`/`FAILED`/`DENIED` ánh xạ thành `tool_call_failed`.
    - `step_type == "agent_response"`: có `text_delta` khác rỗng ánh xạ thành `text_delta`; không có `text_delta` ánh xạ thành `step_update`, mang `telemetry` khi có `usage`.
    - `step_type` là `user_input` hoặc `system_message`: ánh xạ thành `step_update`.
    - `step_type` chưa biết hoặc `state` lạ của bước công cụ: giữ nguyên là `unknown_provider_event` với trạng thái `UNKNOWN`.
  - `event == "result"`: chứa `result` gồm `conversation_id`, `duration_seconds`, `num_turns`, `response`, `status`, `usage` và `denied_actions` tùy chọn. Khi `status == "SUCCESS"` ánh xạ thành `execution_completed`; các trạng thái khác ánh xạ thành `provider_error`.
  - `event == "error"`: ánh xạ thành `provider_error` với thông điệp đã lọc bí mật.
- **Trường nguồn `source`:** Phân tách bắt buộc giữa `provider_cli` cho các sự kiện do `agy.exe` phát ra và `adapter` cho các sự kiện do Python tự quan sát gồm `process_started`, `process_exited`, `process_timed_out`, `stream_parse_failed` và `stream_interrupted`.
- **Giới hạn đã xác minh của CLI `1.5.15`:**
  - `step_update` của công cụ trong bản `1.5.15` không phát trường `tool_call_id`, vì vậy trường `tool_call_id` được giữ nguyên là `None`.
  - Định danh điều phối `task_id`, `run_id`, `agent_id` không được tạo giả ở tầng Engine Adapter và thuộc trách nhiệm Task Runtime tại `PHASE 04`.
  - Hủy tiến trình chủ động từ bên ngoài và cô lập giữa hai phiên song song nằm ngoài phạm vi `P02.03` và thuộc `P02.04`–`P02.05`.

## 3. Implementation

- [engines/antigravity/models.py](../../engines/antigravity/models.py): Bổ sung mã lỗi `STREAM_PARSE_ERROR`, `STREAM_INTERRUPTED`, `MISSING_COMPLETION_EVENT` và ba model Pydantic v2 gồm `AntigravityStreamRequest`, `AntigravityStreamEvent` và `AntigravityStreamResult`.
- [engines/antigravity/streaming.py](../../engines/antigravity/streaming.py): Xây dựng `AntigravityStreamParser`, `StreamingProcessOutcome`, `sanitize_stream_text()`, `build_telemetry_from_payload()` và `run_streaming_process()`.
  - **Parser design:** Áp dụng bộ lọc allowlist chặt chẽ theo từng loại `event` và `step_type`, cấp phát `sequence_number` tăng đơn điệu, giữ nguyên `tool_call_id=None` khi CLI không phát, xử lý dòng trống, JSON cắt ngang, dòng không phải JSON object, vượt quá `max_line_bytes`, dòng cuối chưa xuống dòng ở EOF và chuỗi Unicode.
  - **Stream reader design:** Dùng `subprocess.Popen` với luồng đọc `stdout` từng dòng đưa vào `queue.Queue` có timestamp UTC thực tế lúc nhận, kết hợp luồng đọc `stderr` liên tục vào bộ đệm giới hạn `65536` byte để tránh khóa đầy pipe trên Windows, chuyển tiếp sự kiện ngay cho callback `on_event` trước khi tiến trình kết thúc và dọn pipe trong `finally`.
- [engines/antigravity/adapter.py](../../engines/antigravity/adapter.py): Bổ sung tham số tùy chọn `popen_factory` vào `AntigravityAdapter.__init__`, tái sử dụng `discover()`, `probe()`, `_classify_cli_error()`, `format_command_summary()` và `build_telemetry_from_payload()`, đồng thời cung cấp `AntigravityAdapter.stream_task()` và hàm tiện ích `stream_task()`. Phân tách rõ `exit_code == 0` với trường hợp thiếu sự kiện kết thúc `execution_completed`: tiến trình thoát `0` nhưng thiếu `result:SUCCESS` sẽ phát `stream_interrupted` và trả `MISSING_COMPLETION_EVENT`.
- [engines/antigravity/__init__.py](../../engines/antigravity/__init__.py): Xuất toàn bộ class và hàm mới của `P02.03`.
- [tests/engines/test_antigravity_streaming_unit.py](../../tests/engines/test_antigravity_streaming_unit.py) và [tests/integration/test_antigravity_streaming_live.py](../../tests/integration/test_antigravity_streaming_live.py): Bộ unit test giả lập và bộ live test opt-in cho ba kịch bản A/B/C.
- [tests/README.md](../../tests/README.md) và [tests/standards/test_standards_consistency.py](../../tests/standards/test_standards_consistency.py): Cập nhật lệnh chạy kiểm thử streaming và mở khóa trạng thái DONE cho `P02.03` trong roadmap.

## 4. Live Evidence

Lượt chạy bằng chứng thực tế qua `AntigravityAdapter().stream_task()` trên fixture cô lập `p02_03_evidence_lawk9spf` đọc `package.json` chứa marker runtime `mk-36c716d54060` không nằm trong prompt, tạo `proof.txt`, đọc lại và kiểm chứng độc lập trên đĩa bằng `verify_artifact()`:

- **Executable & Command summary:** `C:\Users\taine\.gemini\bin\agy.exe --sandbox --mode accept-edits --output-format stream-json --print-timeout=90s --print=<445_chars>`
- **Started at:** `2026-11-23T17:35:28.988725+00:00`
- **First provider event at:** `2026-11-23T17:35:33.012176+00:00`, đến sớm hơn `37.087s` so với lúc tiến trình kết thúc
- **Finished at:** `2026-11-23T17:36:10.099413+00:00` (`duration_ms: 41110`, `provider_duration_seconds: 36.3305901`)
- **Process exit code & completion status:** `exit_code: 0`, `status: "SUCCEEDED"`, `provider_status: "SUCCESS"`, `completion_event_received: true`, `conversation_id: "b5bdc0e4-b5f6-4f2f-9a8f-b00e68c1d551"`, `parse_error_count: 0`
- **Tổng số event thực nhận:** `45` sự kiện tuần tự từ sequence `1` đến `45`, gồm `2` sự kiện `source: "adapter"` và `43` sự kiện `source: "provider_cli"`:
  - `process_started` (`adapter`): `1`
  - `execution_started` (`provider_cli`): `1`
  - `step_update` (`provider_cli`): `5`
  - `tool_call_started` (`provider_cli`): `3`
  - `tool_call_completed` (`provider_cli`): `3`
  - `text_delta` (`provider_cli`): `30`
  - `execution_completed` (`provider_cli`): `1`
  - `process_exited` (`adapter`): `1`
- **Sự kiện tool quan sát được:** `observed_tool_names: ["read_file", "write_file"]`. Chuỗi sự kiện ghi nhận thật lần lượt là `read_file` tại sequence `5`–`6`, `write_file` tại sequence `8`–`9` và `read_file` đọc lại tệp tại sequence `12`–`13`, toàn bộ xuất hiện trước `execution_completed` ở sequence `44`.
- **Telemetry đo được (`VERIFIED_MEASURED`):** `input_tokens: 10667`, `output_tokens: 1232`, `thinking_tokens: 869`, `cache_read_tokens: 81964`, `total_tokens: 11899`.
- **Bằng chứng file & SHA-256 kiểm chứng độc lập trên đĩa:**
  - `expected_sha256`: `9b543d9e74693b059917ac7fcb4dd1c55c4f50793c78ef2acfdb5af2f1ac33cb`
  - `actual_sha256`: `9b543d9e74693b059917ac7fcb4dd1c55c4f50793c78ef2acfdb5af2f1ac33cb`
  - `file_size_bytes: 67`, `business_verified: true`, `hash_verified: true`, `source_integrity_verified: true`.

## 5. Testing

- **Unit, Standards & Stage A suite:** `python -m pytest tests -v` đạt **`34 passed, 5 skipped`** với exit code `0`, không lỗi hồi quy trên bộ test `P02.01` và `P02.02`. Trong đó gồm 5 unit tests mới tại [test_antigravity_streaming_unit.py](../../tests/engines/test_antigravity_streaming_unit.py) bao phủ toàn bộ 15 kịch bản chuẩn hóa, bảo mật, lỗi và hồi quy. Năm bài opt-in live mặc định ở trạng thái `SKIPPED`.
- **Live Streaming Integration Tests:** `$env:AI_VILLAGE_RUN_LIVE="1"; python -m pytest tests/integration/test_antigravity_streaming_live.py -v` đạt **`3 passed in 51.97s`** với exit code `0`, vượt qua cả ba kịch bản A phản hồi đơn giản theo thời gian thực, B thao tác công cụ `read_file`/`write_file` kèm xác minh SHA-256 trên đĩa và C xử lý tiến trình thật thoát khác `0`.

## 6. Security

- **Secret redaction & allowlisting:** Mọi trường chuỗi từ CLI đi qua `sanitize_stream_text()` hoặc `redact_secrets()`, lọc bỏ mẫu API key (`AIza...`, `sk-...`, `ghp_...`), `Bearer` token, `api_key`/`token`/`secret`/`password` và khóa riêng tư.
- **Chặn rò rỉ đường dẫn và nội dung công cụ:** `AntigravityStreamParser` áp dụng allowlist tường minh trên `init` và `tool_info`. Trường `init.cwd`, giá trị tham số công cụ `tool_info.parameters` và nội dung trả về `tool_info.output` bị loại bỏ hoàn toàn khỏi `redacted_payload`, chỉ giữ `permission_mode`, `tools_count`, tên khóa `parameter_keys`, `has_output` và `output_length`.
- **Giới hạn kích thước:** Chuỗi trong `redacted_payload` bị cắt ở ngưỡng `max_payload_chars` mặc định `512` ký tự kèm hậu tố `...[truncated]`; mỗi dòng NDJSON bị chặn khi vượt `max_line_bytes` mặc định `262144` byte và luồng `stderr` được giới hạn lưu tối đa `65536` byte với `redacted_stderr` cắt ở `2000` ký tự.
- **Rủi ro còn tồn tại:** Các biểu thức chính quy lọc secret chỉ chặn được mẫu định danh chuẩn và khóa cấu hình phổ biến; chuỗi văn bản do mô hình tự diễn giải trong `text_delta` hoặc `response_text` vẫn cần tránh truyền trực tiếp thông tin mật vào prompt hoặc tệp nguồn ngoài kiểm soát.

## 7. Verdict

- **Verdict:** **`PASS`**
- **Căn cứ:** Antigravity CLI `1.5.15` phát luồng NDJSON thực thời với `--output-format stream-json`, bộ phân tích và adapter truyền sự kiện theo thứ tự ngay khi tiến trình đang chạy trước thời điểm kết thúc, quan sát được `execution_started`, `tool_call_started`, `tool_call_completed` cho `read_file` và `write_file`, `text_delta`, `execution_completed`, tách biệt nguồn `provider_cli` và `adapter`, xử lý trung thực lỗi JSON lẫn mã thoát khác `0`, bảo vệ dữ liệu nhạy cảm bằng allowlist, khớp SHA-256 trên đĩa và vượt qua toàn bộ unit tests lẫn live integration tests mà không gây hồi quy cho `P02.01` và `P02.02`.
