# AI VILLAGE — PROGRESS & DEVELOPMENT ROADMAP

> **Trạng thái:** **CHẶNG A: DONE (`Gate A: PASS`)**. **PHASE 00: DONE**. **PHASE 01: DONE**. **PHASE 01B: DONE**. **PHASE 02: DONE (`Gate B: PASS`)** (`P02.01`–`P02.06` DONE theo định hướng Antigravity-first và biên bản điều chỉnh tại `docs/reviews/PHASE_02_P02_06_CRITERIA_ADDENDUM.md`; kiểm toán tổng hợp tại `docs/reviews/PHASE_02_GATE_B_AUDIT.md`, giữ nguyên lịch sử tại `docs/reviews/PHASE_02_P02_06_AUDIT.md`). **PHASE 03: DONE** (`P03.01` DONE tại `docs/reviews/PHASE_03_P03_01_AUDIT.md`; `P03.02` DONE với quyết định `custom_orchestrator` tại `docs/decisions/ADR-0002-orchestration-architecture.md` và `docs/reviews/PHASE_03_P03_02_AUDIT.md`; `P03.03` DONE tại `docs/reviews/PHASE_03_P03_03_AUDIT.md`). **PHASE 04: DONE (`Gate C: PASS`)** (`P04.01`–`P04.07` DONE, `P04.04` SHA `759558e`, `P04.05` SHA `57b9a44`, `P04.06` SHA `984f1c9`, `P04.07` SHA `37a3339`; nghiệm thu Gate C SHA `dd2de05` tại `docs/reviews/PHASE_04_GATE_C_AUDIT.md`). **PHASE 05: ĐANG TRIỂN KHAI** (`P05.01` DONE tại `docs/reviews/PHASE_05_P05_01_AUDIT.md`; `P05.02`–`P05.07` TODO). Chi tiết ghi nhận tại Mục 7 (Nhật ký thực thi).
>
> **Nguyên tắc:** Một tiểu phase = một thư mục có trách nhiệm rõ ràng **hoặc** một năng lực có thể kiểm thử độc lập. Không gom nhiều tính năng không liên quan vào một lượt làm việc.
>
> **Dự án mới (đề xuất):** `C:\DoCho\AI-Village`. **Không xóa, di chuyển hoặc sửa trực tiếp** `C:\DoCho\marketting` hay repository Multi-Agent-Pentest-System. Hai repo cũ chỉ là tài liệu và mã tham khảo được lựa chọn tái sử dụng sau khi kiểm tra.

## 0. Tuyên ngôn sản phẩm và tiêu chuẩn tiến độ

**AI Village** là hệ thống Multi-Agent đa chức năng dưới dạng môi trường làm việc trực quan. Người dùng giao việc cho **Master**; Master tự lập nhóm nhân viên chuyên môn, cho phép thảo luận/handoff trực tiếp, tự kiểm chứng bằng công cụ và tổng hợp sản phẩm cuối cùng. Phòng ban là **cách tổ chức năng lực**, không phải dây chuyền xử lý cố định. Một nhân viên có phòng ban chủ quản nhưng có thể tham gia nhiều nhóm dự án.

- Engine & Provider: **Antigravity CLI là Engine thực thi chính** được ưu tiên hoàn thiện và kiểm chứng vận hành thật qua Universal Engine Contract tại `engines/`. Thiết kế vẫn duy trì kiến trúc **Multi-Provider, Multi-Model, Multi-API-Key (BYOK) và Multi-Platform** để mở rộng tùy chọn sang Google Gemini, OpenAI, Ollama hoặc các API Provider khác qua `connection_id` và `credential_ref` khi có nhu cầu và kết nối được cấp phép (không mặc định hai key cùng Project có hai quota độc lập).
- Framework điều phối: **CrewAI là ứng viên**, chưa được khóa lựa chọn cho đến khi kiểm chứng adapter Antigravity, API Provider, quản lý phiên, sự kiện và handoff. Không được biến Antigravity thành một model giả chỉ trả JSON, và không giả định LLM API có sẵn quyền thao tác công cụ hệ thống mà không qua Tool Runtime.
- Chủ hệ thống quyết định quyền và các hành động quan trọng. Master điều phối; nhân viên được trao đổi ngang hàng trong phạm vi công việc.
- Bắt đầu với **một engine và nhiều session logic**; có thể tuần tự để kiểm soát quota. Đồng thời chỉ mở sau khi được kiểm thử.
- **Evidence first:** trạng thái, số liệu, báo cáo và hành động 3D dựa trên sự kiện thực; Agent không được tự tuyên bố PASS thay cho bộ kiểm chứng.
- **Tự cải tiến phương án C:** ghi nhận sau mỗi nhiệm vụ; chỉ phân tích/tạo Skill khi có đủ bằng chứng; kiểm thử trước khi kích hoạt.
- Giao diện 2D vận hành được trước; 3D được xây trên cùng mô hình sự kiện, không có logic nghiệp vụ riêng.
- Không lấy test mock làm bằng chứng nghiệm thu tác vụ live. Không tự sửa phiên bản production, thay đổi quyền hoặc chạy hành động có hậu quả lớn khi chưa được duyệt.

### Cách cập nhật file này

- `[ ]` TODO · `[x]` DONE (đã có bằng chứng) · `[!]` BLOCKED (ghi rõ lý do) · `[~]` IN PROGRESS.
- Chỉ đánh dấu DONE khi tiểu phase có **mã nguồn/tài liệu thực tế, test tương ứng và bằng chứng**.
- Sau mỗi tiểu phase: cập nhật `PROGRESS.md`, commit riêng, ghi `SHA`, test đã chạy, artifact/bằng chứng và nhiệm vụ kế tiếp.
- Một agent/công cụ không được tự cập nhật DONE chỉ vì nó nói “đã xong”.
- Khi chuyển phiên, đọc `PROGRESS.md` + `docs/decisions/` + Git diff, không kiểm toán lại mọi file vô ích.

### Mẫu ghi nhận sau mỗi tiểu phase

```text
ID: Pxx.yy
Status: TODO / IN_PROGRESS / BLOCKED / DONE
Scope: <1 folder hoặc 1 nhiệm vụ>
Git SHA:
Acceptance: <tiêu chí được kiểm chứng>
Tests / Evidence: <đường dẫn và kết quả>
Blocking issue:
Next:
```

## 1. Cấu trúc repository được đề xuất

```text
AI-Village/
├── README.md                 # Cách chạy, mô tả dự án
├── PROGRESS.md               # File duy nhất theo dõi roadmap và tình trạng
├── AGENTS.md                 # Quy tắc chung cho coding agent tham gia phát triển
├── .env.example              # Tên biến cấu hình, KHÔNG chứa secret
├── .gitignore
│
├── agents/                   # CHỈ định nghĩa agent, role, department và manifest
│   ├── master/               # Hồ sơ Master, quy tắc điều hành
│   ├── departments/          # Manifest 9 nhóm chức năng
│   ├── roles/                # Định nghĩa nhân viên, năng lực, tool permissions
│   └── registry/             # Schema/validation cho agent động
│
├── core/                     # CHỈ thuật toán và nghiệp vụ điều phối
│   ├── contracts/            # Data contracts & event schemas
│   ├── tasks/                # State machine, phụ thuộc nhiệm vụ
│   ├── events/               # Event bus, journal, subscribe
│   ├── sessions/             # Bối cảnh/session agent, lifecycle
│   ├── orchestration/        # Giao việc, team động, hội họp, handoff
│   ├── governance/           # Permissions, approvals, policy, audit
│   └── optimization/         # Phân bổ tài nguyên, học và tối ưu quy trình
│
├── engines/                  # Adapter LLM/Agent runtime, không chứa logic phòng ban
│   ├── antigravity/          # Adapter agy: spawn, session, stream, diagnostics
│   └── providers/            # Adapter API tùy chọn theo contract chung
│
├── tools/                    # Công cụ thực thi dùng chung (không chứa role/prompt)
│   ├── files/                # File operations theo workspace và quyền
│   ├── code/                 # Chạy code an toàn
│   ├── browser/              # Browser automation
│   ├── research/             # Thu thập và kiểm chứng nguồn
│   └── office/               # Word, Excel, PDF, data processing
│
├── skills/                   # Skill đã kiểm thử, ứng viên và quy tắc vòng đời
│   ├── approved/
│   ├── candidates/
│   └── schemas/
│
├── storage/                  # Mã truy cập SQLite, artifacts, memory, search index
│   ├── db/
│   ├── repositories/
│   ├── artifacts/
│   └── knowledge/
│
├── api/                      # HTTP + realtime events; không quyết định nghiệp vụ
├── ui/                       # Chat, dashboard, quản trị, 3D; app web riêng
│   ├── src/
│   │   ├── chat/
│   │   ├── dashboard/
│   │   ├── agents/
│   │   ├── meetings/
│   │   ├── vault/
│   │   ├── approvals/
│   │   └── village3d/
│   └── public/
├── sandbox/                  # Docker/isolated fixtures & sandbox policies
├── tests/                    # Unit / contract / integration / E2E / acceptance
├── docs/                     # Specs, ADRs, kiến trúc, hướng dẫn
│   ├── decisions/
│   ├── architecture/
│   └── operations/
│
├── workspace/                # Dữ liệu đầu vào & thư mục tạm THEO task (không commit)
├── output/                   # Sản phẩm bàn giao THEO task (không commit mặc định)
└── .runtime/                 # SQLite thật, log, cache, trace, session (gitignored)
```

**Không trùng trách nhiệm:** `agents/` chỉ mô tả *ai*; `core/` quyết định *ai làm gì và khi nào*; `engines/` gọi *bộ não nào*; `tools/` thực thi *hành động nào*; `skills/` lưu *cách làm được kiểm chứng*; `storage/` quản lý *dữ liệu bền vững*; `api/` vận chuyển dữ liệu; `ui/` hiển thị; `output/` chứa sản phẩm thật. Mã lõi **không import trực tiếp từ UI**.

**Quy tắc đặt tên:** thư mục và file Python `snake_case`, UI React `PascalCase` cho component, còn schema/JSON `snake_case` nhất quán. Không tạo các tên chung chung như `utils2`, `final_new`, `fix_all`; không để báo cáo và hình ảnh lẫn trong root.

## 2. Cấu trúc con người/nhân viên bên trong hệ thống

| Nhóm | Trách nhiệm | Quy tắc thiết kế |
| --- | --- | --- |
| **Master / Sếp** | Tiếp nhận mục tiêu, chọn đội, triệu tập họp khi cần, quyết định điều phối và bàn giao cuối | Không tự chấm chất lượng thay cho QA |
| **Nhân sự & Đào tạo** | Agent Registry, tuyển/đổi vai trò, đánh giá năng lực, đào tạo | Tạo agent logic, không mua tài khoản AI |
| **Tài nguyên & Tối ưu** | CPU/RAM, thời gian, quota/token, kế hoạch phân bổ, kiểm kê sau job | Thu thập số liệu bằng code trước; không bịa token usage |
| **Dịch vụ & Trải nghiệm** | Làm rõ yêu cầu, kiểm tra kết quả có phù hợp người dùng, xử lý feedback | Khác QA kỹ thuật |
| **Marketing & Sáng tạo** | Nghiên cứu thị trường, chiến lược, nội dung, sáng tạo | Là năng lực tùy chọn, không chi phối mọi task |
| **IT & R&D** | Code, công cụ, tích hợp, kiểm thử bản nâng cấp cách ly | Không tự cập nhật production |
| **Phản ứng nhanh / Lễ tân** | Nhận nhiệm vụ mới giữa chừng, nhận sự cố, tìm người xử lý, leo thang cho Master | Phân loại mức độ; không triệu tập toàn bộ phòng ban cho mọi tin nhắn |
| **Quy trình & Kỹ năng** | Ghi kinh nghiệm sau job, tìm điểm lặp, đề xuất/đánh giá Skill mới | Phương án C: tự động ghi, tối ưu khi đủ chứng cứ |
| **Vận hành & QA** | Quan sát tiến độ, kiểm thử, phản biện, xác minh bằng chứng | Độc lập khỏi agent sản xuất |
| **Kho & Tri thức** | Lưu bản bàn giao, version, nguồn, log, skill, lịch sử và tra cứu | Phân quyền và liên kết đúng task/run |

**Quy tắc hợp tác:** một agent có phòng ban chủ quản nhưng được tham gia nhóm liên phòng ban. Nhân viên được trao đổi trực tiếp có cấu trúc; chỉ Master ra quyết định thay đổi hướng lớn/bàn giao. Nhiệm vụ đơn giản không bắt buộc họp. Nhiệm vụ phức tạp có agenda, phản biện, quyết định, action items và giới hạn lượt họp.

### Mission Control & Functional Office — Cross-Phase UX Requirement

- **Mục tiêu:** Xây dựng môi trường điều hành hợp nhất giúp người vận hành giao việc, theo dõi checklist thực tế, giám sát Agent/Engine/Usage/Quota đa nhà cung cấp, nhận cảnh báo và ra quyết định tại Action Inbox, kiểm định QA, truy xuất và đối chiếu phiên bản tài sản, sau đó ánh xạ lên các đồ vật tương tác trong văn phòng 3D mà không phải lục từng thư mục hay log.
- **Nguyên tắc:** Mission Control không phải phòng ban thứ 10, không phải Agent mới, không phải Runtime thứ hai và không tự tạo số liệu giả (`null` giữ nguyên `Không có dữ liệu từ Provider`, không thay bằng `0`); mọi thông báo và thẻ tổng quan đều dẫn thẳng tới đối tượng gốc; UI chỉ hiển thị và gửi lệnh qua `api/` cùng Permission Manager.
- **Các thành phần chính (18 thành phần + Bàn Master):** Task Checklist Board, Kanban, Operations Monitor, Usage & Cost Control Center, Task Timing, Notification Center, Action Inbox, Scheduler, Meeting Room, QA Inspection Station, Artifact Cabinet, Comparison Board, Activity Journal, Engine Status Board, Provider & API Key Manager, Trash & Restore, Global Search, Settings & Governance Panel và Master Desk.
- **Nguồn dữ liệu:** Master, Task Runtime, Agent Registry, Engine Adapter, Tool Runtime, Event Journal, Resource Ledger, Notification System, Approval Center, Artifact Registry, QA Evidence và Provider Connection Registry.
- **Tài liệu chi tiết:** [docs/architecture/MISSION_CONTROL_UX.md](architecture/MISSION_CONTROL_UX.md) (kèm Mục 12 trong [docs/architecture/SYSTEM_BLUEPRINT.md](architecture/SYSTEM_BLUEPRINT.md)).
- **Phân bổ sở hữu theo Phase:** Dữ liệu nguồn từ `PHASE 02`–`11`, hợp đồng API/Realtime tại `PHASE 12`, giao diện 2D vận hành đầy đủ tại `PHASE 13` (đạt `Gate H` trước), ánh xạ vật thể 3D tại `PHASE 14`, kiểm thử tổng hợp tại `PHASE 15`–`16` và tài liệu vận hành tại `PHASE 17`.

---

## 3. ROADMAP — Phases nhỏ, có điểm nghiệm thu

### CHẶNG A — Chốt kiến trúc và tạo repository sạch

**PHASE 00 — Quy tắc nền tảng** *(không viết ứng dụng ở chặng này)*

- [x] **P00.01 — Đặc tả mục tiêu:** chốt phạm vi MVP, phần chưa làm, định nghĩa DONE/FAIL/BLOCKED và định hướng Multi-Provider/Multi-Model/Multi-Key (BYOK). **DoD:** `docs/architecture/vision.md` được duyệt *(SHA: `ac67b88`, `e4522aa`; đã được người vận hành xác nhận phê duyệt trực tiếp `"Oke duyệt"` ngày 2026-11-23)*.
- [x] **P00.02 — Danh mục ranh giới:** ghi rõ dữ liệu input/output, nơi lưu bí mật, quyền thực thi và các hành động cần người duyệt. **DoD:** `docs/architecture/boundaries.md` *(SHA: `ac67b88`)*.
- [x] **P00.03 — Kiểm kê mã cũ:** phân loại thành phần nào được tái sử dụng từ Marketing Village và AutoGen Pentest, không copy bừa. **DoD:** `docs/architecture/reuse_inventory.md` *(SHA: `ac67b88`)*.
- [x] **P00.04 — Chốt stack tạm thời:** Python/FastAPI + SQLite; React/Vite + Three.js hoặc React Three Fiber là ứng viên; ghi lý do, chưa khóa framework điều phối. **DoD:** một ADR trong `docs/decisions/` *(SHA: `ac67b88`)*.

**PHASE 01 — Khởi tạo cấu trúc** *(mỗi tiểu phase đúng một folder)*

- [x] **P01.01** — Tạo repo mới, root files (`README.md`, `PROGRESS.md`, `AGENTS.md`, `.gitignore`, `.env.example`) và commit khởi tạo *(SHA: `d1270da`, `caef2c1`, hoàn thiện `.env.example`, root `PROGRESS.md`, `AGENTS.md` và `.gitignore` tại `6fadd1c`)*.
- [x] **P01.02** — Tạo **`agents/`** với README quy định chỉ giữ agent manifests/definitions *(SHA: `29e0662`, `6fadd1c`)*.
- [x] **P01.03** — Tạo **`core/`** với README các nhóm thuật toán và dependency boundaries *(SHA: `29e0662`, `6fadd1c`)*.
- [x] **P01.04** — Tạo **`engines/`** với hợp đồng adapter tối thiểu, không tích hợp model vội *(SHA: `29e0662`, `6fadd1c`)*.
- [x] **P01.05** — Tạo **`tools/`** với quy tắc đăng ký tool và quyền *(SHA: `29e0662`, `6fadd1c`)*.
- [x] **P01.06** — Tạo **`skills/`** với schema/lifecycle rỗng *(SHA: `29e0662`, `6fadd1c`)*.
- [x] **P01.07** — Tạo **`storage/`** với phân tách mã persistence khỏi data runtime *(SHA: `29e0662`, `6fadd1c`)*.
- [x] **P01.08** — Tạo **`api/`** với contract cho client *(SHA: `29e0662`, `6fadd1c`)*.
- [x] **P01.09** — Tạo **`ui/`** với web app tối thiểu, chưa có 3D *(SHA: `29e0662`, `6fadd1c`)*.
- [x] **P01.10** — Tạo **`sandbox/`** và tài liệu cách ly *(SHA: `29e0662`, `6fadd1c`)*.
- [x] **P01.11** — Tạo **`tests/`** với test runner/fixture tối thiểu *(SHA: `29e0662`, `3d25cf5`, `6fadd1c`)*.
- [x] **P01.12** — Tạo **`docs/`** với nơi ADR, sơ đồ và vận hành *(SHA: `29e0662`, `6fadd1c`)*.
- [x] **P01.13** — Tạo **`workspace/`** (được gitignore; dùng cho input/scratch của task) *(SHA: `29e0662`, `6fadd1c`)*.
- [x] **P01.14** — Tạo **`output/`** (được gitignore; dùng cho artifact bàn giao) *(SHA: `29e0662`, `6fadd1c`)*.
- [x] **P01.15** — Tạo **`.runtime/`** (được gitignore; chứa state/log/cache/private data) *(SHA: `29e0662`, kiểm chứng cục bộ và `.gitignore` tại `6fadd1c`)*.

**PHASE 01B — Tiêu chuẩn và Kiến trúc** *(nhánh bổ sung ngoài lộ trình gốc PHASE 00–17 nhằm chuẩn hóa đặc tả trước khi lập trình)* — **PHASE 01B: DONE**

- [x] **P01B.01 — Đặc tả Agent:** Thiết lập 16 khía cạnh chuẩn mực định nghĩa Agent. **DoD:** `docs/standards/AGENT_SPEC.md`. (SHA: `715976b`)
- [x] **P01B.01-R — Rà soát chuẩn hóa Agent Spec:** Đồng bộ tên trường, bổ sung BLOCKED, quyền IT, telemetry. (SHA: `a7d377d`)
- [x] **P01B.02 — Đặc tả Phòng ban:** Thiết lập 8 thành phần phòng ban, 9 bộ phận mặc định và cơ chế mở rộng. **DoD:** `docs/standards/DEPARTMENT_SPEC.md`. (SHA: `8e828c3`)
- [x] **P01B.02A — Bản thiết kế kiến trúc toàn diện:** Single Source of Truth cho toàn bộ dự án. **DoD:** `docs/architecture/SYSTEM_BLUEPRINT.md`. (SHA: `78c8457`)
- [x] **P01B.02A-G — Khóa quy tắc kiến trúc:** Cập nhật thứ tự tham chiếu, quy trình kiểm tra 6 bước và bảo vệ định hướng dự án trong AGENTS.md. (SHA: `ea7a780`)
- [x] **P01B.03 — Đặc tả Kỹ năng:** Định nghĩa cấu trúc kỹ năng, schema đầu vào/đầu ra và vòng đời. **DoD:** `docs/standards/SKILL_SPEC.md`. (SHA: `e4f48e0`)
- [x] **P01B.04 — Đặc tả Quy tắc và Quản trị:** Thiết lập kiến trúc 8 lớp Rule System, schema, xử lý xung đột, Approval Gate và kịch bản kiểm chứng. **DoD:** `docs/standards/RULE_SPEC.md`. (SHA: `1678f1d`)
- [x] **P01B.05 — Đặc tả Công cụ:** Thiết lập kiến trúc Tool Runtime, Tool Manifest Schema, chuẩn ToolResult, phân định Tool ID vs Action ID và 12 kịch bản kiểm chứng. **DoD:** `docs/standards/TOOL_SPEC.md`. (SHA: `74a78d0`)
- [x] **P01B.06 — Tạo bộ Template chuẩn:** Khởi tạo 3 mẫu cấu hình tại `docs/templates/` (`agent/agent.yaml`, `department/department.yaml`, `skill/SKILL.md`). (SHA: `74c0b17`)
- [x] **P01B.07 — Kiểm toán và đồng bộ chéo toàn bộ Specification:** Xử lý 8 phát hiện bất nhất, kiểm thử `tests/standards/test_standards_consistency.py` (6/6 PASSED), xuất báo cáo `docs/reviews/PHASE_01B_AUDIT.md`.

**Gate A (`PASS`):** cấu trúc 14 thư mục, tài liệu nền tảng (`vision.md` đã được người vận hành duyệt `"Oke duyệt"`, `boundaries.md`, `reuse_inventory.md`, `ADR-0001`, `MISSION_CONTROL_UX.md`), bộ chuẩn `PHASE 01B`, UI scaffold và bộ kiểm thử nền tảng đã chạy thật, Git sạch, không có mã nghiệp vụ sai folder (`docs/reviews/CHANG_A_AUDIT.md`).

### CHẶNG B — Chứng minh bộ não thật, trước khi xây dân làng

**PHASE 02 — Antigravity Execution Proof** *(tạo nguồn dữ liệu đáng tin cậy cho Mission Control; chưa xây Dashboard trong phase này)*

- [x] **P02.01 — CLI handshake:** kiểm tra phiên bản/các cờ thực tế và gọi `agy` (`C:\Users\taine\.gemini\bin\agy.exe` v`1.5.14`) từ `engines/antigravity/`; ghi exit/status (`exit_code: 0`, `SUCCEEDED`, `conversation_id`, `VERIFIED_MEASURED` tokens) và engine availability phục vụ Engine Status về sau, không hardcode tên model *(chưa kiểm chứng tool execution, streaming `stream-json`, session isolation, cancel hay multi-provider; chi tiết tại `docs/reviews/PHASE_02_P02_01_AUDIT.md`)*. **DoD:** nhận phản hồi live có bằng chứng.
- [x] **P02.02 — Tool execution:** từ một workspace fixture, Agent đọc `package.json` (không đưa `verification_marker` vào prompt), tạo `proof.txt` mới, đọc lại và Python kiểm chứng độc lập trên đĩa khớp nội dung, tính toàn vẹn `package.json` và mã SHA-256 (`VERIFIED_PASS`, `actual_sha256 == expected_sha256`, chi tiết tại `docs/reviews/PHASE_02_P02_02_AUDIT.md`; `Tool events: NOT OBSERVABLE FROM CURRENT CLI OUTPUT`, chưa kiểm chứng `stream-json`, session isolation hay process cancellation). **DoD:** file và SHA đúng, không dựa vào lời AI.
- [x] **P02.03 — Streaming events:** chuyển CLI `stream-json` thành luồng NDJSON chuẩn hóa `AntigravityStreamEvent` trong `engines/antigravity/streaming.py` và `stream_task()`; quan sát thực thời `execution_started`, `tool_call_started`, `tool_call_completed` cho `read_file`/`write_file`, `text_delta`, `execution_completed` với `source='provider_cli'`, tách biệt sự kiện tiến trình `source='adapter'`, xử lý đường lỗi và kiểm chứng file/SHA-256 trên đĩa tại `docs/reviews/PHASE_02_P02_03_AUDIT.md` *(chưa kiểm chứng session isolation, full process cancel hay multi-provider; `tool_call_id` để `None` do CLI `1.5.15` chưa phát)*. **DoD:** quan sát được started/tool-used/completed/failed thực tế.
- [x] **P02.04 — Session isolation:** tạo hai phiên logic A/B kèm `session_id` tách biệt (`AntigravitySessionManager`), ràng buộc thư mục không lồng nhau ngoài `%TEMP%`, truyền `--conversation <id>` tường minh (không dùng `--continue`) và `--settings` với `permissions.deny` chặn đọc/ghi sang workspace anh em cùng `command(*)`, gắn `AntigravitySessionEvent` theo phiên, chạy song song A/B và kiểm chứng SHA-256 độc lập cho `proof_A.txt`, `proof_B.txt` cùng chặn truy cập `a_sentinel.txt` tại `docs/reviews/PHASE_02_P02_04_AUDIT.md` *(quản lý phiên trong bộ nhớ, chưa lưu SQLite của PHASE 04; chưa kiểm chứng process cancel đầy đủ của P02.05)*. **DoD:** có test cross-session isolation.
- [x] **P02.05 — Process lifecycle:** timeout, cancel, exit code, redacted stderr, quota/auth/rate-limit và trạng thái process thực tế. **DoD:** lỗi thật trả trạng thái chuẩn; không retry vô hạn. Chi tiết tại `docs/reviews/PHASE_02_P02_05_AUDIT.md`: chuẩn hóa `ProcessLifecycleState`, triển khai `AntigravityProcessManager` tại `engines/antigravity/lifecycle.py` và `Win32JobGuard` dùng `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` cho từng `execution_id`/`pid`, chứng minh real cancellation `CANCELLED`, cross-session isolation hủy A giữ nguyên B, controlled timeout `TIMED_OUT`, phân loại `UNSUPPORTED_ARGUMENT` live và `AUTH_ERROR`/`QUOTA_EXCEEDED`/`RATE_LIMITED`/`retry_after_seconds` unit; đạt 5/5 live tests và unit suite không hồi quy; giới hạn là chưa cố ý kích hoạt `AUTH_ERROR`/`QUOTA_EXCEEDED`/`RATE_LIMITED` hay treo inference trên tài khoản live.
- [x] **P02.06 — Provider contract:** triển khai Universal Engine Contract trong `engines/` gồm `EngineRequest`, `EngineResult`, `EngineEvent`, `EngineTelemetry`, `EngineCapabilities`, `EngineRegistry`, `ConnectionRegistry`, `EnvCredentialResolver`, `verify_engine_artifact()`, `AntigravityEngineAdapter` và `GoogleGeminiAdapter`, tách biệt `agent_execution_platform` với `model_api` và không sửa `core/`. **DoD (Antigravity-first đã phê duyệt 2026-11-24 tại `docs/reviews/PHASE_02_P02_06_CRITERIA_ADDENDUM.md`):** chứng minh Antigravity chạy thật qua hợp đồng chung, chặn Fail-Closed `TOOL_UNSUPPORTED_BY_PROVIDER` cùng `DATA_BOUNDARY_VIOLATION` và bảo toàn kiến trúc đa kết nối BYOK; kiểm chứng HTTP live tới API Provider thứ hai như Gemini/OpenAI/Ollama là hạng mục mở rộng tùy chọn chuyển sang `P15.02` (bảo toàn lịch sử `BLOCKED` tại `docs/reviews/PHASE_02_P02_06_AUDIT.md` và hai bài live Gemini đang `SKIPPED`). Chi tiết tổng hợp tại `docs/reviews/PHASE_02_GATE_B_AUDIT.md`.

**PHASE 03 — Quyết định CrewAI**

- [x] **P03.01 — Spike tích hợp:** một Master + một Worker gọi Engine Adapter (Antigravity hoặc API Provider hỗ trợ Tool Runtime), không chỉ gọi model trả văn bản. **DoD:** Worker tạo artifact thật (`docs/reviews/PHASE_03_P03_01_AUDIT.md`).
- [x] **P03.02 — So sánh:** đánh giá CrewAI Crew/Flow so với orchestrator mỏng về session, handoff, cancel, khôi phục, event streaming, routing policy, quota, liên kết Task/Agent/Run, truy vết công cụ, báo cáo lỗi và khả năng cung cấp dữ liệu quan sát cho UI (không chọn chỉ vì dễ tạo nhiều Agent). **DoD:** ADR `use_crewai` hoặc `custom_orchestrator` có bằng chứng (`custom_orchestrator` tại `docs/decisions/ADR-0002-orchestration-architecture.md` và `docs/reviews/PHASE_03_P03_02_AUDIT.md`).
- [x] **P03.03 — Khóa abstraction:** core gọi `AgentRuntime` interface, không phụ thuộc trực tiếp API nội bộ của CrewAI hoặc một framework routing cụ thể. **DoD:** unit test fake adapter và một live smoke. *(DONE: `AgentRuntime` Protocol, `AgentWorkflowRunner` và `CustomAgentRuntime` kiểm chứng qua 5 unit tests và 1 live Antigravity CLI smoke test tại `docs/reviews/PHASE_03_P03_03_AUDIT.md`.)*

**Gate B (`PASS`):** đã kiểm chứng trọn vẹn chuỗi End-to-End thật `Ứng dụng -> EngineRegistry -> Universal Engine Contract -> AntigravityEngineAdapter -> agy.exe (v1.5.15) -> thao tác read_file/write_file thật -> tạo proof.txt -> verify_engine_artifact() kiểm tra độc lập nội dung và SHA-256` (`req-ea47a176818c`, khớp `866664c9a1e95842c185b0b8d849dd61ee5fb41b03096f698973a520e1aecaf0`, tách biệt `business_verification_status='UNVERIFIED'` khỏi `VERIFIED_PASS`, phát hiện tệp sai SHA-256 trả `VERIFIED_FAIL`, và chặn Fail-Closed `TOOL_UNSUPPORTED_BY_PROVIDER` khi gọi Gemini Model API). Đối soát toàn bộ `P02.01`–`P02.06` tại `docs/reviews/PHASE_02_GATE_B_AUDIT.md`. **Đủ điều kiện chuyển tiếp sang `PHASE 03` khi có lệnh.**

### CHẶNG C — Lõi quản lý nhiệm vụ và trạng thái

**PHASE 04 — Contracts & Task Runtime** *(nền tảng dữ liệu lõi cho Mission Control)*

- [x] **P04.01** — Định nghĩa `Task`, `Run`, `Agent`, `Session`, `Artifact`, `Event`, `ConnectionRef` cùng các tham chiếu nền tảng cho Project grouping (`project_id` mang tính tổ chức, không phải Runtime thứ hai), `ChecklistItem` và `NotificationRef` trong `core/contracts/` *(DONE: bộ Core Contracts tại `core/contracts/` kiểm chứng offline qua 17 ca unit test tại `tests/core/test_p04_01_core_contracts_unit.py` và kiểm toán tại `docs/reviews/PHASE_04_P04_01_AUDIT.md`; không dồn logic chi tiết mọi miền vào P04.01; không lưu raw API key; chưa triển khai State Machine, DB hay Event Journal)*.
- [x] **P04.02** — Task state machine: QUEUED → RUNNING → WAITING/FAILED/COMPLETED/CANCELLED/BLOCKED; chuyển trạng thái có guard bảo đảm Kanban, Checklist và Timing hiển thị trung thực từ timestamp gốc (không ETA giả). *(DONE: triển khai `core/tasks/` gồm ma trận Task/Run độc lập, guards fail-closed, QA completion gate, phát `Event` trong bộ nhớ và projections Kanban/Checklist/Timing tại `tests/core/test_p04_02_task_state_machine_unit.py` cùng biên bản `docs/reviews/PHASE_04_P04_02_AUDIT.md`; chưa triển khai SQLite hay Event Journal persistence).*
- [x] **P04.03** — Event journal: event_id, task_id, run_id, session_id, actor_id, timestamp, loại sự kiện, `provider_id`/`connection_id`/`model_id`, `artifact_id`/`evidence_id`, payload được lọc secrets; replay được làm nguồn chính cho Activity Timeline. *(DONE in-memory: triển khai `core/events/` với `InMemoryEventJournal` append-only, `sequence_number` đơn điệu, chống trùng ID, batch atomicity, replay xác định, `ActivityTimelineProjection` và bổ sung `run.status_changed`; kiểm chứng tại `tests/core/test_p04_03_event_journal_unit.py` và `docs/reviews/PHASE_04_P04_03_AUDIT.md`; lưu bền vững SQLite thuộc `P04.04`–`P04.05`.)*
- [x] **P04.04** — SQLite migrations trong `storage/db/`, kiểm foreign keys, transaction, bảng lưu checklist/history/notification metadata và connection metadata tách khỏi secret thô *(DONE: triển khai `storage/db/` với kết nối `PRAGMA foreign_keys=ON`, giao dịch `BEGIN IMMEDIATE`/`SAVEPOINT`, runner migration `0001_initial.sql` không dùng `executescript()` hỗ trợ rollback toàn phần, bảng khớp Core Contracts và triggers chặn `UPDATE`/`DELETE` trên `events`; kiểm chứng tại `tests/storage/test_p04_04_sqlite_migrations.py` và `docs/reviews/PHASE_04_P04_04_AUDIT.md`.)*
- [x] **P04.05** — Repository task/run/checklist/event/session/connection metadata trong `storage/repositories/`; restart không mất run và tái dựng được trạng thái Task cùng các bước của nó từ dữ liệu thật *(DONE: triển khai `storage/repositories/` gồm Protocol interfaces, `SQLiteRepositoryStore`, `SQLiteEventJournal`, `PersistentWorkflowService` cam kết Task/Run/Event trong một transaction `BEGIN IMMEDIATE` kèm khóa lạc quan `version`, và `recover_task`/`recover_run` tái dựng từ `baseline_json` + phát hiện `RUNNING` cần reconciliation; kiểm chứng tại `tests/storage/test_p04_05_persistent_repositories.py`, `tests/storage/test_p04_05_persistence_acceptance.py` và `docs/reviews/PHASE_04_P04_05_AUDIT.md`.)*
- [x] **P04.06** — Cancellation/timeout, fallback hợp lệ theo chính sách và idempotency trên core; bảo đảm UI không bao giờ hiển thị `RUNNING` nếu tác vụ đã dừng *(DONE: triển khai `core/tasks/lifecycle.py`, `core/tasks/fallback.py`, `0002_workflow_lifecycle.sql`, `SQLiteLifecycleRepository`, `OwnedSubprocessHandle` và `AntigravityOwnedExecutionHandle` tại `engines/antigravity/lifecycle.py`; kiểm chứng tại `tests/core/test_p04_06_workflow_lifecycle_unit.py`, `tests/integration/test_p04_06_workflow_lifecycle.py` và `docs/reviews/PHASE_04_P04_06_AUDIT.md`.)*
- [x] **P04.07** — Governance: workspace scopes, tool allowlist, provider/connection routing policy, approval gate và permission cho mọi thao tác từ Mission Control (kể cả chuyển trạng thái Kanban, xóa hàng loạt, sửa kết nối, chuyển provider đổi vùng dữ liệu/vượt ngân sách), audit và Fail-Closed *(đã hoàn thành; bằng chứng chi tiết tại `docs/reviews/PHASE_04_P04_07_AUDIT.md`)*.

**Gate C: PASS (Offline End-to-End Verified)** — một task đơn chạy, tạm dừng/khôi phục/hủy đúng; event, checklist và artifact traceable; sau restart tái dựng đúng trạng thái từ dữ liệu thật, không giả trạng thái *(bằng chứng kiểm toán tại `docs/reviews/PHASE_04_GATE_C_AUDIT.md`)*.

### CHẶNG D — Agent động và phối hợp thông minh

**PHASE 05 — Hồ sơ nhân viên và Master**

- [x] **P05.01** — Agent manifest schema: name, role, department, capabilities, engine (hỗ trợ trường tùy chọn `provider_id`, `adapter_id`, `connection_id`, `model_id`, `routing_profile`, `fallback_policy`; cấm raw API key), permissions, memory scope *(DONE: chuẩn hóa `AgentManifest`, `DepartmentManifest`, bộ nạp YAML an toàn chống khóa trùng/tag độc hại/symlink escape tại `core/agents/`, tạo `agents/master/agent.yaml` và `agents/roles/it_artifact_worker/agent.yaml`, ánh xạ thuần sang `AgentIdentity` và `SpikeAgentSpec`; kiểm chứng tại `tests/agents/test_p05_01_agent_manifest.py` và `docs/reviews/PHASE_05_P05_01_AUDIT.md` — chưa triển khai Registry hoặc khởi chạy Runtime)*.
- [ ] **P05.02** — Agent Registry: create/update/disable, version và validation; cung cấp dữ liệu Registry tách biệt rõ với Runtime State đang chạy cho Agent Explorer (chưa sửa production runtime).
- [ ] **P05.03** — Master Agent: hiểu yêu cầu, tạo và quản lý kế hoạch nhiệm vụ dạng Checklist có cấu trúc (gồm các bước và Agent phụ trách), xác định output/acceptance, chọn người cần tham gia và đề xuất routing phù hợp trong phạm vi Governance cho phép.
- [ ] **P05.04** — Team Builder: tạo team động từ capability thay vì danh sách phòng ban cứng.
- [ ] **P05.05** — Worker session lifecycle: mỗi nhân viên có session/context riêng ngay cả khi dùng chung Provider/Connection; phân biệt rõ Agent có trong Registry với Agent thực sự đang chạy.
- [ ] **P05.06** — Handoff giữa worker: liên kết chặt Task, Run, Artifact, decision và bằng chứng; không truyền nguyên chat quá dài.
- [ ] **P05.07** — Agent tuyển mới: đề xuất role, công cụ, quyền và đánh giá; chỉ kích hoạt khi hợp lệ.

**PHASE 06 — Họp và phản biện**

- [ ] **P06.01** — Agenda & participant selection: chỉ gọi người liên quan; nhiệm vụ đơn giản được phép bỏ họp (không dựng hội thoại giả).
- [ ] **P06.02** — Phản biện có cấu trúc: proposal, objection, evidence references, uncertainty, action items phục vụ Meeting Timeline.
- [ ] **P06.03** — Giới hạn lượt và chống đồng thuận giả/vòng tranh luận vô tận.
- [ ] **P06.04** — Quyết định của Master: lưu Decision Records với lý do hiển thị được cho người dùng (user-visible rationale) và tiếp tục công việc; không thay thế QA.
- [ ] **P06.05** — Worker-to-worker collaboration & handoff history; xử lý khi hai worker tranh chấp cùng file/task.
- [ ] **P06.06** — Bàn giao Master: tổng hợp Master delivery summary kèm artifact cuối, các giới hạn và link tải; thiếu bằng chứng thì NOT VERIFIED.

**Gate D:** nhiệm vụ cần 2 Agent có một cuộc họp thật (không phải đoạn văn dựng sẵn), handoff có định danh, QA tìm được lỗi được sửa trước khi Master bàn giao.

### CHẶNG E — Các năng lực dùng chung và kho lưu trữ

**PHASE 07 — Tools độc lập với phòng ban**

- [ ] **P07.01** — File tools: read/write/list, path scope, không ghi đè ngoài ý muốn; ghi Tool Execution Records với input/output metadata đã lọc secret.
- [ ] **P07.02** — Code tools: chạy test/script trong sandbox, lưu stdout/stderr có kiểm soát, đo execution duration và exit/error state.
- [ ] **P07.03** — Browser tools: mở web, chụp ảnh, ghi hành động thật theo phiên được cấp quyền, xuất evidence references.
- [ ] **P07.04** — Research tools: truy xuất web/API và lưu URL, thời điểm, bằng chứng, ranh giới dữ liệu.
- [ ] **P07.05** — Office tools: đọc/ghi Word, Excel, PDF và tính toán bằng code.
- [ ] **P07.06** — Tool Registry & Tool Usage Summary: khả năng, quyền, chi phí, inputs/outputs và tổng hợp lượt dùng công cụ theo Task/Agent (không log nội dung bí mật, không cài công cụ ngẫu nhiên vào host).

**PHASE 08 — Storage, Vault & Knowledge**

- [ ] **P08.01** — Artifact Registry: task/run/owner/version/hash/content type/đường dẫn cùng lịch sử phiên bản (`artifact version history`).
- [ ] **P08.02** — Kho sản phẩm: lưu, tìm lại, cung cấp khả năng xem trước theo loại file (`file preview capability`) và cung cấp comparison metadata (không tạo diff giả bằng LLM); không nhân bản tùy tiện.
- [ ] **P08.03** — Kho bằng chứng: QA logs, evidence links, nguồn research, trace, build/test phục vụ truy vết QA Inspection Station.
- [ ] **P08.04** — Knowledge retrieval & Metadata search: tìm kiếm theo metadata có phân quyền trước, semantic search chỉ thêm khi có hạ tầng phù hợp.
- [ ] **P08.05** — Backup/restore, retention, trash & restore, safe bulk actions (kiểm tra kỹ tình huống dữ liệu dùng chung giữa nhiều Task và bảo vệ file tham chiếu).

**Gate E:** một nhiệm vụ mới sử dụng lại được artifact/tri thức cũ đúng phiên bản, tạo DOCX/XLSX thật và tải về qua API; xóa/restore không mất file của task khác.

### CHẶNG F — Chuyển 9 bộ phận từ ý tưởng thành chức năng

**PHASE 09 — Department Behaviors** *(mỗi tiểu phase một bộ phận, chỉ xây chức năng cốt lõi trước; giữ nguyên 9 phòng ban và không đặt logic Mission Control vào riêng một phòng ban)*

- [ ] **P09.01 — HR:** quản lý agent, thay role, đề xuất tuyển người, cấp kỹ năng và cung cấp Agent status/capability metadata cho Explorer.
- [ ] **P09.02 — Resources:** đo CPU/RAM/time/token nếu engine trả số liệu; theo dõi ngân sách, chi phí và trạng thái quota theo Provider, Project/Billing Scope, `connection_id` và `model_id` (đặt `null` + `UNAVAILABLE_FROM_PROVIDER` nếu thiếu; không mặc định 2 key có 2 quota riêng); đề xuất phân bổ và kiểm kê sau run *(chưa triển khai)*.
- [ ] **P09.03 — Service/UX:** làm rõ yêu cầu, nhận phản hồi người dùng (feedback/chỉnh sửa), so sản phẩm với brief.
- [ ] **P09.04 — Marketing:** một skill nghiên cứu/lập kế hoạch/content có đầu ra thật, không hardcode mọi task.
- [ ] **P09.05 — IT/R&D:** tạo project/code và kiểm thử trong workspace cách ly; không tự thay mã production.
- [ ] **P09.06 — Reception:** lắng nghe yêu cầu mới/sự kiện phát sinh, định tuyến hoặc báo động lên Master; đóng góp phân loại cảnh báo, thông báo và đầu việc cho Action Inbox.
- [ ] **P09.07 — Process/Skills:** ghi nhận lỗi, công cụ, thời gian và lịch sử cải tiến sau mỗi task, đề xuất skill khi gặp mẫu lặp.
- [ ] **P09.08 — Operations/QA:** kiểm chứng độc lập, xuất trạng thái xác minh và evidence (`PASS`/`FAIL`/`UNVERIFIED`), cảnh báo lỗi và từ chối PASS thiếu chứng cứ.
- [ ] **P09.09 — Vault:** luồng nộp tài liệu, gắn metadata, quản lý dữ liệu hồ sơ/phiên bản, truy xuất và giao lại artifact cũ.

**Gate F:** giao 3 đề bài khác loại (code / research / Office), Master huy động nhóm khác nhau và tạo sản phẩm thật. Không yêu cầu mọi phòng ban cùng tham gia.

### CHẶNG G — Tự học, phản ứng nhanh và phân bổ tài nguyên

**PHASE 10 — Continuous Improvement (phương án C)**

- [ ] **P10.01** — Mỗi task tạo bản tóm tắt thực thi bằng dữ liệu: steps, errors, fixes, tools, time, source, cost nếu biết, kèm skill usage history.
- [ ] **P10.02** — Tìm pattern từ nhiều task: lỗi lặp, thao tác dư, kỹ năng thiếu và đề xuất cải tiến (suggested improvements) dựa trên dữ liệu kiểm tra được.
- [ ] **P10.03** — Skill candidate: điều kiện dùng, inputs/outputs, rủi ro, thử nghiệm và version.
- [ ] **P10.04** — IT + QA kiểm chứng candidate bằng benchmark A/B trên fixture và lưu benchmark status minh bạch.
- [ ] **P10.05** — Master đề xuất kích hoạt (pending skill approvals); người dùng duyệt khi skill thay quyền, code hoặc workflow nhạy cảm; lưu improvement outcomes và rollback được.

**PHASE 11 — Interrupts & Resources**

- [ ] **P11.01** — Nhận yêu cầu mới khi task cũ đang chạy, không mất dữ liệu/nhầm conversation.
- [ ] **P11.02** — Phân loại mức khẩn, tác động, phát sinh notification theo chính sách severity & deduplication (không tạo thông báo lặp vô hạn) và lựa chọn Agent cần hỏi; khẩn cấp báo Master.
- [ ] **P11.03** — Scheduler có giới hạn concurrency/rate/quota theo từng phạm vi kết nối hợp lệ, hỗ trợ hàng đợi, hiển thị task queue visibility, active execution tracking và scheduled task metadata cho Lịch làm việc thật (cấm xoay vòng key để né giới hạn Provider) *(chưa triển khai)*.
- [ ] **P11.04** — Resource ledger: thời gian, bộ nhớ, chi phí báo cáo được gắn `provider_id`/`connection_id`/`model_id`, phát budget warnings và quota alerts; ước lượng phải có nhãn *(chưa triển khai)*.
- [ ] **P11.05** — Sau task, tài nguyên được dọn an toàn, kiểm kê và gửi dữ liệu học hỏi cho Process.

**Gate G:** đang xử lý task A, người dùng bổ sung task B; Reception tiếp nhận, Master quyết định ưu tiên; task A không bị ghi đè; nhiệm vụ hoàn thành có đánh giá tài nguyên và cải tiến.

### CHẶNG H — Giao diện thật, sau đó mới 3D

**PHASE 12 — API & Realtime** *(cung cấp DTO và read model thống nhất cho Mission Control, tránh duplicate logic)*

- [ ] **P12.01** — HTTP endpoints / unified read models: overview, projects/tasks/checklist, activity timeline, notifications/action inbox, agents/runtime status, meetings, resource usage, provider connections, artifacts/versions/comparison/preview, trash/restore, approvals, skills, QA evidence và global search.
- [ ] **P12.02** — SSE/WebSocket event stream dựa trên event journal (có reconnect, dedupe và đồng bộ snapshot với event stream).
- [ ] **P12.03** — API auth/local policy: mọi hành động nhạy cảm qua Permission Manager và Approval Gate; không công khai quyền tool, `credential_ref` nội bộ hoặc secrets sang browser; endpoint cấu hình Connection chỉ trả tên, trạng thái và chuỗi nhận diện đã che *(chưa triển khai)*.

**PHASE 13 — Giao diện 2D vận hành trước** *(Notifications và Global Search là năng lực dùng chung của App Shell; Mission Control Home tổng hợp từ cùng nguồn dữ liệu)*

- [ ] **P13.01** — App shell + navigation: Mission Control Home (7 khu vực Overview, My Tasks, Resource Snapshot, Action Required, Recent Deliveries, Activity Timeline, Global Search), thanh thông báo dùng chung, truy cập cài đặt hợp lệ, responsive trên màn hình lớn và nhỏ.
- [ ] **P13.02** — Chat với Master: chat gắn Task/Project, attachment, plan display, stream, interrupt, cancel; lịch sử đúng phiên và deep-link thẳng tới Checklist, Artifact và QA.
- [ ] **P13.03** — Task dashboard: Task Checklist Board (số bước/tổng bước, không suy đoán `%`), Kanban đồng bộ runtime (kéo/thả theo guard), Task Timing (tính từ timestamp gốc), Activity Journal gốc, Scheduler view (chỉ khi có Scheduler thật), QA summary, log và retry/failure display.
- [ ] **P13.04** — Agent & department explorer: chọn nhân viên xem capabilities, department, runtime status thực tế (tách với registry), model/provider metadata hợp lệ, Engine/Session status (không báo `Online` giả) và các Task liên quan.
- [ ] **P13.05** — Meeting room 2D: ai tham gia, agenda, transcript/events, ý kiến, phản biện, quyết định Master, bằng chứng và action items.
- [ ] **P13.06** — Approval center & Action Inbox: tách biệt thông báo đọc với hộp thư việc cần quyết định (duyệt lệnh, câu hỏi Master, thiếu file, QA fail, thiếu cấu hình), risk summary, thao tác approve/reject và decision history.
- [ ] **P13.07** — Vault UI (Artifact Cabinet): tìm/lọc/xem trước theo định dạng/tải artifact theo task và phiên bản, QA inspection & evidence links, Comparison board (diff xác định/metadata, không LLM bịa diff), Trash & restore kèm xử lý hàng loạt có xác nhận và bảo vệ tệp dùng chung.
- [ ] **P13.08** — Resource, connection & incident dashboard: Usage Monitor & Cost (tách nguồn dữ liệu, giữ nguyên `null` / `Không có dữ liệu từ Provider`), Provider/Connection BYOK manager cho phép thêm, đặt tên, bật/tắt và kiểm tra kết nối mở rộng tùy chọn như Gemini/OpenAI/Ollama bên cạnh Engine chính Antigravity (không lộ key thô hay `localStorage`), Engine status, incident history, resource warnings và bảng cài đặt hợp lệ *(chưa triển khai)*.

**Gate H:** toàn bộ một nhiệm vụ đa agent chạy **qua UI**, dùng engine thật, thao tác được, có kết quả tải được và có thể can thiệp giữa chừng; người dùng theo dõi được từ lúc giao việc đến khi hoàn thành, truy cập checklist, thông báo, usage, QA và artifact mà không cần tra cứu thủ công trong log hoặc các thư mục hệ thống. Nếu chưa qua gate này thì 3D chỉ được làm prototype tách biệt.

**PHASE 14 — Thế giới văn phòng 3D** *(chỉ ánh xạ vật thể lên các bảng 2D đã có, không chứa logic nghiệp vụ riêng)*

- [ ] **P14.01** — 3D scene shell + camera + di chuyển cơ bản + chế độ chuyển đổi 2D/3D; không gắn logic AI vào animation.
- [ ] **P14.02** — Sơ đồ phòng ban từ Agent Registry, không hardcode phòng ban trong scene.
- [ ] **P14.03** — Avatar/nhân viên từ dữ liệu Agent Registry, trạng thái idle/running/blocked/in_meeting gắn sự kiện thật (không tự tạo hoạt ảnh đang làm việc giả).
- [ ] **P14.04** — Phòng họp: sự kiện họp thật quyết định nhân vật tham gia và trạng thái Bàn họp (`mc.meeting_room`).
- [ ] **P14.05** — Đồ vật tương tác & liên kết 2D: ánh xạ đủ 18 thành phần chức năng (`Bảng trắng`, `Bảng ghim công việc`, `Màn hình trung tâm`, `Màn hình thống kê`, `Đồng hồ treo tường`, `Chuông/đèn cảnh báo`, `Khay thư`, `Lịch treo tường`, `Bàn họp`, `Bàn kiểm định`, `Tủ hồ sơ`, `Bảng đối chiếu`, `Sổ nhật ký`, `Tủ máy chủ`, `Bảng cấu hình`, `Thùng rác`, `Bảng tra cứu`, `Bàn Master`); click vật thể/nhân viên/phòng chỉ xác định `component_id`/`action_id` để mở bảng 2D tương ứng dùng chung API/Store và Permission Manager.
- [ ] **P14.06** — Kiểm hiệu năng, giảm tải GPU và fallback sang UI 2D khi cần (không ép buộc dùng 3D cho bất kỳ tác vụ cốt lõi nào).

**Gate I:** 3D phản chiếu cùng event/session/object state với UI 2D; reload đồng nhất; không dựng hoạt cảnh hoặc tiến độ “đang làm” giả; không có logic nghiệp vụ riêng trong 3D.

### CHẶNG I — Vận hành, nghiệm thu và bàn giao

**PHASE 15 — Quality & Reliability**

- [ ] **P15.01** — Unit/contract tests: event schemas, checklist/kanban mapping, task state, role/permission.
- [ ] **P15.02** — Integration: CLI/API/multi-provider/multi-connection/mock, fallback hợp lệ, xử lý `TOOL_UNSUPPORTED_BY_PROVIDER`, two-key Gemini không giả định quota độc lập, session isolation, cancellation, restart/reconnect không sai dashboard; tiếp nhận backlog kiểm chứng HTTP live và chuyển đổi live tới API Provider thứ hai như Google Gemini (`test_live_alternative_api_provider_gemini`, `test_live_provider_switching_without_core_changes`), OpenAI hoặc Ollama khi có kết nối hợp lệ *(chưa triển khai)*.
- [ ] **P15.03** — Fault injection: timeout, quota, network, DB rollback, file shared, agent crash, không rò rỉ hoặc sai lệch notification/dedupe.
- [ ] **P15.04** — Browser E2E: Mission Control Home, chat, checklist, kanban, usage (không bịa số liệu / `null` giữ nguyên), notifications dẫn đúng đối tượng, action inbox, meeting, QA evidence liên kết đúng artifact version, comparison, global search tuân quyền, trash/restore & batch delete có xác nhận không mất file Task khác, 2D/3D đồng bộ.
- [ ] **P15.05** — Security: workspace isolation, secret leakage (bao gồm credential BYOK không lọt vào manifest/log/API/browser), unsafe command permission, backup/restore *(chưa triển khai)*.

**PHASE 16 — Nghiệm thu nghiệp vụ live**

- [ ] **P16.01 — Coding:** yêu cầu tạo ứng dụng nhỏ → IT code thật → QA thử → Master bàn giao source và log.
- [ ] **P16.02 — Office:** cung cấp Excel lớn → tính bằng code → xuất Word/Excel → đối chiếu giá trị độc lập.
- [ ] **P16.03 — Research:** câu hỏi mới đa lĩnh vực → tìm nguồn thật → trích dẫn có locator → bàn giao.
- [ ] **P16.04 — Họp & phản biện:** hai Agent nêu bất đồng thực chất và xử lý bằng bằng chứng.
- [ ] **P16.05 — Phản ứng nhanh:** bổ sung yêu cầu giữa lúc chạy → Reception báo đúng người → Master quyết định.
- [ ] **P16.06 — Self-improvement:** chạy các task tương tự → tạo skill candidate → benchmark → phê duyệt hoặc từ chối.
- [ ] **P16.07 — Bàn giao & Mission Control End-to-End:** giao nhiệm vụ nhiều bước cho Master và xác minh xuyên suốt 11 bước trên Mission Control (Checklist được tạo -> Task hiện ở Home -> phân công Agent -> Timeline ghi nhận việc thật -> Usage hiện số liệu có nguồn hoặc `Không có dữ liệu từ Provider` -> Notification xuất hiện khi sự cố -> Action Inbox hiện yêu cầu duyệt -> QA kiểm chứng -> Artifact bàn giao -> tìm lại, xem lịch sử và đối chiếu phiên bản -> reload không sai lệch, chạy cả 2D và 3D).

**PHASE 17 — Distribution & Documentation**

- [ ] **P17.01** — Một lệnh khởi động local, phát hiện dependencies và hiển thị lỗi rõ ràng.
- [ ] **P17.02** — Config examples cho Engine chính Antigravity và cấu hình mở rộng tùy chọn cho nhiều API Provider / multi-connection như mẫu hai kết nối Gemini, OpenAI hoặc Ollama dùng `credential_ref` đồng bộ với backlog `P15.02` và `P13.08`; không đóng gói secrets *(chưa triển khai)*.
- [ ] **P17.03** — Hướng dẫn vận hành Mission Control, đọc usage và hiểu trạng thái chưa khả dụng, quản lý notifications, tìm và khôi phục artifact, quản lý Provider connections, backup/restore, quản lý quyền và quy trình xử lý lỗi UI/Realtime.
- [ ] **P17.04** — Release package + source hash + acceptance report theo đúng commit.

**Gate cuối:** hệ thống **thực sự nhận việc mới, dùng công cụ, các Agent trao đổi và tạo sản phẩm sử dụng được**, QA xác minh bằng chứng; nhiệm vụ đang chạy/đang lỗi/hết quota được mô tả trung thực. Không có khái niệm “100% không bao giờ lỗi”; yêu cầu là không còn lỗi nghiêm trọng đã biết, có kiểm thử và phục hồi đáng tin cậy.

---

## 4. Các mốc sản phẩm có thể xem và quyết định tiếp

| Milestone | Sau phase | Thành quả thấy được | Có được làm 3D chưa? |
| --- | --- | --- | --- |
| **M0** | 00–01 | Repo sạch, folder rõ ràng, roadmap | Chưa |
| **M1** | 02–03 | Antigravity làm file thật, giao nhận và stream event | Chưa |
| **M2** | 04–06 | Task có state, Master + Worker phối hợp/họp | Chưa |
| **M3** | 07–11 | Các phòng ban có chức năng, Tool/Skill/Vault, học từ việc, phản ứng nhanh | Chưa ưu tiên |
| **M4** | 12–13 | Giao diện 2D vận hành đầy đủ với tác vụ live | Có thể bắt đầu 3D |
| **M5** | 14–15 | Văn phòng 3D phản chiếu trạng thái thật, test ổn định | Đang hoàn thiện |
| **M6** | 16–17 | Nghiệm thu đa lĩnh vực và bản bàn giao | Sẵn sàng sử dụng/đi thi nếu gate đạt |

## 5. Các quyết định còn chờ m duyệt (KHÔNG tự coi là đã chốt)

- [ ] Tên dự án chính thức: `AI Village` hay tên khác.
- [ ] Đường dẫn repo mới: đề xuất `C:\DoCho\AI-Village`.
- [ ] Trình bày 3D: văn phòng nhiều tầng, bản đồ làng, hay cho phép chuyển đổi giữa hai cách nhìn.
- [ ] Quyền Master mặc định: tự động trong workspace; phê duyệt riêng cho quyền/cost/dữ liệu thật.
- [ ] CrewAI: chỉ chốt sau P03.01/P03.02 có bằng chứng tích hợp Antigravity.
- [ ] Công nghệ UI 3D: đánh giá 2 phương án sau khi M4 đạt.

## 6. Quy định triển khai một tiểu phase

Mọi tiểu phase phải theo thứ tự: **đọc trạng thái → xác định đúng scope → làm đúng một việc → chạy test liên quan → kiểm diff → ghi evidence → commit → cập nhật PROGRESS.md**. Nếu ảnh hưởng nhiều folder, tách thành tiểu phase chuẩn bị contract, triển khai adapter, sau đó tích hợp; không dùng một prompt để thay toàn bộ codebase.

**Công việc tiếp theo hiện tại:** chờ lệnh triển khai **`P04.04 — SQLite migrations`** của `PHASE 04 — Contracts & Task Runtime` (tiếp nối `P04.01`–`P04.03` DONE offline tại `docs/reviews/PHASE_04_P04_03_AUDIT.md`). Chưa tự đánh dấu `P04.04`–`P04.07`, `PHASE 04 DONE` hay `Gate C PASS`; chưa triển khai SQLite, Repository, Permission Manager, `agents/*.yaml`, Mission Control UI hay dựng 3D.

---

## 7. Nhật ký thực thi các Phase (Execution Log)

### P01 — Khởi tạo cấu trúc thư mục AI Village
ID: P01
Status: DONE
Scope: Khởi tạo 14 thư mục cấp cao theo kiến trúc hệ thống
Git SHA: 29e0662
Acceptance: 14 thư mục cấp cao tồn tại trên hệ thống, 11 thư mục kỹ thuật chứa file .gitkeep, không tạo mã nguồn hay thư mục con ngoài phạm vi
Tests / Evidence: Lệnh PowerShell Get-ChildItem xác nhận đủ 14 thư mục, đồng bộ thành công lên remote GitHub
Blocking issue: Không có
Next: P02 — Khởi tạo tài liệu nền tảng AI Village

### P02 — Khởi tạo tài liệu nền tảng AI Village
ID: P02
Status: DONE
Scope: README.md, AGENTS.md, .gitignore
Git SHA: d1270da (Lưu ý: commit hoàn tất trên nhánh main sau khi bổ sung quy tắc đồng bộ là caef2c1)
Acceptance: 
- README.md: Mô tả mục tiêu, kiến trúc Multi-Agent linh hoạt, vai trò Master, 9 nhóm chức năng, Antigravity engine, cấu trúc 14 thư mục và nguyên tắc phát triển theo từng phase.
- AGENTS.md: Quy định 11 nguyên tắc phát triển bắt buộc cho coding agent, bao gồm tự động push lên Git khi hoàn tất.
- .gitignore: Thiết lập quy tắc loại trừ secrets, cache, logs, database, runtime, dependencies và bảo toàn file markdown của Agent và Skill.
Tests / Evidence: Kiểm tra tệp tin thực tế tại thư mục gốc, xác nhận không chứa secret hay code nghiệp vụ
Blocking issue: Không có
Next: P01B.01 — Tiêu chuẩn định nghĩa AI Agent (Agent Specification)

### P01B.01 — Tiêu chuẩn định nghĩa AI Agent (Agent Specification)
ID: P01B.01
Status: DONE
Scope: docs/standards/AGENT_SPEC.md
Git SHA: 715976b (mã commit ban đầu trước khi đồng bộ là 468ac66)
Acceptance: 
- Thiết lập đầy đủ 16 khía cạnh định nghĩa Agent: Identity, Mission, Capabilities, AI Engine, Session, Memory, Skills, Tools, Permissions, Task Lifecycle, Communication, Handoff, Quality, Error Handling, Dynamic Registration, Security Boundaries.
- Phân biệt rõ cấu hình tĩnh (Static Manifest) và trạng thái động (Runtime State).
- Quy định bảng trường dữ liệu bắt buộc, tùy chọn, kiểu dữ liệu và ràng buộc validation.
- Bổ sung hồ sơ minh họa mẫu cho vai trò IT Code Reviewer.
- Đảm bảo tính tương thích với CrewAI nhưng không phụ thuộc vào framework này.
Tests / Evidence: Tệp docs/standards/AGENT_SPEC.md tồn tại, kiểm tra cú pháp và cấu trúc dữ liệu đạt yêu cầu
Blocking issue: Không có
Next: P01B.01-R — Rà soát và chuẩn hóa Agent Spec

### P01B.01-R — Rà soát và chuẩn hóa Agent Spec
ID: P01B.01-R
Status: DONE
Scope: docs/standards/AGENT_SPEC.md, docs/PROGRESS.md
Git SHA: a7d377d (mã commit ban đầu trước khi đồng bộ là 3693066)
Acceptance: 
- Thống nhất 100% tên trường giữa phần mô tả chi tiết, bảng Static Manifest Schema và ví dụ minh họa YAML.
- Bổ sung trạng thái BLOCKED vào Task Lifecycle để phân biệt với FAILED và WAITING_APPROVAL; quy định trạng thái do Agent Runtime xác nhận độc lập thay vì tin lời mô hình.
- Cho phép mở rộng Department Registry ngoài 9 phòng ban mặc định.
- Phân biệt số liệu tài nguyên thực tế với số liệu không có từ provider; nghiêm cấm tạo token usage giả.
- Xác lập rõ quyền phát triển phần mềm của IT Agent: được phép sửa code trong workspace hoặc nhánh dev trong sandbox; tuyệt đối không tự sửa production.
- Khớp nối hoàn toàn ví dụ YAML với schema định nghĩa.
- Đối chiếu và cập nhật chính xác lịch sử commit SHA.
Tests / Evidence: Tệp docs/standards/AGENT_SPEC.md được rà soát và chuẩn hóa, không có từ cấm, không có vi phạm quy tắc
Blocking issue: Không có
Next: P01B.02 — Tiêu chuẩn định nghĩa phòng ban (Department Specification)

### P01B.02 — Tiêu chuẩn định nghĩa phòng ban (Department Specification)
ID: P01B.02
Status: DONE
Scope: docs/standards/DEPARTMENT_SPEC.md, docs/PROGRESS.md
Git SHA: 8e828c3
Acceptance: 
- Thiết lập đầy đủ 8 thành phần đặc tả phòng ban: Department Identity, 9 bộ phận mặc định, Department Membership, Collaboration & Communication, Department Autonomy, Dynamic Department Registry, Coordination Boundaries, Department Lifecycle & Schema.
- Phân biệt rõ ranh giới trách nhiệm của 9 bộ phận chuyên môn, khẳng định Master là cơ quan điều hành trung tâm không thuộc 9 phòng ban.
- Cho phép mở rộng Department Registry tại agents/departments/, không đặt tại repository root.
- Cho phép Agent tham gia đa nhóm dự án liên phòng ban mà không nhân bản nhân sự.
- Không áp đặt quy trình cứng bắt buộc mọi tác vụ phải đi qua toàn bộ phòng ban.
- Cung cấp ví dụ manifest YAML cho phòng ban IT khớp 100% với schema và liên kết nhất quán với AGENT_SPEC.
Tests / Evidence: Tệp docs/standards/DEPARTMENT_SPEC.md tồn tại, cú pháp chuẩn xác, không chứa mã nghiệp vụ hay từ cấm
Blocking issue: Không có
Next: P01B.02A — Bản thiết kế kiến trúc toàn diện (System Blueprint)

### P01B.02A — Bản thiết kế kiến trúc toàn diện (System Blueprint)
ID: P01B.02A
Status: DONE
Scope: docs/architecture/SYSTEM_BLUEPRINT.md, docs/PROGRESS.md
Git SHA: 78c8457
Acceptance: 
- Thiết lập tài liệu kiến trúc trung tâm Single Source of Truth cho toàn bộ dự án tại docs/architecture/SYSTEM_BLUEPRINT.md.
- Làm rõ tầm nhìn sản phẩm, phân định ranh giới sở hữu logic, trạng thái và dữ liệu của 12 thư mục cấp cao.
- Đặc tả vai trò điều phối trung tâm của Master Agent và giải pháp biểu diễn trong schema (id: "core.master", department: "orchestration", system_role: "orchestrator").
- Quy định chi tiết mục đích, trách nhiệm, đầu vào, đầu ra, quyền hạn, phối hợp, non-goals và ví dụ thực tế cho 9 phòng ban chức năng mặc định.
- Phân định rõ 3 mức độ sẵn sàng kỹ thuật (đã kiểm chứng, đang thiết kế, cần thử nghiệm); không khẳng định tính năng chưa chạy thực tế.
- Đặc tả quy trình tự học (Phương án C), cơ chế phân loại 5 cấp độ ngắt của Reception, quản lý kho lưu trữ Vault và đo lường tài nguyên thực tế.
- Thiết lập 9 nguyên tắc bất biến của kiến trúc và ma trận truy xuất 17 yêu cầu hệ thống (Traceability Matrix) liên kết với lộ trình roadmap.
- Ghi nhận cách xử lý 3 vấn đề kiến trúc đã làm rõ và đề xuất cập nhật tiêu chuẩn tương lai.
Tests / Evidence: Tệp docs/architecture/SYSTEM_BLUEPRINT.md tồn tại, kiểm tra cú pháp và cấu trúc dữ liệu đạt yêu cầu, không chứa từ cấm
Blocking issue: Không có
Next: P01B.02A-G — Khóa quy tắc kiến trúc (System Blueprint Lock)

### P01B.02A-G — Khóa quy tắc kiến trúc (System Blueprint Lock)
ID: P01B.02A-G
Status: DONE
Scope: AGENTS.md, docs/PROGRESS.md
Git SHA: ea7a780
Acceptance: 
- Bổ sung thứ tự tài liệu tham chiếu vào AGENTS.md, xác lập docs/architecture/SYSTEM_BLUEPRINT.md là Single Source of Truth cho tầm nhìn và nguyên tắc kiến trúc.
- Thiết lập quy trình kiểm tra 6 bước bắt buộc trước khi Coding Agent triển khai bất kỳ nhiệm vụ nào.
- Bổ sung giao thức xử lý mâu thuẫn kiến trúc: cấm tự ý lựa chọn hoặc âm thầm thay đổi, ghi nhận vấn đề Master Agent và trường system_role đang chờ đồng bộ schema.
- Thiết lập các ranh giới bảo vệ định hướng dự án: cấm biến hệ thống thành tool marketing, cấm ép workflow qua 9 phòng ban, cấm giả lập AI, cấm chốt CrewAI trước khi thử nghiệm, cấm làm 3D trước khi 2D và Runtime vượt qua các Gate.
- Cập nhật chính xác commit bàn giao P01B.02A là 78c8457.
- Không đánh dấu các chức năng Runtime là DONE chỉ vì đã có Blueprint.
Tests / Evidence: Tệp AGENTS.md và docs/PROGRESS.md được cập nhật nhất quán, kiểm tra cú pháp và quy tắc không phát sinh lỗi
Blocking issue: Không có
Next: P01B.03 — Tiêu chuẩn định nghĩa kỹ năng (Skill Specification)

### P01B.03 — Tiêu chuẩn định nghĩa kỹ năng (Skill Specification)
ID: P01B.03
Status: DONE
Scope: docs/standards/SKILL_SPEC.md, docs/PROGRESS.md
Git SHA: e4f48e0
Acceptance: 
- Thiết lập tài liệu tiêu chuẩn kỹ thuật toàn diện cho hệ thống kỹ năng tại docs/standards/SKILL_SPEC.md.
- Phân định rõ 4 khái niệm: Agent (chủ thể), Tool (công cụ), Skill (quy trình), Capability (nhãn năng lực).
- Quy định cấu trúc gói kỹ năng và schema SKILL.md gồm YAML frontmatter kết hợp Markdown body.
- Thiết lập hợp đồng thực thi chi tiết (triggers, anti-triggers, inputs, outputs, execution steps, evidence, error handling).
- Đặc tả cơ chế tìm kiếm, nạp kỹ năng tinh gọn và bảo đảm ranh giới an toàn, cấm tự nâng quyền.
- Định nghĩa 7 trạng thái vòng đời kỹ năng và chu trình tự học 9 bước theo Phương án C.
- Thiết lập tiêu chí kiểm thử độc lập, cơ chế phiên bản hóa SemVer và quy trình rollback an toàn.
- Phân định trách nhiệm đa bên giữa Master, HR, Process, IT, QA và Vault.
- Bổ sung 2 ví dụ thiết kế hoàn chỉnh: excel_data_analysis và git_diff_code_review.
Tests / Evidence: Tệp docs/standards/SKILL_SPEC.md tồn tại, kiểm tra cú pháp và cấu trúc dữ liệu đạt yêu cầu, không chứa từ cấm
Blocking issue: Không có
Next: P01B.04 — Tiêu chuẩn định nghĩa quy tắc và quản trị (Rule Specification)

### P01B.04 — Tiêu chuẩn định nghĩa quy tắc và quản trị (Rule Specification)
ID: P01B.04
Status: DONE
Scope: docs/standards/RULE_SPEC.md, docs/PROGRESS.md
Git SHA: 1678f1d
Acceptance: 
- Thiết lập kiến trúc Rule System 8 lớp và phân biệt rõ Rule, Permission, Policy, Approval; khẳng định Rule System là thành phần quản trị trong Core Runtime (không phải phòng ban hay LLM).
- Quy định đầy đủ Rule Manifest Schema gồm 14 trường dữ liệu chuẩn, kiểu dữ liệu, tính bắt buộc và ràng buộc giá trị.
- Xác lập phân tầng thứ bậc 7 cấp, nguyên tắc Deny-Overrides, Approval-Before-Allow và Default-Deny (Fail-Closed).
- Đặc tả kiểm soát quyền Agent trên 9 nhóm hành vi và ranh giới quyền hạn của Master Agent.
- Quy định chính sách Tool và Skill, cổng phê duyệt con người (Approval Gate) 8 bước và mô hình Runtime Enforcement có cấu trúc (`PolicyDecision`).
- Định nghĩa vòng đời 5 trạng thái Rule, SemVer bất biến, snapshot cho tác vụ đang chạy và rollback an toàn.
- Thiết lập bảng xử lý Fail-Closed cho 10 sự cố bảo mật, 7 kịch bản kiểm chứng độc lập và 3 ví dụ YAML thiết kế khớp schema.
- Ghi nhận 4 điểm cần đồng bộ chéo giữa AGENT_SPEC, SKILL_SPEC, DEPARTMENT_SPEC và SYSTEM_BLUEPRINT mà không tự ý sửa các tài liệu ngoài phạm vi.
Tests / Evidence: Tệp docs/standards/RULE_SPEC.md tồn tại, kiểm tra cấu trúc và từ cấm đạt 100%, git diff chỉ nằm trong RULE_SPEC.md và PROGRESS.md
Blocking issue: Không có
Next: P01B.05 — Tiêu chuẩn định nghĩa công cụ (Tool Specification)

### P01B.05 — Tiêu chuẩn định nghĩa công cụ (Tool Specification)
ID: P01B.05
Status: DONE
Scope: docs/standards/TOOL_SPEC.md, docs/PROGRESS.md
Git SHA: 74a78d0
Acceptance: 
- Thiết lập tài liệu đặc tả toàn diện cho hệ thống Tools tại docs/standards/TOOL_SPEC.md, phân biệt 7 thành phần từ Agent, Skill, Tool, Registry, Tool Runtime, Permission Manager đến Engine Adapter.
- Quy định Tool Manifest Schema gồm 16 trường chuẩn và 5 nhóm danh mục công cụ (`files`, `code`, `browser`, `research`, `office`).
- Thiết lập chuẩn cấu trúc đầu ra `ToolResult`, tách biệt rõ trạng thái gọi công cụ kỹ thuật (`EXECUTION_SUCCEEDED`, `EXECUTION_FAILED`, `PERMISSION_DENIED`, `APPROVAL_REQUIRED`, `CANCELLED`, `TIMED_OUT`) với kiểm chứng nghiệp vụ (`UNVERIFIED`, `VERIFIED_PASS`, `VERIFIED_FAIL`).
- Đặc tả vòng đời gọi công cụ 10 bước, cơ chế Fail-Closed khi Policy Engine không phản hồi, cách ly sandbox, idempotency guard chống retry mù quáng và lọc bỏ secrets trong log.
- Chuẩn hóa phân biệt và ánh xạ giữa Tool ID, Action ID, Capability ID và Permission ID; ghi nhận danh sách các điểm mâu thuẫn cần đồng bộ tại P01B.07 (gồm `skills.allowed`, `status: APPROVED` ở ví dụ Skill, `system_role` của Master, đường dẫn `agents/departments/` và chuẩn hóa Action ID).
- Thiết lập bảng 12 kịch bản kiểm chứng độc lập và 3 ví dụ manifest YAML khớp 100% với schema (`tools.files.read`, `tools.code.run_test`, `tools.research.query`).
Tests / Evidence: Tệp docs/standards/TOOL_SPEC.md tồn tại, kiểm tra cấu trúc và từ cấm đạt 100%, không tạo mã nguồn hay chỉnh sửa tệp ngoài phạm vi
Blocking issue: Không có
Next: P01B.06 — Tạo bộ Template chuẩn

### P01B.06 — Tạo bộ Template chuẩn
ID: P01B.06
Status: DONE
Scope: docs/templates/agent/agent.yaml, docs/templates/department/department.yaml, docs/templates/skill/SKILL.md
Git SHA: 74c0b17
Acceptance:
- Tạo 3 tệp template chuẩn tại `docs/templates/` tuân thủ đúng schema của `AGENT_SPEC.md`, `DEPARTMENT_SPEC.md` và `SKILL_SPEC.md`.
- Template Agent bổ sung đầy đủ `system_role: "worker"`, dùng `skill_id` `snake_case`, Tool ID và Action ID chuẩn.
- Template Department đặt `status: "INACTIVE"` và khẳng định `default_tools` chỉ mang tính khuyến nghị.
- Template Skill đặt `status: "DRAFT"` (không dùng `APPROVED`), có đủ 10 mục hợp đồng thực thi.
Tests / Evidence: PyYAML parse thành công cả 3 template (`Agent OK: it.example_worker worker`, `Dept OK: example_dept INACTIVE`, `Skill OK: example_skill_template DRAFT`); không có Agent hay Skill thực tế nào trong `agents/` và `skills/`
Blocking issue: Không có
Next: P01B.07 — Kiểm toán và đồng bộ chéo toàn bộ Specification

### P01B.07 — Kiểm toán, đồng bộ chéo và Nghiệm thu PHASE 01B
ID: P01B.07
Status: DONE
Scope: docs/standards/AGENT_SPEC.md, DEPARTMENT_SPEC.md, SKILL_SPEC.md, RULE_SPEC.md, TOOL_SPEC.md, docs/architecture/SYSTEM_BLUEPRINT.md, docs/reviews/PHASE_01B_AUDIT.md, tests/standards/test_standards_consistency.py, docs/PROGRESS.md
Git SHA: PENDING_COMMIT (xem mã commit bàn giao thực tế trên git log sau khi push)
Acceptance:
- Đồng bộ định danh Master Agent (`id: core.master`, `system_role: orchestrator`, `department: orchestration`) và quy tắc ràng buộc có điều kiện giữa Worker vs Master trong `AGENT_SPEC.md` và `SYSTEM_BLUEPRINT.md`.
- Thống nhất đường dẫn Department Registry duy nhất tại `agents/departments/` và khẳng định `default_tools` chỉ mang tính khuyến nghị.
- Đồng bộ `skills.allowed` dùng `skill_id` `snake_case`, chuyển các ví dụ thiết kế Skill/Rule/Tool về trạng thái `DRAFT` và bổ sung 6 điều kiện công nhận Skill `APPROVED`.
- Chuẩn hóa phân biệt Tool ID, Action ID, Capability ID, Permission; bổ sung bảng ánh xạ 3 tầng `Rule.effect` -> `PolicyDecision.decision` -> `ToolResult.status` và phân tách 6 miền trạng thái vòng đời.
- Cập nhật Traceability Matrix trong `SYSTEM_BLUEPRINT.md` cho toàn bộ PHASE 01B, giữ nguyên các hạng mục PHASE 02–17 ở trạng thái chưa triển khai.
- Xuất báo cáo nghiệm thu đầy đủ tại `docs/reviews/PHASE_01B_AUDIT.md` với kết luận **PHASE 01B: PASS**.
Tests / Evidence: `python -m pytest tests/standards/test_standards_consistency.py -v` đạt `6 passed`; `git diff --check` sạch; quét từ cấm đạt chuẩn; báo cáo kiểm toán tại `docs/reviews/PHASE_01B_AUDIT.md`.
Blocking issue: Không có (ghi nhận 1 mục Informational tại `AGENTS.md` dòng 35 giữ nguyên ngoài phạm vi chỉnh sửa P01B.07).
Next: Dừng sau nghiệm thu PHASE 01B; chờ chỉ đạo thực hiện các hạng mục còn lại của PHASE 00–01 / Gate A trước khi bước sang PHASE 02.

### PHASE 00 (P00.01–P00.04) — Hoàn thiện Tài liệu Kiến trúc Nền tảng
ID: P00.01–P00.04
Status: PARTIAL (P00.02–P00.04 DONE; P00.01 IN_PROGRESS / PENDING_USER_APPROVAL)
Scope: docs/architecture/vision.md, docs/architecture/boundaries.md, docs/architecture/reuse_inventory.md, docs/decisions/ADR-0001-temporary-technology-stack.md
Git SHA: ac67b88
Acceptance:
- `P00.01`: Tạo `docs/architecture/vision.md` tổng hợp Product Vision, MVP, ngoài MVP, trạng thái DoD, tiêu chí nghiệm thu và đối chiếu phê duyệt; đánh dấu `PENDING_USER_APPROVAL` do chưa có xác nhận phê duyệt chính thức từ người vận hành.
- `P00.02`: Tạo `docs/architecture/boundaries.md` định nghĩa 5 nhóm ranh giới (Trust, Data, Permission, Protected Operations, Failure) khớp `RULE_SPEC.md` và `TOOL_SPEC.md`.
- `P00.03`: Tạo `docs/architecture/reuse_inventory.md` kiểm kê chỉ đọc `C:\DoCho\marketting` và `C:\Users\taine\Downloads\Multi-Agent-Pentest-System-main.zip` (ghi `UNVERIFIED` cho phần lịch sử git ngoài file nén), phân loại rõ `ADAPT` / `REWRITE` / `REJECT` / `UNVERIFIED`.
- `P00.04`: Tạo `docs/decisions/ADR-0001-temporary-technology-stack.md` ghi nhận Python/FastAPI/SQLite + React/Vite (3D để sau M4), chưa khóa CrewAI và chưa kiểm chứng Antigravity CLI.
Tests / Evidence: `python -m pytest tests -v` (`11 passed`), kiểm tra liên kết và không rò rỉ bí mật.
Blocking issue: `P00.01` cần người vận hành duyệt `docs/architecture/vision.md`.
Next: Hoàn thiện phần thiếu của PHASE 01 và kiểm toán Gate A.

### PHASE 01 (P01.01–P01.15) — Hoàn thiện Cấu trúc Repository & UI Scaffold
ID: P01.01–P01.15
Status: DONE (bổ sung hoàn thiện trên nền các commit P01 `29e0662` và P02 `d1270da`/`caef2c1` trước đây)
Scope: .env.example, PROGRESS.md, AGENTS.md, .gitignore, README.md trong 11 thư mục kỹ thuật/tài liệu, workspace/.gitkeep, output/.gitkeep, ui/ (React + Vite tối thiểu), tests/stage_a/test_stage_a_baseline.py
Git SHA: 6fadd1c (kế thừa `29e0662`, `d1270da`, `caef2c1`)
Acceptance:
- `P01.01`: Tạo `.env.example` chỉ gồm placeholder an toàn, tạo root `PROGRESS.md` trỏ tới `docs/PROGRESS.md`, sửa ghi chú cũ về `system_role` tại `AGENTS.md` dòng 35, sửa `.gitignore` đảm bảo `workspace/**/*` và `output/**/*` luôn bị ignore kể cả tệp `.md`.
- `P01.02`–`P01.08`, `P01.10`, `P01.12`: Tạo `README.md` đúng ranh giới thiết kế cho `agents/`, `core/`, `engines/`, `tools/`, `skills/`, `storage/`, `api/`, `sandbox/`, `docs/`.
- `P01.09`: Tạo scaffold React + Vite tối thiểu tại `ui/` (chưa có 3D, hiển thị trạng thái `Runtime chưa được kết nối`), cài dependencies cục bộ trong `ui/` và build thành công.
- `P01.11`: Tạo `tests/README.md` và `tests/stage_a/test_stage_a_baseline.py` kiểm chứng cấu trúc, liên kết, bảo mật và hợp đồng scaffold.
- `P01.13`–`P01.15`: Kiểm chứng trực tiếp `workspace/`, `output/` (có `.gitkeep`) và `.runtime/` (trống, không tạo DB giả) đều tồn tại cục bộ và bị Git bỏ qua dữ liệu tác vụ/runtime.
Tests / Evidence: `cd ui && npm install && npm run build` (exit 0, `built in 3.28s`); `python -m pytest tests -v` (`11 passed`); `git check-ignore` xác nhận loại trừ `workspace/`, `output/`, `.runtime/`, `.env`.
Blocking issue: Không có.
Next: Kiểm toán tổng thể và xuất `docs/reviews/CHANG_A_AUDIT.md`.

### Gate A — Kiểm toán và Nghiệm thu Chặng A
ID: Gate-A
Status: PASS_WITH_LIMITATIONS
Scope: docs/reviews/CHANG_A_AUDIT.md, docs/PROGRESS.md
Git SHA: PENDING_COMMIT (xem commit bàn giao `docs(audit): verify and close stage A` trên git log sau khi push)
Acceptance:
- Tổng hợp bằng chứng 19 tiểu mục `P00.01`–`P00.04` và `P01.01`–`P01.15` (18 `PASS`, 1 `PARTIAL` là `P00.01` chờ người vận hành duyệt `vision.md`), giữ nguyên `PHASE 01B: DONE`.
- Toàn bộ kiểm thử (`11 passed`), build UI (`npm run build`) và kiểm tra `.gitignore` / bảo mật đều vượt qua sau chỉnh sửa cuối.
Tests / Evidence: `python -m pytest tests -v` (`11 passed`); `cd ui && npm run build` (exit 0); `git diff --check` sạch; báo cáo chi tiết tại `docs/reviews/CHANG_A_AUDIT.md`.
Blocking issue: Chờ người vận hành xác nhận phê duyệt `docs/architecture/vision.md` để đóng `P00.01` và chuyển Gate A sang `PASS`.
Next: Sau khi người vận hành phê duyệt `vision.md`, chuyển sang `PHASE 02 — Antigravity Execution Proof`.

### Pre-Vision Approval Alignment — Chuẩn hóa Kiến trúc Multi-Provider, Multi-Model & Multi-Key
ID: P00.01 (Alignment)
Status: IN_PROGRESS (`PENDING_USER_APPROVAL`)
Scope: docs/architecture/vision.md, docs/architecture/SYSTEM_BLUEPRINT.md, docs/architecture/boundaries.md, docs/standards/AGENT_SPEC.md, docs/standards/RULE_SPEC.md, docs/standards/TOOL_SPEC.md, docs/templates/agent/agent.yaml, docs/decisions/ADR-0001-temporary-technology-stack.md, docs/PROGRESS.md, tests/standards/test_standards_consistency.py
Git SHA: PENDING_COMMIT (xem commit bàn giao trên git log sau khi push)
Acceptance:
- Bổ sung tầm nhìn Multi-Provider, Multi-Model, Multi-API-Key (BYOK), Multi-Platform và use case chính thức hai kết nối Google Gemini vào `docs/architecture/vision.md`, phân rõ phạm vi MVP vs Sau MVP.
- Đồng bộ 8 thành phần kiến trúc Multi-Provider/Multi-Key vào `SYSTEM_BLUEPRINT.md` mà không tạo phòng ban thứ 10 hay thay đổi cơ chế Master điều phối động.
- Bổ sung các trường tùy chọn mở rộng cho `engine.*` trong `AGENT_SPEC.md` và `docs/templates/agent/agent.yaml` (tương thích ngược, cấm lưu raw API key, không đóng cứng danh sách Provider).
- Đối chiếu và làm rõ ranh giới quản trị Connection, `credential_ref`, chi phí, bảo mật trình duyệt và `TOOL_UNSUPPORTED_BY_PROVIDER` trong `RULE_SPEC.md`, `TOOL_SPEC.md`, `boundaries.md`.
- Cập nhật `ADR-0001` khẳng định Antigravity không độc quyền, BYOK/Multi-Key là yêu cầu sản phẩm, tiếp tục giữ mở CrewAI, Routing Framework và Credential Vault.
- Cập nhật `docs/PROGRESS.md` đối chiếu các phase liên quan, giữ nguyên `P00.01: PENDING_USER_APPROVAL` và `Gate A: PASS_WITH_LIMITATIONS`, không đánh dấu DONE cho chức năng chưa chạy.
Tests / Evidence: `python -m pytest tests/standards/test_standards_consistency.py -v`, `python -m pytest tests -v`, `git diff --check`.
Blocking issue: Chờ người vận hành xem xét và phê duyệt `docs/architecture/vision.md` trước khi đóng `P00.01` và Gate A.
Next: Dừng sau bàn giao bản Vision cập nhật để chờ người vận hành phê duyệt `P00.01`.

### Mission Control UX Planning & Vision Approval (`P00.01` DONE / `Gate A: PASS`)
ID: P00.01 + Gate-A + Roadmap UX Alignment
Status: DONE (đối với chuẩn hóa kế hoạch UX, phê duyệt `vision.md` và đóng Gate A; các tiểu mục triển khai PHASE 02–17 giữ nguyên chưa thực hiện)
Scope: docs/architecture/MISSION_CONTROL_UX.md, docs/architecture/SYSTEM_BLUEPRINT.md, docs/architecture/vision.md, docs/PROGRESS.md, docs/README.md, docs/reviews/CHANG_A_AUDIT.md, tests/standards/test_standards_consistency.py
Git SHA: PENDING_COMMIT (xem commit bàn giao trên git log sau khi push; kế thừa `e4522aa`, `3bd20de`, `6fadd1c`, `ac67b88`, `3d25cf5`)
Acceptance:
- Xây dựng đặc tả `docs/architecture/MISSION_CONTROL_UX.md` gồm định nghĩa Mission Control, User Journeys, Mission Control Home (7 khu vực A–G), 18 thành phần chức năng + Bàn Master, ánh xạ 18 vật thể 3D (`PHASE 14`), 7 nhóm hợp đồng dữ liệu đầu cuối, ma trận truy xuất giai đoạn, phân quyền bảo mật và quy chuẩn 5 trạng thái UI.
- Đồng bộ Mục 12 và REQ-15/16 trong `docs/architecture/SYSTEM_BLUEPRINT.md`; phân bổ chi tiết yêu cầu dữ liệu/API/2D/3D/kiểm thử vào `PHASE 02`–`17` trong `docs/PROGRESS.md` mà không tạo Phase mới hay đánh dấu DONE cho chức năng chưa chạy.
- Ghi nhận xác nhận phê duyệt trực tiếp `"Oke duyệt"` của người vận hành (2026-11-23) vào `docs/architecture/vision.md`, chuyển `P00.01` sang DONE và `Gate A` sang `PASS` tại `docs/PROGRESS.md` và `docs/reviews/CHANG_A_AUDIT.md`.
Tests / Evidence: `python -m pytest tests -v`, `git diff --check`.
Blocking issue: Không còn blocker Chặng A.
Next: `PHASE 02 — Antigravity Execution Proof` (`P02.01 — CLI handshake`).

### P02.01 — Antigravity CLI Handshake (Real Execution Proof)
ID: P02.01
Status: DONE
Scope: engines/__init__.py, engines/antigravity/__init__.py, engines/antigravity/models.py, engines/antigravity/diagnostics.py, engines/antigravity/adapter.py, pyproject.toml, tests/engines/test_antigravity_adapter_unit.py, tests/integration/test_antigravity_handshake_live.py, tests/stage_a/test_stage_a_baseline.py, tests/README.md, docs/reviews/PHASE_02_P02_01_AUDIT.md, docs/PROGRESS.md
Git SHA: PENDING_COMMIT (xem commit bàn giao `feat(engine): verify antigravity CLI handshake` trên git log sau khi push)
Acceptance:
- Xác minh thực tế trên Windows tồn tại CLI độc lập `C:\Users\taine\.gemini\bin\agy.exe` (`1.5.14`), tách biệt với IDE launcher `C:\Antigravity\Antigravity\bin\antigravity.cmd` (`1.107.0`); phát hiện `--help` ghi ra `stderr` và `--print` cần truyền dạng `--print=<prompt>`.
- Triển khai adapter tối thiểu tại `engines/antigravity/` với Pydantic v2, tách biệt `discover()`, `probe()`, `handshake()`, `normalize_result()`, phân loại lỗi và `redact_secrets()`, không hardcode model hay API key, không phụ thuộc CrewAI.
- Thực thi live handshake thật qua `AntigravityAdapter` trong workspace cô lập (`exit_code: 0`, `status: SUCCEEDED`, `conversation_id: 2253c701-19da-438b-abd9-a05bb8d187b3`, `duration_ms: 20573`, `response_text: AI_VILLAGE_HANDSHAKE_OK`, `telemetry_status: VERIFIED_MEASURED`).
- Các năng lực `tool execution`, `stream-json` live, `session isolation`, `cancellation` và `multi-provider` giữ nguyên trạng thái chưa kiểm chứng cho `P02.02`–`P02.06`.
Tests / Evidence: `python -m pytest tests -v` (`24 passed, 1 skipped`); `$env:AI_VILLAGE_RUN_LIVE="1"; python -m pytest tests/integration/test_antigravity_handshake_live.py -v` (`1 passed in 20.34s`); báo cáo tại `docs/reviews/PHASE_02_P02_01_AUDIT.md`.
Blocking issue: Không có.
Next: `P02.02 — Tool execution`.

### P02.02 — Real Tool Execution & Independent Artifact Verification
ID: P02.02
Status: DONE
Scope: engines/antigravity/models.py, engines/antigravity/verification.py, engines/antigravity/adapter.py, engines/antigravity/__init__.py, tests/engines/test_antigravity_tool_execution_unit.py, tests/integration/test_antigravity_tool_execution_live.py, tests/README.md, tests/standards/test_standards_consistency.py, docs/reviews/PHASE_02_P02_02_AUDIT.md, docs/PROGRESS.md
Git SHA: PENDING_COMMIT (xem commit bàn giao `feat(engine): verify real Antigravity tool execution` trên git log sau khi push)
Acceptance:
- Mở rộng tối thiểu adapter trên nền `P02.01`: bổ sung `verification.py` kiểm tra canonical path trong workspace, snapshot SHA-256 bảo vệ tệp nguồn và đối chiếu độc lập nội dung + SHA-256 trên đĩa.
- Xác minh chạy thật với `agy.exe` (`v1.5.15`) dưới `--sandbox --mode accept-edits --output-format json` trong fixture cô lập (`p02_02_evidence__84pdvn5`): Agent đọc `package.json` mà không biết trước marker trong prompt, tạo `proof.txt` (`67` bytes), đọc lại và giữ nguyên SHA-256 của `package.json`; Python xác minh khớp SHA-256 `542d483e2e9286131a5a4f50d4ed6188575e1e65a613cf375d5d35dd124ccba2` (`VERIFIED_PASS`).
- Kiểm chứng bảo vệ tệp sentinel ngoài workspace (`../p02_02_outer_.../sentinel.txt`): không bị thay đổi (`OUTER_UNCHANGED: True`) và shell `Bash` bị auto-deny trong chế độ headless.
- Ghi nhận rõ `Tool events: NOT OBSERVABLE FROM CURRENT CLI OUTPUT`; chưa kiểm chứng `stream-json`, session isolation, cancel hay multi-provider (`P02.03`–`P02.06`).
Tests / Evidence: `python -m pytest tests -v` (`29 passed, 2 skipped`); `$env:AI_VILLAGE_RUN_LIVE="1"; python -m pytest tests/integration/test_antigravity_tool_execution_live.py -v` (`1 passed in 35.86s`); báo cáo tại `docs/reviews/PHASE_02_P02_02_AUDIT.md`.
Blocking issue: Không có.
Next: `P02.03 — Streaming events`.

### P02.03 — Live Streaming Events & Verified Event Normalization
ID: P02.03
Status: DONE
Scope: engines/antigravity/models.py, engines/antigravity/streaming.py, engines/antigravity/adapter.py, engines/antigravity/__init__.py, tests/engines/test_antigravity_streaming_unit.py, tests/integration/test_antigravity_streaming_live.py, tests/README.md, tests/standards/test_standards_consistency.py, docs/reviews/PHASE_02_P02_03_AUDIT.md, docs/PROGRESS.md
Git SHA: PENDING_COMMIT (xem commit bàn giao `feat(engine): implement verified Antigravity event streaming` trên git log sau khi push)
Acceptance:
- Xác minh thực tế trên `C:\Users\taine\.gemini\bin\agy.exe` (`v1.5.15`): cờ `--output-format stream-json` hoạt động cùng `--sandbox --mode accept-edits --print`, phát NDJSON thực thời gồm `init`, `step_update` (`user_input`, `tool`, `agent_response`, `system_message`) và `result`.
- Mở rộng tối thiểu trong `engines/antigravity/`: bổ sung `streaming.py` với `AntigravityStreamParser` allowlist nghiêm ngặt (không lộ `init.cwd`, giá trị `tool_info.parameters`, `tool_info.output` hay secret), `run_streaming_process()` dùng luồng đọc `stdout`/`stderr` tránh deadlock trên Windows, và `AntigravityAdapter.stream_task()` phân tách `source='provider_cli'` với `source='adapter'`, không tạo sự kiện `tool_used` hay ID giả.
- Thực thi live streaming thật với fixture `p02_03_evidence_lawk9spf`: nhận 45 sự kiện tuần tự trước khi thoát tiến trình (`execution_started` đến sớm hơn lúc kết thúc `37.087s`, quan sát thật `tool_call_started`/`tool_call_completed` cho `read_file` và `write_file`, `text_delta`, `execution_completed`, `telemetry_status: VERIFIED_MEASURED`), đồng thời kiểm chứng độc lập `proof.txt` khớp SHA-256 `9b543d9e74693b059917ac7fcb4dd1c55c4f50793c78ef2acfdb5af2f1ac33cb`.
- Xác minh chưa triển khai session isolation (`P02.04`), hủy tiến trình ngoài (`P02.05`), đa nhà cung cấp (`P02.06`), Event Journal hay UI.
Tests / Evidence: `python -m pytest tests -v` (`34 passed, 5 skipped`); `$env:AI_VILLAGE_RUN_LIVE="1"; python -m pytest tests/integration/test_antigravity_streaming_live.py -v` (`3 passed in 51.97s`); báo cáo tại `docs/reviews/PHASE_02_P02_03_AUDIT.md`.
Blocking issue: Không có.
Next: `P02.04 — Session isolation`.

### P02.04 — Real Session Isolation & Cross-Session Leakage Verification
ID: P02.04
Status: DONE
Scope: engines/antigravity/models.py, engines/antigravity/diagnostics.py, engines/antigravity/verification.py, engines/antigravity/sessions.py, engines/antigravity/adapter.py, engines/antigravity/__init__.py, tests/engines/test_antigravity_session_isolation_unit.py, tests/integration/test_antigravity_session_isolation_live.py, tests/README.md, tests/standards/test_standards_consistency.py, docs/reviews/PHASE_02_P02_04_AUDIT.md, docs/PROGRESS.md
Git SHA: PENDING_COMMIT (xem commit bàn giao `feat(engine): verify Antigravity session isolation` trên git log sau khi push)
Acceptance:
- Xác minh thực tế trên `agy.exe` (`v1.5.15`): lời gọi mới tạo `conversation_id` tách biệt, `--conversation <id>` nối đúng hội thoại trước đó; đồng thời phát hiện `%TEMP%` bị xếp vào vùng được phép dùng chung nên đặt quy tắc Fail-Closed từ chối thư mục thuộc `tempfile.gettempdir()` theo mặc định tại `validate_session_workspace()`.
- Triển khai `AntigravitySessionManager` tại `engines/antigravity/sessions.py`, ràng buộc `session_id` độc lập với `provider_conversation_id`, chống trùng lặp/lồng workspace (`paths_overlap`), kiểm tra symlink/junction (`has_symlink_or_junction_component`), gắn `AntigravitySessionEvent` theo ngữ cảnh phiên, ẩn `--settings` khỏi `format_command_summary()` và truyền `permissions.deny` chặn workspace anh em cùng `command(*)`.
- Xác minh live trên fixture `ev_e67b7967`: hai phiên chạy song song nhận hai `conversation_id` riêng (`f12ce0e5-69f7-40e7-9ecb-0ec13f498664` và `d3dd008d-f264-4524-b01e-70488c83dd9e`), không rò rỉ ngữ cảnh sang B (`ctx_a_leaked_in_b1: false`), khôi phục đúng A qua `--conversation`, B bị từ chối đọc/ghi chéo `a_sentinel.txt` (`denied_actions: [{"action": "read_file", "display_name": "ReadFile"}]`, giữ nguyên SHA-256 `70b02ba87e1b14c52b43e4af97bb388e2b1cca5520319cc286f8db9a0cfdc193`), và `proof_A.txt` / `proof_B.txt` khớp SHA-256 độc lập.
- Không triển khai SQLite Session Registry (`PHASE 04`), process lifecycle đầy đủ (`P02.05`), đa nhà cung cấp (`P02.06`) hay UI.
Tests / Evidence: `python -m pytest tests -v` (`39 passed, 8 skipped`); `$env:AI_VILLAGE_RUN_LIVE="1"; python -m pytest tests/integration/test_antigravity_session_isolation_live.py -v` (`3 passed in 256.22s`); báo cáo tại `docs/reviews/PHASE_02_P02_04_AUDIT.md`.
Blocking issue: Không có.
Next: `P02.05 — Process lifecycle`.

### P02.05 — Real Process Lifecycle, Cancellation, Timeout & Error Recovery
ID: P02.05
Status: DONE
Scope: engines/antigravity/models.py, engines/antigravity/streaming.py, engines/antigravity/lifecycle.py, engines/antigravity/adapter.py, engines/antigravity/sessions.py, engines/antigravity/__init__.py, tests/engines/test_antigravity_lifecycle_unit.py, tests/integration/test_antigravity_lifecycle_live.py, tests/README.md, tests/standards/test_standards_consistency.py, docs/reviews/PHASE_02_P02_05_AUDIT.md, docs/PROGRESS.md
Git SHA: PENDING_COMMIT (xem commit bàn giao `feat(engine): implement verified Antigravity process lifecycle` trên git log sau khi push)
Acceptance:
- Thăm dò thực tế trên `agy.exe` phiên bản `1.5.15` cho thấy lời gọi CLI sinh tiến trình con `cmd.exe` và `uvx.exe`; `proc.terminate()` đơn thuần làm rò tiến trình con và khóa thư mục chạy, còn `Win32JobGuard` dùng thư viện chuẩn `ctypes.WinDLL("kernel32")` với `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` dọn trọn vẹn cụm tiến trình sở hữu mà không chạm tới tiến trình khác.
- Chuẩn hóa `ProcessLifecycleState`, `AntigravityExecutionStatus`, `AntigravityCancelOutcome` và mã lỗi `CANCELLED`, `EXECUTION_NOT_FOUND`, `EXECUTION_CONFLICT`, `error_source`, `retry_after_seconds` tại `engines/antigravity/models.py`; triển khai `AntigravityProcessManager` tại `engines/antigravity/lifecycle.py` phân tách `execution_id`, `session_id`, `conversation_id`, `pid`, tách `active_tool_name` từ `tool_call_*` với trạng thái tiến trình và không suy diễn phần trăm tiến độ giả.
- Chứng minh live trên `ev_0a52964d` và `test_antigravity_lifecycle_live.py`: hủy thật `exec-1e3e5111973d` PID `13704` đang chạy cho kết quả `CANCELLED` qua `win32_job_object` trong `9159 ms` với `alive_after: false`; phiên B `exec-912bed3f3e83` PID `2716` chạy song song vẫn hoàn tất `SUCCEEDED` với `42` sự kiện, `VERIFIED_MEASURED` tokens `31397` và khớp SHA-256 `6e07b89282f887cb65ab23a178e7856dce66c0f54c648d3676eb379d4860c167`; controlled timeout PID `1748` ngắt trong `1509 ms`; cờ CLI sai trên `agy.exe` trả `UNSUPPORTED_ARGUMENT` với `error_source: cli` và `exit_code: 2`.
- Giới hạn ghi nhận trung thực: chưa cố ý kích hoạt `AUTH_ERROR`, `QUOTA_EXCEEDED`, `RATE_LIMITED` hoặc treo suy luận trên tài khoản live; các nhánh này được kiểm chứng bằng unit fixture, không giả lập kết quả live.
Tests / Evidence: `python -m pytest tests -v` (`44 passed, 13 skipped`); `$env:AI_VILLAGE_RUN_LIVE="1"; python -m pytest tests/integration/test_antigravity_lifecycle_live.py -v` (`5 passed in 72.63s`); báo cáo tại `docs/reviews/PHASE_02_P02_05_AUDIT.md`.
Blocking issue: Không có.
Next: `P02.06 — Provider contract`.

### P02.06 — Provider-Agnostic Engine Contract & Multi-Provider Integration
ID: P02.06
Status: BLOCKED
Scope: engines/__init__.py, engines/contract.py, engines/connections.py, engines/registry.py, engines/providers/__init__.py, engines/providers/antigravity.py, engines/providers/gemini.py, pyproject.toml, tests/README.md, tests/stage_a/test_stage_a_baseline.py, tests/engines/test_engine_provider_contract_unit.py, tests/integration/test_engine_provider_contract_live.py, docs/reviews/PHASE_02_P02_06_AUDIT.md, docs/PROGRESS.md
Git SHA: PENDING_COMMIT (xem commit bàn giao `feat(engine): implement engine contract and gemini adapter (live blocked)` trên git log sau khi push)
Acceptance:
- Triển khai Universal Engine Contract tại `engines/contract.py` gồm `EngineRequest`, `EngineResult`, `EngineEvent`, `EngineTelemetry`, `EngineCapabilities`, `EngineSessionReference`, `EngineToolCallProposal`, `EngineError` và `EngineAdapter`; phân tách `agent_execution_platform` với `model_api`, cấm Model API tự khai báo `native_tool_execution` hoặc `workspace_access` ở trạng thái `SUPPORTED`.
- Triển khai `EngineConnectionRef`, `ProviderConnectionGroup`, `CredentialResolver`, `EnvCredentialResolver` và `ConnectionRegistry` tại `engines/connections.py` cùng `EngineRegistry` tại `engines/registry.py` để chọn kết nối/nhà cung cấp trung lập không sửa `core/` và không tự chuyển dự phòng ngoài chính sách.
- Triển khai `AntigravityEngineAdapter` tái sử dụng `AntigravityAdapter` và `GoogleGeminiAdapter` tại `engines/providers/gemini.py`; xác minh `LIVE_VERIFIED` cho `agy.exe` `1.5.15` qua hợp đồng chung (`req-ef60c6fc90e0`, `SUCCEEDED`, `VERIFIED_MEASURED` `6040` tokens trong `16036 ms`), chặn Fail-Closed `TOOL_UNSUPPORTED_BY_PROVIDER` và cách ly multi-connection BYOK `gemini_primary`/`gemini_secondary`.
- Chưa xác minh HTTP live tới Provider API thứ hai do chưa được cấu hình khóa API hợp lệ trên máy; giữ `streaming` và `model_discovery` của Gemini ở trạng thái `NOT_VERIFIED`, chưa đánh dấu `P02.06` DONE hay `Gate B` PASS.
Tests / Evidence: `python -m pytest tests -v` (`49 passed, 18 skipped`); `$env:AI_VILLAGE_RUN_LIVE="1"; python -m pytest tests/integration/test_engine_provider_contract_live.py -v -rs` (`3 passed, 2 skipped in 17.15s`); báo cáo tại `docs/reviews/PHASE_02_P02_06_AUDIT.md`.
Blocking issue: Máy trạm chưa có `.env`, biến môi trường hoặc Windows Registry chứa khóa API hợp lệ của Provider thay thế như `GEMINI_API_KEY` hay `GOOGLE_API_KEY`, và không có server mô hình cục bộ nào đang chạy nên Test B và Test C chưa thể gọi API thật.
Next: Người vận hành cấu hình khóa API hợp lệ của một Provider độc lập để chạy lại Test B và Test C trước khi kiểm toán tổng hợp `PHASE 02` và `Gate B`.

### PHASE 02 / Gate B — Antigravity-First P02.06 Closure & Gate B End-to-End Audit
ID: P02.06 / Gate B
Status: DONE (P02.06: DONE; PHASE 02: DONE; Gate B: PASS)
Scope: engines/__init__.py, engines/verification.py, tests/engines/test_gate_b_verification_unit.py, tests/integration/test_gate_b_end_to_end_live.py, tests/README.md, tests/standards/test_standards_consistency.py, docs/architecture/vision.md, docs/architecture/SYSTEM_BLUEPRINT.md, docs/decisions/ADR-0001-temporary-technology-stack.md, docs/reviews/PHASE_02_P02_06_CRITERIA_ADDENDUM.md, docs/reviews/PHASE_02_GATE_B_AUDIT.md, docs/PROGRESS.md
Git SHA: PENDING_COMMIT (xem commit bàn giao `feat(engine): complete Gate B end-to-end audit under Antigravity-first criteria` trên git log sau khi push; bảo toàn commit lịch sử `094dd83`)
Acceptance:
- Phê duyệt của người vận hành ngày 2026-11-24 chốt Antigravity CLI là Engine thực thi chính tại `PHASE 02`, giữ nguyên Universal Engine Contract, `GoogleGeminiAdapter` cùng cấu trúc Multi-Provider/BYOK và chuyển kiểm chứng HTTP live tới API Provider thứ hai sang backlog `P15.02` (`P13.08`, `P17.02`), giữ nguyên bản ghi lịch sử `BLOCKED` trong `docs/reviews/PHASE_02_P02_06_AUDIT.md` và hai bài Gemini live ở trạng thái `SKIPPED`.
- Triển khai `verify_engine_artifact()` tại `engines/verification.py` để tách biệt trạng thái kỹ thuật `EngineResult.status == 'SUCCEEDED'` và `business_verification_status == 'UNVERIFIED'` khỏi kiểm chứng độc lập trên đĩa `VERIFIED_PASS`/`VERIFIED_FAIL`.
- Chứng minh End-to-End thật xuyên suốt `Ứng dụng -> EngineRegistry.execute() -> AntigravityEngineAdapter -> agy.exe (v1.5.15) -> read_file/write_file -> proof.txt -> verify_engine_artifact()`: lượt chạy `req-ea47a176818c` đạt `SUCCEEDED`, nhận 36 sự kiện với `tool_execution_observed` cho `["read_file", "write_file"]`, `VERIFIED_MEASURED` `30799` tokens trong `45320 ms`, `package.json` nguyên vẹn SHA-256 `11019d5793d2fbef107ad81331b630215f6d33189c27f42fe53b1b09d521f00c`, `proof.txt` (`68` bytes) khớp SHA-256 `866664c9a1e95842c185b0b8d849dd61ee5fb41b03096f698973a520e1aecaf0` với `VERIFIED_PASS`; chặn Fail-Closed `TOOL_UNSUPPORTED_BY_PROVIDER` trên Gemini Model API và trả `VERIFIED_FAIL` khi `proof.txt` bị làm sai nội dung/SHA-256.
Tests / Evidence: `python -m pytest tests -v` (`53 passed, 20 skipped`); `$env:AI_VILLAGE_RUN_LIVE="1"; python -m pytest tests/integration/test_gate_b_end_to_end_live.py -v` (`2 passed in 48.55s`); biên bản điều chỉnh tại `docs/reviews/PHASE_02_P02_06_CRITERIA_ADDENDUM.md` và kiểm toán tổng hợp tại `docs/reviews/PHASE_02_GATE_B_AUDIT.md`.
Blocking issue: Không có đối với luồng Antigravity-first và `Gate B` (hai bài kiểm thử HTTP live của Google Gemini vẫn giữ ở trạng thái tùy chọn `SKIPPED` thuộc backlog `P15.02`).
Next: `P03.01 — Spike tích hợp` (chỉ bắt đầu khi có lệnh triển khai `PHASE 03`).

### P03.01 — Master -> Worker Integration Spike
ID: P03.01
Status: DONE (P03.01: DONE; P03.02-P03.03: TODO)
Scope: engines/contract.py, engines/providers/antigravity.py, core/__init__.py, core/orchestration/__init__.py, core/orchestration/spike_models.py, core/orchestration/spike_coordinator.py, tests/stage_a/test_stage_a_baseline.py, tests/standards/test_standards_consistency.py, tests/README.md, tests/core/test_master_worker_spike_unit.py, tests/integration/test_master_worker_spike_live.py, docs/reviews/PHASE_03_P03_01_AUDIT.md, docs/PROGRESS.md
Git SHA: PENDING_COMMIT (xem commit bàn giao `feat(orchestration): verify real master worker handoff` trên git log sau khi push)
Acceptance:
- Mở rộng `EngineRequest` (`denied_paths`, `deny_shell_commands`) và chuyển tiếp qua `AntigravityEngineAdapter` vào `AntigravityStreamRequest`, bảo đảm `agy.exe --sandbox --settings` nhận danh sách chặn chéo thư mục và chặn shell trên đường dẫn gọi chuẩn `EngineRegistry`.
- Triển khai `core/orchestration/spike_models.py` (`parse_strict_json_object`, `SpikeAgentSpec`, `MasterPlan`, `WorkerHandoff`, `WorkerExecutionRecord`, `MasterSynthesis`, `AttributedSpikeEvent`, `MasterWorkerSpikeResult`, `OrchestrationError`) và `core/orchestration/spike_coordinator.py` (`validate_master_plan_policy`, `run_master_worker_spike`) tuân thủ fail-closed, phân tách vai trò `core.master` (`plan`, không quyền ghi) với `it.artifact_worker` (`accept-edits`, không quyền shell), kiểm soát snapshot cây thư mục trước và sau mỗi bước.
- Chứng minh live end-to-end thật qua Antigravity CLI (`corr-59b284583d7f`, trạng thái `DELIVERED`, tổng thời lượng `105618 ms`, `211` sự kiện gắn nhãn): `master_plan` chạy trong `master_ws` (`conv ff24c4f4-537c-4495-9e25-11fc74cf000b`, `32714 ms`, `25240` tokens `VERIFIED_MEASURED`), `worker_execute` chạy trong `worker_ws` (`conv 7d469871-272a-49da-88ac-a2dbb6609946`, `43022 ms`, `31504` tokens `VERIFIED_MEASURED`, `read_file` và `write_file`), `verify_engine_artifact()` xác nhận `package.json` nguyên vẹn và `proof.txt` đạt `VERIFIED_PASS` khớp SHA-256 `c50b77fd0ab5f284d208122b3c98620d85382870405238f92e8e6efb1d871d0f`, `master_synthesize` tổng hợp chuẩn theo QA (`conv 1030d732-fb38-4b90-9617-53ba6f50a24a`, `26244 ms`, `25467` tokens `VERIFIED_MEASURED`); sau chạy `workspace/` sạch còn `[".gitkeep"]` và `agents/` giữ nguyên `[".gitkeep", "README.md"]`.
Tests / Evidence: `python -m pytest tests -v` (`76 passed, 21 skipped in 4.06s`); `$env:AI_VILLAGE_RUN_LIVE="1"; python -m pytest tests/integration/test_master_worker_spike_live.py -v -rs --log-cli-level=INFO` (`1 passed in 106.24s`); báo cáo kiểm toán tại `docs/reviews/PHASE_03_P03_01_AUDIT.md`.
Blocking issue: Không có.
Next: `P03.02 — Đánh giá đối chứng CrewAI với Custom Orchestrator` (chỉ bắt đầu khi có lệnh triển khai tiếp).

### P03.02 — CrewAI Crew/Flow vs Custom Orchestrator Evaluation & ADR-0002
ID: P03.02
Status: DONE (P03.01-P03.02: DONE; P03.03: TODO)
Scope: pyproject.toml, tests/benchmarks/p03_02/__init__.py, tests/benchmarks/p03_02/models.py, tests/benchmarks/p03_02/custom_harness.py, tests/benchmarks/p03_02/crewai_flow_prototype.py, tests/benchmarks/p03_02/crewai_crew_prototype.py, tests/core/test_p03_02_custom_error_recovery_unit.py, tests/benchmarks/p03_02/test_crewai_evaluation_unit.py, tests/integration/test_p03_02_crewai_benchmark_live.py, tests/README.md, tests/standards/test_standards_consistency.py, docs/decisions/ADR-0002-orchestration-architecture.md, docs/reviews/PHASE_03_P03_02_AUDIT.md, docs/PROGRESS.md
Git SHA: PENDING_COMMIT (xem commit bàn giao `feat(orchestration): benchmark CrewAI versus custom orchestrator and decide ADR-0002` trên git log sau khi push)
Acceptance:
- Cài đặt cô lập `crewai==1.15.45` cùng `pytest==9.1.1` tại `.runtime/crewai-bench-venv` trên Python `3.13.13` giữ Python toàn cục sạch (`crewai_installed: False`) và `pyproject.toml` không thêm dependency production; đo được `124` gói chiếm `556.62 MB` và thời gian import nguội `.pyc` ấm `12963.0 ms` (`crewai`) / `15266.7 ms` (`crewai.flow.flow`) so với `1018.5 ms` của `core.orchestration`.
- Xây dựng nguyên mẫu công bằng gọi qua `EngineRegistry` trên `agy.exe` `1.5.15` cho Phương án A (`custom_harness.py` bọc `run_master_worker_spike` giữ nguyên bản), Phương án B (`crewai_flow_prototype.py` 6 bước kèm `SQLiteFlowPersistence` cô lập) và Phương án C (`crewai_crew_prototype.py` qua cầu `AntigravityEngineBaseLLM`).
- Chứng minh live thật qua Antigravity CLI trên cùng bài toán `package.json -> proof.txt` với QA độc lập `VERIFIED_PASS`: Phương án A `P03.01` `105618 ms`, `5989` ký tự prompt, `211` sự kiện; Phương án B Flow `112066 ms`, `6032` ký tự prompt, `3757 ms` overhead, `213` + `24` sự kiện, thiếu API hủy; Phương án C Crew `115466 ms`, `7369` ký tự prompt (+`23.0%`), `3891 ms` overhead, `0` sự kiện `ToolUsage*`, thiếu API hủy, xung đột retry nội bộ trên `BaseLLM` và `Task.guardrail` từ chối PEP 563.
- Phê duyệt quyết định kiến trúc **`custom_orchestrator`** tại `docs/decisions/ADR-0002-orchestration-architecture.md` và đối chiếu đủ `19` tiêu chí tại `docs/reviews/PHASE_03_P03_02_AUDIT.md`; bảo toàn `ADR-0001`, `agents/` (`['.gitkeep', 'README.md']`) và `workspace/` (`['.gitkeep']`).
Tests / Evidence: `python -m pytest tests -v` (`80 passed, 23 skipped in 5.24s`); `.runtime/crewai-bench-venv/Scripts/python.exe -m pytest tests/benchmarks/p03_02/test_crewai_evaluation_unit.py -v` (`4 passed, 91 warnings in 12.15s`); `$env:AI_VILLAGE_RUN_LIVE="1"; .runtime/crewai-bench-venv/Scripts/python.exe -m pytest tests/integration/test_p03_02_crewai_benchmark_live.py -v -rs` (`2 passed, 39 warnings in 237.80s`).
Blocking issue: Không có.
Next: `P03.03 — Khóa abstraction AgentRuntime` (chỉ bắt đầu khi có lệnh triển khai tiếp).

---

### [2026-11-24] PHASE 03 / P03.03: Agent Runtime Abstraction & Custom Orchestrator Integration
Commit target: `feat(runtime): establish provider-independent AgentRuntime abstraction` (tại nhánh `main`, nối tiếp `516d34cc0b9cbf4accaed37c3eb85f527f60dd53`; giữ nguyên `ADR-0001`, `ADR-0002`, `core/orchestration/spike_models.py`, `core/orchestration/spike_coordinator.py` và `agents/`)
Changes:
- Xây dựng gói `core/runtime/` (`__init__.py`, `contract.py`, `application.py`, `custom_runtime.py`) định nghĩa `@runtime_checkable class AgentRuntime(Protocol)` cùng các Pydantic v2 models `AgentRuntimeInfo`, `WorkspaceScope`, `ExecutionRouting`, `ArtifactAcceptanceSpec`, `AgentRuntimeRequest`, `AgentRuntimeEvent`, `AgentRuntimeArtifactRef`, `AgentRuntimeTelemetry`, `AgentRuntimeError`, `AgentRuntimeResult`.
- Triển khai tầng gọi ứng dụng `execute_agent_workflow` và `AgentWorkflowRunner` chỉ phụ thuộc `core.runtime.contract`, bảo vệ ranh giới ngoại lệ qua `redact_secrets` và chặn `DELIVERED` khi thiếu `VERIFIED_PASS`.
- Triển khai `CustomAgentRuntime` (`custom.master_worker_v1`, `supports_workflow_cancellation=False`) kết nối `EngineRegistry` với `run_master_worker_spike` của P03.01, chuyển tiếp `AgentRuntimeEvent` và giữ nguyên `None` cho telemetry thiếu từ provider.
- Cập nhật `tests/stage_a/test_stage_a_baseline.py`, `tests/standards/test_standards_consistency.py`, `tests/README.md`, bổ sung `tests/core/test_agent_runtime_contract_unit.py` và `tests/integration/test_agent_runtime_live.py`, tạo báo cáo `docs/reviews/PHASE_03_P03_03_AUDIT.md` và đánh dấu `PHASE 03: DONE` (không tự đánh dấu `Gate C` hay `PHASE 04`).
Tests / Evidence: `python -m pytest tests -v` (`85 passed, 24 skipped`, 0 failed); `$env:AI_VILLAGE_RUN_LIVE="1"; python -m pytest tests/integration/test_agent_runtime_live.py -v -rs --log-cli-level=INFO` (`1 passed in 127.53s`, `inv-03e9d5039fc8`, `corr-006ceff3bee0`, `DELIVERED`, `VERIFIED_PASS`, SHA-256 `26d6cbc3dcb476f5e68a435edc2406ea841b05c378d4cc67a490fc8593f48149`, `233` events, `62626` total tokens).
Blocking issue: Không có.
Next: Chờ lệnh nghiệm thu **Gate C** hoặc triển khai **PHASE 04 / P04.01** (dừng sau `P03.03`).

---

### [2026-11-24] PHASE 04 / P04.01: Core Contracts & Foundation Data Models
Commit target: `feat(core): establish task runtime foundation contracts` (tại nhánh `main`, nối tiếp `d7c1600e31832c4fea22883bd7d16b9a257c9c30`; giữ nguyên `ADR-0001`, `ADR-0002`, `core/runtime/*`, `core/orchestration/*`, `engines/*` và `agents/`)
Changes:
- Xây dựng gói `core/contracts/` gồm `10` tệp (`__init__.py`, `identifiers.py`, `references.py`, `agent.py`, `session.py`, `artifact.py`, `run.py`, `task.py`, `event.py`, `compat.py`) dùng Pydantic v2 `ConfigDict(extra="forbid")`.
- Chuẩn hóa bộ định danh tách miền (`task_*`, `run_*`, `art_*`, `evt_*`, `chk_*`, `ntf_*`, `prj_*`, `evd_*`, UUID v4 `session_id` hỗ trợ `legacy_session_ref`), `AgentIdentity` kèm ngoại lệ `core.master`/`orchestration`, tách biệt `Task`/`Run`, `Agent`/`Session`, `Artifact`/`QA VERIFIED_PASS` và `ConnectionRef` không chứa raw API key.
- Bổ sung cầu nối thuần chỉ đọc trong `core/contracts/compat.py` bảo toàn `AgentRuntime` của P03.03 và `EngineConnectionRef`; cập nhật `tests/stage_a/test_stage_a_baseline.py`, `tests/standards/test_standards_consistency.py`, `tests/README.md`, tạo `tests/core/test_p04_01_core_contracts_unit.py` và `docs/reviews/PHASE_04_P04_01_AUDIT.md`.
Tests / Evidence: `python -m pytest tests/core/test_p04_01_core_contracts_unit.py -v` (`17 passed`); `python -m pytest tests -v -rs` (offline contract/regression suite xác minh PASS với 0 failed, giữ 24 ca live/CrewAI skipped; không chạy live `agy.exe` do thuần dữ liệu).
Blocking issue: Không có.
Next: Chờ lệnh triển khai **`P04.02 — Task state machine`** (dừng sau `P04.01`, chưa tự đánh dấu `Gate C` hay `PHASE 04 DONE`).

---

### [2026-11-24] PHASE 04 / P04.02: Task State Machine, Guarded Transitions & Truthful Progress
Commit target: `feat(core): implement guarded task and run state machines` (tại nhánh `main`, nối tiếp `00d2de223b97143a24609afb62b50aa6b344f612`; giữ nguyên `core/contracts/*`, `core/runtime/*`, `core/orchestration/*`, `engines/*` và `agents/`)
Changes:
- Xây dựng gói `core/tasks/` gồm `5` mô-đun (`__init__.py`, `transitions.py`, `guards.py`, `state_machine.py`, `projections.py`) tách riêng ma trận `Task` và `Run`, giữ bất biến đầu vào và kiểm chứng mọi chuyển đổi bằng các guard fail-closed.
- Thực thi QA completion gate chặn `llm_self_report`, `worker_agent`, producer tự nghiệm thu, thiếu `QaVerificationEvidence` hoặc artifact/checklist chưa xác minh; cung cấp cặp thao tác `start_task_run`, `complete_run_only`, `fail_task_run`, `timeout_task_run`, `cancel_task_run` giữ nhất quán `active_run_id`.
- Phát `Event` hợp lệ trong bộ nhớ (`task.status_changed`, `run.started`, `run.completed`, `run.failed`, `run.cancelled`, `run.timed_out`), ghi nhận khoảng trống chưa có `run.status_changed` cho trạng thái chờ của `Run` để `P04.03` xem xét; bổ sung projection chỉ đọc cho Kanban `6` cột chuẩn UX, Checklist có bằng chứng và Timing tính từ timestamp gốc không tạo ETA giả.
- Cập nhật `tests/stage_a/test_stage_a_baseline.py`, `tests/standards/test_standards_consistency.py`, `tests/README.md`, tạo `tests/core/test_p04_02_task_state_machine_unit.py` và `docs/reviews/PHASE_04_P04_02_AUDIT.md`.
Tests / Evidence: `python -m pytest tests/core/test_p04_02_task_state_machine_unit.py -v` (`6 passed` với fixture Python thuần); `python -m pytest tests -v -rs` (`108 passed, 24 skipped`, 0 failed; không chạy live `agy.exe` hay benchmark CrewAI).
Blocking issue: Không có.
Next: Chờ lệnh triển khai **`P04.03 — Event journal`** (dừng sau `P04.02`, chưa tự đánh dấu `PHASE 04 DONE` hay `Gate C PASS`).

---

### [2026-11-24] PHASE 04 / P04.03: Event Journal, Deterministic Replay & Activity Timeline
Commit target: `feat(core): implement append-only event journal and replay` (tại nhánh `main`, nối tiếp `c45c2ad3955300ba172c09057e17cfa0e2b70964`; giữ nguyên `ADR-0001`, `ADR-0002`, `core/runtime/*`, `engines/*` và `agents/`)
Changes:
- Bổ sung `run.status_changed` tương thích ngược vào `core/contracts/event.py` và cập nhật `transition_run` tại `core/tasks/state_machine.py` để phát Event đầy đủ cho các trạng thái chờ/chặn và phục hồi của `Run`.
- Xây dựng gói `core/events/` gồm `4` mô-đun (`__init__.py`, `journal.py`, `replay.py`, `projections.py`) với `InMemoryEventJournal` chỉ ghi thêm an toàn đa luồng (`RLock`), cấp phát `sequence_number` đơn điệu, phân tách duplicate giống hệt vs xung đột, batch atomicity trong bộ nhớ, kiểm tra toàn vẹn lịch sử replay và dựng `ActivityTimelineProjection`.
- Tích hợp `record_task_transition`, `record_run_transition`, `record_paired_transition` và `record_runtime_event`; cập nhật `tests/stage_a/test_stage_a_baseline.py`, `tests/standards/test_standards_consistency.py`, `tests/README.md`, `tests/core/test_p04_02_task_state_machine_unit.py`, bổ sung `tests/core/test_p04_03_event_journal_unit.py` và `docs/reviews/PHASE_04_P04_03_AUDIT.md`.
Tests / Evidence: `python -m pytest tests/core/test_p04_03_event_journal_unit.py -v` (`12 passed`); `python -m pytest tests/core/test_p04_02_task_state_machine_unit.py -v` (`6 passed`); `python -m pytest tests -v -rs` (`120 passed, 24 skipped`, 0 failed; vận hành trong bộ nhớ, chưa lưu SQLite hay chạy live `agy.exe`).
Blocking issue: Không có.
Next: Chờ lệnh triển khai **`P04.04 — SQLite migrations`** (dừng sau `P04.03`, chưa tự đánh dấu `P04.04`–`P04.07`, `PHASE 04 DONE` hay `Gate C PASS`).

---

### [2026-11-25] PHASE 04 / P04.04: SQLite Schema Foundation & Versioned Migrations
Git SHA: `759558e12722eb9732757194d5c58f9b79e25f41` (`feat(storage): establish versioned SQLite schema and migrations` tại nhánh `main`, nối tiếp `b5dad2da6ac9269d5a240ccd047fc7259efaa028`; giữ nguyên `core/*`, `engines/*`, `agents/*`)
Changes:
- Xây dựng gói `storage/db/` (`__init__.py`, `connection.py`, `migrations.py`, `migrations/0001_initial.sql`) sử dụng `sqlite3` chuẩn Python `3.13.13` (`SQLite 3.50.4`), tách hoàn toàn khỏi `core/`, không tạo tệp database khi import và bật `PRAGMA foreign_keys=ON` trên mọi kết nối.
- Thiết lập 10 bảng trong `0001_initial.sql` (`schema_migrations`, `projects`, `connections`, `tasks`, `runs`, `sessions`, `checklist_items`, `artifacts`, `events`, `notifications`), khóa ngoại phức hợp `(active_run_id, task_id) -> runs(run_id, task_id)` ngăn sở hữu chéo Task/Run, `CHECK` bảo toàn `NULL` telemetry và QA `VERIFIED_PASS`, `credential_ref` chặn chuỗi secret, `16` chỉ mục và trigger `trg_events_no_update`/`trg_events_no_delete` chặn sửa/xóa `events`.
- Triển khai `split_sql_statements` qua `sqlite3.complete_statement()` thay cho `executescript()` để bảo toàn giao dịch `BEGIN IMMEDIATE` khi rollback migration thất bại giữa chừng, cùng `verify_schema_compatibility()` chặn mismatch checksum, downgrade hoặc schema vượt phiên bản mã.
- Cập nhật `tests/stage_a/test_stage_a_baseline.py`, `tests/standards/test_standards_consistency.py`, `tests/README.md`, bổ sung `tests/storage/test_p04_04_sqlite_migrations.py` và xuất báo cáo `docs/reviews/PHASE_04_P04_04_AUDIT.md`.
Tests / Evidence: `python -m pytest tests/storage/test_p04_04_sqlite_migrations.py -v` (`7 passed`); `python -m pytest tests -v -rs` (`127 passed, 24 skipped`, 0 failed); `git diff --check` (0 lỗi).
Blocking issue: Không có.
Next: Chuyển tiếp sang **`P04.05 — Persistent repositories and restart recovery`** theo kế hoạch batch đã duyệt (chưa đánh dấu `P04.05`–`P04.07`, `PHASE 04 DONE` hay `Gate C PASS`).

---

### [2026-11-25] PHASE 04 / P04.05: Persistent Repositories, Transactional Service & Restart Recovery
Git SHA: `57b9a44eef0e98e954c63ba58f7c1c957bae71f7` (`feat(storage): implement persistent repositories and recovery` tại nhánh `main`, nối tiếp `759558e12722eb9732757194d5c58f9b79e25f41`; giữ nguyên `core/contracts/*`, `core/tasks/*`, `core/events/*`, `core/runtime/*`, `engines/*`, `agents/*`)
Changes:
- Xây dựng gói `storage/repositories/` (`__init__.py`, `errors.py`, `interfaces.py`, `event_journal.py`, `sqlite_store.py`, `service.py`, `recovery.py`) cung cấp Protocol interfaces (`TaskRepository`, `RunRepository`, `SessionRepository`, `MetadataRepository`), `SQLiteEventJournal` tương thích `core.events.journal.EventJournal`, `SQLiteRepositoryStore` kiểm soát revision/ownership/secret/NULL telemetry, `PersistentWorkflowService` ghi nguyên tử State Machine + Task/Run + Event trong một giao dịch `BEGIN IMMEDIATE`, và `recover_task`/`recover_run` tái dựng lịch sử từ `baseline_json` + phát hiện `RUNNING` cần reconciliation.
- Cập nhật `tests/stage_a/test_stage_a_baseline.py` (mở phép `storage/repositories/`), `tests/standards/test_standards_consistency.py`, `tests/README.md`, bổ sung `tests/storage/test_p04_05_persistent_repositories.py`, `tests/storage/test_p04_05_persistence_acceptance.py` và xuất báo cáo `docs/reviews/PHASE_04_P04_05_AUDIT.md`.
Tests / Evidence: `python -m pytest tests/storage -v` (`20 passed`); `python -m pytest tests/core -v` (`67 passed`); `python -m pytest tests -v -rs` (`140 passed, 24 skipped`, 0 failed); kiểm chứng acceptance restart `recovery_status=VERIFIED_CLEAN`, `before_count=7`, `after_count=7`, `seq_first=1`, `seq_last=7`; `git diff --check` (0 lỗi).
Blocking issue: Không có.
Next: Chờ lệnh triển khai **`P04.06 — Workflow cancellation, timeout, fallback & idempotency`** (dừng sau `P04.05`, chưa tự đánh dấu `P04.06`–`P04.07`, `PHASE 04 DONE` hay `Gate C PASS`).

---

### [2026-11-25] PHASE 04 / P04.06: Workflow Lifecycle, Cancellation, Timeout, Fallback & Idempotency
Commit target: `feat(core): implement workflow lifecycle and idempotency` (tại nhánh `main`, nối tiếp `57b9a44eef0e98e954c63ba58f7c1c957bae71f7`; giữ nguyên `0001_initial.sql`, `CustomAgentRuntime.supports_workflow_cancellation=False` và `agents/`)
Changes:
- Bổ sung `workflow.cancel_requested`, `workflow.fallback_evaluated`, `workflow.reconciliation_recorded` vào `core/contracts/event.py`; thêm migration `storage/db/migrations/0002_workflow_lifecycle.sql` (`idempotency_keys`, `run_executions`) và `get_latest_migration_version()` trong `storage/db/migrations.py`.
- Xây dựng `core/tasks/fallback.py` và `core/tasks/lifecycle.py` (`WorkflowLifecycleController`, `OwnedExecutionHandle`, `LifecyclePersistencePort`) tách biệt yêu cầu cancel khỏi xác nhận dừng tiến trình, chống xung đột timeout/cancel/completion, đối chiếu sau restart (`NEEDS_RECONCILIATION`) và chặn xoay vòng API key.
- Kết nối `AntigravityOwnedExecutionHandle` và `OwnedSubprocessHandle` tại `engines/antigravity/lifecycle.py`, triển khai `SQLiteLifecycleRepository` và `SQLiteLifecyclePersistenceAdapter` tại `storage/repositories/lifecycle_repo.py`.
- Bổ sung `tests/core/test_p04_06_workflow_lifecycle_unit.py`, `tests/integration/test_p04_06_workflow_lifecycle.py` và xuất báo cáo `docs/reviews/PHASE_04_P04_06_AUDIT.md`.
Tests / Evidence: `python -m pytest tests/core/test_p04_06_workflow_lifecycle_unit.py tests/integration/test_p04_06_workflow_lifecycle.py tests/storage -v` (`29 passed`); `python -m pytest tests -v -rs` (`149 passed, 24 skipped`, 0 failed); `git diff --check` (0 lỗi). Commit `P04.06` thực tế: `984f1c963b119736a8f5981a1b582d9744ebcd74`.
Blocking issue: Không có.
Next: Chuyển tiếp sang **`P04.07 — Governance, permission manager & human approval gate`** (chưa đánh dấu `P04.07`, `PHASE 04 DONE` hay `Gate C PASS`).

### [2026-11-26] PHASE 04 / P04.07: Governance, Permission Manager & Human Approval Gate
Phase: PHASE 04 / P04.07
Actor: AI Village Agent
Commit target: `feat(governance): implement permission and approval gates`
Summary:
- Bổ sung migration SQLite v3 `storage/db/migrations/0003_governance_approvals.sql` và `SQLiteApprovalRepository` (`storage/repositories/approval_repo.py`) cùng `transaction_scope()` và `shared_conn_provider` trên `SQLiteRepositoryStore` và `SQLiteEventJournal`.
- Triển khai tầng Governance thuần túy tại `core/governance/`: `TrustedAuthContext`, `PolicyRule`, `RoutingContext`, `PolicyEvaluator` (ưu tiên `DENY > REQUIRE_APPROVAL > ALLOW`, chặn `AUDIT_ONLY` cấp quyền, fail-closed), `validate_workspace_path` (chặn traversal, cross-task, symlink/junction, tệp bí mật, đường dẫn hệ thống), `ApprovalService` (chống tự duyệt, quá hạn, giả mạo fingerprint, tái sử dụng một lần, rollback nguyên tử khi handler lỗi) và `PermissionManager` gắn kiểm toán bắt buộc với `WorkflowLifecycleController`.
- Bổ sung `tests/core/test_p04_07_governance_unit.py`, `tests/integration/test_p04_07_governance_persistence.py`, cập nhật `tests/stage_a/test_stage_a_baseline.py`, `tests/standards/test_standards_consistency.py`, `tests/README.md` và xuất báo cáo `docs/reviews/PHASE_04_P04_07_AUDIT.md`.
Tests / Evidence: `python -m pytest tests/core/test_p04_07_governance_unit.py tests/integration/test_p04_07_governance_persistence.py tests/storage -v` (`32 passed in 4.56s`); `python -m pytest tests -v -rs` (`161 passed, 24 skipped in 14.95s`); `git diff --check` (0 lỗi).
Commit actual: `37a333941624ee576c763d7b74ba3c1a1ff2aca1` (`feat(governance): implement permission and approval gates`)
Blocking issue: Không có.
Next: Chuyển tiếp sang **Stage 3 — Conditional Gate C Audit**.

### [2026-11-26] PHASE 04 / GATE C: Phase 04 End-to-End Verification & Acceptance Audit
Phase: PHASE 04 / Gate C
Actor: AI Village Agent
Commit target: `docs(audit): verify phase 04 gate c`
Summary:
- Bổ sung bộ kiểm thử nghiệm thu `tests/integration/test_phase_04_gate_c_acceptance.py` (4 kịch bản end-to-end offline: Task -> SQLite -> Governance allow/deny -> Approval checkpoint `WAITING_APPROVAL` pause/restart/resume & tiêu thụ một lần -> tiến trình con sở hữu `OwnedSubprocessHandle` -> QA SHA-256 độc lập `verify_engine_artifact` -> `COMPLETED` -> tái dựng `recover_task`/`replay_task_history`/`project_activity_timeline`; cô lập tiến trình anh em khi cancel; timeout xác thực deadline trước `TIMED_OUT`; idempotent retry & đối soát `RUNNING` thành `NEEDS_RECONCILIATION`).
- Cập nhật `tests/README.md` và xuất báo cáo nghiệm thu `docs/reviews/PHASE_04_GATE_C_AUDIT.md` xác nhận 14/14 tiêu chí đạt chuẩn (tách bạch rõ `OFFLINE_VERIFIED_PASS` và không gắn `LIVE_VERIFIED` vì chưa chạy `AI_VILLAGE_RUN_LIVE=1`).
Tests / Evidence: `tests/integration/test_phase_04_gate_c_acceptance.py` (`4 passed in 1.99s`); `tests/core` (`79 passed in 4.71s`); `tests/storage` (`20 passed in 3.53s`); `tests/integration` (`13 passed, 23 skipped in 7.81s`); `tests/stage_a tests/standards` (`13 passed in 0.63s`); toàn kho `python -m pytest tests -v -rs` (`165 passed, 24 skipped in 16.62s`); `git diff --check` (0 lỗi).
Commit actual: `dd2de05da1158b59dcced3bf3ffe7edd2efc4faf` (`docs(audit): verify phase 04 gate c`)
Blocking issue: Không có.
Next: Hoàn tất `PHASE 04: DONE (Gate C: PASS)`; chuyển tiếp sang **PHASE 05 / Batch 01 (`P05.01` + `P05.02`)**.

### [2026-11-26] PHASE 05 / P05.01: Agent Manifest Schema & Safe YAML Loader
Phase: PHASE 05 / P05.01
Actor: AI Village Agent
Commit target: `feat(agents): establish validated agent manifests`
Summary:
- Triển khai tầng schema và bộ nạp YAML an toàn tại `core/agents/` (`department.py`, `manifest.py`, `loader.py`, `__init__.py`) dùng Pydantic v2 `extra="forbid"` và `_StrictNoDuplicateSafeLoader(yaml.SafeLoader)` phát hiện khóa trùng, thẻ YAML nguy hiểm, symlink/junction thoát ranh giới, file vượt ngưỡng và sai lệch thư mục với `id`.
- Khởi tạo manifest tĩnh chính thức tại `agents/master/agent.yaml` (`core.master`, `system_role: "orchestrator"`, `department: "orchestration"`, `fs_write: []`, `tools.allowed: []`) và `agents/roles/it_artifact_worker/agent.yaml` (`it.artifact_worker`), bảo toàn ánh xạ sang `AgentIdentity` và `SpikeAgentSpec` mà không tự cấp quyền hay gọi Engine.
- Bổ sung `tests/agents/test_p05_01_agent_manifest.py`, cập nhật allowlist Stage A và Standards tại `tests/stage_a/test_stage_a_baseline.py`, `tests/standards/test_standards_consistency.py`, `tests/README.md`, và xuất báo cáo `docs/reviews/PHASE_05_P05_01_AUDIT.md`.
Tests / Evidence: `python -m pytest tests/agents/test_p05_01_agent_manifest.py tests/stage_a tests/standards -v` (`22 passed in 9.48s`); `python -m pytest tests -v -rs` (`174 passed, 24 skipped in 56.57s`); `git diff --check` (0 lỗi).
Blocking issue: Không có.
Next: Chuyển tiếp sang **P05.02 — Agent Registry** (chưa đánh dấu `P05.02` hay `PHASE 05 DONE`).









