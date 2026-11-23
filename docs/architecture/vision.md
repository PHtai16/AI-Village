# AI Village: Tầm nhìn Sản phẩm và Phạm vi MVP (Product Vision & MVP Definition)

Tài liệu này hệ thống hóa tầm nhìn sản phẩm, phạm vi MVP, ranh giới các hạng mục chưa triển khai trong MVP, định nghĩa hoàn thành và tiêu chí nghiệm thu dựa trên các quyết định đã thống nhất tại [SYSTEM_BLUEPRINT.md](file:///C:/DoCho/AI-Village/docs/architecture/SYSTEM_BLUEPRINT.md), [README.md](file:///C:/DoCho/AI-Village/README.md) và [PROGRESS.md](file:///C:/DoCho/AI-Village/docs/PROGRESS.md). Tài liệu không đặt ra tầm nhìn mới ngoài các văn bản nền tảng đã thiết lập.

## 1. Tầm nhìn Sản phẩm (Product Vision)

AI Village là hệ thống Multi-Agent đa chức năng dưới dạng môi trường làm việc trực quan, tổ chức các nhân viên AI theo miền năng lực chuyên môn và điều phối động thông qua Master Agent.

1. **Tổ chức theo năng lực, không ép quy trình cứng:** Chín phòng ban mặc định (`hr`, `resources`, `service`, `marketing`, `it`, `reception`, `process`, `qa`, `vault`) đóng vai trò là các miền chuyên môn và không gian quản lý năng lực. Phòng ban không phải dây chuyền xử lý cố định bắt mọi nhiệm vụ đều phải đi qua đủ 9 bộ phận.
2. **Master điều phối động:** Người dùng giao nhiệm vụ trực tiếp cho Master (`core.master`). Master phân tích đề bài, lựa chọn nhân sự phù hợp theo nhãn năng lực (`capabilities`), thành lập nhóm công tác liên phòng ban khi cần thiết, tổ chức trao đổi có cấu trúc và bàn giao sản phẩm cuối cùng.
3. **Thực thi thật và chứng thực bằng bằng chứng (Evidence First):** Hệ thống phải giải quyết nhiệm vụ thật thông qua Engine Adapter và công cụ thực tế tại `tools/`, tạo ra artifact thật trên đĩa và xác minh bằng dữ liệu đo đạc độc lập (mã băm SHA-256, mã thoát lệnh, nhật ký kiểm thử). Hệ thống nghiêm cấm hành vi mô phỏng tiến độ giả, bịa đặt số liệu token hoặc để Agent sản xuất tự chứng nhận hoàn thành thay cho QA.

## 2. Phạm vi MVP

Phiên bản MVP (tương ứng các chặng chứng minh lõi từ Gate B đến Gate D và nền tảng kiểm chứng cơ bản, trước khi mở rộng toàn bộ 9 phòng ban đầy đủ hay giao diện 3D) cần chứng minh tối thiểu các năng lực vận hành thực tế sau:

1. **Tiếp nhận yêu cầu từ người dùng:** Người dùng gửi được nhiệm vụ tới Master thông qua giao diện hoặc API nền tảng ở mức tối thiểu.
2. **Phân tích và lập kế hoạch của Master:** Master tiếp nhận mục tiêu, làm rõ yêu cầu và xác định đầu ra cùng tiêu chí nghiệm thu.
3. **Tuyển chọn Agent phù hợp:** Hệ thống lựa chọn được Agent phù hợp dựa trên năng lực khai báo trong Agent Registry thay vì gọi cứng toàn bộ phòng ban.
4. **Thực thi Engine và Tool thật:** Agent gọi được Engine Adapter (mặc định dự kiến là Antigravity) và bộ công cụ thực tế trong phạm vi workspace được cấp quyền.
5. **Tạo artifact thật:** Tác vụ sinh ra tệp tin hoặc mã nguồn thật nằm trong vùng lưu trữ hợp lệ và truy xuất được kèm mã băm kiểm chứng.
6. **Theo dõi trạng thái nhiệm vụ trung thực:** Hệ thống cập nhật và hiển thị đúng trạng thái thực thi của nhiệm vụ dựa trên Event Journal và tiến trình thực tế.
7. **Xử lý lỗi, timeout và hủy tác vụ cơ bản:** Runtime phát hiện được lỗi thực thi, áp dụng giới hạn timeout, cho phép cancel an toàn và thu hồi tiến trình con mà không treo trạng thái.
8. **Kiểm chứng kết quả độc lập:** Sản phẩm trước khi hoàn tất phải qua bước kiểm tra độc lập của QA hoặc bộ kiểm thử tự động, tách biệt với Agent tạo sản phẩm.
9. **Bàn giao sản phẩm có bằng chứng:** Master tổng hợp kết quả cuối cùng, chỉ ra giới hạn nếu có và bàn giao kèm đường dẫn artifact cùng bằng chứng xác minh.

MVP không yêu cầu triển khai đầy đủ đồng thời toàn bộ 9 phòng ban ở mức hoàn chỉnh trước khi chứng minh được vòng lặp thực thi nền tảng nêu trên.

## 3. Những việc chưa thuộc MVP

Các hạng mục sau đây nằm ngoài phạm vi MVP và chỉ triển khai ở các phase sau khi các Gate tiền điều kiện đã đạt:

- Không gian văn phòng 3D hoàn chỉnh (`PHASE 14`).
- Hoạt cảnh nhân vật 3D và hiệu ứng trực quan nâng cao chưa gắn với sự kiện thực tế.
- Tự mở rộng vô hạn số lượng Agent hoặc tự động sinh nhân sự không qua kiểm soát đăng ký hợp lệ.
- Cơ chế tự cải tiến quy trình tự động hoàn toàn không có bước kiểm thử benchmark và phê duyệt của con người.
- Hỗ trợ đồng thời mọi AI Provider bên ngoài ngay trong giai đoạn đầu.
- Toàn bộ bộ công cụ văn phòng, tự động hóa trình duyệt và phát triển phần mềm ở quy mô đầy đủ.
- Các tính năng thuộc các phase tiếp theo khi chưa hoàn tất kiểm chứng qua Gate tương ứng (như quản lý ngắt đa nhiệm nâng cao, Vault tìm kiếm ngữ nghĩa hay bộ kiểm thử trình duyệt E2E đầy đủ).

## 4. Definition of Done

### 4.1. Phân biệt trạng thái Task và trạng thái hoàn thành Phase

AI Village tách biệt hoàn toàn hai hệ thống trạng thái:

- **Trạng thái Task (`Task Lifecycle`):** Phản ánh trạng thái vận hành của một nhiệm vụ cụ thể trong Runtime, được quản lý bởi máy trạng thái tại `core/` và xác nhận bằng sự kiện hệ thống thực tế. Trong [AGENT_SPEC.md](file:///C:/DoCho/AI-Village/docs/standards/AGENT_SPEC.md), trạng thái hoàn thành hợp lệ của Task là `COMPLETED` (tương ứng khái niệm `DONE` ở mức mô tả nghiệp vụ).
- **Trạng thái hoàn thành Phase (`Phase / Roadmap Status`):** Phản ánh mức độ hoàn tất của một tiểu phase phát triển phần mềm trong [PROGRESS.md](file:///C:/DoCho/AI-Village/docs/PROGRESS.md) (`TODO`, `IN_PROGRESS`, `BLOCKED`, `DONE`), chỉ được công nhận `DONE` khi có mã nguồn hoặc tài liệu thực tế, test vượt qua và commit Git đã kiểm chứng.

Không được dùng trạng thái `DONE` của một tiểu phase tài liệu để suy diễn rằng chức năng Runtime của Task đã chạy được.

### 4.2. Định nghĩa các trạng thái nhiệm vụ chính

| Trạng thái | Định nghĩa chuẩn trong hệ thống | Điều kiện chuyển trạng thái hợp lệ |
| :--- | :--- | :--- |
| **`DONE` (`COMPLETED`)** | Nhiệm vụ đã hoàn tất toàn bộ đầu ra cam kết và vượt qua bước kiểm chứng độc lập. | Có artifact thực tế, có bằng chứng xác minh (`VERIFIED_PASS` từ QA hoặc test runner độc lập) và được Master xác nhận bàn giao. Agent sản xuất không được tự gán trạng thái này. |
| **`FAILED`** | Nhiệm vụ thất bại do lỗi thực thi nội bộ, sai hợp đồng dữ liệu hoặc không vượt qua kiểm chứng sau khi hết giới hạn thử lại. | Ghi nhận mã lỗi có cấu trúc, nhật ký đã lọc bí mật (`redacted`), dừng mọi retry và báo cáo rõ nguyên nhân thất bại. |
| **`BLOCKED`** | Nhiệm vụ bị đình trệ do yếu tố khách quan bên ngoài hoặc điều kiện tiên quyết chưa đáp ứng (mất kết nối provider, thiếu tệp đầu vào, công cụ chưa khả dụng, thiếu dependency). | Phân biệt rõ với `FAILED` và `WAITING_APPROVAL`; hệ thống giữ nguyên hiện trường an toàn và nêu rõ tác nhân gây nghẽn để xử lý. |
| **`WAITING_APPROVAL`** | Nhiệm vụ hoặc nhánh thực thi tạm dừng có chủ đích để chờ người vận hành phê duyệt hành động nhạy cảm qua Approval Gate. | Phát sinh từ quyết định `APPROVAL_REQUIRED` của Permission Manager, có bản ghi `ApprovalRequest` gắn `approval_id` và thời hạn hiệu lực. |
| **`CANCELLED`** | Nhiệm vụ bị hủy bỏ chủ động theo lệnh của người dùng hoặc Master trước khi hoàn tất. | Tín hiệu hủy chấm dứt toàn bộ tiến trình con trong sandbox, dọn dẹp tài nguyên cấp phát và ghi nhận sự kiện hủy vào Event Journal. |

## 5. Tiêu chí Nghiệm thu MVP (Success Criteria)

MVP chỉ được công nhận đạt khi toàn bộ các tiêu chí kiểm chứng sau có bằng chứng thực thi thật (chưa có tiêu chí nào trong số này được tuyên bố đã triển khai tại Chặng A):

1. **Thực thi thật qua Engine mặc định:** Từ một yêu cầu gửi vào ứng dụng, Engine Adapter gọi được Antigravity CLI (hoặc engine chuẩn hóa theo contract) thao tác trên workspace thật và tạo ra tệp tin có mã băm SHA-256 khớp thực tế trên đĩa.
2. **Cách ly phiên và xử lý sự cố:** Hai phiên nhiệm vụ chạy tách biệt không trộn ngữ cảnh hay tệp tin; các tình huống timeout, cancel và lỗi provider trả về trạng thái chuẩn xác mà không lặp vô hạn.
3. **Vòng đời Task bền vững:** Nhiệm vụ chuyển trạng thái đúng guard (`QUEUED` -> `RUNNING` -> `WAITING_APPROVAL`/`BLOCKED`/`COMPLETED`/`FAILED`/`CANCELLED`), lưu vào SQLite và không mất hoặc giả mạo trạng thái sau khi khởi động lại hệ thống.
4. **Phối hợp Master và Worker có kiểm chứng độc lập:** Master chọn đúng nhân sự theo năng lực, ghi nhận handoff kèm định danh và QA độc lập phát hiện hoặc xác minh lỗi trước khi Master bàn giao sản phẩm cuối cùng.
5. **Tuân thủ ranh giới an toàn:** Mọi truy cập tệp và công cụ đều đi qua Permission Manager theo cơ chế Fail-Closed; không rò rỉ khóa bí mật ra nhật ký, phản hồi API hay kho lưu trữ Git.

## 6. Trạng thái Phê duyệt (Approval)

### 6.1. Đối chiếu các quyết định đã được người vận hành xác nhận

- Định hướng AI Village là hệ thống Multi-Agent đa chức năng (không phải phần mềm marketing chuyên biệt), tổ chức theo 9 phòng ban mặc định mang tính miền năng lực và điều phối qua Master Agent tại [README.md](file:///C:/DoCho/AI-Village/README.md) và [SYSTEM_BLUEPRINT.md](file:///C:/DoCho/AI-Village/docs/architecture/SYSTEM_BLUEPRINT.md).
- Nguyên tắc bất biến: Evidence First, tách biệt QA độc lập, không khóa cứng CrewAI trước khi kiểm chứng Phase 03, ưu tiên 2D trước 3D, bảo mật Fail-Closed và cấu trúc 14 thư mục gốc tại [AGENTS.md](file:///C:/DoCho/AI-Village/AGENTS.md) và bộ tiêu chuẩn `docs/standards/`.

### 6.2. Nội dung chờ người vận hành phê duyệt

- **Trạng thái phê duyệt tài liệu `vision.md`:** `PENDING_USER_APPROVAL`
- **Các mục cụ thể cần người vận hành xác nhận để đóng P00.01 và Mục 5 của `PROGRESS.md`:**
  1. Phê duyệt chính thức bản tổng hợp Phạm vi MVP, Danh mục ngoài MVP và Tiêu chí nghiệm thu MVP trong tài liệu này.
  2. Chốt các quyết định còn mở tại Mục 5 của [PROGRESS.md](file:///C:/DoCho/AI-Village/docs/PROGRESS.md): tên dự án chính thức (`AI Village`), đường dẫn repository (`C:\DoCho\AI-Village`), phạm vi quyền tự động mặc định của Master trong workspace so với các thao tác cần người duyệt, phương án hiển thị 3D và lựa chọn công nghệ 3D sau khi đạt M4, cũng như việc chốt CrewAI chỉ thực hiện sau P03.01–P03.02.
