# AI Village: Đặc tả Chức năng Mission Control và Văn phòng Tương tác (Mission Control & Interactive Office UX)

Tài liệu này đặc tả trải nghiệm người dùng, danh mục 18 thành phần chức năng, ánh xạ vật thể văn phòng 3D, hợp đồng dữ liệu đầu cuối và lộ trình phân bổ cho Mission Control trên nền tảng [SYSTEM_BLUEPRINT.md](SYSTEM_BLUEPRINT.md) và [PROGRESS.md](../PROGRESS.md). Tài liệu không thay thế Blueprint (Single Source of Truth) hay Roadmap, và không tuyên bố bất kỳ thành phần giao diện hoặc Runtime nào dưới đây đã được lập trình trong Chặng A.

## 1. Định nghĩa Mission Control

### 1.1. Bản chất và Nguồn tích hợp
Mission Control là trung tâm quan sát và điều hành chung của AI Village, đóng vai trò lớp trải nghiệm tích hợp dữ liệu thực tế từ:

- Master Agent (`core.master`)
- Task Runtime và State Machine (`core/tasks/`)
- Agent Registry (`agents/`)
- Engine Adapter (`engines/`)
- Tool Runtime (`tools/`)
- Event Journal (`core/events/`)
- Resource Ledger (`core/optimization/` và bộ phận `resources`)
- Notification System (`core/events/` và bộ phận `reception`)
- Approval Center (`core/governance/`)
- Artifact Registry (`storage/artifacts/`)
- QA Evidence (`storage/` và bộ phận `qa`)
- Provider Connection Registry (`storage/` và `engines/`)

### 1.2. Ranh giới Phủ định (Mission Control KHÔNG phải là gì)
- Không phải phòng ban thứ 10.
- Không phải một Agent mới hoặc Master thứ hai.
- Không phải framework điều phối mới.
- Không phải một Runtime song song với `core/`.
- Không phải kho dữ liệu độc lập tự lưu trạng thái riêng.
- Không phải bảng thống kê chứa số liệu giả hoặc tiến độ ước đoán vô căn cứ.

Mission Control không sở hữu nghiệp vụ lõi; mọi dữ liệu hiển thị phải đọc qua `api/` từ trạng thái thật, và mọi thao tác điều khiển gửi qua `api/` đều phải đi qua kiểm tra quyền của Permission Manager.

### 1.3. Nguyên tắc UX Bắt buộc
1. **Hiển thị tập trung ngay màn hình chính:** Người dùng không cần mở lần lượt từng phòng ban để biết hệ thống đang vận hành ra sao.
2. **Thứ tự ưu tiên thông tin:** (1) việc cần người dùng xử lý, (2) nhiệm vụ đang chạy, (3) nhiệm vụ lỗi hoặc bị chặn, (4) sản phẩm vừa hoàn thành, (5) trạng thái tài nguyên, quota và kết nối, (6) hoạt động gần đây.
3. **Truy cập trực tiếp theo liên kết sâu (Deep Linking):** Từ bất kỳ thẻ tổng quan hoặc thông báo nào trên Mission Control, người dùng phải mở thẳng được đúng `Task`, `Run`, `Agent`, `Approval` hoặc phiên bản `Artifact` liên quan mà không phải tự nhớ ID hay tra cứu thủ công trong log hay thư mục hệ thống.

## 2. Hành trình Người dùng Chính (User Journeys)

1. **Giao việc và theo dõi kế hoạch:** Người dùng mở Mission Control Home, gửi yêu cầu qua bàn Master, nhận danh sách bước kiểm tra (`Task Checklist Board`) do Master đề xuất và xác nhận hoặc góp ý kế hoạch.
2. **Giám sát thực thi và tiêu hao:** Người dùng theo dõi tiến độ theo bước thật trên `Task Checklist Board` hoặc `Kanban`, quan sát đồng hồ nhiệm vụ và kiểm tra lượng token/chi phí có nguồn tại `Usage & Cost` cùng tình trạng kết nối tại `Server Room / Engine Status`.
3. **Xử lý ngắt và phê duyệt:** Khi có hành động nhạy cảm, thiếu dữ liệu đầu vào hoặc sự cố Provider, `Notification Center` phát cảnh báo và `Action Inbox` tập hợp đúng việc cần quyết định; người dùng duyệt hoặc từ chối trực tiếp tại `Approval Center`.
4. **Kiểm định QA, nhận hàng và đối chiếu:** Sau khi sản phẩm được tạo, người dùng xem kết quả kiểm thử độc lập tại `QA Inspection Station`, tải hoặc xem trước tại `Artifact Cabinet`, đối chiếu các phiên bản tại `Comparison Board` và truy vết lịch sử tại `Activity Journal`.
5. **Quản trị tài sản và cấu hình an toàn:** Người dùng tìm kiếm toàn hệ thống qua `Global Search`, khôi phục hoặc xóa có kiểm soát trong `Trash & Restore`, quản trị nhiều kết nối/API key tại `Provider & API Key Manager` mà không lộ key thô, và về sau tương tác các chức năng này qua vật thể 3D tương ứng.

## 3. Cấu trúc Điều hướng và Màn hình Chính (Mission Control Home)

Mission Control Home là điểm truy cập mặc định của giao diện 2D (`P13.01`), tổng hợp 7 khu vực dữ liệu từ các màn hình chuyên sâu mà không tạo hệ thống lưu trữ riêng:

| Khu vực Home | Nội dung hiển thị | Nguồn dữ liệu chính |
| :--- | :--- | :--- |
| **A. Overview** | Active Tasks, Queued Tasks, Blocked Tasks, Failed Tasks, Recently Completed, Active Agents, Pending Approvals | Task State Machine, Agent Runtime State, Approval Queue |
| **B. My Tasks** | Bảng Task và Checklist rút gọn, ưu tiên các nhiệm vụ cần xử lý hoặc đang chạy | Task Runtime, Checklist Items |
| **C. Resource Snapshot** | Tóm tắt Engine activity, API usage, Cost, Budget và Provider status (không tự tạo metric thiếu nguồn) | Resource Ledger, Engine Telemetry, Connection Registry |
| **D. Action Required** | Tổng hợp Pending Approvals, Missing Inputs, User Questions, QA Rejections, Critical Incidents | Action Inbox, Approval Queue, QA Records |
| **E. Recent Deliveries** | Các artifact và báo cáo vừa hoàn thành kèm trạng thái kiểm chứng QA và liên kết tải | Artifact Registry, QA Evidence |
| **F. Activity Timeline** | Dòng sự kiện quan trọng gần nhất trích xuất từ Event Journal | Event Journal |
| **G. Global Search** | Thanh tìm kiếm toàn hệ thống theo Project, Task ID, Agent, Phòng ban, Artifact, Run, trạng thái, thời gian | Metadata Search Index (tuân thủ phân quyền) |

**Tiêu chí nghiệm thu UX của Mission Control Home:**
- Người dùng biết toàn trạng thái hệ thống mà không phải mở từng phòng ban.
- Mọi thẻ tổng quan đều mở trực tiếp được màn hình chi tiết tương ứng.
- Mọi chỉ số đều có nguồn dữ liệu thực; mọi cảnh báo đều liên kết thẳng tới đối tượng cần xử lý.
- Đầy đủ trạng thái `Loading`, `Empty`, `Error`, `Offline` và `Unavailable`.
- Vận hành ổn định trên màn hình máy tính lẫn màn hình nhỏ; không ép buộc mở không gian 3D để thực hiện bất kỳ thao tác nền tảng nào.

## 4. Danh mục 18 Thành phần Chức năng Văn phòng

Toàn bộ các thành phần sử dụng chung nguồn dữ liệu chuẩn từ backend (`core/`, `storage/`, `engines/`, `tools/`), không tạo nguồn sự thật song song.

### 4.1. `mc.task_checklist`: Bảng công việc (Task Checklist Board)
- **Ý nghĩa trong văn phòng:** Bảng trắng theo dõi các bước thực hiện của từng nhiệm vụ.
- **Chức năng nghiệp vụ:** Hiển thị Task và các bước công việc kèm Agent phụ trách, trạng thái từng bước (`TODO`, `QUEUED`, `RUNNING`, `WAITING_APPROVAL`, `BLOCKED`, `FAILED`, `COMPLETED`, `CANCELLED` ánh xạ chuẩn với Task Lifecycle), kết quả và bằng chứng đính kèm; cho phép mở chi tiết bước và phản hồi kế hoạch. Chỉ hiển thị `số bước hoàn thành / tổng số bước xác định`, không tự suy đoán `%` nếu chưa đủ dữ liệu và không tự đánh dấu đạt chỉ theo lời Agent.
- **Dữ liệu đầu vào:** `task_id`, `run_id`, `checklist_item_id`, phản hồi kế hoạch của người dùng.
- **Nguồn dữ liệu:** Task Runtime (`core/tasks/`), Master Plan (`core/orchestration/`), QA Evidence (`storage/`).
- **Hành động người dùng:** Xem bước, mở chi tiết, gửi phản hồi/yêu cầu chỉnh kế hoạch.
- **Quyền cần thiết:** Quyền xem Task sở hữu; gửi phản hồi kế hoạch (`task.comment` / `task.update_plan`).
- **Trạng thái Loading / Empty / Error / Unavailable:** Skeleton tải bước; Empty khi Task chưa lập kế hoạch; Error kèm mã lỗi API; Unavailable khi Runtime ngoại tuyến.
- **Phase triển khai:** Backend `PHASE 04–05`, API `PHASE 12`, UI 2D `P13.03`, 3D `P14.05` (`Bảng trắng`).
- **Tiêu chí nghiệm thu (DoD):** Khôi phục đúng trạng thái các bước sau reload/restart; mỗi bước `COMPLETED` phải trỏ được tới bằng chứng thực tế.

### 4.2. `mc.kanban`: Bảng Kanban
- **Ý nghĩa trong văn phòng:** Bảng ghim phân cột công việc theo trạng thái vận hành.
- **Chức năng nghiệp vụ:** Gom nhóm nhiệm vụ theo 6 cột (`Chờ`, `Đang thực hiện`, `Chờ phê duyệt`, `Bị chặn`, `Thất bại`, `Hoàn thành`) với bộ lọc Project, Task, Agent, Phòng ban, Trạng thái, Thời gian và Provider/Model. Đọc trực tiếp từ Task Runtime và Event Journal, không tạo trạng thái riêng cho Kanban.
- **Dữ liệu đầu vào:** Tham số lọc truy vấn và yêu cầu chuyển trạng thái hợp lệ.
- **Nguồn dữ liệu:** Task State Machine (`core/tasks/`) và Event Journal (`core/events/`).
- **Hành động người dùng:** Lọc danh sách, mở Task detail, kéo/thả chuyển trạng thái (chỉ thành công khi State Machine guard và Permission Manager cho phép).
- **Quyền cần thiết:** Quyền đọc Task; quyền chuyển trạng thái (`task.transition`, `task.cancel`) hoặc phê duyệt nếu chạm ngưỡng.
- **Trạng thái Loading / Empty / Error / Unavailable:** Cột tải trạng thái rỗng theo bộ lọc; từ chối kéo/thả kèm lý do rõ nếu guard không cho phép.
- **Phase triển khai:** Backend `PHASE 04`, API `PHASE 12`, UI 2D `P13.03`, 3D `P14.05` (`Bảng ghim công việc`).
- **Tiêu chí nghiệm thu (DoD):** Mọi thẻ cột khớp 100% với Task Dashboard và Event Journal; thao tác chuyển trạng thái trái guard bị chặn Fail-Closed.

### 4.3. `mc.operations_monitor`: Màn hình điều hành tổng hợp (Operations Monitor)
- **Ý nghĩa trong văn phòng:** Màn hình trung tâm quan sát toàn bộ nhịp vận hành hệ thống.
- **Chức năng nghiệp vụ:** Hiển thị số lượng Task đang chạy, chờ, bị chặn, thất bại, hoàn thành; số Agent thực sự đang chạy; số phiên Engine đang mở và các sự kiện quan trọng gần nhất. Đồng nhất hoàn toàn với Dashboard, Agent Explorer và Event Journal; không giữ trạng thái `RUNNING` khi sự kiện dừng đã phát hoặc phiên mất kết nối.
- **Dữ liệu đầu vào:** Phạm vi thời gian hoặc bộ lọc trạng thái tổng quan.
- **Nguồn dữ liệu:** Task Runtime, Agent Runtime State, Engine Session Tracker, Event Journal.
- **Hành động người dùng:** Xem số liệu tổng hợp, bấm vào từng thẻ để đi thẳng tới danh sách Task/Agent/Event tương ứng.
- **Quyền cần thiết:** Quyền đọc tổng quan vận hành cục bộ.
- **Trạng thái Loading / Empty / Error / Unavailable:** Hiển thị rõ trạng thái mất kết nối realtime/SSE (`Offline`) và cơ chế đồng bộ snapshot khi kết nối lại.
- **Phase triển khai:** Backend `PHASE 04–05`, API `PHASE 12`, UI 2D `P13.01`, 3D `P14.05` (`Màn hình trung tâm`).
- **Tiêu chí nghiệm thu (DoD):** Không sai lệch đếm số giữa các màn hình; sau reload hoặc mất mạng đồng bộ lại chính xác từ snapshot.

### 4.4. `mc.usage_cost`: Màn hình Usage & Cost (Resource Control Center)
- **Ý nghĩa trong văn phòng:** Màn hình thống kê tài nguyên, hạn mức và chi phí đa nhà cung cấp.
- **Chức năng nghiệp vụ:** Theo dõi theo Provider, Account/Project/Billing Scope an toàn, `connection_id`, `model_id`, Agent, Task, Run và khoảng thời gian. Hiển thị input/output/total tokens, số request API, thời gian thực thi, CPU/RAM đo được, chi phí thực tế hoặc tính toán kèm nguồn bảng giá, ngân sách, quota còn lại (nếu có), trạng thái kết nối và lỗi rate-limit/quota. Bắt buộc tách 5 nhãn nguồn dữ liệu: đo trực tiếp, nhà cung cấp trả về, tính toán có căn cứ, ước lượng có nhãn và không có dữ liệu. Không thay `null` bằng `0`, không suy đoán hai key Gemini cùng Project có quota riêng, không lộ `credential_ref` hay key thô và hiển thị `Không có dữ liệu từ Provider` khi thiếu telemetry.
- **Dữ liệu đầu vào:** Bộ lọc theo Provider/Connection/Model/Project/Task/Run/thời gian; cấu hình ngân sách.
- **Nguồn dữ liệu:** Engine Telemetry (`engines/`), Resource Ledger (`core/optimization/`, bộ phận `resources`), Connection Metadata (`storage/`).
- **Hành động người dùng:** Xem, lọc, đối chiếu nguồn số liệu và cập nhật ngưỡng cảnh báo ngân sách (theo quyền).
- **Quyền cần thiết:** Quyền đọc Resource Ledger; quyền quản trị nếu thay đổi ngưỡng ngân sách.
- **Trạng thái Loading / Empty / Error / Unavailable:** Nếu thiếu telemetry từ nhà cung cấp hiển thị nguyên văn `Không có dữ liệu từ Provider` và không vẽ thanh phần trăm quota giả.
- **Phase triển khai:** Backend `P02.06`, `PHASE 04`, `P09.02`, `P11.04`, API `PHASE 12`, UI 2D `P13.08`, 3D `P14.05` (`Màn hình thống kê`).
- **Tiêu chí nghiệm thu (DoD):** Kiểm thử xác nhận `null` không bị biến thành `0`, không rò rỉ secret/credential và không giả lập quota cho mô hình hai khóa Gemini.

### 4.5. `mc.task_timing`: Đồng hồ và thời gian nhiệm vụ
- **Ý nghĩa trong văn phòng:** Đồng hồ treo tường phản ánh mốc thời gian thực của công việc.
- **Chức năng nghiệp vụ:** Hiển thị thời điểm bắt đầu, tổng thời gian xử lý, thời gian chờ phê duyệt, thời gian bị chặn, thời điểm kết thúc và ngưỡng timeout. Tính lại từ các mốc timestamp lưu trong backend sau khi reload; không dùng bộ đếm trình duyệt làm nguồn chuẩn và không hiển thị ETA nếu thiếu cơ sở tính hợp lệ.
- **Dữ liệu đầu vào:** `task_id`, `run_id`.
- **Nguồn dữ liệu:** Task Runtime timestamps và Event Journal (`core/tasks/`, `core/events/`).
- **Hành động người dùng:** Xem phân tích thời gian theo từng pha thực thi và ngưỡng timeout còn lại.
- **Quyền cần thiết:** Quyền xem Task.
- **Trạng thái Loading / Empty / Error / Unavailable:** Hiển thị `Chưa bắt đầu` khi ở `QUEUED`; dừng đồng hồ ngay khi chuyển `COMPLETED`/`FAILED`/`CANCELLED`.
- **Phase triển khai:** Backend `PHASE 04`, API `PHASE 12`, UI 2D `P13.03`, 3D `P14.05` (`Đồng hồ treo tường`).
- **Tiêu chí nghiệm thu (DoD):** Tải lại trang trình duyệt giữ nguyên chính xác thời gian đã trôi qua và thời gian chờ/chặn khớp Event Journal.

### 4.6. `mc.notifications`: Trung tâm thông báo
- **Ý nghĩa trong văn phòng:** Chuông hoặc đèn cảnh báo các sự kiện quan trọng của hệ thống.
- **Chức năng nghiệp vụ:** Phát và liệt kê thông báo cho các sự kiện: hoàn thành, thất bại, bị chặn, cần phê duyệt, artifact mới, QA từ chối, lỗi Agent, mất kết nối Provider, vượt ngưỡng ngân sách, quota không khả dụng và cảnh báo bảo mật. Mỗi thông báo chứa ID, loại, độ ưu tiên, thời điểm, nguồn phát sinh, Task/Run liên quan, trạng thái đã xem/chưa xem, đường dẫn đích và hành động hợp lệ. Áp dụng chính sách chống trùng lặp (deduplication); đóng thông báo không đồng nghĩa đã xử lý lỗi Runtime.
- **Dữ liệu đầu vào:** Bộ lọc chưa đọc, mức độ ưu tiên, loại sự kiện.
- **Nguồn dữ liệu:** Event Journal (`core/events/`), Notification Dispatcher (`PHASE 11`), bộ phận `reception`.
- **Hành động người dùng:** Đánh dấu đã đọc/xác nhận, bấm mở thẳng đối tượng gốc.
- **Quyền cần thiết:** Quyền đọc thông báo theo phạm vi nhiệm vụ.
- **Trạng thái Loading / Empty / Error / Unavailable:** Empty khi không có cảnh báo mới; hiển thị trạng thái mất kết nối stream nếu realtime gián đoạn.
- **Phase triển khai:** Backend `PHASE 04`, `PHASE 11`, API `PHASE 12`, UI 2D `P13.01`, 3D `P14.05` (`Chuông hoặc đèn cảnh báo`).
- **Tiêu chí nghiệm thu (DoD):** Không sinh thông báo lặp vô hạn cho cùng một sự cố; bấm thông báo mở đúng đối tượng liên kết.

### 4.7. `mc.action_inbox`: Hộp thư công việc (Action Inbox)
- **Ý nghĩa trong văn phòng:** Khay thư tập hợp toàn bộ đầu việc bắt buộc người vận hành quyết định.
- **Chức năng nghiệp vụ:** Gom riêng các mục đòi hỏi thao tác từ người dùng (duyệt hành động nhạy cảm, trả lời câu hỏi Master, bổ sung tệp thiếu, xác nhận đổi yêu cầu, xử lý kết quả QA thất bại, khắc phục kết nối/cấu hình thiếu, duyệt sản phẩm bàn giao), tách biệt hoàn toàn khỏi thông báo chỉ để đọc. Mọi mục liên kết trực tiếp tới đối tượng gốc.
- **Dữ liệu đầu vào:** Trạng thái chờ xử lý (`WAITING_INPUT`, `WAITING_APPROVAL`, `BLOCKED`, `QA FAIL`).
- **Nguồn dữ liệu:** Approval Gate (`core/governance/`), Reception Interrupts (`PHASE 11`), Task State Machine, QA Records.
- **Hành động người dùng:** Phê duyệt/từ chối, trả lời Master, tải lên tệp thiếu, mở QA report, sửa cấu hình kết nối.
- **Quyền cần thiết:** Tùy hành động cụ thể (`approval.decide`, `task.provide_input`, `connection.manage`).
- **Trạng thái Loading / Empty / Error / Unavailable:** Empty rõ ràng (`Không có đầu việc chờ xử lý`); tự động gỡ khỏi hàng đợi chờ ngay khi hành động được giải quyết.
- **Phase triển khai:** Backend `PHASE 04`, `PHASE 09` (`Reception`), `PHASE 11`, API `PHASE 12`, UI 2D `P13.06`, 3D `P14.05` (`Khay thư`).
- **Tiêu chí nghiệm thu (DoD):** Không trộn thông báo thông tin thuần túy vào Action Inbox; giải quyết xong một mục thì đối tượng gốc tiếp tục chạy đúng quy trình.

### 4.8. `mc.scheduler`: Lịch làm việc
- **Ý nghĩa trong văn phòng:** Lịch treo tường hiển thị công việc có thời hạn và lịch chạy tự động.
- **Chức năng nghiệp vụ:** Hiển thị nhiệm vụ có hạn hoàn thành, công việc đã được Scheduler lập lịch hợp lệ, tác vụ lặp lại (khi Scheduler hỗ trợ) và lịch sử các lượt chạy theo thời gian. Tuyệt đối không tạo lịch giả khi Scheduler ở `PHASE 11` chưa được xây dựng.
- **Dữ liệu đầu vào:** Khoảng ngày/tuần/tháng, bộ lọc loại lịch.
- **Nguồn dữ liệu:** Task Scheduler và Execution History (`PHASE 11`, `storage/`).
- **Hành động người dùng:** Xem lịch, tạm dừng/hủy lịch chạy hợp lệ (qua Governance).
- **Quyền cần thiết:** Quyền xem lịch; quyền quản trị Scheduler cho thao tác tạo/hủy lịch.
- **Trạng thái Loading / Empty / Error / Unavailable:** Hiển thị `Chưa có tác vụ lập lịch` hoặc `Scheduler chưa khả dụng` nếu thành phần nền chưa kích hoạt.
- **Phase triển khai:** Backend `PHASE 11`, API `PHASE 12`, UI 2D `P13.03`, 3D `P14.05` (`Lịch treo tường`).
- **Tiêu chí nghiệm thu (DoD):** Chỉ hiển thị các tác vụ thực sự được Scheduler quản lý và khớp lịch sử chạy trong `.runtime/`.

### 4.9. `mc.meeting_room`: Phòng họp
- **Ý nghĩa trong văn phòng:** Bàn họp nơi các Agent thảo luận có cấu trúc khi gặp bài toán phức tạp.
- **Chức năng nghiệp vụ:** Hiển thị danh sách cuộc họp gắn với Task, thành phần tham dự, agenda, đề xuất, phản biện kèm bằng chứng, quyết định của Master, action items và trạng thái phiên họp. Tuyệt đối không dựng đoạn hội thoại giả khi không có sự kiện họp thật.
- **Dữ liệu đầu vào:** `task_id`, `meeting_id`.
- **Nguồn dữ liệu:** Meeting & Collaboration Engine (`PHASE 06`), Event Journal (`core/events/`).
- **Hành động người dùng:** Quan sát diễn biến cuộc họp, xem bằng chứng trích dẫn và danh sách action items.
- **Quyền cần thiết:** Quyền đọc Task và phiên họp liên quan.
- **Trạng thái Loading / Empty / Error / Unavailable:** Empty rõ ràng đối với các nhiệm vụ đơn giản không cần họp (`Nhiệm vụ không phát sinh phiên họp`).
- **Phase triển khai:** Backend `PHASE 06`, API `PHASE 12`, UI 2D `P13.05`, 3D `P14.04`–`P14.05` (`Bàn họp`).
- **Tiêu chí nghiệm thu (DoD):** Nội dung phòng họp khớp 100% sự kiện thật và bằng chứng; nhiệm vụ đơn giản không bị ép mở họp giả.

### 4.10. `mc.qa_station`: Trạm kiểm định QA
- **Ý nghĩa trong văn phòng:** Bàn kiểm định độc lập chứng thực chất lượng trước khi bàn giao.
- **Chức năng nghiệp vụ:** Hiển thị artifact nào đã kiểm tra, chủ thể hoặc bộ test thực hiện, tiêu chí áp dụng, bằng chứng sinh ra (log test, mã băm SHA-256), kết quả (`PASS` / `FAIL` / `UNVERIFIED`), danh sách lỗi chưa đóng và lịch sử yêu cầu sửa rồi kiểm tra lại. Cho phép truy ngược thẳng tới Task, Run và đúng phiên bản Artifact.
- **Dữ liệu đầu vào:** `task_id`, `artifact_id`, `verification_id`.
- **Nguồn dữ liệu:** QA Verification Records (`storage/`, bộ phận `qa` tại `PHASE 09`), `TOOL_SPEC.md` (`business_verification_status`).
- **Hành động người dùng:** Xem biên bản QA, đọc log lỗi, yêu cầu kiểm tra lại nếu có quyền.
- **Quyền cần thiết:** Quyền xem bằng chứng QA; quyền kích hoạt kiểm tra lại (`qa.rerun` nếu được cấu hình).
- **Trạng thái Loading / Empty / Error / Unavailable:** Ghi nhãn rõ `UNVERIFIED` nếu công cụ đã chạy xong nhưng QA chưa thẩm định.
- **Phase triển khai:** Backend `PHASE 04`, `PHASE 08`, `P09.08`, API `PHASE 12`, UI 2D `P13.03` / `P13.07`, 3D `P14.05` (`Bàn kiểm định`).
- **Tiêu chí nghiệm thu (DoD):** Mọi bản ghi QA đều truy ngược được tới đúng `task_id`, `run_id` và `version` của Artifact; Agent tạo sản phẩm không thể tự cấp `PASS`.

### 4.11. `mc.artifact_cabinet`: Tủ hồ sơ (Artifact Cabinet)
- **Ý nghĩa trong văn phòng:** Tủ hồ sơ lưu giữ toàn bộ tài sản bàn giao đã qua kiểm soát.
- **Chức năng nghiệp vụ:** Quản lý file đầu ra, mã nguồn, báo cáo, hình ảnh, tài liệu, kết quả test và lịch sử phiên bản SemVer. Hỗ trợ tìm kiếm, lọc, xem metadata (SHA-256, dung lượng, MIME), xem trước định dạng hỗ trợ, tải file, xem lịch sử phiên bản và bằng chứng QA đính kèm. Chặn hiển thị file đã xóa hoặc ngoài quyền như thể tải được.
- **Dữ liệu đầu vào:** Bộ lọc Task, loại file, trạng thái QA, từ khóa metadata.
- **Nguồn dữ liệu:** Artifact Registry (`storage/artifacts/`, `PHASE 08`, bộ phận `vault` tại `P09.09`).
- **Hành động người dùng:** Tìm, xem trước, tải xuống, xem lịch sử phiên bản, chuyển vào thùng rác (theo quyền).
- **Quyền cần thiết:** Quyền đọc/tải Artifact trong phạm vi cho phép (`artifact.read`, `artifact.download`), kiểm tra canonical path.
- **Trạng thái Loading / Empty / Error / Unavailable:** Thông báo rõ `Định dạng không hỗ trợ xem trước trực tiếp` (chỉ cho tải về) hoặc `Không có quyền truy cập` thay vì báo lỗi chung chung.
- **Phase triển khai:** Backend `PHASE 08`, `P09.09`, API `PHASE 12`, UI 2D `P13.07`, 3D `P14.05` (`Tủ hồ sơ`).
- **Tiêu chí nghiệm thu (DoD):** Tải đúng tệp khớp SHA-256; tệp đã đưa vào thùng rác hoặc không đủ quyền không thể tải trực tiếp như tệp đang hoạt động.

### 4.12. `mc.comparison_board`: Bảng đối chiếu (Comparison Board)
- **Ý nghĩa trong văn phòng:** Bảng đối chiếu sự khác biệt giữa các phiên bản và tiêu chí nghiệm thu.
- **Chức năng nghiệp vụ:** Cho phép so sánh các bản mã nguồn, hai phiên bản tài liệu, đầu ra trước và sau chỉnh sửa, checklist ban đầu với kết quả cuối và danh sách lỗi QA trước và sau khi sửa. Với định dạng chưa hỗ trợ diff nội dung, hiển thị rõ giới hạn đó cùng bảng metadata đối chiếu (kích thước, SHA-256, thời gian, trạng thái QA); tuyệt đối không dùng LLM bịa diff giả.
- **Dữ liệu đầu vào:** Hai `artifact_id`/`version` hoặc cặp snapshot checklist/QA.
- **Nguồn dữ liệu:** Artifact Registry (`PHASE 08`), QA Records, Checklist Snapshots.
- **Hành động người dùng:** Chọn hai đối tượng để đối chiếu, chuyển đổi chế độ xem diff hoặc metadata.
- **Quyền cần thiết:** Quyền đọc cả hai phiên bản/đối tượng tham gia so sánh.
- **Trạng thái Loading / Empty / Error / Unavailable:** Hiển thị rõ `Chưa hỗ trợ so sánh nội dung cho định dạng này` kèm đối chiếu metadata khi gặp tệp nhị phân chưa có bộ diff.
- **Phase triển khai:** Backend `PHASE 08`, API `PHASE 12`, UI 2D `P13.07`, 3D `P14.05` (`Bảng đối chiếu`).
- **Tiêu chí nghiệm thu (DoD):** Diff code/text xuất phát từ thuật toán so sánh xác định trên nội dung thật; không sinh diff giả bằng LLM.

### 4.13. `mc.activity_journal`: Nhật ký hoạt động (Activity Journal)
- **Ý nghĩa trong văn phòng:** Sổ nhật ký ghi lại toàn bộ dấu vết thực thi theo thời gian thực.
- **Chức năng nghiệp vụ:** Liệt kê chuỗi sự kiện thật: tạo Task, Master lập kế hoạch, Worker nhận việc, gọi Engine, gọi Tool, tạo Artifact, bắt đầu/kết thúc QA, phê duyệt của người dùng và hoàn thành/thất bại. Mọi sự kiện gắn ID liên kết (`event_id`, `task_id`, `run_id`, `session_id`, `actor_id`), dấu thời gian thực tế và liên kết mở bằng chứng tương ứng (đã lọc secret). Không thay thế Event Journal gốc bằng văn bản tóm tắt của LLM.
- **Dữ liệu đầu vào:** Bộ lọc theo Task, Run, Agent, loại sự kiện, khoảng thời gian.
- **Nguồn dữ liệu:** Event Journal (`core/events/`, `PHASE 04`) và Tool Execution Records (`PHASE 07`).
- **Hành động người dùng:** Xem dòng thời gian, lọc sự kiện, mở payload an toàn và nhảy tới bằng chứng/artifact.
- **Quyền cần thiết:** Quyền đọc nhật ký nhiệm vụ (payload luôn được redact secret ở backend).
- **Trạng thái Loading / Empty / Error / Unavailable:** Hỗ trợ phân trang/tải thêm lịch sử và hiển thị rõ trạng thái stream realtime.
- **Phase triển khai:** Backend `P04.03`, `PHASE 06`–`07`, API `P12.02`, UI 2D `P13.03`, 3D `P14.05` (`Sổ nhật ký`).
- **Tiêu chí nghiệm thu (DoD):** Sự kiện hiển thị khớp 100% bản ghi Event Journal sau restart và replay; không rò rỉ token/mật khẩu trong payload.

### 4.14. `mc.engine_status`: Phòng máy chủ và bảng trạng thái Engine
- **Ý nghĩa trong văn phòng:** Tủ máy chủ phản ánh tình trạng kỹ thuật sống của các động cơ AI và phiên chạy.
- **Chức năng nghiệp vụ:** Hiển thị danh sách Engine đã cấu hình, Provider khả dụng, Model đang chạy (khi có dữ liệu), các phiên đang chạy, lỗi kết nối, timeout, trạng thái tiến trình hệ điều hành thực tế và CPU/RAM đo được. Không tự gắn nhãn `Online` chỉ vì đã có cấu hình trên đĩa mà chưa kiểm tra kết nối hoặc handshake thực tế.
- **Dữ liệu đầu vào:** Tín hiệu sức khỏe Engine Adapter và tiến trình runtime.
- **Nguồn dữ liệu:** Engine Adapter Capability/Health Discovery (`PHASE 02`), Session Tracker (`core/sessions/`), OS Process Telemetry.
- **Hành động người dùng:** Xem trạng thái chi tiết từng Engine/Session, kích hoạt kiểm tra sức khỏe kết nối theo yêu cầu người dùng.
- **Quyền cần thiết:** Quyền đọc trạng thái hệ thống; quyền kiểm tra kết nối (`engine.health_check`).
- **Trạng thái Loading / Empty / Error / Unavailable:** Phân tách rõ các trạng thái `Configured (Not Checked)`, `Verified Available`, `Degraded`, `Disconnected` và `Unavailable`.
- **Phase triển khai:** Backend `PHASE 02`, `PHASE 04`, `PHASE 11`, API `PHASE 12`, UI 2D `P13.04` / `P13.08`, 3D `P14.05` (`Tủ máy chủ`).
- **Tiêu chí nghiệm thu (DoD):** Không báo `Online` giả khi chưa kiểm chứng kết nối thật; tiến trình kết thúc hoặc bị hủy phải phản ánh dừng ngay trên giao diện.

### 4.15. `mc.provider_manager`: Bảng quản lý Provider & API Key
- **Ý nghĩa trong văn phòng:** Bảng cấu hình quản trị các kết nối AI và thông tin xác thực BYOK.
- **Chức năng nghiệp vụ:** Liệt kê các Provider hỗ trợ và nhiều `connection_id` thuộc cùng một Provider (bao gồm tình huống hai kết nối Google Gemini); cho phép thêm, sửa tên/ưu tiên, bật/tắt `connection_id`, thay đổi credential, kiểm tra kết nối khi người dùng cho phép, xem danh sách model khả dụng nếu Provider trả về, phân tách rõ khóa API với phạm vi quota theo Project/Account và xem giới hạn được biết. Tuyệt đối không hiển thị lại API key thô sau khi lưu, không lưu key vào `localStorage`, không truyền key qua URL và không có tính năng tự xoay vòng key để né giới hạn.
- **Dữ liệu đầu vào:** `provider_id`, `connection_id`, tên hiển thị, `project_ref`, mức ưu tiên, trạng thái bật/tắt, chuỗi API key nhập mới (chỉ gửi một chiều qua HTTPS/local backend bảo mật).
- **Nguồn dữ liệu:** Provider Connection Registry (`storage/`) và Engine Adapter Discovery (`engines/`).
- **Hành động người dùng:** Thêm/sửa/bật/tắt Connection, thay thế/thu hồi credential, chạy kiểm tra kết nối thủ công.
- **Quyền cần thiết:** Thẩm quyền người vận hành (`connection.manage` đi qua chính sách Governance; Agent/Master không có quyền này).
- **Trạng thái Loading / Empty / Error / Unavailable:** Chỉ hiển thị chuỗi nhận diện đã che (`masked`) và thông báo rõ nếu Provider không cung cấp danh sách model hoặc hạn mức.
- **Phase triển khai:** Backend `P02.06`, `P04.04`–`P04.07`, API `P12.03`, UI 2D `P13.08`, 3D `P14.05` (`Bảng cấu hình`).
- **Tiêu chí nghiệm thu (DoD):** Browser Network/Storage/DOM không chứa raw API key sau khi lưu; hai kết nối Gemini quản lý độc lập không gộp nhầm quota.

### 4.16. `mc.trash_restore`: Thùng rác và khôi phục
- **Ý nghĩa trong văn phòng:** Thùng rác văn phòng xử lý thu hồi và dọn dẹp tài sản an toàn.
- **Chức năng nghiệp vụ:** Hiển thị các mục đã xóa mềm (`soft-deleted`), cho phép khôi phục về vị trí gốc hoặc xóa vĩnh viễn theo quyền. Hỗ trợ chọn nhiều mục và xử lý hàng loạt có hộp thoại xác nhận rõ ràng; phân biệt rạch ròi giữa xóa từng mục và xóa tất cả; bảo vệ các tệp dùng chung hoặc đang được Task khác tham chiếu. Cấm lỗi cài đặt nút "Xóa tất cả" nhưng thực chất chỉ đẩy ngược mục trở lại thùng rác.
- **Dữ liệu đầu vào:** Danh sách `artifact_id` hoặc `task_id`, hành động `restore` hoặc `purge`, xác nhận của người dùng.
- **Nguồn dữ liệu:** Trash & Retention Manager (`storage/`, `P08.05`).
- **Hành động người dùng:** Khôi phục một/nhiều mục, xóa vĩnh viễn có xác nhận kèm cảnh báo tệp tham chiếu chéo.
- **Quyền cần thiết:** Quyền quản lý tài sản (`trash.restore`, `trash.purge` đi qua Approval/Confirm Gate với thao tác xóa hàng loạt hoặc vĩnh viễn).
- **Trạng thái Loading / Empty / Error / Unavailable:** Cảnh báo khóa bảo vệ nếu tệp đang được Task khác tham chiếu; hiển thị báo cáo kết quả từng mục sau xử lý hàng loạt.
- **Phase triển khai:** Backend `P08.05`, API `PHASE 12`, UI 2D `P13.07`, 3D `P14.05` (`Thùng rác`).
- **Tiêu chí nghiệm thu (DoD):** Có bài kiểm thử riêng (`PHASE 15`) chứng minh xóa hàng loạt không làm mất tệp của Task khác và nút xóa vĩnh viễn thực sự xóa đúng mục đã chọn.

### 4.17. `mc.global_search`: Tìm kiếm toàn hệ thống
- **Ý nghĩa trong văn phòng:** Bảng tra cứu nhanh mọi đối tượng trong AI Village.
- **Chức năng nghiệp vụ:** Cho phép tra cứu theo Project, Task ID, Agent, Phòng ban, Artifact, Run, Trạng thái, Khoảng thời gian và tên/nhãn tài liệu. Ưu tiên tìm kiếm theo metadata có chỉ mục trước; chỉ bổ sung tìm kiếm ngữ nghĩa khi hạ tầng dữ liệu phù hợp. Toàn bộ kết quả phải tuân thủ phân quyền dữ liệu hiện hành.
- **Dữ liệu đầu vào:** Chuỗi từ khóa, bộ lọc loại thực thể, phạm vi thời gian.
- **Nguồn dữ liệu:** Metadata Search Index (`storage/knowledge/`, `storage/repositories/`, `P08.04`).
- **Hành động người dùng:** Gõ tìm kiếm, lọc theo nhóm, bấm vào kết quả để đi thẳng tới màn hình chi tiết đối tượng.
- **Quyền cần thiết:** Kết quả trả về tự động lọc theo quyền truy cập của phiên người dùng.
- **Trạng thái Loading / Empty / Error / Unavailable:** Gợi ý điều chỉnh bộ lọc khi không có kết quả; không trả về tệp bí mật hay dữ liệu bị khóa quyền.
- **Phase triển khai:** Backend `P08.04`, API `PHASE 12`, UI 2D `P13.01`, 3D `P14.05` (`Bảng tra cứu`).
- **Tiêu chí nghiệm thu (DoD):** Tìm thấy Artifact/Task/Run/Agent mà không cần nhớ mã UUID; kiểm thử xác nhận kết quả không vượt quyền truy cập.

### 4.18. `mc.settings_governance`: Bảng cài đặt và điều hành
- **Ý nghĩa trong văn phòng:** Bảng điều khiển quy tắc vận hành và tùy chọn giao diện (kết hợp cùng bàn Master điều phối tổng thể).
- **Chức năng nghiệp vụ:** Cho phép cấu hình hợp lệ Provider và Model ưu tiên mặc định, trần ngân sách, chính sách thông báo, phạm vi Tool allowlist được phép hiển thị/gợi ý, điều kiện kích hoạt phê duyệt, tùy chọn giao diện và chế độ chuyển đổi 2D/3D sau này. UI không bao giờ có quyền ghi đè hoặc vô hiệu hóa Permission Manager hay Architectural Invariants.
- **Dữ liệu đầu vào:** Các tham số cấu hình hệ thống trong phạm vi được phép chỉnh sửa.
- **Nguồn dữ liệu:** System Settings & Governance Policy (`core/governance/`, `storage/`).
- **Hành động người dùng:** Xem và cập nhật tùy chọn hợp lệ; gửi yêu cầu duyệt đối với thay đổi chính sách nhạy cảm.
- **Quyền cần thiết:** Quyền người vận hành; thay đổi chính sách bảo mật phải tuân thủ `RULE_SPEC.md` và không hạ chuẩn Invariants.
- **Trạng thái Loading / Empty / Error / Unavailable:** Đọc rõ giá trị đang áp dụng tại backend; từ chối Fail-Closed nếu tham số vi phạm Rule System.
- **Phase triển khai:** Backend `P04.07`, `PHASE 11`, API `PHASE 12`, UI 2D `P13.01` / `P13.08`, 3D `P14.01`–`P14.05`.
- **Tiêu chí nghiệm thu (DoD):** Mọi thay đổi trên bảng cài đặt được lưu bền vững, kiểm chứng qua Permission Manager và áp dụng nhất quán cho các phiên sau.

*(Ghi chú đồng bộ: Trong không gian 3D còn có vật thể **Bàn Master (`mc.master_desk`)** đại diện cho điểm tương tác giao việc và điều khiển tác vụ qua **Master Chat & Task Control** tại `P13.02`, liên kết trực tiếp với `mc.settings_governance` và `Mission Control Home`.)*

## 5. Danh mục Vật thể Văn phòng Tương tác 3D (`PHASE 14`)

Không gian 3D chỉ là lớp trình chiếu trực quan trên cùng mô hình sự kiện và dữ liệu của giao diện 2D, chỉ được triển khai sau khi hoàn tất Gate H (`PHASE 12`–`13`).

### 5.1. Bảng Ánh xạ Đồ vật 3D sang Thành phần Chức năng

| Đồ vật trong văn phòng 3D | `component_id` / Nhóm | Thành phần chức năng 2D mở ra | Phase Backend / API / 2D |
| :--- | :--- | :--- | :--- |
| Bảng trắng | `mc.task_checklist` | Task Checklist Board | `04–05` / `12` / `P13.03` |
| Bảng ghim công việc | `mc.kanban` | Kanban Board | `04` / `12` / `P13.03` |
| Màn hình trung tâm | `mc.operations_monitor` | Mission Control Home & Operations Monitor | `04–05` / `12` / `P13.01` |
| Màn hình thống kê | `mc.usage_cost` | Resource Monitor (Usage & Cost) | `02, 04, 09, 11` / `12` / `P13.08` |
| Đồng hồ treo tường | `mc.task_timing` | Task Timing & SLA Clocks | `04` / `12` / `P13.03` |
| Chuông hoặc đèn cảnh báo | `mc.notifications` | Notification Center | `04, 11` / `12` / `P13.01` |
| Khay thư | `mc.action_inbox` | Action Inbox | `04, 09, 11` / `12` / `P13.06` |
| Lịch treo tường | `mc.scheduler` | Task Scheduler & History | `11` / `12` / `P13.03` |
| Bàn họp | `mc.meeting_room` | Meeting Room 2D | `06` / `12` / `P13.05` |
| Bàn kiểm định | `mc.qa_station` | QA Inspection Station | `04, 08, 09` / `12` / `P13.03, P13.07` |
| Tủ hồ sơ | `mc.artifact_cabinet` | Artifact Vault & File Cabinet | `08, 09` / `12` / `P13.07` |
| Bảng đối chiếu | `mc.comparison_board` | Version & QA Comparison Board | `08` / `12` / `P13.07` |
| Sổ nhật ký | `mc.activity_journal` | Activity Journal (Event Timeline) | `04, 06, 07` / `12` / `P13.03` |
| Tủ máy chủ | `mc.engine_status` | Engine & Process Status Board | `02, 04, 11` / `12` / `P13.04, P13.08` |
| Bảng cấu hình | `mc.provider_manager` | Provider & API Key Manager (`+ Settings`) | `02, 04` / `12` / `P13.08` |
| Thùng rác | `mc.trash_restore` | Trash & Restore Manager | `08` / `12` / `P13.07` |
| Bảng tra cứu | `mc.global_search` | Global Search | `08` / `12` / `P13.01` |
| Bàn Master | `mc.master_desk` | Master Chat, Task Control & Governance Entry | `04, 05` / `12` / `P13.02, P13.01` |

### 5.2. Hợp đồng Tương tác khi Click Vật thể 3D
Mỗi lần người dùng click một vật thể trong `ui/src/village3d/` bắt buộc tuân theo luồng 5 bước:

1. Xác định `component_id` hoặc `action_id` gắn với vật thể được chọn.
2. Mở đúng panel/màn hình giao diện 2D có sẵn tương ứng với `component_id` đó.
3. Đọc dữ liệu từ cùng một API và client store dùng chung với 2D.
4. Thực thi mọi hành động điều khiển thông qua cùng Permission Manager ở backend.
5. Ghi nhận sự kiện vào Event Journal nếu thao tác tạo ra thay đổi trạng thái thật.

**Ràng buộc tuyệt đối cho không gian 3D:**
- Không viết lại logic nghiệp vụ riêng trong scene 3D.
- Không tạo hoạt ảnh nhân vật đang làm việc khi Runtime không phát sự kiện làm việc thực tế (`IDLE`, `RUNNING`, `BLOCKED`, `IN_MEETING`).
- Không bắt buộc người dùng phải vào 3D mới hoàn thành được công việc; toàn bộ tính năng phải chạy đầy đủ trên 2D trước.

## 6. Hợp đồng Dữ liệu Đầu cuối Cần Có Trước Khi Viết UI

Tầng backend phải cung cấp 7 nhóm hợp đồng dữ liệu chuẩn (không mâu thuẫn với `AGENT_SPEC.md`, `RULE_SPEC.md` và `TOOL_SPEC.md`). Tuyệt đối không dồn toàn bộ logic chi tiết của cả 7 nhóm vào `P04.01`; `P04.01` chỉ thiết lập hợp đồng định danh và liên kết nền tảng, còn logic từng miền hoàn thiện tại phase sở hữu:

| Nhóm dữ liệu | Các trường bắt buộc / định hướng | Phase triển khai backend sở hữu | Nguồn sở hữu | Phase được phép hiển thị UI |
| :--- | :--- | :--- | :--- | :--- |
| **1. Project / Task / Checklist** | `project_id` (nhóm tổ chức công việc, không phải Runtime thứ hai), `task_id`, `run_id`, `parent_task_id` / `subtask_id`, `checklist_item_id`, `owner_agent_id`, `actual_status`, `created_at`, `started_at`, `updated_at`, `completed_at`, `expected_artifact`, `actual_artifact`, `verification_ref` | Hợp đồng lõi tại `P04.01`–`P04.02`, kế hoạch & phân công bước tại `P05.03`–`P05.04`, lịch tại `P11.03` | `core/tasks/`, `core/orchestration/`, `storage/repositories/` | `PHASE 12`–`13` (`P13.01`–`P13.03`), `PHASE 14` |
| **2. Activity / Events** | `event_id`, `task_id`, `run_id`, `session_id`, `actor_id`, `event_type`, `timestamp`, `related_artifact_id` / `evidence_id`, `redacted_payload` | Chuẩn stream tại `P02.03`, Event Journal tại `P04.03`, sự kiện họp/tool tại `PHASE 06`–`07` | `core/events/`, `storage/repositories/` | `PHASE 12` (`P12.02`), `PHASE 13`, `PHASE 14` |
| **3. Usage / Telemetry** | `provider_id`, `connection_id`, `project_billing_scope` (an toàn), `model_id`, `agent_id`, `task_id`, `run_id`, `token_usage` (`null` nếu thiếu), `usage_source`, `cost_source`, `telemetry_status`, `cpu_ram_time` (nếu đo được), `timestamp` (cấm raw API key) | Hợp đồng adapter tại `P02.06`, bộ phận Resources tại `P09.02`, Resource Ledger tại `P11.04` | `engines/`, `core/optimization/`, `storage/` | `PHASE 12`–`13` (`P13.08`), `PHASE 14` |
| **4. Notifications** | `notification_id`, `event_ref`, `category`, `severity`, `read_state` / `acknowledged`, `target_url` / `target_object_id`, `required_action`, `created_at` | Phát sự kiện tại `P04.03`, phân loại & khử trùng lặp tại `P09.06` (`Reception`) và `PHASE 11` | `core/events/`, `storage/repositories/` | `PHASE 12`–`13` (`P13.01`, `P13.06`), `PHASE 14` |
| **5. QA & Evidence** | `verification_id`, `tested_artifact_version`, `test_type`, `test_result` (`PASS`/`FAIL`/`UNVERIFIED`), `evidence_ref`, `failure_details`, `verified_timestamp` | Hợp đồng kết quả tại `PHASE 04`, lưu kho bằng chứng tại `P08.03`, thẩm định độc lập tại `P09.08` | `storage/`, bộ phận `qa` | `PHASE 12`–`13` (`P13.03`, `P13.07`), `PHASE 14` |
| **6. Artifact & Version** | `artifact_id`, `task_id`, `run_id`, `version` (SemVer), `mime_type`, `sha256`, `storage_ref`, `qa_status`, `lifecycle_trash_status` | Lưu kho & phiên bản tại `P08.01`–`P08.02`, thùng rác/tìm kiếm tại `P08.04`–`P08.05`, Vault tại `P09.09` | `storage/artifacts/`, bộ phận `vault` | `PHASE 12`–`13` (`P13.07`), `PHASE 14` |
| **7. Approval** | `approval_id`, `requested_action`, `requesting_agent`, `target_resource`, `risk_summary`, `status`, `operator_decision`, `timestamp` | Approval Gate tại `P04.07`, quy tắc kiểm tra `RULE_SPEC.md` | `core/governance/`, `storage/repositories/` | `PHASE 12`–`13` (`P13.06`), `PHASE 14` |

## 7. Ma trận Truy xuất Giai đoạn (Component Traceability Matrix)

| Component | Backend Phase | API Phase | UI 2D Phase | 3D Phase | Acceptance |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `mc.task_checklist` | `PHASE 04`, `PHASE 05` | `PHASE 12` | `P13.03` | `P14.05` | Khớp trạng thái thật sau restart; hoàn thành có bằng chứng |
| `mc.kanban` | `PHASE 04` | `PHASE 12` | `P13.03` | `P14.05` | Đồng bộ Task Runtime; kéo/thả bị chặn nếu trái guard |
| `mc.operations_monitor` | `PHASE 04`, `PHASE 05` | `PHASE 12` | `P13.01` | `P14.05` | Không lệch số liệu với Dashboard/Journal; reconnect chuẩn |
| `mc.usage_cost` | `P02.06`, `PHASE 04`, `P09.02`, `P11.04` | `PHASE 12` | `P13.08` | `P14.05` | Phân tách `null` và `0`; không lộ secret; không giả quota |
| `mc.task_timing` | `PHASE 04` | `PHASE 12` | `P13.03` | `P14.05` | Tính lại từ timestamps gốc sau reload; không ETA giả |
| `mc.notifications` | `PHASE 04`, `PHASE 11` | `PHASE 12` | `P13.01` | `P14.05` | Khử trùng lặp; deep-link mở đúng đối tượng gốc |
| `mc.action_inbox` | `PHASE 04`, `P09.06`, `PHASE 11` | `PHASE 12` | `P13.06` | `P14.05` | Tách biệt khỏi thông báo đọc; xử lý xong gỡ đúng mục |
| `mc.scheduler` | `PHASE 11` | `PHASE 12` | `P13.03` | `P14.05` | Chỉ hiển thị lịch do Scheduler thật quản lý |
| `mc.meeting_room` | `PHASE 06` | `PHASE 12` | `P13.05` | `P14.04`, `P14.05` | Phản ánh phiên họp và quyết định thật, không hội thoại giả |
| `mc.qa_station` | `PHASE 04`, `PHASE 08`, `P09.08` | `PHASE 12` | `P13.03`, `P13.07` | `P14.05` | Truy ngược tới `task_id`/`run_id`/`version`; QA độc lập |
| `mc.artifact_cabinet` | `PHASE 08`, `P09.09` | `PHASE 12` | `P13.07` | `P14.05` | Tải tệp khớp SHA-256; khóa tệp đã xóa hoặc thiếu quyền |
| `mc.comparison_board` | `PHASE 08` | `PHASE 12` | `P13.07` | `P14.05` | Diff xác định trên dữ liệu thật; không dùng LLM bịa diff |
| `mc.activity_journal` | `P04.03`, `PHASE 06`–`07` | `P12.02` | `P13.03` | `P14.05` | Replay đúng Event Journal; payload đã lọc bí mật |
| `mc.engine_status` | `PHASE 02`, `PHASE 04`, `PHASE 11` | `PHASE 12` | `P13.04`, `P13.08` | `P14.05` | Không báo `Online` khi chưa kiểm tra kết nối thật |
| `mc.provider_manager` | `P02.06`, `P04.04`–`P04.07` | `P12.03` | `P13.08` | `P14.05` | Không trả raw key về browser; hỗ trợ đa kết nối Gemini |
| `mc.trash_restore` | `P08.05` | `PHASE 12` | `P13.07` | `P14.05` | Xác nhận xóa hàng loạt; bảo vệ tệp dùng chung |
| `mc.global_search` | `P08.04` | `PHASE 12` | `P13.01` | `P14.05` | Tìm theo metadata trước; lọc kết quả theo phân quyền |
| `mc.settings_governance` | `P04.07`, `PHASE 11` | `PHASE 12` | `P13.01`, `P13.08` | `P14.01`–`P14.05` | Tuân thủ Permission Manager; không hạ chuẩn Invariants |
| `mc.master_desk` | `PHASE 04`, `PHASE 05` | `PHASE 12` | `P13.02`, `P13.01` | `P14.05` | Giao việc, gián đoạn/hủy, liên kết thẳng Checklist/QA |

## 8. Phân quyền và Bảo mật (Permissions & Security)

- **Kiểm soát hành động qua Permission Manager:** Giao diện không bao giờ tự ghi thẳng vào cơ sở dữ liệu lõi hay bỏ qua Permission Manager. Mọi hành động nhạy cảm (duyệt lệnh, hủy tác vụ, xóa hàng loạt trong thùng rác, thêm/sửa/bật-tắt Provider Connection, thay đổi chính sách cài đặt) đều phải qua `api/` auth, guard của `core/` và xác nhận người vận hành khi chạm `REQUIRE_APPROVAL`.
- **Bảo vệ Credential BYOK:** Trình duyệt không lưu raw API key trong `localStorage`, URL hay DOM; API chỉ trả về tên kết nối, nhà cung cấp, trạng thái và chuỗi nhận diện đã che (`masked`), tuyệt đối không lộ `credential_ref` nội bộ hoặc secret thô.
- **Bảo vệ dữ liệu dùng chung và tìm kiếm theo quyền:** Kết quả tìm kiếm `Global Search` lọc theo quyền phiên hiện hành; thao tác xóa vĩnh viễn trong `Trash & Restore` khóa bảo vệ tệp còn được Task khác tham chiếu.

## 9. Quy chuẩn Trạng thái Loading, Empty, Error và Unavailable

Mọi thành phần trên Mission Control phải tách biệt rõ 5 trạng thái giao diện:

| Trạng thái | Điều kiện xuất hiện | Hành vi hiển thị bắt buộc |
| :--- | :--- | :--- |
| **Loading** | Đang truy vấn API hoặc đồng bộ snapshot ban đầu | Hiển thị skeleton/indicator rõ ràng, không hiển thị số `0` giả trong lúc chờ tải. |
| **Empty** | Backend phản hồi hợp lệ nhưng chưa có bản ghi (chưa có Task, chưa có cuộc họp, Action Inbox trống) | Hiển thị thông điệp trạng thái trống rõ nghĩa kèm gợi ý hành động hợp lệ tiếp theo. |
| **Error** | Lỗi API, lỗi guard State Machine, hoặc mất quyền truy cập | Hiển thị mã lỗi và thông điệp kỹ thuật an toàn (đã lọc secret), cho phép thử lại có kiểm soát. |
| **Offline / Disconnected** | Mất kết nối backend hoặc gián đoạn luồng SSE/WebSocket | Gắn nhãn cảnh báo mất kết nối realtime, ngừng coi trạng thái cũ là đang chạy và đồng bộ snapshot khi nối lại. |
| **Unavailable** | Thành phần/Scheduler chưa triển khai hoặc Provider không trả số liệu telemetry/quota | Hiển thị nguyên văn `Không có dữ liệu từ Provider` hoặc trạng thái chưa khả dụng; tuyệt đối không thay `null` bằng `0` hay tự bịa `%` tiến độ/quota. |

## 10. Danh mục Kiểm tra Nghiệm thu UX (Acceptance Checklist)

- [ ] Đủ 18 thành phần chức năng và Bàn Master có nguồn dữ liệu backend xác định rõ trong lộ trình `PHASE 02`–`12`.
- [ ] Không tồn tại hai nguồn sự thật song song cho trạng thái Task, Checklist, Kanban, Thông báo hay Usage.
- [ ] Không thành phần UI nào yêu cầu dữ liệu mà backend roadmap không tạo ra.
- [ ] Mọi hành động cần quyền đều đi qua Permission Manager và Approval Gate; không có nút xóa hàng loạt hoặc sửa Connection vượt quyền.
- [ ] Không có vật thể 3D nào mang logic nghiệp vụ riêng hoặc hiển thị trước `PHASE 14` (sau khi đạt Gate H).
- [ ] Toàn bộ trạng thái `null` / thiếu telemetry được giữ nguyên tính trung thực (`Không có dữ liệu từ Provider`), không rò rỉ API key hay thông tin nhạy cảm.
