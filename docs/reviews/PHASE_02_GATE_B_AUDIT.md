# Báo cáo Kiểm toán Tổng thể: PHASE 02 và Gate B

- **Phạm vi kiểm toán:** `PHASE 02` từ `P02.01` đến `P02.06` cùng chuỗi End-to-End nghiệm thu `Gate B`
- **Ngày kiểm toán:** 2026-11-24
- **Kết luận cuối cùng:** **`PASS`** cho `P02.06` theo tiêu chí Antigravity-first tại [PHASE_02_P02_06_CRITERIA_ADDENDUM.md](PHASE_02_P02_06_CRITERIA_ADDENDUM.md), **`PHASE 02: DONE`** và **`Gate B: PASS`**

## 1. Kiến trúc Thực tế

Toàn bộ mã nguồn thực thi của `PHASE 02` nằm gọn trong tầng `engines/` và giữ cho `core/` sạch hoàn toàn không có mã Python nghiệp vụ:

- **Lớp điều khiển gốc Antigravity CLI:** thư mục `engines/antigravity/` gồm [adapter.py](../../engines/antigravity/adapter.py), [models.py](../../engines/antigravity/models.py), [streaming.py](../../engines/antigravity/streaming.py), [lifecycle.py](../../engines/antigravity/lifecycle.py), [sessions.py](../../engines/antigravity/sessions.py), [verification.py](../../engines/antigravity/verification.py) và [diagnostics.py](../../engines/antigravity/diagnostics.py).
- **Lớp hợp đồng trung lập và registry:** [contract.py](../../engines/contract.py) định nghĩa `EngineRequest`, `EngineResult`, `EngineEvent`, `EngineTelemetry`, `EngineCapabilities`, `EngineSessionReference`, `EngineToolCallProposal`, `EngineError` và `EngineAdapter`; [connections.py](../../engines/connections.py) quản lý `EngineConnectionRef`, `ProviderConnectionGroup`, `CredentialResolver`, `EnvCredentialResolver` và `ConnectionRegistry`; [registry.py](../../engines/registry.py) cung cấp `EngineRegistry`; [verification.py](../../engines/verification.py) cung cấp `verify_engine_artifact()` và `EngineArtifactVerificationResult`.
- **Các Provider adapter:** [engines/providers/antigravity.py](../../engines/providers/antigravity.py) bọc nguyên vẹn `AntigravityAdapter` dưới tên `AntigravityEngineAdapter` với `platform_kind = "agent_execution_platform"`; [engines/providers/gemini.py](../../engines/providers/gemini.py) giữ nguyên `GoogleGeminiAdapter` với `platform_kind = "model_api"` phục vụ mở rộng tùy chọn.

## 2. Chuỗi End-to-End Được Kiểm chứng

Bài kiểm thử [test_gate_b_end_to_end_live.py](../../tests/integration/test_gate_b_end_to_end_live.py) và lượt thu thập bằng chứng `req-ea47a176818c` chứng minh đầy đủ chuỗi:

1. Ứng dụng gọi thuần túy qua `EngineRegistry.execute()` với `EngineRequest` yêu cầu ba năng lực `streaming`, `native_tool_execution` và `workspace_access` cùng `execution_mode="accept-edits"`.
2. `EngineRegistry` phân giải `provider_id="antigravity"` sang `AntigravityEngineAdapter` mà không dùng đường tắt gọi trực tiếp lớp CLI bên dưới.
3. `AntigravityEngineAdapter` khởi chạy `C:\Users\taine\.gemini\bin\agy.exe` phiên bản `1.5.15` trong thư mục cô lập dưới `workspace/` kèm `--sandbox --mode accept-edits --output-format stream-json`.
4. `agy.exe` đọc tệp `package.json` chứa `verification_marker` ngẫu nhiên không nằm trong prompt, tạo `proof.txt` mới rồi đọc lại để tự đối chiếu.
5. `EngineResult` trả về `status: "SUCCEEDED"` nhưng giữ `business_verification_status: "UNVERIFIED"` để không tự cấp dấu QA.
6. Hàm `verify_engine_artifact()` đọc độc lập hệ thống tệp trên đĩa, đối chiếu nội dung, xác nhận `package.json` không bị sửa đổi và khớp SHA-256 64/64 ký tự để cấp `status: "VERIFIED_PASS"`.

## 3. Bằng chứng Tool Execution và Streaming

- **`LIVE_VERIFIED` từ lượt chạy Gate B `req-ea47a176818c`:**
  - Tổng số sự kiện nhận được theo thời gian thực qua callback `on_event` là `36` sự kiện với `duration_ms: 45320` và `conversation_id: "72ed9de3-d01b-4e92-a018-92655d4e72b1"`.
  - Chuỗi sự kiện `tool_execution_observed` mang `source: "provider"` ghi nhận thật theo đúng thứ tự: sequence `5`–`6` là `read_file` đọc `package.json`, sequence `8`–`9` là `write_file` tạo `proof.txt`, và sequence `12`–`13` là `read_file` đọc lại tệp đầu ra; `observed_tool_names` trả về `["read_file", "write_file"]`.
  - `EngineTelemetry` đo thật ở mức `telemetry_status: "VERIFIED_MEASURED"`, `telemetry_source: "provider_cli"` với `input_tokens: 29820`, `output_tokens: 979`, `total_tokens: 30799`, `thinking_tokens: 674`, `cached_tokens: 62704`, `provider_duration_seconds: 35.777029`, giữ `estimated_cost_usd: null` và `actual_cost_usd: null`.
- **Đối chiếu lịch sử `P02.02` và `P02.03`:** khớp nhất quán với bằng chứng tại [PHASE_02_P02_02_AUDIT.md](PHASE_02_P02_02_AUDIT.md) và [PHASE_02_P02_03_AUDIT.md](PHASE_02_P02_03_AUDIT.md).

## 4. Bằng chứng SHA-256 và Kiểm chứng Độc lập

- **Lượt chạy Gate B E2E `req-ea47a176818c`:**
  - `package_json_sha_before`: `11019d5793d2fbef107ad81331b630215f6d33189c27f42fe53b1b09d521f00c`, `source_integrity_verified: true`.
  - `proof.txt` trên đĩa có dung lượng `68` bytes, `expected_sha256` và `actual_sha256` cùng bằng `866664c9a1e95842c185b0b8d849dd61ee5fb41b03096f698973a520e1aecaf0`.
  - Kết quả từ `verify_engine_artifact()`: `status: "VERIFIED_PASS"`, `engine_business_status_before_qa: "UNVERIFIED"`, `content_verified: true`, `hash_verified: true`, `within_workspace: true`, `business_verified: true`.
- **Kiểm chứng từ chối tệp bị làm sai lệch:** khi ghi sai nội dung `proof.txt` ngay trong cùng phiên thực nghiệm và truyền cùng `EngineResult` có `status: "SUCCEEDED"`, `verify_engine_artifact()` trả ngay `status: "VERIFIED_FAIL"` với `engine_succeeded: true`, `content_verified: false`, `hash_verified: false`, `business_verified: false`. Nhóm unit test trong [test_gate_b_verification_unit.py](../../tests/engines/test_gate_b_verification_unit.py) phủ thêm các kịch bản thiếu tệp, sửa trộm `package.json`, thoát thư mục `../` và kết quả `BLOCKED`.

## 5. Session Isolation

Từ hồ sơ kiểm chứng [PHASE_02_P02_04_AUDIT.md](PHASE_02_P02_04_AUDIT.md) tại commit `ac8cdd1`:

- `AntigravitySessionManager` cấp phát `session_id` độc lập với `provider_conversation_id`, chặn Fail-Closed thư mục thuộc `%TEMP%`, kiểm tra không lồng workspace, chặn symlink lẫn NTFS junction và truyền `--conversation` cùng `--settings` chặn đọc/ghi sang phiên anh em.
- Kiểm chứng live song song A/B cho kết quả hai `conversation_id` tách biệt, khôi phục đúng ngữ cảnh phiên A, phiên B bị từ chối truy cập `a_sentinel.txt` bằng `denied_actions` thật và SHA-256 tệp nguồn giữ nguyên vẹn.

## 6. Process Lifecycle và Cancellation

Từ hồ sơ kiểm chứng [PHASE_02_P02_05_AUDIT.md](PHASE_02_P02_05_AUDIT.md) tại commit `3cfbb9f`:

- `AntigravityProcessManager` chuẩn hóa `ProcessLifecycleState` với 9 trạng thái từ `CREATED` đến `CANCELLED`, `TIMED_OUT`, `SUCCEEDED`, `FAILED`; tách riêng `active_tool_name` với trạng thái tiến trình và loại bỏ retry vô hạn.
- Trên Windows, `Win32JobGuard` dùng `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` kết thúc trọn vẹn `agy.exe` cùng các tiến trình con MCP sở hữu trong khi phiên song song khác vẫn hoàn tất bình thường.
- Kiểm chứng `LIVE_VERIFIED` hủy thật PID `13704` đang chạy, controlled timeout PID `1748` và phân loại lỗi CLI `UNSUPPORTED_ARGUMENT`; các nhánh `AUTH_ERROR`, `QUOTA_EXCEEDED` và `RATE_LIMITED` đạt `UNIT_VERIFIED`.

## 7. Provider Contract và Định hướng Antigravity-first

- Theo [PHASE_02_P02_06_CRITERIA_ADDENDUM.md](PHASE_02_P02_06_CRITERIA_ADDENDUM.md), Antigravity CLI là Engine thực thi chính và đã chứng minh hoạt động hoàn chỉnh qua Universal Engine Contract từ handshake, text mode đến tool streaming End-to-End.
- Kiến trúc Multi-Provider và BYOK tại `engines/contract.py`, `engines/connections.py`, `engines/registry.py` cùng `GoogleGeminiAdapter` vẫn giữ nguyên vẹn. Trong kiểm chứng `test_live_gate_b_gemini_tool_rejection_and_tamper_detection` và lượt thu thập `req-ea47a176818c`, yêu cầu `native_tool_execution` và `workspace_access` gửi sang `gemini_primary` bị chặn Fail-Closed bằng `status: "BLOCKED"`, `error_code: "TOOL_UNSUPPORTED_BY_PROVIDER"`, `observed_tool_names: []` và không tự fallback.
- Phần gọi HTTP live tới Model API độc lập thứ hai giữ nguyên lịch sử `BLOCKED` trong [PHASE_02_P02_06_AUDIT.md](PHASE_02_P02_06_AUDIT.md) và được đặt vào backlog tùy chọn của `P15.02`, đồng bộ với `P13.08` và `P17.02`.

## 8. Các Giới hạn Bảo mật

- Cô lập tiến trình trên Windows sử dụng `--sandbox --mode accept-edits`, `--settings` deny rules, chuẩn hóa canonical path và `Win32JobGuard` chứ chưa chạy trong container hay máy ảo cấp hệ điều hành.
- Các phiên làm việc live của `agy.exe` bắt buộc đặt dưới `workspace/` thay vì `%TEMP%` do chính sách quyền mặc định của CLI đối với thư mục tạm hệ điều hành.
- Trạng thái phiên, vòng đời tiến trình và Connection Registry hiện lưu trong bộ nhớ tiến trình Python; lưu trữ SQLite bền vững và Permission Manager đầy đủ thuộc `PHASE 04`.
- `EnvCredentialResolver` là giải pháp backend tạm thời chờ Secret Vault chính thức; `redact_secrets()` lọc theo mẫu chuỗi chuẩn nên chuỗi tự do trong prompt hay tệp nguồn vẫn cần tránh đưa thông tin mật.

## 9. Kết quả Unit và Live Tests

- **Bộ kiểm thử offline mặc định `python -m pytest tests -v`:**
  - Chạy lại sau khi hoàn tất mã nguồn và tài liệu đạt **`53 passed, 20 skipped`** với exit code `0`, toàn bộ bài unit `P02.01` đến `P02.06`, `4` unit test mới tại [test_gate_b_verification_unit.py](../../tests/engines/test_gate_b_verification_unit.py), `tests/stage_a/` và `tests/standards/` đều `PASSED`; toàn bộ `20` bài live opt-in ở trạng thái `SKIPPED` khi không bật `AI_VILLAGE_RUN_LIVE=1`.
- **Bộ kiểm thử live tích hợp theo từng giai đoạn:**
  - `test_gate_b_end_to_end_live.py` qua `AI_VILLAGE_RUN_LIVE=1`: **`2 passed in 48.55s`** với `WORKSPACE: ['.gitkeep']`.
  - `test_engine_provider_contract_live.py` ghi nhận tại `094dd83`: **`3 passed, 2 skipped in 17.15s`** giữ nguyên hai bài Gemini live ở trạng thái `SKIPPED`.
  - Các suite live `P02.01` đến `P02.05` đã nghiệm thu trước đó trên từng commit tương ứng: `1 passed` ở `P02.01`, `1 passed` ở `P02.02`, `3 passed` ở `P02.03`, `3 passed` ở `P02.04` và `5 passed` ở `P02.05`.

## 10. Chức năng Đã Triển Khai và Chưa Triển Khai

| Nhóm chức năng | Trạng thái xác minh | Ghi chú phạm vi |
| :--- | :--- | :--- |
| Antigravity CLI discovery, probe, handshake, tool execution, SHA-256 | `LIVE_VERIFIED` | Hoàn tất `P02.01` đến `P02.02` và `Gate B` |
| NDJSON live streaming và chuẩn hóa sự kiện | `LIVE_VERIFIED` | `tool_call_id` để `None` do `agy.exe` `1.5.15` chưa phát |
| Cách ly phiên logic song song và chặn truy cập chéo | `LIVE_VERIFIED` | Lưu trong bộ nhớ tiến trình |
| Quản lý vòng đời, Win32 Job cancel thật, controlled timeout | `LIVE_VERIFIED` | Lỗi `AUTH_ERROR`/`QUOTA_EXCEEDED`/`RATE_LIMITED` ở mức `UNIT_VERIFIED` |
| Universal Engine Contract, `EngineRegistry`, `verify_engine_artifact()` | `LIVE_VERIFIED` | Chạy End-to-End thật qua Antigravity CLI |
| `GoogleGeminiAdapter`, BYOK multi-connection metadata | `UNIT_VERIFIED` | Kiểm chứng chặn capability và metadata ở mức live; HTTP live chờ khóa API ở `P15.02` |
| `core/`, Tool Runtime, SQLite Storage, CrewAI, UI 2D/3D | Chưa triển khai | Đúng kỷ luật lộ trình từ `PHASE 03` đến `PHASE 17` |

## 11. Các Rủi ro Còn Tồn tại và Verdict

- **Rủi ro kỹ thuật còn lại:** `agy.exe` phụ thuộc trạng thái đăng nhập và hạn mức tài khoản cục bộ trên máy chủ; khi chuyển qua Linux/macOS cần xác minh cơ chế thay thế cho `Win32JobGuard`; các lỗi quota và auth của CLI lẫn Provider API hiện mới được kiểm thử trên fixture chứ chưa cố ý kích hoạt lỗi trên tài khoản thật; kiểm thử HTTP live tới API Provider thứ hai sẽ thực hiện khi có khóa API tại backlog `P15.02`.
- **Verdict tổng hợp:** **`P02.06: DONE`** theo tiêu chí Antigravity-first đã phê duyệt, **`PHASE 02: DONE`** và **`Gate B: PASS`**. Hệ thống đủ điều kiện chuyển sang **`PHASE 03: Quyết định CrewAI`** khi người vận hành ra lệnh triển khai.
