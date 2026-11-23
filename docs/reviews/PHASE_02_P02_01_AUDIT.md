# Báo cáo Kiểm chứng và Nghiệm thu: PHASE 02 / P02.01 (Antigravity CLI Handshake)

- **Tiểu phase:** `P02.01: CLI handshake`
- **Ngày kiểm chứng:** 2026-11-24
- **Kết luận:** **`PASS`**

## 1. Environment

- **Hệ điều hành:** Windows (PowerShell 5.1.26100.9818)
- **Python:** Python 3.13.13 (`pydantic 2.12.5`, `pytest 9.1.1`)
- **Các tệp thực thi đã kiểm tra:**
  1. `C:\Antigravity\Antigravity\bin\antigravity.cmd` (`Antigravity 1.107.0`): CLI quản lý cửa sổ IDE Electron (`chat`, `serve-web`, `tunnel`), không phải giao diện headless chuyên dụng cho subagent runtime.
  2. `C:\Users\taine\.gemini\bin\agy.exe` (`1.5.14`): Bản CLI độc lập của Antigravity, chưa nằm trong `PATH` hệ thống mặc định (`where agy` không trả về kết quả) nhưng nằm tại thư mục người dùng `%USERPROFILE%\.gemini\bin\agy.exe`.
- **Cơ chế phát hiện (Discovery):** Đọc lần lượt `cli_path` truyền tường minh -> biến môi trường `ANTIGRAVITY_CLI_PATH` -> `shutil.which("agy")` -> đường dẫn mặc định `%USERPROFILE%\.gemini\bin\agy.exe`, không sửa `PATH` hệ thống và không tạo binary giả.

## 2. CLI / API Discovery

- **Tình huống xác định:** **A (Supported CLI/API)**. `agy.exe` phiên bản `1.5.14` hỗ trợ gọi không tương tác trực tiếp từ `subprocess` của Python mà không cần mở giao diện IDE.
- **Nguồn xác minh hợp đồng:** Chạy thực tế `agy.exe --version`, `agy.exe --help`, `agy.exe models` và `agy.exe --output-format json --print-timeout=60s --print=...`.
- **Chi tiết kỹ thuật quan trọng phát hiện từ chạy thật:**
  1. `agy.exe --help` xuất toàn bộ nội dung hướng dẫn ra luồng **`stderr`** (`3323` bytes, exit code `0`) thay vì `stdout` (`0` bytes). Adapter đã xử lý đọc kết hợp `stdout` và `stderr` khi `returncode == 0`.
  2. Tham số `--print` nhận trực tiếp chuỗi prompt làm giá trị; nếu viết tách `--print --output-format json <prompt>`, CLI báo lỗi exit code `2` do hiểu nhầm `"--output-format"` là prompt. Phải truyền dạng `--output-format json --print-timeout=<N>s --print=<prompt>`.
- **Khả năng được xác nhận trong `probe()` (`probe_exit_code == 0`):**
  - `--print` (`supports_print_mode: true`)
  - `--output-format json` (`supports_json_output: true`)
  - Khai báo cờ `stream-json` trong `--help` (`supports_stream_json_flag: true`, chưa kiểm chứng chạy thực tế)
  - `--print-timeout` (`supports_print_timeout: true`)
  - `--sandbox` (`supports_sandbox_flag: true`)
  - Lệnh con `models` (`supports_models_subcommand: true`; chạy thật `agy.exe models` trả danh sách model từ hồ sơ đã đăng nhập sẵn).

## 3. Handshake

- **Cách gọi:** Gọi qua `AntigravityAdapter.handshake()` trong `engines/antigravity/adapter.py` từ tiến trình Python với thư mục làm việc tạm cô lập nằm trong `workspace/` (`p02_01_live_*`).
- **Tóm tắt lệnh:** `C:\Users\taine\.gemini\bin\agy.exe --output-format json --print-timeout=60s --print=<46_chars>` (không hardcode `--model`).
- **Kết quả kiểm chứng live gần nhất (sau bản sửa cuối của `probe()`):**
  - **Trạng thái adapter:** `SUCCEEDED`
  - **Provider status:** `SUCCESS`
  - **Exit code:** `0`
  - **Started at (UTC):** `2026-11-23T17:02:09.802815Z`
  - **Finished at (UTC):** `2026-11-23T17:02:30.376814Z`
  - **Wall-clock duration:** `20573 ms` (`provider_duration_seconds: 12.7458259`)
  - **Conversation ID:** `2253c701-19da-438b-abd9-a05bb8d187b3`
  - **Model ID:** `null` (không hardcode tên model; CLI trả JSON không kèm `model_id`)
  - **Response text:** `AI_VILLAGE_HANDSHAKE_OK\n`
  - **Telemetry (`VERIFIED_MEASURED`):** `input_tokens: 22247`, `output_tokens: 92`, `thinking_tokens: 81`, `cache_read_tokens: 0`, `total_tokens: 22339`.
  - **Lỗi:** `null`, `redacted_stderr: ""`.

## 4. Adapter

Các module tối thiểu đã tạo trong `engines/antigravity/` (không phụ thuộc CrewAI, không chứa logic phòng ban, không hardcode model hay API key):

- [engines/__init__.py](../../engines/__init__.py): Khai báo package `engines`.
- [engines/antigravity/__init__.py](../../engines/antigravity/__init__.py): Xuất các lớp và hàm công khai (`AntigravityAdapter`, `discover`, `probe`, `handshake`, `normalize_result`, `redact_secrets`, `format_command_summary`, các Pydantic v2 models).
- [engines/antigravity/models.py](../../engines/antigravity/models.py): Các Pydantic v2 models (`AntigravityDiscoveryResult`, `AntigravityCapabilities`, `AntigravityTelemetry`, `AntigravityError`, `AntigravityHandshakeRequest`, `AntigravityHandshakeResult`).
- [engines/antigravity/diagnostics.py](../../engines/antigravity/diagnostics.py): Che giấu chuỗi bí mật (`redact_secrets`) và tóm tắt lệnh an toàn (`format_command_summary`).
- [engines/antigravity/adapter.py](../../engines/antigravity/adapter.py): Tách biệt 6 trách nhiệm: `discover()`, `probe()`, `handshake()`, `normalize_result()`, `_classify_cli_error()`, `_build_telemetry()`.
- [pyproject.toml](../../pyproject.toml): Đăng ký pytest marker `live` tách biệt kiểm thử live khỏi mặc định.

## 5. Tests

- **Unit & Standards Tests (`python -m pytest tests -v`):**
  - Kết quả sau chỉnh sửa cuối: **`24 passed, 1 skipped in 0.92s`** (exit code `0`).
  - Bao gồm `11 passed` trong [tests/engines/test_antigravity_adapter_unit.py](../../tests/engines/test_antigravity_adapter_unit.py) (mock runner cho discovery, probe đọc stderr, JSON success + telemetry `VERIFIED_MEASURED`, thiếu usage -> `null`/`UNAVAILABLE_FROM_PROVIDER`, phân loại lỗi `UNSUPPORTED_ARGUMENT`/`AUTH_ERROR`/`QUOTA_EXCEEDED`/`RATE_LIMITED`/`PROVIDER_ERROR`, `EXECUTABLE_NOT_FOUND`, `INVALID_RESPONSE`, `TIMED_OUT`, `PROCESS_LAUNCH_ERROR`, workspace không tồn tại, lọc bí mật và validation đầu vào), `5 passed` từ `tests/stage_a/`, `8 passed` từ `tests/standards/`, và `1 skipped` cho test live khi chạy mặc định.
- **Live Integration Test (`$env:AI_VILLAGE_RUN_LIVE="1"; python -m pytest tests/integration/test_antigravity_handshake_live.py -v`):**
  - Kết quả sau chỉnh sửa cuối: **`1 passed in 20.34s`** (exit code `0`), xác minh thật `discover()`, `probe()`, `handshake()`, `exit_code == 0`, marker `AI_VILLAGE_HANDSHAKE_OK`, `conversation_id` và `duration_ms > 0`.

## 6. Limitations

Các khả năng sau **chưa được kiểm chứng thực tế** trong phạm vi `P02.01` và chỉ được đánh giá ở các tiểu phase tiếp theo:

- **Tool execution trong workspace (`P02.02`):** Chưa kiểm chứng Agent đọc `package.json`, tạo tệp và xác minh SHA-256.
- **Streaming events (`P02.03`):** Dù `--help` liệt kê `stream-json`, chưa kiểm chứng phân tích luồng NDJSON `started`/`tool-used`/`completed`/`failed` thực tế.
- **Session isolation (`P02.04`):** Chưa kiểm chứng chạy hai phiên logic A/B độc lập không trộn lịch sử và tệp.
- **Process lifecycle đầy đủ (`P02.05`):** Chưa kiểm chứng hủy tiến trình giữa chừng (cancellation/process-tree kill) và xử lý timeout/quota/rate-limit trên tiến trình thật.
- **Provider contract đa nhà cung cấp (`P02.06`):** Chưa triển khai hợp đồng Provider trừu tượng dùng chung hay API Provider thay thế trong `engines/providers/`.

## 7. Verdict

- **Kết luận chính thức:** **`PASS`**
- **Lý do:** Đã xác minh tồn tại CLI thực tế (`C:\Users\taine\.gemini\bin\agy.exe` v`1.5.14`), kiểm chứng hợp đồng tham số không tương tác bằng lệnh thật, xây dựng adapter tối thiểu tách biệt trách nhiệm tại `engines/antigravity/` với Pydantic v2 và redaction, chạy vượt qua toàn bộ 24 unit/standards tests và 1 live integration handshake test (`exit_code == 0`, có `conversation_id`, response thật và token telemetry đo được). Đủ điều kiện đóng `P02.01` và chuyển sang `P02.02`.
