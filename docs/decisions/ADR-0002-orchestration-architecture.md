# ADR-0002: Lựa chọn Kiến trúc Điều phối Chính thức cho AI Village

- **Mã quyết định:** `ADR-0002`
- **Ngày lập:** `2026-11-24`
- **Quyết định chính thức:** `custom_orchestrator`
- **Trạng thái quyết định:** `ACCEPTED`
- **Commit tham chiếu đầu vào:** `e5239ab7e36e11148ba725543e09e172a72540ba`
- **Tài liệu tiền đề:** `docs/decisions/ADR-0001-temporary-technology-stack.md` giữ nguyên trạng thái lịch sử ban đầu

## 1. Bối cảnh

`ADR-0001` trì hoãn việc khóa framework điều phối cho tới khi AI Village hoàn tất kiểm chứng Antigravity CLI ở `PHASE 02` và chạy thử nghiệm đối chứng ở `PHASE 03`. Từ kết quả `P03.01`, Antigravity CLI đã chứng minh khả năng vận hành như một nền tảng thực thi tác nhân hoàn chỉnh qua `EngineRegistry` gồm vòng lặp suy luận riêng, công cụ đọc ghi tệp nội sinh `read_file` và `write_file`, cờ `--sandbox`, `--mode` và `--settings` cách ly đường dẫn chéo.

Bài toán đặt ra cho `P03.02` là xác định tầng `core/` nên giữ và phát triển bộ điều phối Python nội bộ `Custom Orchestrator` hay đưa `CrewAI` ở dạng `Flow` hoặc `Crew` vào làm bộ khung điều phối dài hạn. Quyết định này phải bảo đảm giữ Antigravity CLI làm Engine chính, không tạo thêm tầng lặp AI dư thừa, không phá vỡ cơ chế hủy tiến trình `Win32JobGuard`, cách ly phiên, kiểm soát quyền fail-closed và kiểm toán QA độc lập `verify_engine_artifact()`.

## 2. Các phương án xem xét

1. **Phương án A, `Custom Orchestrator`:** Giữ nguyên và phát triển tiếp bộ điều phối Python thuần đang hoạt động tại `core/orchestration/`, gọi `EngineRegistry` trực tiếp theo hợp đồng trung lập và tích hợp lớp trừu tượng `AgentRuntime` ở `P03.03` cùng Event Journal SQLite ở `PHASE 04`.
2. **Phương án B, `CrewAI Flow`:** Sử dụng `crewai.flow.flow.Flow` với `@start`, `@router`, `@listen` và `SQLiteFlowPersistence` làm khung điều phối bước, vẫn gọi thẳng `EngineRegistry.execute()` cho mọi lượt suy luận Antigravity.
3. **Phương án C, `CrewAI Crew`:** Dùng `Agent`, `Task`, `Process.sequential` và `Crew.kickoff()` của `CrewAI`, nối qua cầu nối `AntigravityEngineBaseLLM` kế thừa `crewai.BaseLLM` để gọi `EngineRegistry.execute()`.

## 3. Tiêu chí đánh giá

Quyết định kỹ thuật căn cứ vào sáu nhóm tiêu chí định lượng và kiến trúc:

1. **Tương thích với nền tảng tác nhân Antigravity:** Không làm phát sinh vòng lặp Agent kép hoặc gọi chồng chéo công cụ.
2. **Độ trễ, kích thước prompt và chi phí nạp:** Thời gian import nguội, overhead tầng điều phối và độ phình prompt gửi sang CLI.
3. **Kiểm soát quyền hạn, phiên và QA độc lập:** Chặn bàn giao fail-closed trước khi Worker chạy, cách ly hai chiều thư mục và cấm đánh dấu `DELIVERED` nếu `verify_engine_artifact()` trả `VERIFIED_FAIL`.
4. **Hủy tiến trình và khôi phục trạng thái:** Khả năng chấm dứt cụm tiến trình đang chạy từ `core/` qua `AntigravityProcessManager` và lưu vết Event Journal phục vụ `Mission Control`.
5. **Trung thực telemetry:** Giữ nguyên `None` kèm `UNAVAILABLE_FROM_PROVIDER` khi thiếu dữ liệu token thay vì ép về `0`.
6. **Gánh nặng phụ thuộc và bảo trì:** Số lượng gói kéo theo, dung lượng môi trường và độ ổn định API trên Python `3.13` Windows.

## 4. Bằng chứng thực nghiệm thu thập được

Thử nghiệm được tiến hành giữa mã gốc `Custom Orchestrator` tại commit `e5239ab` và môi trường cô lập `.runtime/crewai-bench-venv` chạy `crewai==1.15.45` trên `Python 3.13.13` Windows với cùng kịch bản `package.json -> proof.txt` trên `agy.exe` phiên bản `1.5.15`.

| Chỉ số đo trực tiếp | Phương án A: Custom `corr-59b284583d7f` | Phương án B: CrewAI Flow `corr-flow-a75da5b33a` | Phương án C: CrewAI Crew `corr-crew-74f3dd05f4` |
| :--- | :--- | :--- | :--- |
| Phụ thuộc ngoài thêm vào production | `0` gói ngoài `pydantic` và stdlib | `124` gói trong venv chiếm `556.62 MB` | `124` gói trong venv chiếm `556.62 MB` |
| Thời gian nạp nguội `.pyc` ấm | `1018.5 ms` global và `1628.6 ms` venv | `15266.7 ms` | `12963.0 ms` |
| Số lần gọi `agy.exe` live | `3` lượt | `3` lượt | `3` lượt khi khóa `max_calls=1` |
| Tổng thời gian chạy live | `105618 ms` | `112066 ms` | `115466 ms` |
| Thời gian Provider `agy.exe` | `101980 ms` | `108302 ms` | `111573 ms` |
| Overhead khung điều phối | `3638 ms` gồm cả kiểm tra đĩa | `3757 ms` | `3891 ms` |
| Tổng ký tự prompt gửi CLI | `5989` ký tự | `6032` ký tự | `7369` ký tự, tăng `23.0%` |
| Số sự kiện gán nhãn AI Village | `211` sự kiện | `213` sự kiện kèm `24` sự kiện Flow | `232` sự kiện kèm `18` sự kiện Crew |
| Sự kiện công cụ `ToolUsage*` trong CrewAI | Không áp dụng | `0` sự kiện | `0` sự kiện |
| Tổng token thực đo | `82211` cộng `64089` cache | `62301` cộng `84354` cache | `43532` cộng `104705` cache |
| Xác minh QA SHA-256 trên đĩa | `VERIFIED_PASS` | `VERIFIED_PASS` | `VERIFIED_PASS` |
| API hủy tác vụ cấp framework | Có `AntigravityProcessManager.request_cancel` | Không có trên `Flow` | Không có trên `Crew` |

## 5. Các phép thử đã thực hiện

1. **Kiểm chứng cấu trúc gói và nạp nguội:** Chạy `uv pip compile` và cài đặt thật trong `.runtime/crewai-bench-venv` xác nhận `crewai==1.15.45` kéo theo `lancedb`, `qdrant-client`, `pyarrow`, `openai`, `grpcio`, `tokenizers` và `opentelemetry-*`, đồng thời Python toàn cục giữ `crewai_installed: False`.
2. **Khảo sát API nội bộ CrewAI `1.15.45`:** Xác nhận `BaseLLM` là `BaseModel` kế thừa `ABC`, tự động bọc `call()` qua `run_with_rate_limit_retry`, khởi tạo `UsageMetrics` bằng số `0` thay vì `None`, mặc định `SQLiteFlowPersistence()` ghi vào `C:\Users\taine\AppData\Local\CrewAI\AI-Village` nếu không truyền `db_path`, và phát hiện `Task.guardrail` từ chối PEP 563 `from __future__ import annotations`.
3. **Kiểm thử đơn vị ngoại tuyến cả hai nhánh:** `80 passed, 23 skipped in 5.24s` trên Python toàn cục xác nhận `14` kịch bản lỗi và hủy ở Mục XI trên Phương án A; `4 passed, 91 warnings in 12.15s` trên `.runtime/crewai-bench-venv` xác nhận hành vi Phương án B và C gồm phát sinh lượt gọi phụ khi bật `Task.guardrail_max_retries=1` và khôi phục snapshot SQLite của Flow.
4. **Benchmark live thật với `agy.exe`:** `$env:AI_VILLAGE_RUN_LIVE="1"` trên `test_p03_02_crewai_benchmark_live.py` đạt `2 passed, 39 warnings in 237.80s`, chứng minh cả `CrewAI Flow` và `CrewAI Crew` chạy thật với Antigravity nhưng không mang lại lợi thế về tốc độ, độ gọn hay khả năng kiểm soát quyền so với `Custom Orchestrator`.

## 6. Đánh đổi của lựa chọn `custom_orchestrator`

Chọn `custom_orchestrator` đánh đổi việc nhóm phát triển phải tự viết máy trạng thái tác vụ, Event Journal SQLite, giao diện `AgentRuntime` và cơ chế phục hồi sau restart ở `P03.03` và `PHASE 04`, thay vì tận dụng `@start`/`@listen` hoặc `SQLiteFlowPersistence` có sẵn của `CrewAI Flow`. Đổi lại, AI Village giữ `core/` sạch tuyệt đối với `0` phụ thuộc AI framework ngoài, thời gian nạp nguội nhanh gấp `~8` tới `15` lần, không chịu `91` cảnh báo suy giảm nội bộ của thư viện ngoài, và giữ trọn quyền kiểm soát từng bước bàn giao, telemetry `None`, cũng như tín hiệu hủy `Win32JobGuard`.

## 7. Rủi ro và biện pháp giảm thiểu

1. **Rủi ro tự viết trùng lặp hạ tầng điều phối ở `PHASE 04`–`06`:** Giảm thiểu bằng cách chuẩn hóa ngay interface `AgentRuntime` tại `P03.03` và tái sử dụng mẫu Pydantic v2 cộng SQLite repository tường minh theo đúng thiết kế `PHASE 04`.
2. **Rủi ro thiếu chuẩn giao tiếp đa Agent khi mở rộng nhiều Worker:** Giảm thiểu bằng hợp đồng `WorkerHandoff` đã kiểm chứng tại `P03.01` kết hợp hàng đợi sự kiện và máy trạng thái `TaskLifecycle` thống nhất theo `AGENT_SPEC.md`.
3. **Rủi ro độ trễ từ nhiều lượt gọi `agy.exe`:** Cả ba phương án đều mất `~102s` đến `~112s` cho ba lượt `agy.exe`; việc tối ưu gộp lượt hoặc chỉ định model nhẹ hơn nằm ở lớp chiến lược `core/` mà không bị framework cản trở.

## 8. Phạm vi áp dụng

Quyết định `custom_orchestrator` áp dụng cho toàn bộ tầng `core/` của AI Village bắt đầu từ `P03.03` và các giai đoạn `PHASE 04` đến `PHASE 17`. Mã thử nghiệm CrewAI chỉ nằm trong phạm vi đánh giá tại `tests/benchmarks/p03_02/` và môi trường `.runtime/crewai-bench-venv`, không đưa `crewai` vào `pyproject.toml` production.

## 9. Phương án hoàn tác

Do `P03.03` sẽ khóa tầng giao tiếp trừu tượng `AgentRuntime` giữa `core/` và bộ thực thi bên dưới, dự án luôn giữ khả năng cắm một adapter `CrewAIFlowRuntime` phía sau `AgentRuntime` trong tương lai mà không phải sửa bảng trạng thái `PHASE 04`, `tools/`, `engines/` hay `ui/` nếu nhu cầu thực tế sau này thay đổi.

## 10. Hệ quả với `P03.03` và `PHASE 04`–`13`

- **`P03.03`:** Định nghĩa giao diện `AgentRuntime` gọn nhẹ trong `core/`, hiện thực triển khai đầu tiên dựa trên `Custom Orchestrator` nối qua `EngineRegistry` mà không phụ thuộc `crewai`.
- **`PHASE 04`:** Toàn bộ `Task`, `Run`, `Session`, append-only Event Journal, `SQLite` migrations, `Cancellation`/`Idempotency` và `PermissionManager` được xây thẳng vào `core/` và `storage/` bằng Python chuẩn xác định, không phải bọc vòng quanh `CrewAI Flow` state dict hay `AsyncIOScheduler`/luồng ẩn.
- **`PHASE 05`–`13`:** `Agent Registry` YAML, hội họp nhiều Worker có giới hạn lượt, `Tool Runtime` nội bộ và `Mission Control UI` đọc trực tiếp từ Event Journal hợp nhất của `core/`, giữ nhất quán một nguồn sự thật duy nhất từ tiến trình con tới giao diện người dùng.
