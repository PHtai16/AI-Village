# Quy tắc phát triển cho Coding Agent trong AI Village

Tài liệu này quy định các nguyên tắc bắt buộc đối với tất cả Agent tham gia xây dựng và bảo trì dự án AI Village. Mọi vi phạm quy tắc đều bị coi là không đạt tiêu chuẩn bàn giao.

## 1. Nghiên cứu tài liệu trước khi thao tác

Trước khi chỉnh sửa bất kỳ tệp tin nào trong kho mã nguồn, Agent bắt buộc phải đọc kỹ tài liệu kiến trúc tại thư mục `docs/` và trạng thái lộ trình trong `docs/PROGRESS.md`. Không được bắt đầu viết code khi chưa nắm rõ ngữ cảnh và ranh giới trách nhiệm của thư mục đang làm việc.

## 2. Kỷ luật theo phạm vi Phase

Mỗi nhiệm vụ chỉ được triển khai đúng phạm vi của tiểu phase được giao. Tuyệt đối không tự ý viết trước các module của phase tiếp theo, không cài đặt dependencies đón đầu và không tạo file rỗng bừa bãi. Khi hoàn thành đúng mục tiêu được chỉ định, Agent phải dừng lại để nghiệm thu thay vì tự ý bước sang công đoạn khác.

## 3. Phân định ranh giới kiến trúc

Mã nguồn phải tuân thủ nghiêm ngặt ranh giới trách nhiệm giữa các tầng:

- `agents/`: Chỉ chứa manifest, định nghĩa vai trò, mô tả năng lực và quyền hạn của nhân viên AI. Không đưa logic xử lý nghiệp vụ phức tạp vào đây.
- `core/`: Chứa thuật toán điều phối, máy trạng thái nhiệm vụ, cơ chế phân xử hội họp, quản lý phiên và chính sách kiểm soát. Không phụ thuộc trực tiếp vào bất kỳ thư viện UI nào.
- `engines/`: Lớp adapter trung gian kết nối runtime của Antigravity hoặc các nhà cung cấp AI. Không chứa logic phân bổ phòng ban.
- `tools/`: Các công cụ thực thi độc lập về file, mã nguồn, trình duyệt, nghiên cứu và văn phòng. Không chứa prompt hoặc logic gắn liền với một nhân vật cụ thể.
- `skills/`: Quy trình nghiệp vụ tái sử dụng đã được kiểm chứng.
- `ui/`: Tầng giao diện người dùng, chỉ nhận dữ liệu hiển thị và gửi tín hiệu điều khiển qua API. Không chứa mã điều phối trung tâm.

## 4. Tránh hardcode quy trình

Không thiết lập các luồng làm việc cứng nhắc áp đặt cho mọi loại bài toán. Cơ chế điều phối cần tạo điều kiện để Master lập nhóm linh hoạt theo năng lực của từng Agent và tính chất thực tế của nhiệm vụ được giao.

## 5. Nguyên tắc kiểm chứng bằng chứng thực tế

Không bao giờ công bố nhiệm vụ đã hoàn thành nếu thiếu dữ liệu chứng minh. Bằng chứng hợp lệ bao gồm kết quả thực thi lệnh, mã băm tập tin, nội dung file đầu ra và log kiểm thử tự động. Những tuyên bố bằng lời nói mà không kèm kết quả chạy thực tế đều không có giá trị nghiệm thu.

## 6. Cấm giả lập hành vi công cụ

Khi được yêu cầu chạy lệnh, đọc dữ liệu hoặc thực thi bài kiểm tra, Agent phải sử dụng công cụ hệ thống thật. Tuyệt đối không tự bịa đặt nội dung phản hồi, không giả lập log kết quả và không đoán trước trạng thái hệ thống.

## 7. Độc lập trong kiểm chứng chất lượng

Agent tạo ra mã nguồn hoặc sản phẩm không được phép tự đóng vai người kiểm thử để cấp trạng thái đạt yêu cầu. Việc nghiệm thu phải được thực hiện thông qua module QA hoặc bài kiểm thử độc lập với các tiêu chí định lượng rõ ràng.

## 8. Bảo mật dữ liệu và tôn trọng quyền hạn

Không đưa khóa API, mật khẩu, chuỗi kết nối cơ sở dữ liệu hoặc thông tin cá nhân vào mã nguồn và lịch sử Git. File `.env` và dữ liệu trong `.runtime/` phải luôn được bảo vệ ngoài phạm vi theo dõi của kho lưu trữ. Agent không được tự ý thực thi các lệnh phá hủy hệ thống hoặc can thiệp vào các thư mục ngoài phạm vi dự án.

## 9. Yêu cầu kiểm thử bắt buộc

Mọi thay đổi trong mã nguồn cần đi kèm bài kiểm thử tương ứng tại thư mục `tests/`. Trước khi bàn giao, Agent phải chạy kiểm thử để xác nhận mã mới không phá vỡ các chức năng sẵn có.

## 10. Cập nhật tiến độ chính xác

Chỉ được đánh dấu hoàn thành cho các mục công việc trong `docs/PROGRESS.md` khi toàn bộ tiêu chí nghiệm thu đã được kiểm chứng thực tế và có mã định danh commit tương ứng. Mọi vướng mắc kỹ thuật phải được ghi nhận trung thực vào mục cảnh báo của tài liệu theo dõi.

## 11. Tự động đồng bộ lên Git khi hoàn tất công việc

Mỗi khi hoàn thành một nhiệm vụ hoặc một tiểu phase và đã nghiệm thu đạt yêu cầu, Agent có trách nhiệm tạo commit theo chuẩn Conventional Commits, cập nhật thông tin commit vào `docs/PROGRESS.md` và lập tức thực thi `git push` lên kho lưu trữ từ xa. Không để mã nguồn đã kiểm chứng ở trạng thái chưa đồng bộ.

