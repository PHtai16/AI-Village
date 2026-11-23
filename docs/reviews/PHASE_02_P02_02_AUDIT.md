# Báo cáo Kiểm chứng và Nghiệm thu: PHASE 02 / P02.02 (Real Tool Execution & Artifact Verification)

- **Tiểu phase:** `P02.02: Tool execution`
- **Ngày kiểm chứng:** 2026-11-24
- **Kết luận:** **`PASS`**

## 1. Mục tiêu P02.02

Chứng minh Antigravity CLI (`agy.exe`) thực sự thao tác đọc và ghi tệp trong phạm vi `workspace/` được cấp phép thông qua mã nguồn Python, đồng thời Python kiểm tra độc lập sự tồn tại của tệp, nội dung, tính toàn vẹn tệp nguồn (`package.json`) và mã băm SHA-256 trên đĩa thay vì tin lời mô hình ngôn ngữ.

## 2. Các File Đã Tạo / Sửa

- [engines/antigravity/models.py](../../engines/antigravity/models.py): Bổ sung `supports_mode_flag`, trường `denied_actions`, các mã lỗi xác minh (`UNSUPPORTED_MODE`, `SANDBOX_UNVERIFIED`, `ARTIFACT_MISSING`, `ARTIFACT_OUTSIDE_WORKSPACE`, `CONTENT_MISMATCH`, `HASH_MISMATCH`, `SOURCE_MODIFIED`, `SCOPE_VIOLATION`) cùng 3 Pydantic v2 models (`AntigravityToolExecutionRequest`, `ArtifactVerificationResult`, `AntigravityToolExecutionResult`).
- [engines/antigravity/verification.py](../../engines/antigravity/verification.py): Module kiểm chứng độc lập trên hệ thống tệp (`compute_sha256_bytes`, `compute_sha256_file`, `resolve_within_workspace`, `snapshot_file_hashes`, `verify_artifact`).
- [engines/antigravity/adapter.py](../../engines/antigravity/adapter.py): Bổ sung nhận diện `--mode` trong `probe()`, trích xuất `denied_actions` trong `normalize_result()`, tách helper `_run_cli_command()` / `_preflight_failure_cli_result()` và triển khai `AntigravityAdapter.execute_tool_task()` cùng wrapper `execute_tool_task()`.
- [engines/antigravity/__init__.py](../../engines/antigravity/__init__.py): Xuất đầy đủ các hàm và models mới của `P02.02`.
- [tests/engines/test_antigravity_tool_execution_unit.py](../../tests/engines/test_antigravity_tool_execution_unit.py): Bộ unit test kiểm chứng toàn bộ các kịch bản hợp lệ, lỗi thiếu CLI/sandbox/mode/workspace, path traversal, thiếu artifact, sai nội dung, sai SHA-256, sửa đổi tệp nguồn, `denied_actions`, timeout, provider error và lọc bí mật.
- [tests/integration/test_antigravity_tool_execution_live.py](../../tests/integration/test_antigravity_tool_execution_live.py): Bài kiểm thử live opt-in qua `AI_VILLAGE_RUN_LIVE=1`.
- [tests/README.md](../../tests/README.md) và [tests/standards/test_standards_consistency.py](../../tests/standards/test_standards_consistency.py): Bổ sung lệnh chạy test mới và cho phép trạng thái DONE của `P02.02` trong roadmap.

## 3. Các Capability CLI Được Sử Dụng

Kiểm tra trực tiếp qua `probe()` trên `C:\Users\taine\.gemini\bin\agy.exe` (phiên bản `1.5.15` sau khi CLI tự cập nhật từ `1.5.14`):

- `--sandbox` (`supports_sandbox_flag: true`): Bật hạn chế sandbox cho phiên làm việc.
- `--mode accept-edits` (`supports_mode_flag: true`): Tự động chấp nhận thao tác tạo/sửa tệp trong workspace mà không tự động duyệt chạy lệnh shell/terminal (`Bash` vẫn bị chặn ở chế độ không tương tác).
- `--output-format json` (`supports_json_output: true`): Trả kết quả JSON có cấu trúc.
- `--print-timeout=90s` (`supports_print_timeout: true`): Giới hạn thời gian lượt chạy phía CLI.
- `--print=<prompt>` (`supports_print_mode: true`): Chạy một lượt không tương tác.
- Tuyệt đối không sử dụng `--dangerously-skip-permissions` hay `--mode auto`.

## 4. Cơ chế Workspace và Sandbox

- Mọi lượt chạy thực thi trong thư mục fixture tạm độc lập nằm trong `workspace/` (`p02_02_live_*` / `p02_02_evidence_*`).
- Phía Python thực hiện `resolve_within_workspace()` chuẩn hóa đường dẫn tuyệt đối (canonical path) để chặn tham chiếu tuyệt đối hoặc thoát thư mục (`..`) đối với cả tệp đầu ra lẫn danh sách tệp nguồn cần bảo vệ.
- **Kiểm chứng thực tế hành vi sentinel ngoài workspace:** Khi thử nghiệm yêu cầu ghi đè tệp sentinel vô hại ở thư mục anh em (`../p02_02_outer_.../sentinel.txt`) dưới `--sandbox --mode accept-edits`, mã băm SHA-256 của tệp sentinel trước và sau khi chạy giữ nguyên (`OUTER_UNCHANGED: True`), và CLI ghi nhận `"denied_actions": [{"action": "command", "display_name": "Bash"}]` do chặn thực thi lệnh shell ngoài quyền.

## 5. Fixture Description

- Mỗi lượt chạy tạo mới tệp `package.json` gồm `name` (`"ai-village-p02-02-fixture"`), `version` (`"0.0.1"`), `private` (`true`) và một `verification_marker` sinh ngẫu nhiên tại runtime (`mk-7ca08b779776` trong lượt thu thập bằng chứng).
- Giá trị `verification_marker` tuyệt đối không được truyền vào nội dung prompt gửi cho Agent; prompt chỉ hướng dẫn Agent đọc `package.json` (không sửa đổi), trích giá trị, tạo `proof.txt` chuẩn UTF-8 LF với hai dòng `name=<name>` và `verification_marker=<verification_marker>`, không chạy lệnh shell, rồi đọc lại `proof.txt`.

## 6. Live Invocation Summary

Lượt chạy bằng chứng qua `AntigravityAdapter().execute_tool_task()` sau khi hoàn thiện mã nguồn:

- **Executable:** `C:\Users\taine\.gemini\bin\agy.exe` (`v1.5.15`)
- **Workspace fixture ID:** `p02_02_evidence__84pdvn5`
- **Command summary:** `C:\Users\taine\.gemini\bin\agy.exe --sandbox --mode accept-edits --output-format json --print-timeout=90s --print=<470_chars>`
- **Started at (UTC):** `2026-11-23T17:18:04.042265Z`
- **Finished at (UTC):** `2026-11-23T17:18:39.843049Z`
- **Duration:** `35800 ms` (`provider_duration_seconds: 26.9913423`)
- **Exit code:** `0` (`provider_status: SUCCESS`, `denied_actions: []`, `error: null`)
- **Conversation ID:** `c276812b-fefb-4211-9398-4e1d144fdc8c`
- **Model ID:** `null` (không hardcode model; CLI JSON không trả trường `model_id`)
- **Telemetry (`VERIFIED_MEASURED`):** `input_tokens: 9508`, `output_tokens: 1090`, `thinking_tokens: 659`, `cache_read_tokens: 82646`, `total_tokens: 10598`.
- **Tool events:** `Tool events: NOT OBSERVABLE FROM CURRENT CLI OUTPUT` (định dạng `--output-format json` chỉ trả kết quả tổng hợp cuối; truy vết từng sự kiện công cụ chi tiết thuộc phạm vi `P02.03`).

## 7. File Verification Results

Kiểm tra trực tiếp bằng Python trên đĩa sau khi tiến trình CLI kết thúc:

- `cli_success`: `true`
- `artifact_exists`: `true` (`proof.txt`)
- `within_workspace`: `true`
- `content_verified`: `true` (đúng 2 dòng UTF-8 LF, khớp tên và marker đọc từ `package.json`)
- `hash_verified`: `true`
- `source_integrity_verified`: `true` (`package.json` giữ nguyên SHA-256 trước và sau khi chạy)
- `business_verified`: `true` (`file_size_bytes: 67`)

## 8. Expected / Actual SHA-256

- **Expected SHA-256 (Python tính độc lập từ dữ liệu kỳ vọng):** `542d483e2e9286131a5a4f50d4ed6188575e1e65a613cf375d5d35dd124ccba2`
- **Actual SHA-256 (Python đọc trực tiếp `proof.txt` trên đĩa):** `542d483e2e9286131a5a4f50d4ed6188575e1e65a613cf375d5d35dd124ccba2`
- **Kết quả đối chiếu:** Khớp tuyệt đối 64/64 ký tự hex.

## 9. Execution Status

- `AntigravityToolExecutionResult.status`: **`VERIFIED_PASS`**

## 10. Unit / Live Test Results

- **Unit & Standards Tests (`python -m pytest tests -v`):** **`29 passed, 2 skipped`** (exit code `0`), trong đó gồm `5 passed` bộ test mới tại [test_antigravity_tool_execution_unit.py](../../tests/engines/test_antigravity_tool_execution_unit.py) và `11 passed` từ `test_antigravity_adapter_unit.py`. Hai bài test live mặc định ở trạng thái `SKIPPED`.
- **Live Integration Test (`$env:AI_VILLAGE_RUN_LIVE="1"; python -m pytest tests/integration/test_antigravity_tool_execution_live.py -v`):** **`1 passed in 35.86s`** (exit code `0`) sau bản sửa dọn thư mục tạm trên Windows (`ignore_cleanup_errors=True` kết hợp thu hồi sau trễ handle).

## 11. Security Limitations

- Cờ `--sandbox` của Antigravity CLI cô lập thao tác terminal theo mô hình sandbox của CLI, trong khi thao tác đọc/ghi tệp trong `--mode accept-edits` vẫn dựa vào chính sách permission nội bộ của CLI kết hợp với bước xác minh đường dẫn chuẩn hóa ở phía Python (`resolve_within_workspace()` và `snapshot_file_hashes()`). Không thể coi riêng `cwd=workspace` là cơ chế ngăn đọc/ghi cấp hệ điều hành tuyệt đối.
- Trên Windows, tiến trình con hoặc dịch vụ nền của `agy.exe` có thể giữ khóa handle thư mục tạm trong thời gian ngắn ngay sau khi tiến trình chính kết thúc, đòi hỏi cơ chế thu hồi có trễ khi dọn `workspace/`.
- CLI JSON hiện tại chưa phát sự kiện gọi công cụ chi tiết (`Tool events: NOT OBSERVABLE FROM CURRENT CLI OUTPUT`).

## 12. Những Khả năng Chưa Kiểm chứng

Các hạng mục sau nằm ngoài phạm vi `P02.02` và chưa được kiểm chứng:

- Phân tích luồng sự kiện `stream-json` thực tế (`P02.03`).
- Cô lập lịch sử và tệp giữa hai phiên logic song song hoặc kế tiếp (`P02.04`).
- Hủy tiến trình đang chạy và kiểm thử đầy đủ vòng đời tiến trình (`P02.05`).
- Hợp đồng đa nhà cung cấp và thay thế API Provider song song (`P02.06`).

## 13. Kết luận

- **Verdict:** **`PASS`**
- **Căn cứ:** Agent thông qua `agy.exe` (`--sandbox --mode accept-edits`) đã đọc `package.json` thật mà không có marker trong prompt, tạo `proof.txt` đúng định dạng, giữ nguyên tệp nguồn, và Python kiểm chứng độc lập trên đĩa khớp chính xác kích thước cùng mã băm SHA-256; đồng thời toàn bộ unit tests và live integration test đều vượt qua. Đủ điều kiện đóng `P02.02` và bước sang `P02.03`.
