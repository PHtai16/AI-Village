# Báo cáo Kiểm chứng và Nghiệm thu: PHASE 02 / P02.04

- **Tiểu phase:** `P02.04: Session isolation`
- **Ngày kiểm chứng:** 2026-11-24
- **Kết luận:** **`PASS`**

## 1. CLI Version và Session Contract

- **CLI executable path:** `C:\Users\taine\.gemini\bin\agy.exe`
- **CLI version:** `1.5.15`
- **Cờ quản lý phiên được xác minh trực tiếp:**
  - Mặc định khi gọi `--print` không kèm `--conversation` hay `--continue`: CLI khởi tạo một `conversation_id` mới độc lập.
  - `--conversation <id>`: khôi phục đúng hội thoại trước đó theo ID xác thực. Không dùng `--continue` hay `-c` để tránh nối nhầm hội thoại gần nhất toàn cục.
  - `--settings <json>`: áp dụng cấu hình tạm riêng cho phiên hiện tại và không ghi ra ổ đĩa toàn cục. Dùng để truyền quy tắc `permissions.deny` chặn `read_file`, `write_file` sang thư mục phiên anh em cùng `command(*)`.
  - `--sandbox --mode accept-edits --output-format stream-json`: bật chế độ đọc/ghi trong workspace của phiên và xuất luồng sự kiện NDJSON theo thời gian thực.

## 2. Những Khả năng Quản lý Phiên Được Hỗ trợ Thật

Kiểm chứng thực tế trên `agy.exe` phiên bản `1.5.15` xác nhận các năng lực sau đã hoạt động thật:

- **Khởi tạo phiên mới tách biệt:** hai lời gọi `--print` riêng biệt nhận hai `conversation_id` khác nhau, không tự trộn ngữ cảnh.
- **Tiếp tục phiên tường minh:** truyền `--conversation <conv_a>` giữ nguyên `conversation_id` và truy xuất lại dữ liệu đã cung cấp ở lượt trước của chính phiên đó.
- **Chạy đồng thời:** hai tiến trình `agy.exe` chạy song song trong hai thư mục con tách biệt dưới `workspace/` hoàn thành độc lập với hai `conversation_id` riêng và không ghi đè tệp của nhau.
- **Khóa truy cập chéo theo quy tắc phiên:** trong phạm vi không phải thư mục tạm hệ điều hành, `--sandbox --mode accept-edits` kết hợp `--settings` với `permissions.deny` từ chối hành động `read_file` và `write_file` hướng tới thư mục phiên anh em bằng `denied_actions`.

## 3. Thiết kế Logical Session ID

`AntigravitySessionConfig` và `SessionExecutionContext` trong [engines/antigravity/models.py](../../engines/antigravity/models.py) tách biệt danh tính phiên do AI Village cấp phát khỏi danh tính hội thoại của Provider:

- `session_id` bắt buộc khớp biểu thức `^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$`, cấm `..`, khoảng trắng, ký tự đường dẫn và mẫu khóa bí mật.
- `AntigravitySessionManager` trong [engines/antigravity/sessions.py](../../engines/antigravity/sessions.py) lưu tập `_all_session_ids` có bảo vệ `threading.RLock`, từ chối trùng lặp bằng `DUPLICATE_SESSION_ID` và ngăn tái sử dụng sau khi đóng phiên.
- Không mang thông tin xác thực, không làm thay đổi danh tính Agent và không chứa khóa cơ sở dữ liệu hoặc trường điều phối `task_id` / `run_id` của `PHASE 04`.

## 4. Mapping sang Provider Conversation ID

- Khi tạo phiên, `provider_conversation_id` khởi tạo là `None` với `conversation_binding_source="none"`.
- Sau mỗi lượt `run_turn()`, adapter lấy `stream_result.conversation_id` từ sự kiện CLI thật và cập nhật `conversation_binding_source="provider_cli"`, tuyệt đối không tự sinh ID giả khi Provider không trả về.
- Bảng `_conversation_owners` đối chiếu quyền sở hữu `conversation_id`. Nếu Provider trả về một `conversation_id` đã gắn với phiên khác hoặc sai lệch khi tiếp tục phiên, kết quả trả ngay lỗi `CROSS_SESSION_CONVERSATION_VIOLATION`.

## 5. Phân tách Workspace

- `validate_session_workspace()` yêu cầu mỗi phiên nhận một thư mục con riêng nằm chặt bên trong `allowed_root_path`, không được trùng nhau và không được lồng vào nhau thông qua kiểm tra `paths_overlap()` trả lỗi `WORKSPACE_OVERLAP`.
- **Phát hiện quan trọng về `%TEMP%`:** thử nghiệm thăm dò trước khi thiết kế trên `%TEMP%` cho thấy chính sách quyền mặc định của `agy.exe` xếp thư mục tạm hệ điều hành vào nhóm được phép truy cập dùng chung, khiến phiên B đọc và ghi đè được `a_sentinel.txt` của phiên A nếu đặt hai workspace trong `%TEMP%`. Do đó `validate_session_workspace()` đặt mặc định `allow_temp_workspace=False` theo nguyên tắc Fail-Closed để chặn thư mục thuộc `tempfile.gettempdir()`.
- `has_symlink_or_junction_component()` trong [engines/antigravity/verification.py](../../engines/antigravity/verification.py) kiểm tra từng thành phần đường dẫn mà không đi theo liên kết, từ chối cả symlink lẫn Windows junction bằng `SYMLINK_ESCAPE_REJECTED`.
- Phía kiểm chứng đường dẫn `resolve_within_workspace()` bổ sung lớp kiểm tra symlink/junction trước khi chuẩn hóa canonical path.

## 6. Context Isolation Evidence

Từ bài kiểm thử live `test_live_session_context_isolation_and_continuation` và lượt thu thập bằng chứng `ev_e67b7967`:

- Phiên `ev_session_A` được cấp marker runtime riêng ở lượt 1 và nhận `conversation_id: "f12ce0e5-69f7-40e7-9ecb-0ec13f498664"`.
- Phiên `ev_session_B` chạy độc lập không mang marker của A nhận `conversation_id: "d3dd008d-f264-4524-b01e-70488c83dd9e"`.
- Kiểm tra chéo xác nhận `conv_a1 != conv_b1`, `ctx_a_leaked_in_b1: false` trên cả `response_text` lẫn toàn bộ `session_events` của phiên B.

## 7. File Isolation Evidence

Từ bài kiểm thử live `test_live_session_workspace_and_cross_access_isolation` và lượt thu thập bằng chứng `ev_e67b7967` dưới `workspace/p02_04_sessions/ev_e67b7967/`:

- Phiên A đọc `package.json` cùng `a_private.txt` và tạo `proof_A.txt` đúng trong `session_A`; phiên B đọc `package.json` cùng `b_private.txt` và tạo `proof_B.txt` đúng trong `session_B`. Kết quả kiểm tra chéo cho thấy `cross_ws_clean: true`, không tệp đầu ra nào xuất hiện bên thư mục còn lại.
- Khi phiên B thực hiện lượt truy cập chéo đọc và ghi đè trực tiếp `a_sentinel.txt` nằm trong `session_A` dưới `--sandbox --mode accept-edits` kèm `--settings` chặn 3 quy tắc là `read_file` và `write_file` vào `session_A` cùng `command(*)`:
  - `cross_enforced_settings_applied: true`, `cross_deny_rules_count: 3`.
  - CLI trả về từ chối thật `denied_actions: [{"action": "read_file", "display_name": "ReadFile"}]`.
  - `sentinel_leaked_to_b: false` và `sentinel_unchanged: true`.

## 8. Event Isolation Evidence

- Mỗi sự kiện `AntigravityStreamEvent` được bao bọc trong `AntigravitySessionEvent` mang `session_id` và `session_id_source="adapter_session_context"` tách biệt khỏi trường do Provider phát.
- Từng lần gọi `run_turn()` tạo một thể hiện `AntigravityStreamParser` cục bộ mới, không chia sẻ trạng thái toàn cục; `sequence_number` được đếm riêng từ `1` trong từng luồng và kết hợp cùng `session_id` và `turn_index` để phân biệt hoàn toàn.
- Lượt thu thập bằng chứng ghi nhận phiên A nhận `47` sự kiện cùng telemetry `VERIFIED_MEASURED` gồm `30275` input tokens, `1275` output tokens, `791` thinking tokens, `63723` cache read tokens và `31550` total tokens trong `51527 ms`. Phiên B nhận `53` sự kiện cùng telemetry `VERIFIED_MEASURED` gồm `9316` input tokens, `1599` output tokens, `1096` thinking tokens, `84425` cache read tokens và `10915` total tokens trong `58697 ms`. Cả hai luồng đều ghi nhận đúng công cụ thật `["read_file", "write_file"]` mà không lẫn `conversation_id` hay dữ liệu riêng sang callback bên kia.

## 9. Continuation / Resume Evidence

- Ở lượt 2 của `ev_session_A`, `run_turn(..., resume_conversation=True)` truyền tường minh `--conversation f12ce0e5-69f7-40e7-9ecb-0ec13f498664` và không truyền `--continue`.
- CLI trả về cùng `conv_a2_resumed: "f12ce0e5-69f7-40e7-9ecb-0ec13f498664"` với `resumed_match: true` và tái hiện chính xác token `A_MARKER_` của phiên A với `remembered_ctx_a: true`.

## 10. Concurrency Evidence

- Phiên `ev_session_A` và `ev_session_B` cùng chạy lượt 1 song song qua `ThreadPoolExecutor(max_workers=2)` với hai bộ quy tắc `--settings` chặn chéo nhau.
- Cả hai luồng đều đạt `SUCCEEDED` với hai `conversation_id` khác nhau, nhận `47` và `53` sự kiện độc lập, không đụng độ khóa thư mục trong quá trình chạy và tạo hai artifact hợp lệ đồng thời.
- Giới hạn đồng thời kiểm chứng là hai phiên song song trên một máy; chưa thử tải cao hàng chục phiên vì thuộc các phase mở rộng sau.

## 11. SHA-256 Verification

Kiểm chứng độc lập trên đĩa bằng `verify_session_artifact()` trong lượt chạy bằng chứng `ev_e67b7967`:

- **`proof_A.txt`:** `expected_sha_a` và `actual_sha_a` cùng là `0e7b2aa7ae69eaaecaf0a71b1c7b2697a5ee458e7e6095334fa7bf1b943e2a40`, `ver_a_pass: true`.
- **`proof_B.txt`:** `expected_sha_b` và `actual_sha_b` cùng là `6324aef9432c026ad565997c3fb32fc714ae26b35a572e958b1603117d39aa32`, `ver_b_pass: true`.
- **`a_sentinel.txt` sau thử nghiệm xâm nhập chéo:** `sentinel_sha_before` và `sentinel_sha_after` đều là `70b02ba87e1b14c52b43e4af97bb388e2b1cca5520319cc286f8db9a0cfdc193`, giữ nguyên tuyệt đối `sentinel_unchanged: true`.
- Toàn bộ tệp nguồn `package.json`, `a_private.txt` và `b_private.txt` giữ nguyên vẹn SHA-256 trước và sau thực thi.

## 12. Security Limitations

- **Thư mục `%TEMP%`:** Antigravity CLI mặc định coi thư mục tạm hệ điều hành là vùng được phép dùng chung giữa các workspace. Chỉ khi phiên làm việc nằm ngoài `%TEMP%` mới bảo đảm không bị gộp quyền mặc định; vì vậy cấu hình mặc định của adapter từ chối Fail-Closed các đường dẫn nằm trong `tempfile.gettempdir()`.
- **Mô hình sandbox Windows của CLI:** `--sandbox` cô lập câu lệnh terminal còn thao tác `read_file`/`write_file` dựa vào Permission Engine kết hợp `--settings` của CLI và bước thẩm định canonical path cùng SHA-256 của Python. Đây là kiểm soát cấp tiến trình CLI chứ chưa phải cô lập mức máy ảo hay container cấp hệ điều hành cho từng file tool.
- **Lược bỏ đường dẫn trong tóm tắt lệnh:** `format_command_summary()` trong [engines/antigravity/diagnostics.py](../../engines/antigravity/diagnostics.py) thay thế đối số JSON sau `--settings` bằng `--settings <redacted_rules>` nhằm không đưa danh sách đường dẫn máy chủ vào chuỗi chẩn đoán.
- **Phạm vi lưu trữ:** quản lý phiên hiện giữ trong bộ nhớ tiến trình; lưu vết bền vững qua SQLite thuộc `PHASE 04`.

## 13. Unit / Live Test Results

- **Unit, Standards & Stage A suite:** `python -m pytest tests -v` đạt **`39 passed, 8 skipped`** với exit code `0`, không phát sinh hồi quy cho `P02.01`, `P02.02` và `P02.03`. Trong đó gồm 5 bài unit test mới tại [test_antigravity_session_isolation_unit.py](../../tests/engines/test_antigravity_session_isolation_unit.py) phủ toàn bộ 15 kịch bản xác thực ID, trùng lặp, biên thư mục, path traversal, symlink/junction, ánh xạ/tiếp tục/va chạm `conversation_id`, thiếu `conversation_id`, tương quan event song song, callback tách biệt, sai lệch artifact, che giấu bí mật, dọn dẹp phiên, lỗi và tương thích ngược. Tám bài opt-in live mặc định ở trạng thái `SKIPPED`.
- **Live Session Isolation Tests:** `$env:AI_VILLAGE_RUN_LIVE="1"; python -m pytest tests/integration/test_antigravity_session_isolation_live.py -v` đạt **`3 passed in 256.22s`** với exit code `0` cho cả ba bài kiểm thử live về cách ly ngữ cảnh kèm `--conversation`, cách ly thư mục kèm chặn đọc ghi chéo `a_sentinel.txt`, và chạy song song hai phiên cùng lúc.

## 14. Verdict

- **Verdict:** **`PASS`**
- **Căn cứ:** Hai phiên logic A và B được quản lý với `session_id` độc lập khỏi `conversation_id` của Provider, không rò rỉ ngữ cảnh riêng sang phiên B, khôi phục chính xác hội thoại A qua `--conversation`, thực thi song song hai luồng event và telemetry riêng biệt không nhầm lẫn, chặn thực tế hành vi B đọc hoặc ghi đè `a_sentinel.txt` trong thư mục ngoài `%TEMP%` bằng `denied_actions` thật của CLI, xác minh SHA-256 độc lập cho cả `proof_A.txt`, `proof_B.txt` cùng `a_sentinel.txt`, và vượt qua toàn bộ unit tests lẫn live integration tests mà không gây hồi quy cho `P02.01`–`P02.03`.
