---
name: ai-village-batch-runner
description: >-
  Kỹ năng chuyển một yêu cầu ngắn theo mục tiến độ của AI Village thành bản đặc tả
  batch gọn nhẹ từ docs/PROGRESS.md rồi trực tiếp triển khai theo đúng ranh giới kiến trúc,
  kiểm thử fail-closed, báo cáo kiểm toán và đồng bộ Git. Kích hoạt khi người dùng yêu cầu
  làm một mục hay batch như "làm P05.07", "triển khai mục tiếp theo trong progress",
  "chạy batch P06.01 + P06.02", hoặc nhờ lập prompt ngắn cho một tiểu phase mới.
---

# Hướng dẫn Kỹ năng: `ai-village-batch-runner`

## 1. Mục đích và phạm vi

Kỹ năng này thay thế các bản prompt dài lặp lại quy tắc nền bằng một bản đặc tả thực thi gọn từ 4.000 đến 8.000 ký tự dựng trực tiếp từ `docs/PROGRESS.md`, sau đó điều hành trọn vòng lặp THINK, PROPOSE, EXECUTE, GATE, AUDIT và GIT SYNC.

Mọi nguyên tắc cố định đã chốt trong `AGENTS.md`, `docs/architecture/SYSTEM_BLUEPRINT.md` và `docs/standards/` chỉ được dẫn chiếu theo tên tài liệu, không sao chép lại nguyên văn vào đặc tả mới nhằm tiết kiệm ngữ cảnh hội thoại.

## 2. Điều kiện kích hoạt và tình huống không kích hoạt

Kích hoạt kỹ năng khi người dùng đưa mã mục lộ trình dạng `PXX.YY`, nhắc tới mục tiếp theo trong `PROGRESS.md`, yêu cầu chạy một cặp tiểu phase liên hoàn hoặc yêu cầu soạn bản đặc tả thực thi cho AI Village.

Không kích hoạt khi người dùng chỉ hỏi giải thích kiến trúc, yêu cầu sửa một lỗi nhỏ đơn lẻ ngoài lộ trình phase, đánh giá chất lượng tài liệu thuần túy hoặc tạo một kỹ năng tổng quát dùng cho nhiều dự án khác.

## 3. Quy trình phân tích đầu vào và kiểm tra nền

Trước khi viết dòng mã đầu tiên, thực hiện tuần tự 4 bước kiểm tra thực tế trên kho mã:

1. Đọc đúng mục `PXX.YY` mục tiêu và các tiểu phase lân cận trong `docs/PROGRESS.md` để xác định ranh giới dừng bắt buộc.
2. Kiểm tra trạng thái Git hiện tại qua `git status --short` và `git rev-parse HEAD` nhằm bảo đảm vùng làm việc sạch.
3. Chỉ đọc phần đặc tả trực tiếp liên quan trong `docs/standards/` và mã nguồn nối trực tiếp thay vì đọc tràn lan toàn bộ tài liệu các phase cũ.
4. Rà soát 4 điểm chạm cố định của kho mã AI Village:
   - Danh sách mã được phép đánh dấu `[x]` trong `tests/standards/test_standards_consistency.py`.
   - Cấu trúc thư mục và tệp cho phép trong `tests/stage_a/test_stage_a_baseline.py`.
   - Danh sách `PERMISSION_MATRIX_ACTIONS` và các invariant hệ thống trong `core/governance/evaluator.py`.
   - Số hiệu migration mới nhất tại `storage/db/migrations/` cùng các khóa ngoại `tasks`, `runs(run_id, task_id)`, `sessions` và `events`.

## 4. Quy trình thực hiện 6 bước

1. Dựng bản đặc tả batch 5 khối theo mẫu tại [references/batch-brief-template.md](./references/batch-brief-template.md), chỉ ghi các bất biến mới phát sinh của tiểu phase đang làm.
2. Thiết kế ranh giới module sạch trước khi viết mã: chủ động tách `models`, `validator` hoặc `preflight`, `service` và `recovery` khi logic phình to nhằm giữ từng tệp dễ đọc thay vì dồn quá khoảng 800 dòng vào một tệp, đồng thời giữ tính thuần AST trong `core/`.
3. Triển khai tuần tự từng tiểu phase theo cổng riêng: khi chạy batch hai tiểu phase liên hoàn, phần A phải qua kiểm thử đạt ngưỡng mới được mở sang phần B; dừng lại báo cáo ngay nếu gặp xung đột giữa Blueprint và Standards.
4. Xây dựng bộ kiểm thử tự động có cấu trúc rõ ràng: dùng hàm test riêng cho từng kịch bản nghiệp vụ hoặc `@pytest.mark.parametrize` cho ma trận tiêm lỗi để đầu ra pytest hiển thị rành mạch từng ca; phủ đủ happy path, đường lỗi fail-closed, rollback giao dịch SQLite, khôi phục sau restart và kiểm tra AST tầng kiến trúc.
5. Cập nhật báo cáo kiểm toán tại `docs/reviews/PHASE_XX_PXX_YY_AUDIT.md`, cập nhật `docs/PROGRESS.md`, `tests/README.md` và `tests/standards/test_standards_consistency.py`; chạy lại `python -m pytest tests -v -rs` và `git diff --check` strictly sau chỉnh sửa tệp cuối cùng.
6. Tạo Conventional Commit tách biệt cho từng tiểu phase hoàn chỉnh, chạy `git push origin main`, xác nhận `git rev-parse HEAD` trùng với `git ls-remote origin refs/heads/main` và `git status --short` trống.

## 5. Công cụ và lưu ý môi trường Windows

Chạy kiểm thử qua `python -m pytest` và lưu ý kho mã chạy trên Windows PowerShell 5. Khi nối lệnh trên PowerShell, luôn dùng `; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE };` thay cho `&&`. Dùng `Select-String` để tìm kiếm ký hiệu trong mã nguồn khi `rg` không khả dụng trên hệ thống.

## 6. Xử lý ngoại lệ và điểm dừng an toàn

Phải dừng lại và xin phê duyệt của người vận hành trước khi thực hiện bất kỳ thao tác nào thuộc nhóm rủi ro:

- Chạm hoặc sửa các tệp migration lịch sử đã phát hành trong `storage/db/migrations/`.
- Đổi quy tắc dữ liệu hoặc sửa kiến trúc đang xung đột giữa `SYSTEM_BLUEPRINT.md` và `docs/standards/`.
- Kích hoạt biến môi trường `AI_VILLAGE_RUN_LIVE=1` để gọi CLI `agy.exe` thật hoặc thực hiện thao tác xóa dữ liệu và force push Git.

Mọi lượt chạy chưa kích hoạt `AI_VILLAGE_RUN_LIVE=1` chỉ được phép dán nhãn `OFFLINE_INTEGRATION_VERIFIED`, tuyệt đối không báo `LIVE_VERIFIED`, và bắt buộc ghi chính xác số ca skipped kèm nguyên nhân.

## 7. Tiêu chí kiểm soát chất lượng đầu ra

Giữ nguyên ranh giới phân tách 10 trạng thái vòng đời: không gộp trạng thái đăng ký Agent, phân công Team, khởi tạo Session, chạy Engine, sinh Output thô, QA xác minh Artifact, tạo Handoff, tiếp nhận Handoff, QA Task và hoàn thành Task vào cùng một nhãn thành công chung. Chỉ công bố các con số kiểm thử, đường dẫn tệp, tên ký hiệu và mã SHA quan sát trực tiếp sau lần chỉnh sửa cuối. Dừng đúng tại điểm kết của tiểu phase được giao, không viết đón đầu sang phase sau hoặc tự đánh dấu mở khóa Gate tổng hợp khi chưa nghiệm thu.

## 8. Nguyên tắc bảo mật và toàn vẹn dữ liệu

Không đưa khóa API, token, mật khẩu hay đường dẫn tuyệt đối cá nhân vào mã nguồn, tài liệu hay prompt mẫu. Duy trì kiểm tra chuỗi bí mật bằng `ensure_not_secret()`, `redact_secrets()`, giới hạn thư mục bằng `validate_workspace_path()`, và bảo vệ lịch sử SQLite bằng trigger chặn `UPDATE` và `DELETE`.

## 9. Định dạng báo cáo bàn giao gọn

Thay vì lặp lại hàng chục mục đã ghi chi tiết trong tệp `docs/reviews/`, phản hồi kết thúc tới người dùng gồm 6 đến 8 mục súc tích: phạm vi và mã SHA commit, tệp và kiến trúc thay đổi, các bất biến đã khóa, bảng kết quả kiểm thử sau lần sửa cuối, liên kết tài liệu kiểm toán và tiến độ, trạng thái đồng bộ Git, giới hạn hiện tại cùng điểm dừng kế tiếp.

## 10. Ví dụ sử dụng và tham chiếu

Người dùng chỉ cần gõ ngắn gọn như `"làm P05.07"`, `"triển khai P06.01 + P06.02"` hoặc `"lập đặc tả batch cho mục tiếp theo trong progress"`. Cấu trúc bản đặc tả 5 khối và bộ 5 kịch bản kiểm định kỹ năng nằm tại [references/batch-brief-template.md](./references/batch-brief-template.md).
