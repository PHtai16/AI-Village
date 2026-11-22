# AI VILLAGE — PROGRESS & DEVELOPMENT ROADMAP

> **Trạng thái:** ĐANG TRIỂN KHAI. Đã hoàn thành P01, P02, P01B.01, P01B.01-R, P01B.02, P01B.02A và P01B.02A-G (Khóa quy tắc kiến trúc). Chi tiết ghi nhận tại Mục 7 (Nhật ký thực thi).
>
> **Nguyên tắc:** Một tiểu phase = một thư mục có trách nhiệm rõ ràng **hoặc** một năng lực có thể kiểm thử độc lập. Không gom nhiều tính năng không liên quan vào một lượt làm việc.
>
> **Dự án mới (đề xuất):** `C:\DoCho\AI-Village`. **Không xóa, di chuyển hoặc sửa trực tiếp** `C:\DoCho\marketting` hay repository Multi-Agent-Pentest-System. Hai repo cũ chỉ là tài liệu và mã tham khảo được lựa chọn tái sử dụng sau khi kiểm tra.

## 0. Tuyên ngôn sản phẩm và tiêu chuẩn tiến độ

**AI Village** là hệ thống Multi-Agent đa chức năng dưới dạng môi trường làm việc trực quan. Người dùng giao việc cho **Master**; Master tự lập nhóm nhân viên chuyên môn, cho phép thảo luận/handoff trực tiếp, tự kiểm chứng bằng công cụ và tổng hợp sản phẩm cuối cùng. Phòng ban là **cách tổ chức năng lực**, không phải dây chuyền xử lý cố định. Một nhân viên có phòng ban chủ quản nhưng có thể tham gia nhiều nhóm dự án.

- Engine mặc định: **Antigravity** qua giao diện được hỗ trợ; hỗ trợ thêm API/provider sau này.
- Framework điều phối: **CrewAI là ứng viên**, chưa được khóa lựa chọn cho đến khi kiểm chứng adapter Antigravity, quản lý phiên, sự kiện và handoff. Không được biến Antigravity thành một model giả chỉ trả JSON.
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

---

## 3. ROADMAP — Phases nhỏ, có điểm nghiệm thu

### CHẶNG A — Chốt kiến trúc và tạo repository sạch

**PHASE 00 — Quy tắc nền tảng** *(không viết ứng dụng ở chặng này)*

- [ ] **P00.01 — Đặc tả mục tiêu:** chốt phạm vi MVP, phần chưa làm, định nghĩa DONE/FAIL/BLOCKED. **DoD:** `docs/architecture/vision.md` được duyệt.
- [ ] **P00.02 — Danh mục ranh giới:** ghi rõ dữ liệu input/output, nơi lưu bí mật, quyền thực thi và các hành động cần người duyệt. **DoD:** `docs/architecture/boundaries.md`.
- [ ] **P00.03 — Kiểm kê mã cũ:** phân loại thành phần nào được tái sử dụng từ Marketing Village và AutoGen Pentest, không copy bừa. **DoD:** `docs/architecture/reuse_inventory.md`.
- [ ] **P00.04 — Chốt stack tạm thời:** Python/FastAPI + SQLite; React/Vite + Three.js hoặc React Three Fiber là ứng viên; ghi lý do, chưa khóa framework điều phối. **DoD:** một ADR trong `docs/decisions/`.

**PHASE 01 — Khởi tạo cấu trúc** *(mỗi tiểu phase đúng một folder)*

- [ ] **P01.01** — Tạo repo mới, root files (`README.md`, `PROGRESS.md`, `AGENTS.md`, `.gitignore`, `.env.example`) và commit khởi tạo.
- [ ] **P01.02** — Tạo **`agents/`** với README quy định chỉ giữ agent manifests/definitions.
- [ ] **P01.03** — Tạo **`core/`** với README các nhóm thuật toán và dependency boundaries.
- [ ] **P01.04** — Tạo **`engines/`** với hợp đồng adapter tối thiểu, không tích hợp model vội.
- [ ] **P01.05** — Tạo **`tools/`** với quy tắc đăng ký tool và quyền.
- [ ] **P01.06** — Tạo **`skills/`** với schema/lifecycle rỗng.
- [ ] **P01.07** — Tạo **`storage/`** với phân tách mã persistence khỏi data runtime.
- [ ] **P01.08** — Tạo **`api/`** với contract cho client.
- [ ] **P01.09** — Tạo **`ui/`** với web app tối thiểu, chưa có 3D.
- [ ] **P01.10** — Tạo **`sandbox/`** và tài liệu cách ly.
- [ ] **P01.11** — Tạo **`tests/`** với test runner/fixture tối thiểu.
- [ ] **P01.12** — Tạo **`docs/`** với nơi ADR, sơ đồ và vận hành.
- [ ] **P01.13** — Tạo **`workspace/`** (được gitignore; dùng cho input/scratch của task).
- [ ] **P01.14** — Tạo **`output/`** (được gitignore; dùng cho artifact bàn giao).
- [ ] **P01.15** — Tạo **`.runtime/`** (được gitignore; chứa state/log/cache/private data).

**PHASE 01B — Tiêu chuẩn và Kiến trúc**

- [x] **P01B.01 — Đặc tả Agent:** Thiết lập 16 khía cạnh chuẩn mực định nghĩa Agent. **DoD:** `docs/standards/AGENT_SPEC.md`. (SHA: `715976b`)
- [x] **P01B.01-R — Rà soát chuẩn hóa Agent Spec:** Đồng bộ tên trường, bổ sung BLOCKED, quyền IT, telemetry. (SHA: `a7d377d`)
- [x] **P01B.02 — Đặc tả Phòng ban:** Thiết lập 8 thành phần phòng ban, 9 bộ phận mặc định và cơ chế mở rộng. **DoD:** `docs/standards/DEPARTMENT_SPEC.md`. (SHA: `8e828c3`)
- [x] **P01B.02A — Bản thiết kế kiến trúc toàn diện:** Single Source of Truth cho toàn bộ dự án. **DoD:** `docs/architecture/SYSTEM_BLUEPRINT.md`. (SHA: `78c8457`)
- [x] **P01B.02A-G — Khóa quy tắc kiến trúc:** Cập nhật thứ tự tham chiếu, quy trình kiểm tra 6 bước và bảo vệ định hướng dự án trong AGENTS.md.
- [ ] **P01B.03 — Đặc tả Kỹ năng:** Định nghĩa cấu trúc kỹ năng, schema đầu vào/đầu ra và vòng đời. **DoD:** `docs/standards/SKILL_SPEC.md`.

**Gate A:** cấu trúc rõ ràng, Git sạch, có một lệnh chạy test/format; không có mã nghiệp vụ đặt sai folder.

### CHẶNG B — Chứng minh bộ não thật, trước khi xây dân làng

**PHASE 02 — Antigravity Execution Proof**

- [ ] **P02.01 — CLI handshake:** kiểm tra phiên bản/các cờ thực tế và gọi `agy` từ `engines/antigravity/`; ghi exit/status, không hardcode tên model. **DoD:** nhận phản hồi live có bằng chứng.
- [ ] **P02.02 — Tool execution:** từ một workspace fixture, Agent đọc `package.json`, tạo file mới, đọc lại. **DoD:** file và SHA đúng, không dựa vào lời AI.
- [ ] **P02.03 — Streaming events:** chuyển CLI `stream-json` (nếu bản cài hỗ trợ) thành sự kiện có cấu trúc. **DoD:** quan sát được started/tool-used/completed/failed thực tế.
- [ ] **P02.04 — Session isolation:** tạo hai phiên logic A/B, không trộn lịch sử và file. **DoD:** có test cross-session isolation.
- [ ] **P02.05 — Process lifecycle:** timeout, cancel, exit code, redacted stderr, quota/auth/rate-limit. **DoD:** lỗi thật trả trạng thái chuẩn; không retry vô hạn.
- [ ] **P02.06 — Provider contract:** khai báo chung request/result/event, sau đó chỉ làm một API adapter tối giản nếu cần chứng minh thay engine. **DoD:** engine khác có thể thay mà core không đổi.

**PHASE 03 — Quyết định CrewAI**

- [ ] **P03.01 — Spike tích hợp:** một Master + một Worker gọi Antigravity qua adapter, không chỉ gọi model trả văn bản. **DoD:** Worker tạo artifact thật.
- [ ] **P03.02 — So sánh:** đánh giá CrewAI Crew/Flow so với orchestrator mỏng về session, handoff, cancel, khôi phục, event streaming, quota. **DoD:** ADR `use_crewai` hoặc `custom_orchestrator` có bằng chứng.
- [ ] **P03.03 — Khóa abstraction:** core gọi `AgentRuntime` interface, không phụ thuộc trực tiếp API nội bộ của CrewAI. **DoD:** unit test fake adapter và một live smoke.

**Gate B — QUAN TRỌNG NHẤT:** một yêu cầu từ ứng dụng → Antigravity **thao tác công cụ thật** → tạo artifact → backend đọc và kiểm chứng. **Nếu chưa đạt, không phát triển các phase kế tiếp**.

### CHẶNG C — Lõi quản lý nhiệm vụ và trạng thái

**PHASE 04 — Contracts & Task Runtime**

- [ ] **P04.01** — Định nghĩa `Task`, `Run`, `Agent`, `Session`, `Artifact`, `Event` và ID liên kết trong `core/contracts/`.
- [ ] **P04.02** — Task state machine: QUEUED → RUNNING → WAITING/FAILED/COMPLETED/CANCELLED; chuyển trạng thái có guard.
- [ ] **P04.03** — Event journal: event_id, run_id, actor_id, timestamp, loại sự kiện, payload được lọc secrets; replay được.
- [ ] **P04.04** — SQLite migrations trong `storage/db/`, kiểm foreign keys và transaction.
- [ ] **P04.05** — Repository task/event/session trong `storage/repositories/`; restart không mất run.
- [ ] **P04.06** — Cancellation/timeout và idempotency trên core; không hiển thị “đang chạy” nếu đã dừng.
- [ ] **P04.07** — Governance: workspace scopes, tool allowlist, approval gate, audit và fallback khi không đủ quyền.

**Gate C:** một task đơn chạy, tạm dừng/khôi phục/hủy đúng; event và artifact traceable; sau restart không giả trạng thái.

### CHẶNG D — Agent động và phối hợp thông minh

**PHASE 05 — Hồ sơ nhân viên và Master**

- [ ] **P05.01** — Agent manifest schema: name, role, department, capabilities, engine, permissions, memory scope.
- [ ] **P05.02** — Agent Registry: create/update/disable, version và validation (chưa sửa production runtime).
- [ ] **P05.03** — Master Agent: hiểu yêu cầu, xác định output/acceptance, chọn người cần tham gia.
- [ ] **P05.04** — Team Builder: tạo team động từ capability thay vì danh sách phòng ban cứng.
- [ ] **P05.05** — Worker session lifecycle: mỗi nhân viên có session/context riêng, cùng một engine khi phù hợp.
- [ ] **P05.06** — Handoff giữa worker: artifact + decision + evidence; không truyền nguyên chat quá dài.
- [ ] **P05.07** — Agent tuyển mới: đề xuất role, công cụ, quyền và đánh giá; chỉ kích hoạt khi hợp lệ.

**PHASE 06 — Họp và phản biện**

- [ ] **P06.01** — Agenda & participant selection: chỉ gọi người liên quan; nhiệm vụ đơn giản được phép bỏ họp.
- [ ] **P06.02** — Phản biện có cấu trúc: proposal, objection, evidence, uncertainty, action items.
- [ ] **P06.03** — Giới hạn lượt và chống đồng thuận giả/vòng tranh luận vô tận.
- [ ] **P06.04** — Quyết định của Master: ghi rationale và tiếp tục công việc; không thay thế QA.
- [ ] **P06.05** — Worker-to-worker collaboration; xử lý khi hai worker tranh chấp cùng file/task.
- [ ] **P06.06** — Bàn giao Master: tổng hợp artifact cuối, các giới hạn và link tải; thiếu bằng chứng thì NOT VERIFIED.

**Gate D:** nhiệm vụ cần 2 Agent có một cuộc họp thật (không phải đoạn văn dựng sẵn), handoff có định danh, QA tìm được lỗi được sửa trước khi Master bàn giao.

### CHẶNG E — Các năng lực dùng chung và kho lưu trữ

**PHASE 07 — Tools độc lập với phòng ban**

- [ ] **P07.01** — File tools: read/write/list, path scope, không ghi đè ngoài ý muốn.
- [ ] **P07.02** — Code tools: chạy test/script trong sandbox, lưu stdout/stderr có kiểm soát.
- [ ] **P07.03** — Browser tools: mở web, chụp ảnh, ghi hành động thật theo phiên được cấp quyền.
- [ ] **P07.04** — Research tools: truy xuất web/API và lưu URL, thời điểm, bằng chứng, ranh giới dữ liệu.
- [ ] **P07.05** — Office tools: đọc/ghi Word, Excel, PDF và tính toán bằng code.
- [ ] **P07.06** — Tool Registry: khả năng, quyền, chi phí, inputs/outputs; không cài công cụ ngẫu nhiên vào host.

**PHASE 08 — Storage, Vault & Knowledge**

- [ ] **P08.01** — Artifact Registry: task/run/owner/version/hash/content type/đường dẫn.
- [ ] **P08.02** — Kho sản phẩm: lưu và tìm lại file đã bàn giao; không nhân bản tùy tiện.
- [ ] **P08.03** — Kho bằng chứng: QA logs, nguồn research, trace, build/test.
- [ ] **P08.04** — Knowledge retrieval: tìm theo metadata trước, semantic search tùy chọn sau.
- [ ] **P08.05** — Backup/restore, retention, trash, dữ liệu dùng chung và bảo vệ file tham chiếu.

**Gate E:** một nhiệm vụ mới sử dụng lại được artifact/tri thức cũ đúng phiên bản, tạo DOCX/XLSX thật và tải về qua API; xóa/restore không mất file của task khác.

### CHẶNG F — Chuyển 9 bộ phận từ ý tưởng thành chức năng

**PHASE 09 — Department Behaviors** *(mỗi tiểu phase một bộ phận, chỉ xây chức năng cốt lõi trước)*

- [ ] **P09.01 — HR:** quản lý agent, thay role, đề xuất tuyển người và cấp kỹ năng.
- [ ] **P09.02 — Resources:** đo CPU/RAM/time/token nếu engine trả số liệu; đề xuất phân bổ và kiểm kê sau run.
- [ ] **P09.03 — Service/UX:** làm rõ yêu cầu, nhận feedback, so sản phẩm với brief.
- [ ] **P09.04 — Marketing:** một skill nghiên cứu/lập kế hoạch/content có đầu ra thật, không hardcode mọi task.
- [ ] **P09.05 — IT/R&D:** tạo project/code và kiểm thử trong workspace cách ly; không tự thay mã production.
- [ ] **P09.06 — Reception:** lắng nghe yêu cầu mới/sự kiện phát sinh, định tuyến hoặc báo động lên Master.
- [ ] **P09.07 — Process/Skills:** ghi nhận lỗi, công cụ, thời gian sau mỗi task, đề xuất skill khi gặp mẫu lặp.
- [ ] **P09.08 — Operations/QA:** kiểm chứng độc lập, cảnh báo lỗi và từ chối PASS thiếu chứng cứ.
- [ ] **P09.09 — Vault:** luồng nộp tài liệu, gắn metadata, truy xuất và giao lại artifact cũ.

**Gate F:** giao 3 đề bài khác loại (code / research / Office), Master huy động nhóm khác nhau và tạo sản phẩm thật. Không yêu cầu mọi phòng ban cùng tham gia.

### CHẶNG G — Tự học, phản ứng nhanh và phân bổ tài nguyên

**PHASE 10 — Continuous Improvement (phương án C)**

- [ ] **P10.01** — Mỗi task tạo bản tóm tắt thực thi bằng dữ liệu: steps, errors, fixes, tools, time, source, cost nếu biết.
- [ ] **P10.02** — Tìm pattern từ nhiều task: lỗi lặp, thao tác dư, kỹ năng thiếu.
- [ ] **P10.03** — Skill candidate: điều kiện dùng, inputs/outputs, rủi ro, thử nghiệm và version.
- [ ] **P10.04** — IT + QA kiểm chứng candidate bằng benchmark A/B trên fixture.
- [ ] **P10.05** — Master đề xuất kích hoạt; người dùng duyệt khi skill thay quyền, code hoặc workflow nhạy cảm; rollback được.

**PHASE 11 — Interrupts & Resources**

- [ ] **P11.01** — Nhận yêu cầu mới khi task cũ đang chạy, không mất dữ liệu/nhầm conversation.
- [ ] **P11.02** — Phân loại mức khẩn, tác động và lựa chọn Agent cần hỏi; khẩn cấp báo Master.
- [ ] **P11.03** — Scheduler có giới hạn concurrency/rate/quota, hỗ trợ hàng đợi và ưu tiên.
- [ ] **P11.04** — Resource ledger: thời gian, bộ nhớ, chi phí báo cáo được, ước lượng phải có nhãn.
- [ ] **P11.05** — Sau task, tài nguyên được dọn an toàn, kiểm kê và gửi dữ liệu học hỏi cho Process.

**Gate G:** đang xử lý task A, người dùng bổ sung task B; Reception tiếp nhận, Master quyết định ưu tiên; task A không bị ghi đè; nhiệm vụ hoàn thành có đánh giá tài nguyên và cải tiến.

### CHẶNG H — Giao diện thật, sau đó mới 3D

**PHASE 12 — API & Realtime**

- [ ] **P12.01** — HTTP endpoints: tasks, agents, meetings, artifacts, approvals, skills.
- [ ] **P12.02** — SSE/WebSocket event stream dựa trên event journal (có reconnect, dedupe).
- [ ] **P12.03** — API auth/local policy: không công khai quyền tool hoặc secrets sang browser.

**PHASE 13 — Giao diện 2D vận hành trước**

- [ ] **P13.01** — App shell + navigation.
- [ ] **P13.02** — Chat với Master: text, attachment, stream, interrupt, cancel; lịch sử đúng phiên.
- [ ] **P13.03** — Task dashboard: progress dựa trên stage thật, log và retry status.
- [ ] **P13.04** — Agent & department explorer: chọn nhân viên xem role/session/capabilities.
- [ ] **P13.05** — Meeting room 2D: ai tham gia, ý kiến, phản biện, quyết định, action items.
- [ ] **P13.06** — Approval center: người dùng xem rõ hành động đang chờ quyền.
- [ ] **P13.07** — Vault UI: tìm/lọc/xem/tải artifact theo task và phiên bản.
- [ ] **P13.08** — Resource & incident dashboard: quota, error, hàng đợi, khẩn cấp.

**Gate H:** toàn bộ một nhiệm vụ đa agent chạy **qua UI**, dùng engine thật, thao tác được, có kết quả tải được và có thể can thiệp giữa chừng. Nếu chưa qua gate này thì 3D chỉ được làm prototype tách biệt.

**PHASE 14 — Thế giới văn phòng 3D**

- [ ] **P14.01** — 3D scene shell + camera + di chuyển cơ bản; không gắn logic AI vào animation.
- [ ] **P14.02** — Sơ đồ phòng ban từ Agent Registry, không hardcode phòng ban trong scene.
- [ ] **P14.03** — Avatar/nhân viên từ dữ liệu Agent Registry, trạng thái idle/running/blocked.
- [ ] **P14.04** — Phòng họp: sự kiện họp thật quyết định nhân vật tham gia.
- [ ] **P14.05** — Click nhân viên/phòng → mở bảng 2D chứa dữ liệu live tương ứng.
- [ ] **P14.06** — Kiểm hiệu năng, giảm tải GPU và fallback sang UI 2D khi cần.

**Gate I:** 3D phản chiếu cùng event/session với UI 2D; reload đồng nhất; không dựng hoạt cảnh hoặc tiến độ “đang làm” giả.

### CHẶNG I — Vận hành, nghiệm thu và bàn giao

**PHASE 15 — Quality & Reliability**

- [ ] **P15.01** — Unit/contract tests: event schemas, task state, role/permission.
- [ ] **P15.02** — Integration: CLI/API/provider/mock, session isolation, cancellation, restart.
- [ ] **P15.03** — Fault injection: timeout, quota, network, DB rollback, file shared, agent crash.
- [ ] **P15.04** — Browser E2E: chat, meeting, task board, approval, vault, 3D event binding.
- [ ] **P15.05** — Security: workspace isolation, secret leakage, unsafe command permission, backup/restore.

**PHASE 16 — Nghiệm thu nghiệp vụ live**

- [ ] **P16.01 — Coding:** yêu cầu tạo ứng dụng nhỏ → IT code thật → QA thử → Master bàn giao source và log.
- [ ] **P16.02 — Office:** cung cấp Excel lớn → tính bằng code → xuất Word/Excel → đối chiếu giá trị độc lập.
- [ ] **P16.03 — Research:** câu hỏi mới đa lĩnh vực → tìm nguồn thật → trích dẫn có locator → bàn giao.
- [ ] **P16.04 — Họp & phản biện:** hai Agent nêu bất đồng thực chất và xử lý bằng bằng chứng.
- [ ] **P16.05 — Phản ứng nhanh:** bổ sung yêu cầu giữa lúc chạy → Reception báo đúng người → Master quyết định.
- [ ] **P16.06 — Self-improvement:** chạy các task tương tự → tạo skill candidate → benchmark → phê duyệt hoặc từ chối.
- [ ] **P16.07 — Bàn giao:** restart, phục hồi lịch sử, sản phẩm nguyên vẹn, chạy cả UI 2D và 3D.

**PHASE 17 — Distribution & Documentation**

- [ ] **P17.01** — Một lệnh khởi động local, phát hiện dependencies và hiển thị lỗi rõ ràng.
- [ ] **P17.02** — Config examples cho Antigravity và provider khác; không đóng gói secrets.
- [ ] **P17.03** — Hướng dẫn vận hành, backup, restore, quản lý quyền và troubleshoot.
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

**Công việc tiếp theo sau khi duyệt kế hoạch:** thực hiện **P00.01** (chốt phạm vi/MVP) rồi **P00.02**, chưa cần tạo mã nguồn hoặc lập tức dựng 3D.

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
Git SHA: Xem mã commit bàn giao thực tế trên git log sau khi push
Acceptance: 
- Bổ sung thứ tự tài liệu tham chiếu vào AGENTS.md, xác lập docs/architecture/SYSTEM_BLUEPRINT.md là Single Source of Truth cho tầm nhìn và nguyên tắc kiến trúc.
- Thiết lập quy trình kiểm tra 6 bước bắt buộc trước khi Coding Agent triển khai bất kỳ nhiệm vụ nào.
- Bổ sung giao thức xử lý mâu thuẫn kiến trúc: cấm tự ý lựa chọn hoặc âm thầm thay đổi, ghi nhận vấn đề Master Agent và trường system_role đang chờ đồng bộ schema.
- Thiết lập các ranh giới bảo vệ định hướng dự án: cấm biến hệ thống thành tool marketing, cấm ép workflow qua 9 phòng ban, cấm giả lập AI, cấm chốt CrewAI trước khi thử nghiệm, cấm làm 3D trước khi 2D và Runtime vượt qua các Gate.
- Cập nhật chính xác commit bàn giao P01B.02A là 78c8457.
- Không đánh dấu các chức năng Runtime là DONE chỉ vì đã có Blueprint.
Tests / Evidence: Tệp AGENTS.md và docs/PROGRESS.md được cập nhật nhất quán, kiểm tra cú pháp và quy tắc không phát sinh lỗi
Blocking issue: Không có
Next: P01B.03 — SKILL_SPEC.md






