# ADR-0001: Lựa chọn Ngăn xếp Công nghệ Tạm thời (Temporary Technology Stack)

- **Mã quyết định:** ADR-0001
- **Ngày lập:** 2026-11-23
- **Trạng thái quyết định (Decision Status):** `PROPOSED_TEMPORARY` (Chấp nhận tạm thời cho Chặng A; cần xác nhận thực nghiệm qua Phase 02 và Phase 03 trước khi khóa kiến trúc dài hạn)
- **Phạm vi:** Backend Runtime, Lưu trữ cục bộ, Giao diện 2D/3D, Động cơ AI và Hạ tầng Kiểm thử.

## 1. Context (Bối cảnh)

AI Village là hệ thống Multi-Agent đa chức năng yêu cầu:

- Một lõi điều phối trạng thái chặt chẽ (`core/`), kiểm soát quyền hạn xác định (deterministic Policy Engine tại `core/governance/`), thực thi công cụ văn phòng/mã nguồn/nghiên cứu an toàn trong `sandbox/` và lưu trữ bền vững dữ liệu sự kiện/artifact tại `storage/`.
- Khả năng chạy cục bộ trên máy cá nhân (local-first) với chi phí thiết lập tối thiểu, dễ kiểm thử độc lập và không phụ thuộc vào một dịch vụ đám mây duy nhất.
- Một giao diện web 2D vận hành ổn định trước khi mở rộng sang không gian 3D phản chiếu sự kiện thực tế.
- Khả năng kết nối **Multi-Provider, Multi-Model, Multi-API-Key (BYOK) và Multi-Platform**: Antigravity không phải Engine độc quyền; các API Provider (như Google Gemini, OpenAI, Anthropic, OpenAI-compatible, Ollama/Local LLM) là nguồn thực thi song song thông qua lớp Engine Adapter trung lập, đồng thời hỗ trợ nhiều kết nối/API key cho cùng một Provider mà vẫn giữ cổng trừu tượng (`Engine Adapter` và `AgentRuntime`) để đánh giá framework điều phối như CrewAI mà không làm vỡ nghiệp vụ lõi.

Ở Chặng A, hệ thống chưa được phép lập trình mã nghiệp vụ lõi hay gọi Antigravity CLI trực tiếp. Do đó, dự án cần một ADR ghi nhận các lựa chọn công nghệ tạm thời làm nền tảng khởi tạo cấu trúc kho mã nguồn.

## 2. Decision (Quyết định tạm thời)

1. **Backend & Core Runtime:** Sử dụng **Python (3.10+, hiện có 3.13 trên môi trường cục bộ)** kết hợp **FastAPI** cho tầng `api/` và **SQLite** cho lưu trữ bền vững giai đoạn phát triển ban đầu tại `storage/`.
2. **Frontend (UI 2D & định hướng 3D):** Sử dụng **React** và **Vite** cho ứng dụng web tại `ui/`. Trong Chặng A và giai đoạn 2D đầu tiên tuyệt đối chưa tích hợp 3D. **Three.js** hoặc **React Three Fiber** được ghi nhận là hai ứng viên cho giai đoạn văn phòng 3D ở `PHASE 14`, chỉ đánh giá lựa chọn chính thức sau khi hoàn tất giao diện 2D tại Gate H (`M4`).
3. **AI Runtime, Multi-Provider & Orchestration:**
   - **Antigravity CLI là Engine thực thi chính, giữ mở đa Provider qua cùng hợp đồng:** Antigravity CLI là nền tảng thực thi chính được ưu tiên hoàn thiện và kiểm chứng trước; các API Provider như Google Gemini, OpenAI, Anthropic hay Ollama là khả năng mở rộng tùy chọn nối qua lớp **Engine Adapter trung lập** tại `engines/` khi có nhu cầu và kết nối được cấp phép.
   - **BYOK và Multi-Key là yêu cầu sản phẩm:** Hệ thống phải hỗ trợ nhiều kết nối (`connection_id`) và nhiều API key qua tham chiếu `credential_ref` cho cùng một Provider (như trường hợp hai kết nối Google Gemini), tách biệt với Agent Manifest và trình duyệt.
   - **Chưa khóa lựa chọn CrewAI, Framework Routing hoặc Credential Vault:** CrewAI chỉ là ứng viên cho tầng điều phối; quyết định sử dụng CrewAI hay tự xây orchestrator mỏng chỉ được đưa ra sau khi hoàn thành spike tích hợp và so sánh thực nghiệm tại `PHASE 03` (`P03.01`–`P03.02`). Tương tự, chưa tự chốt một thư viện routing hay công nghệ Credential Vault cụ thể trước khi đủ bằng chứng kiểm thử ở các phase phù hợp.
4. **Testing (Kiểm thử):**
   - Sử dụng **pytest** cho các bài kiểm thử Python (unit, contract, standards consistency, integration sau khi có runtime).
   - Thiết lập kiểm thử/build phù hợp cho frontend React/Vite (smoke build và sau này là browser E2E ở giai đoạn thích hợp).
   - Bổ sung contract tests cho giao tiếp `core/` <-> `engines/` <-> `tools/` và integration tests khi các module Runtime được xây dựng ở các phase sau.

## 3. Rationale (Lý do lựa chọn)

- **Python + FastAPI:** Hệ sinh thái Python vượt trội cho xử lý dữ liệu văn phòng (`openpyxl`, `python-docx`, `pandas`, `pypdf`), phân tích mã nguồn (`ast`, `ruff`, `pytest`), tích hợp AI/Agent và viết Policy Engine xác định rõ ràng. FastAPI cung cấp cơ chế kiểm tra hợp đồng dữ liệu chặt chẽ thông qua Pydantic v2, phù hợp với yêu cầu `AGENT_SPEC`, `TOOL_SPEC` và `RULE_SPEC`, đồng thời hỗ trợ tốt SSE/WebSocket cho Event Stream ở Phase 12.
- **SQLite:** Phù hợp mô hình local-first, không đòi hỏi cài đặt database server rời, hỗ trợ transaction, khóa ngoại (`PRAGMA foreign_keys = ON`) và chế độ WAL cho bài toán lưu trữ `tasks`, `runs`, `events`, `sessions`, `artifacts` trong `.runtime/`.
- **React + Vite:** Tách biệt hoàn toàn giao diện người dùng khỏi backend Python qua HTTP/SSE, khởi tạo nhanh, kiểm chứng build rõ ràng và cho phép tái sử dụng tư duy UI 2D hiện đại trước khi kết nối dữ liệu live.
- **Lớp Engine Adapter trung lập + Trì hoãn khóa CrewAI, Routing Framework & Credential Vault:** Bảo vệ `core/` khỏi sự phụ thuộc cứng vào định dạng API của một công cụ, nhà cung cấp hay thư viện điều phối bên ngoài. Chưa có bằng chứng thực nghiệm về cách Antigravity CLI quản lý đa phiên song song, streaming JSON hay hủy tiến trình, cũng như hành vi thực tế của các API Provider khi phối hợp Tool Runtime, nên mọi quyết định chốt framework điều phối, bộ định tuyến hay kho lưu khóa cụ thể trước các phase kiểm chứng đều là suy đoán thiếu căn cứ.

## 4. Alternatives Considered (Các phương án thay thế đã cân nhắc)

1. **Dùng Node.js / Vite middleware làm cả backend lẫn frontend (như Marketing Village):**
   - *Lý do chưa chọn làm lõi:* Gộp API và business logic vào cấu hình Vite gây lẫn ranh giới tầng; hệ sinh thái Node xử lý Excel công thức phức tạp, phân tích AST Python và kiểm thử tự động kém tự nhiên hơn Python khi hệ thống nhắm tới đa lĩnh vực (Code, Office, Research).
2. **Dùng PostgreSQL ngay từ đầu thay cho SQLite:**
   - *Lý do chưa chọn:* Tăng độ phức tạp cài đặt môi trường cục bộ ở giai đoạn đầu khi chưa có nhu cầu phân tán nhiều node. Có thể trừu tượng hóa qua tầng `storage/repositories/` để nâng cấp sau nếu cần.
3. **Khóa ngay CrewAI hoặc AutoGen cho tầng điều phối:**
   - *Lý do từ chối:* Vi phạm quy tắc bất biến tại `AGENTS.md` và `PROGRESS.md`. Hai framework này có cơ chế quản lý state, handoff và cancellation riêng dễ xung đột với mô hình kiểm soát quyền Fail-Closed và Event Journal của AI Village nếu chưa qua spike thực chứng.

## 5. Consequences (Hệ quả)

- Dự án có hai môi trường runtime rõ ràng: Python cho backend/core/engines/tools/storage/tests và Node.js (React + Vite) cho `ui/`.
- Tầng `core/` phải định nghĩa giao diện trừu tượng (`AgentRuntime` và `EngineAdapter`) độc lập với FastAPI, SQLite và CrewAI.
- Tài liệu và scaffold trong Chặng A không được cài đặt sẵn CrewAI hay các thư viện phục vụ Phase 02–17 đón đầu, cũng không được khởi tạo mã API FastAPI hoặc SQLite DB giả trước khi đến phase quy định.

## 6. Risks (Rủi ro kỹ thuật)

1. **Rủi ro từ Antigravity CLI chưa kiểm chứng:** Các khả năng như cờ lệnh thực tế của `agy`, chế độ `stream-json`, cô lập nhiều phiên logic đồng thời và đo lường token/quota chưa được kiểm chứng thực tế.
2. **Giới hạn ghi đồng thời của SQLite:** Khi chạy nhiều Agent song song trong các phase sau, SQLite có thể gặp khóa ghi (`database is locked`) nếu không cấu hình WAL, `busy_timeout` và hàng đợi ghi hợp lý.
3. **Độ trễ khi tích hợp hai tiến trình (Python backend + React frontend):** Cần giữ hợp đồng API/SSE gọn và thống nhất schema `snake_case` để tránh lệch dữ liệu giữa hai ngôn ngữ.
4. **Rủi ro chọn sai framework điều phối:** Nếu CrewAI không hỗ trợ tốt cancellation, event streaming chi tiết hoặc cô lập session, việc cố ép dùng sẽ làm phức tạp hóa `core/`.

## 7. Validation Required (Kiểm chứng bắt buộc ở các Phase sau)

| Mục tiêu cần kiểm chứng | Phase thực hiện | Tiêu chí xác nhận |
| :--- | :--- | :--- |
| **Antigravity CLI execution proof & Universal Engine Contract (`P02.01`–`P02.06`, `Gate B`)** | `PHASE 02` (ưu tiên Antigravity-first; API Provider thứ hai nằm ở `P15.02`) | Xác nhận lệnh `agy` thật hoạt động end-to-end qua Universal Engine Contract, tạo file thật có kiểm chứng SHA-256 độc lập, bắt exit code, timeout, win32 job cancel, session isolation và telemetry trung thực; giữ nguyên kiến trúc Multi-Provider và BYOK. |
| **Đánh giá CrewAI vs Custom Orchestrator** | `PHASE 03` (`P03.01`–`P03.03`) | Chạy spike Master + Worker qua Engine Adapter thật; lập ADR mới (`use_crewai` hoặc `custom_orchestrator`) dựa trên số liệu về session, handoff, cancel, recovery và quota. |
| **Độ bền vững SQLite & State Machine** | `PHASE 04` (`P04.01`–`P04.07`) | Kiểm chứng migrations, khóa ngoại, transaction, event replay và khôi phục trạng thái sau restart. |
| **Tích hợp FastAPI + React/Vite 2D end-to-end** | `PHASE 12`–`13` (`Gate H`) | Chạy trọn vẹn tác vụ live qua 8 phân khu giao diện 2D trước khi bắt đầu đánh giá Three.js / R3F cho `PHASE 14`. |

## 8. Ghi nhận Giới hạn Hiện tại

- Tài liệu này **không** tuyên bố Antigravity CLI hoặc bất kỳ API Provider nào đã được kiểm chứng thực tế.
- Tài liệu này **không** khóa cố định CrewAI, Framework routing, Credential Vault hay phương án đồ họa 3D.
- Chặng A **không** bắt đầu lập trình tích hợp Engine, FastAPI, Secret Manager hay SQLite runtime.
