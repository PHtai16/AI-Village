# Biên bản Bổ sung Tiêu chí Nghiệm thu: PHASE 02 / P02.06

- **Tiểu phase:** `P02.06: Provider contract`
- **Ngày phê duyệt điều chỉnh:** 2026-11-24
- **Nguồn quyết định:** Phê duyệt trực tiếp của người vận hành trong phiên ngày 2026-11-24 theo định hướng ưu tiên Antigravity CLI làm Engine thực thi chính.
- **Trạng thái lịch sử được bảo toàn:** [PHASE_02_P02_06_AUDIT.md](PHASE_02_P02_06_AUDIT.md) tại commit `094dd83` giữ nguyên trạng thái lịch sử `BLOCKED` cho các bài kiểm thử live HTTP của Google Gemini API và không bị chỉnh sửa thành PASS giả.

## 1. Bối cảnh và Lý do Điều chỉnh

Trong phiên thực thi trước tại commit `094dd83`, toàn bộ Universal Engine Contract, `AntigravityEngineAdapter`, `GoogleGeminiAdapter`, `ConnectionRegistry` đa khóa và 20 kịch bản unit test của `P02.06` đã hoàn thiện và chạy thật với Antigravity CLI. Tuy nhiên, máy trạm vận hành không cài đặt dịch vụ suy luận cục bộ như Ollama và chưa cấu hình khóa API thương mại cho Gemini hay các Provider đám mây khác.

Sau khi người vận hành chọn phương án không cài đặt Ollama hay tải model cục bộ, định hướng chính thức của AI Village được chốt lại như sau:

- **Antigravity CLI là Engine thực thi chính**, được ưu tiên hoàn thiện và kiểm chứng vận hành thật qua toàn bộ chuỗi tác vụ của hệ thống.
- **Google Gemini, OpenAI, Ollama và các API Provider khác** là khả năng mở rộng tùy chọn nối qua cùng Universal Engine Contract khi người vận hành có nhu cầu và cấp phép kết nối hợp lệ, không phải điều kiện chặn để hệ thống hoạt động.

## 2. Phân tách Tiêu chí `P02.06` trước và sau Phê duyệt

| Nội dung kiểm chứng | Tiêu chí ban đầu tại `094dd83` | Tiêu chí điều chỉnh Antigravity-first | Nơi lưu chuyển backlog nếu hoãn |
| :--- | :--- | :--- | :--- |
| Hợp đồng trung lập `engines/contract.py`, `engines/registry.py`, `engines/connections.py` | Bắt buộc triển khai | Giữ nguyên bắt buộc | Hoàn tất và giữ nguyên |
| Gọi Antigravity CLI thật xuyên suốt `EngineRegistry` | Bắt buộc `LIVE_VERIFIED` | Bắt buộc `LIVE_VERIFIED` cho cả text và tool execution `Gate B` | Hoàn tất và giữ nguyên |
| Phân biệt `agent_execution_platform` với `model_api`, chặn `TOOL_UNSUPPORTED_BY_PROVIDER` | Bắt buộc | Giữ nguyên bắt buộc và kiểm chứng `LIVE_VERIFIED` | Hoàn tất và giữ nguyên |
| Mã nguồn `GoogleGeminiAdapter`, BYOK multi-connection và unit tests | Bắt buộc | Giữ nguyên nguyên vẹn trong kho mã nguồn | Hoàn tất và giữ nguyên |
| Gọi HTTP live tới một API Provider thứ hai và switching live | Bắt buộc mới cho đóng `P02.06` | Tách thành hạng mục mở rộng tùy chọn khi có kết nối hợp lệ | Chuyển vào `P15.02` và đồng bộ `P13.08`, `P17.02` |

## 3. Ràng buộc Bảo toàn Bằng chứng và Mã nguồn

1. Giữ nguyên nguyên bản [PHASE_02_P02_06_AUDIT.md](PHASE_02_P02_06_AUDIT.md) ghi nhận kết luận `BLOCKED` cho lần gọi HTTP live của Gemini API tại commit `094dd83`.
2. Giữ nguyên toàn bộ mã nguồn [contract.py](../../engines/contract.py), [connections.py](../../engines/connections.py), [registry.py](../../engines/registry.py), [antigravity.py](../../engines/providers/antigravity.py) và [gemini.py](../../engines/providers/gemini.py).
3. Giữ nguyên hai bài kiểm thử live Gemini là `test_live_alternative_api_provider_gemini` và `test_live_provider_switching_without_core_changes` trong [test_engine_provider_contract_live.py](../../tests/integration/test_engine_provider_contract_live.py) ở trạng thái `SKIPPED` khi thiếu khóa API hợp lệ, không xóa bỏ và không làm giả trạng thái `PASSED`.
4. Không tạo thêm phase mới trong [docs/PROGRESS.md](../PROGRESS.md); kiểm chứng live API Provider thứ hai nằm trong hạng mục mở rộng sẵn có của `P15.02`, giao diện quản trị ở `P13.08` và cấu hình mẫu ở `P17.02`.

## 4. Kết luận Nghiệm thu `P02.06` theo Tiêu chí Mới

Căn cứ vào quyết định phê duyệt ngày 2026-11-24 kết hợp bằng chứng unit, integration và end-to-end mới trong [PHASE_02_GATE_B_AUDIT.md](PHASE_02_GATE_B_AUDIT.md), phần tiêu chí bắt buộc Antigravity-first của `P02.06` đạt trạng thái **`DONE`**, trong khi năng lực gọi HTTP live tới Model API thứ hai giữ trạng thái mở rộng tùy chọn **`NOT_VERIFIED` / backlog `P15.02`**.
