# Báo cáo Kiểm chứng và Nghiệm thu: PHASE 02 / P02.05

- **Tiểu phase:** `P02.05: Process lifecycle`
- **Ngày kiểm chứng:** 2026-11-24
- **Kết luận:** **`PASS`**

## 1. Environment và CLI Version

- **Hệ điều hành kiểm chứng:** Windows `win32` với Python `3.13.13` và `pytest 9.1.1`.
- **CLI executable path:** `C:\Users\taine\.gemini\bin\agy.exe`
- **CLI version xác minh trực tiếp:** `1.5.15`
- **Thăm dò hành vi process tree trên Windows trước khi thiết kế:**
  - Thử nghiệm thăm dò 1 trên tiến trình `agy.exe` PID `2192` cho thấy mỗi lần gọi CLI sinh ra ba tiến trình con MCP là `cmd.exe` với PID `1720` cùng hai tiến trình `uvx.exe` với PID `8080` và `14984`. Khi chỉ gọi `proc.terminate()`, tiến trình cha `2192` dừng với mã thoát `1` và `alive_after: False`, nhưng `1720` và `8080` vẫn sống trong `children_after`, giữ handle khóa thư mục và làm `workspace/p02_05_probe_3424f129` không thể xóa.
  - Thử nghiệm thăm dò 2 trên tiến trình `agy.exe` PID `11260` gắn riêng một Win32 Job Object qua thư viện chuẩn Python `ctypes.WinDLL("kernel32")` với cờ `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, ghi nhận `ok_set: True`, `ok_assign: True` và `assign_err: 0`. Tiến trình `11260` sinh ba tiến trình con là `uvx.exe` với PID `7112` và `16600` cùng `cmd.exe` với PID `17136`. Sau khi gọi `TerminateJobObject` và `CloseHandle` với `ok_term: True` và `rc: 1`, toàn bộ `children_after` và `before_pids_still_alive` đều rỗng, thư mục tạm của lượt chạy bị xóa ngay với `run_dir_deleted: True` và `workspace/` chỉ còn `.gitkeep`, không chạm tới tiến trình nào khác ngoài Job Object sở hữu.

## 2. Process Lifecycle Contract

Hợp đồng vòng đời tiến trình được chuẩn hóa trong [engines/antigravity/models.py](../../engines/antigravity/models.py) và thực thi qua [engines/antigravity/lifecycle.py](../../engines/antigravity/lifecycle.py):

- **Tập trạng thái `ProcessLifecycleState`:** `CREATED`, `STARTING`, `RUNNING`, `CANCEL_REQUESTED`, `CANCELLING`, `CANCELLED`, `TIMED_OUT`, `SUCCEEDED`, `FAILED`.
- **Mô hình trạng thái và kết quả hủy:** `AntigravityExecutionStatus` phản ánh trạng thái tại thời điểm đọc theo đồng hồ monotonic; `AntigravityCancelOutcome` ghi nhận `execution_id`, `accepted`, `already_terminal`, `idempotent_repeat`, `state_at_request`, `final_state`, `cancel_requested_at` và thông điệp kết quả.
- **Tách biệt trạng thái tiến trình và hành động công cụ:** `AntigravityProcessManager` chỉ cập nhật `active_tool_name` từ các sự kiện `tool_call_*` thực tế trong luồng CLI, tuyệt đối không suy diễn rằng Agent đang sửa tệp chỉ vì tiến trình còn chạy và không sinh phần trăm tiến độ giả theo thời gian đã trôi qua.
- **Không ghi đè thành công giả:** khi `outcome.cancelled` đúng, lớp adapter ưu tiên giữ trạng thái `CANCELLED` ngay cả trong trường hợp tiến trình kết thúc trả về exit code `0`. Nếu tiến trình đã hoàn tất trước khi nhận lệnh hủy, kết quả giữ nguyên trạng thái kết thúc thực tế.

## 3. Execution ID và PID Ownership

- **Phân định danh tính:** `execution_id` được cấp riêng cho mỗi lượt thực thi thông qua `AntigravityProcessManager`, tách biệt hoàn toàn với `session_id` của phiên logic AI Village, `conversation_id` do CLI trả về và `pid` của hệ điều hành.
- **Kiểm soát quyền sở hữu:** mỗi lượt chạy gắn với một `owner_token` khi gọi `start_execution()`, `get_execution_status()`, `request_cancel()` và `await_result()`. Lệnh hủy chỉ tác động lên `ProcessHandleController` và `Win32JobGuard` thuộc đúng `execution_id` được cấp quyền, tuyệt đối không tìm hoặc dừng tiến trình toàn cục theo tên file thực thi.
- **Tương quan phiên:** `AntigravitySessionManager` trong [engines/antigravity/sessions.py](../../engines/antigravity/sessions.py) điều phối lượt chạy qua `start_turn()`, `get_turn_status()`, `cancel_turn()`, `await_turn()` và `run_turn()`, gắn `execution_id` vào từng `AntigravitySessionEvent` và `AntigravitySessionTurnResult`.

## 4. Start và Completion Evidence

- **Nguồn `LIVE_VERIFIED` từ suite `test_antigravity_lifecycle_live.py`:** bài `test_live_lifecycle_normal_execution` đạt `PASSED` trong lượt chạy opt-in toàn bộ 5 bài live lifecycle.
- **Nguồn `LIVE_VERIFIED` từ lượt thu thập `ev_0a52964d`:** phiên B hoàn thành đồng thời với `exec_id: "exec-912bed3f3e83"`, `pid: 2716`, trạng thái giữa chừng ghi nhận `RUNNING`, `alive_after: false`, `final_status: "SUCCEEDED"`, `exit_code: 0`, `conversation_id: "7972784e-fa1f-47d0-a87f-4ff16c4f3fb6"`, `duration_ms: 44871` và `events_count: 42`.
- **Telemetry và kiểm chứng độc lập đi kèm:** `telemetry_status: "VERIFIED_MEASURED"` với `provider_duration_seconds: 32.6630134`, `input_tokens: 30222`, `output_tokens: 1175`, `thinking_tokens: 717`, `cache_read_tokens: 63444`, `total_tokens: 31397`. Cặp kiểm tra hàm băm trên đĩa khớp `expected_sha256 == actual_sha256 == 6e07b89282f887cb65ab23a178e7856dce66c0f54c648d3676eb379d4860c167` với `business_verified: true`.
- **Nguồn `UNIT_VERIFIED`:** `test_lifecycle_start_status_active_tool_and_normal_completion` kiểm chứng đầy đủ chuỗi chuyển trạng thái `CREATED -> STARTING -> RUNNING -> SUCCEEDED`, đọc `active_tool_name`, exit code khác `0` và đồng hồ monotonic.

## 5. Cancellation Evidence

- **Nguồn `LIVE_VERIFIED` từ suite live và lượt thu thập `ev_0a52964d`:**
  - Phiên A khởi chạy `agy.exe` thật với `exec_id: "exec-1e3e5111973d"` và `pid: 13704`.
  - Lệnh hủy chỉ được gửi sau khi quan sát trực tiếp trạng thái `state_mid: "RUNNING"` và tiến trình thật đang sống `alive_before: true`.
  - `request_cancel()` trả về `accepted: true`, `already_terminal: false`, `idempotent_repeat: false`, `state_at_request: "RUNNING"`, `final_state: "CANCELLED"`, `cancel_requested_at: "2026-11-24T03:17:12.191868Z"`.
  - Kết quả cuối: `final_status: "CANCELLED"`, `exit_code: 1`, `process_tree_terminated: true`, `termination_mechanism: "win32_job_object"`, `duration_ms: 9159`, `events_count: 3` và `alive_after: false`.
- **Nguồn `UNIT_VERIFIED`:** `test_cancellation_while_running_double_cancel_zero_exit_and_kill_fallback` xác minh hủy khi đang chạy, hủy trước khi spawn, hủy lặp lại idempotent, hủy sau khi tiến trình đã hoàn tất, giữ trạng thái `CANCELLED` khi exit code là `0` và fallback buộc dừng khi tắt êm thất bại.

## 6. Timeout Evidence

- **Cơ chế triển khai trong [engines/antigravity/streaming.py](../../engines/antigravity/streaming.py):** `run_streaming_process()` sử dụng đồng hồ monotonic để áp đặt thời hạn cứng, xử lý hủy hợp tác, phân định thứ tự ưu tiên giữa timeout và cancel khi xảy ra tranh chấp đồng thời, rồi giải phóng cụm tiến trình qua `Win32JobGuard`.
- **Nguồn `LIVE_VERIFIED` trên tiến trình con được kiểm soát:** trong `test_live_lifecycle_controlled_process_timeout` và lượt thu thập `ev_0a52964d`, tiến trình thử nghiệm sở hữu thật mang `pid: 1748` bị ngắt đúng hạn với `timed_out: true`, `alive_after: false`, `process_tree_terminated: true`, `termination_mechanism: "win32_job_object"`, `duration_ms: 1509` và `last_event: "process_timed_out"`.
- **Nguồn `NOT_VERIFIED`:** chưa cố tình làm treo lượt suy luận live của Antigravity Provider để ép timeout bằng lượng lớn token hay sự cố mạng thật.
- **Nguồn `UNIT_VERIFIED`:** `test_timeout_timeout_vs_cancel_race_and_bounded_buffer_cleanup` xác minh đủ trường hợp quá hạn cứng, tranh chấp timeout với cancel và thu dọn khi kết thúc.

## 7. Cross-Session Cancellation Evidence

- **Nguồn `LIVE_VERIFIED` từ `test_live_lifecycle_cross_session_cancellation` và `ev_0a52964d`:** hai phiên A và B chạy `agy.exe` song song với `pid: 13704` và `pid: 2716`. Yêu cầu hủy nhắm vào phiên A dừng trọn vẹn cây tiến trình của A qua `win32_job_object` trong `9159 ms`, trong khi phiên B tiếp tục chạy suốt `44871 ms`, hoàn tất với `42` sự kiện streaming và tạo artifact khớp SHA-256 mà không rò rỉ dữ liệu hay bị ngắt nhầm.
- **Nguồn `UNIT_VERIFIED`:** `test_cross_session_cancellation_and_cleanup_guard` kiểm chứng hủy một phiên không ảnh hưởng phiên anh em, từ chối hủy chéo khi sai `owner_token`, đồng thời xác minh `close_session()` chặn bằng `EXECUTION_CONFLICT` khi còn lượt chạy active nếu không bật `cancel_active=True`.

## 8. Error Classification

Lớp adapter trong [engines/antigravity/adapter.py](../../engines/antigravity/adapter.py) phân tách nguồn lỗi qua `error_source` gồm `cli`, `provider`, `adapter`, `capability` và `cancellation`, đồng thời trích xuất `retry_after_seconds` khi có thông tin. Không có vòng lặp retry ẩn vô hạn trong runtime vòng đời tiến trình.

- **Lỗi `LIVE_VERIFIED` trên `agy.exe` thật:** gửi cờ không hợp lệ trả về `exit_code: 2` với thông báo plural `"flags provided but not defined: -invalid-p02-05-flag"`, được `_classify_cli_error()` ánh xạ chính xác thành `UNSUPPORTED_ARGUMENT`, `error_source: "cli"`, `retryable: false`.
- **Lỗi `UNIT_VERIFIED` qua fixture và mock:** `AUTH_ERROR` với mẫu xác thực đã thu hẹp để tránh khớp nhầm chuỗi như `"author"`, `QUOTA_EXCEEDED`, `RATE_LIMITED` kèm bóc tách `retry_after_seconds`, `PROVIDER_ERROR`, `PROCESS_LAUNCH_ERROR`, `MISSING_COMPLETION_EVENT`, `STREAM_INTERRUPTED`, `EXECUTION_NOT_FOUND` và `EXECUTION_CONFLICT`, cùng quy tắc che giấu bí mật khỏi `stdout`/`stderr`.
- **Lỗi `NOT_VERIFIED` trên môi trường live:** chưa cố ý làm hỏng thông tin đăng nhập, vượt hạn mức thật hoặc kích hoạt rate-limit trên tài khoản người vận hành để thu thập log lỗi Provider thật.

## 9. Resource Cleanup

- Mọi luồng `_pump_stdout_lines` và `_drain_stderr` dùng sự kiện dừng và kiểm tra đóng pipe an toàn để tránh treo thread đọc sau khi Job Object đóng.
- Kết thúc mọi kịch bản thăm dò và live integration đều xác minh tiến trình con không còn sống thông qua `is_pid_alive()` cho kết quả `alive_after: false` và thư mục thử nghiệm dưới `workspace/` được dọn sạch hoàn toàn, chỉ giữ lại `.gitkeep`.
- `close_session()` không xóa workspace hoặc đóng tài nguyên phiên khi lượt chạy vẫn đang hoạt động trừ khi nhận tường minh `cancel_active=True` để hủy tiến trình trước.

## 10. Streaming Integration

- Hàm dùng chung `_execute_stream_request()` trong [engines/antigravity/adapter.py](../../engines/antigravity/adapter.py) phục vụ cả `stream_task()` lẫn `AntigravityProcessManager`, bảo đảm mọi cơ chế kiểm soát timeout, hủy tiến trình và chuẩn hóa sự kiện đồng nhất giữa hai đường gọi.
- Hàng đợi dòng xuất `line_queue` và bộ đệm `events` có giới hạn dung lượng rõ ràng. Khi tràn bộ đệm ở kịch bản tải cao, hệ thống phát sự kiện `event_buffer_overflow` và tăng bộ đếm `events_dropped_count` trên `AntigravityStreamResult` thay vì im lặng bỏ qua sự kiện quan trọng.
- Các sự kiện vòng đời mới gồm `execution_cancel_requested`, `process_cancelled`, `process_timed_out` và `event_buffer_overflow` đều tuân thủ thứ tự đơn điệu của luồng NDJSON.

## 11. Security Limitations

- Phạm vi cô lập tiến trình hiện sử dụng Win32 Job Objects cho cây tiến trình thuộc quyền sở hữu của từng invocation và kiểm soát đường dẫn workspace dưới `workspace/`, không chạy trong container hay máy ảo cấp hệ điều hành.
- Các thử nghiệm live phải thực thi dưới `workspace/` thay vì `%TEMP%` theo kết luận từ `P02.04` về chính sách quyền mặc định của Antigravity CLI đối với thư mục tạm hệ điều hành.
- Dữ liệu vòng đời hiện được giữ trong bộ nhớ của tiến trình Python hiện hành; lưu trữ bền vững vào cơ sở dữ liệu thuộc phạm vi `PHASE 04`.
- Nếu chạy trên nền tảng không hỗ trợ `Win32JobGuard`, khả năng chấm dứt cây tiến trình con phụ thuộc cơ chế terminate của hệ điều hành và có thể không bảo đảm dọn sạch toàn bộ tiến trình cháu như đã kiểm chứng trên Windows.

## 12. Unit và Live Tests

- **Bộ Unit, Standards và Stage A:** `python -m pytest tests -v` đạt **`44 passed, 13 skipped`** với exit code `0`, không phát sinh hồi quy đối với `P02.01` đến `P02.04` và giữ đúng 13 bài live opt-in ở trạng thái `SKIPPED` khi không đặt `AI_VILLAGE_RUN_LIVE=1`. Riêng tệp [test_antigravity_lifecycle_unit.py](../../tests/engines/test_antigravity_lifecycle_unit.py) gồm 5 hàm kiểm thử bao phủ đầy đủ 24 kịch bản trong đặc tả `P02.05`.
- **Bộ Live Integration cho P02.05:** lệnh `$env:AI_VILLAGE_RUN_LIVE="1"; python -m pytest tests/integration/test_antigravity_lifecycle_live.py -v` đạt **`5 passed in 72.63s`**:
  - `test_live_lifecycle_normal_execution`: `PASSED`
  - `test_live_lifecycle_real_cancellation`: `PASSED`
  - `test_live_lifecycle_cross_session_cancellation`: `PASSED`
  - `test_live_lifecycle_controlled_process_timeout`: `PASSED`
  - `test_live_lifecycle_cli_error_classification`: `PASSED`

## 13. Git Changes

Các tệp mã nguồn, kiểm thử và tài liệu thuộc đúng phạm vi `P02.05`:

- [engines/antigravity/models.py](../../engines/antigravity/models.py)
- [engines/antigravity/streaming.py](../../engines/antigravity/streaming.py)
- [engines/antigravity/lifecycle.py](../../engines/antigravity/lifecycle.py)
- [engines/antigravity/adapter.py](../../engines/antigravity/adapter.py)
- [engines/antigravity/sessions.py](../../engines/antigravity/sessions.py)
- [engines/antigravity/__init__.py](../../engines/antigravity/__init__.py)
- [tests/engines/test_antigravity_lifecycle_unit.py](../../tests/engines/test_antigravity_lifecycle_unit.py)
- [tests/integration/test_antigravity_lifecycle_live.py](../../tests/integration/test_antigravity_lifecycle_live.py)
- [tests/README.md](../../tests/README.md)
- [tests/standards/test_standards_consistency.py](../../tests/standards/test_standards_consistency.py)
- [docs/reviews/PHASE_02_P02_05_AUDIT.md](../../docs/reviews/PHASE_02_P02_05_AUDIT.md)
- [docs/PROGRESS.md](../../docs/PROGRESS.md)

## 14. Verdict

- **Kết luận cuối cùng:** **`PASS`**.
- Toàn bộ tiêu chí nghiệm thu của `P02.05` đã được chứng minh bằng mã nguồn thật, thử nghiệm thăm dò hệ điều hành, 5 bài kiểm thử live `PASS` liên tiếp, bằng chứng thu thập độc lập `ev_0a52964d`, cùng bộ unit test phủ 24 kịch bản không phát sinh hồi quy đối với `P02.01` đến `P02.04`.
- Không tự ý đánh dấu hoàn thành cho `P02.06`, `PHASE 02`, `Gate B` hay `PHASE 03` trở đi; hệ thống đủ điều kiện chuyển tiếp sang `P02.06` khi được lệnh thực hiện.
