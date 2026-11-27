# Quy tắc phát triển cho Coding Agent trong AI Village

Tài liệu này quy định các nguyên tắc bắt buộc đối với tất cả Agent tham gia xây dựng và bảo trì dự án AI Village. Mọi vi phạm quy tắc đều bị coi là không đạt tiêu chuẩn bàn giao.

## 1. Thứ tự tài liệu tham chiếu

Mọi Agent khi tham gia dự án phải tuân thủ nghiêm ngặt hệ thống tài liệu theo thứ tự ưu tiên sau:

1. **`docs/architecture/SYSTEM_BLUEPRINT.md`:** Single Source of Truth cho toàn bộ dự án, định hình tầm nhìn sản phẩm, ranh giới các thư mục, cơ chế cộng tác và các nguyên tắc kiến trúc bất biến.
2. **`docs/standards/`:** Nơi chứa các hợp đồng dữ liệu, cấu trúc schema và quy định kỹ thuật chi tiết gồm `AGENT_SPEC.md`, `DEPARTMENT_SPEC.md` và các đặc tả tiếp theo.
3. **`docs/PROGRESS.md`:** Nguồn chuẩn duy nhất theo dõi tiến độ, lộ trình phase, trạng thái hoàn thành và bằng chứng nghiệm thu thực tế.
4. **`AGENTS.md`:** Quy chuẩn làm việc, nguyên tắc an toàn và kỷ luật kỹ thuật bắt buộc của Coding Agent.

Các tài liệu trên có ranh giới rõ ràng và không được thay thế trách nhiệm cho nhau.

## 2. Quy trình kiểm tra 6 bước trước khi triển khai

Trước khi chỉnh sửa hoặc tạo mới bất kỳ tệp tin nào, Coding Agent bắt buộc phải thực hiện tuần tự:

1. Xác định chính xác mã tiểu phase và mục tiêu được giao.
2. Đọc kỹ các phần liên quan trực tiếp trong [docs/architecture/SYSTEM_BLUEPRINT.md](file:///C:/DoCho/AI-Village/docs/architecture/SYSTEM_BLUEPRINT.md).
3. Đọc các tiêu chuẩn kỹ thuật tương ứng tại [docs/standards/](file:///C:/DoCho/AI-Village/docs/standards/).
4. Đối chiếu phạm vi và tiêu chí nghiệm thu trong [docs/PROGRESS.md](file:///C:/DoCho/AI-Village/docs/PROGRESS.md).
5. Kiểm tra trạng thái Git HEAD, nhật ký commit và `git diff` để đảm bảo vùng làm việc sạch sẽ.
6. Chỉ triển khai đúng phạm vi được chỉ định, tuyệt đối không mở rộng ra các thành phần khác.

## 3. Xử lý mâu thuẫn kiến trúc và hợp đồng dữ liệu

Khi phát hiện sự không đồng nhất giữa Blueprint và các tài liệu Specification:

- Tuyệt đối không tự ý chọn một cách triển khai theo suy đoán cá nhân.
- Tuyệt đối không âm thầm thay đổi mã nguồn hoặc cấu trúc tài liệu kiến trúc.
- Báo cáo rõ ràng tệp tin, vị trí mâu thuẫn và đề xuất phương án giải quyết cụ thể.
- Yêu cầu người vận hành phê duyệt trước khi áp dụng bất kỳ thay đổi nào lên các hợp đồng dữ liệu.
- Lưu ý đặc biệt: Định danh Master Agent (`id: "core.master"`, `department: "orchestration"`) và trường `system_role` (`"worker"` / `"orchestrator"`) đã được đồng bộ vào `docs/standards/AGENT_SPEC.md`, triển khai tại `core/agents/` (`AgentRegistryService`, `AgentRecruitmentService`) và kích hoạt cùng `agents/master/agent.yaml` từ Phase 05.

## 4. Bảo vệ định hướng dự án

Coding Agent phải tuân thủ các ranh giới định hướng bất biến:

- Không tự ý thay đổi bản chất của AI Village thành phần mềm chuyên biệt về Marketing hoặc giải pháp kinh doanh thương mại.
- Không áp đặt quy trình làm việc cứng nhắc bắt mọi nhiệm vụ phải đi qua toàn bộ 9 phòng ban.
- Không tạo hoạt động AI giả mạo, không viết kết quả mock cho tác vụ live và không bịa đặt số liệu token usage.
- Không tự ý khóa framework CrewAI khi chưa hoàn thành đầy đủ các bài thử nghiệm thực tế.
- Không triển khai giao diện 3D trước khi Agent Runtime và giao diện 2D hoàn thành việc kiểm chứng qua các Gate tương ứng.
- Không tự ý mở rộng phạm vi công việc của phase được giao.

## 5. Kỷ luật theo phạm vi Phase

Mỗi nhiệm vụ chỉ được triển khai đúng phạm vi của tiểu phase được giao. Tuyệt đối không tự ý viết trước các module của phase tiếp theo, không cài đặt dependencies đón đầu và không tạo file rỗng bừa bãi. Khi hoàn thành đúng mục tiêu được chỉ định, Agent phải dừng lại để nghiệm thu thay vì tự ý bước sang công đoạn khác.

## 6. Phân định ranh giới kiến trúc

Mã nguồn phải tuân thủ nghiêm ngặt ranh giới trách nhiệm giữa các tầng:

- `agents/`: Chỉ chứa manifest, định nghĩa vai trò, mô tả năng lực và quyền hạn của nhân viên AI và phòng ban. Không đưa logic xử lý nghiệp vụ phức tạp vào đây.
- `core/`: Chứa thuật toán điều phối, máy trạng thái nhiệm vụ, cơ chế phân xử hội họp, quản lý phiên và chính sách kiểm soát. Không phụ thuộc trực tiếp vào bất kỳ thư viện UI nào.
- `engines/`: Lớp adapter trung gian kết nối runtime của Antigravity hoặc các nhà cung cấp AI. Không chứa logic phân bổ phòng ban.
- `tools/`: Các công cụ thực thi độc lập về file, mã nguồn, trình duyệt, nghiên cứu và văn phòng. Không chứa prompt hoặc logic gắn liền với một nhân vật cụ thể.
- `skills/`: Quy trình nghiệp vụ tái sử dụng đã được kiểm chứng.
- `storage/`: Mã quản lý database, memory, knowledge và metadata.
- `ui/`: Tầng giao diện người dùng, chỉ nhận dữ liệu hiển thị và gửi tín hiệu điều khiển qua API. Không chứa mã điều phối trung tâm.

## 7. Tránh hardcode quy trình

Không thiết lập các luồng làm việc cứng nhắc áp đặt cho mọi loại bài toán. Cơ chế điều phối cần tạo điều kiện để Master lập nhóm linh hoạt theo năng lực của từng Agent và tính chất thực tế của nhiệm vụ được giao.

## 8. Nguyên tắc kiểm chứng bằng chứng thực tế

Không bao giờ công bố nhiệm vụ đã hoàn thành nếu thiếu dữ liệu chứng minh. Bằng chứng hợp lệ bao gồm kết quả thực thi lệnh, mã băm tập tin, nội dung file đầu ra và log kiểm thử tự động. Những tuyên bố bằng lời nói mà không kèm kết quả chạy thực tế đều không có giá trị nghiệm thu.

## 9. Cấm giả lập hành vi công cụ và số liệu

Khi được yêu cầu chạy lệnh, đọc dữ liệu hoặc thực thi bài kiểm tra, Agent phải sử dụng công cụ hệ thống thật. Tuyệt đối không tự bịa đặt nội dung phản hồi, không giả lập log kết quả và không đoán trước trạng thái hệ thống. Khi số liệu token hoặc tài nguyên không có sẵn từ provider, phải đặt giá trị null và gắn nhãn chưa có dữ liệu.

## 10. Độc lập trong kiểm chứng chất lượng

Agent tạo ra mã nguồn hoặc sản phẩm không được phép tự đóng vai người kiểm thử để cấp trạng thái đạt yêu cầu. Việc nghiệm thu phải được thực hiện thông qua module QA hoặc bài kiểm thử độc lập với các tiêu chí định lượng rõ ràng.

## 11. Bảo mật dữ liệu và tôn trọng quyền hạn

Không đưa khóa API, mật khẩu, chuỗi kết nối cơ sở dữ liệu hoặc thông tin cá nhân vào mã nguồn và lịch sử Git. File `.env` và dữ liệu trong `.runtime/` phải luôn được bảo vệ ngoài phạm vi theo dõi của kho lưu trữ. Agent không được tự ý thực thi các lệnh phá hủy hệ thống hoặc can thiệp vào các thư mục ngoài phạm vi dự án.

## 12. Yêu cầu kiểm thử bắt buộc

Mọi thay đổi trong mã nguồn cần đi kèm bài kiểm thử tương ứng tại thư mục `tests/`. Trước khi bàn giao, Agent phải chạy kiểm thử để xác nhận mã mới không phá vỡ các chức năng sẵn có.

## 13. Cập nhật tiến độ chính xác

Chỉ được đánh dấu hoàn thành cho các mục công việc trong `docs/PROGRESS.md` khi toàn bộ tiêu chí nghiệm thu đã được kiểm chứng thực tế và có mã định danh commit tương ứng. Mọi vướng mắc kỹ thuật phải được ghi nhận trung thực vào mục cảnh báo của tài liệu theo dõi.

## 14. Tự động đồng bộ lên Git khi hoàn tất công việc

Mỗi khi hoàn thành một nhiệm vụ hoặc một tiểu phase và đã nghiệm thu đạt yêu cầu, Agent có trách nhiệm tạo commit theo chuẩn Conventional Commits, cập nhật thông tin commit vào `docs/PROGRESS.md` và lập tức thực thi `git push` lên kho lưu trữ từ xa. Không để mã nguồn đã kiểm chứng ở trạng thái chưa đồng bộ.
