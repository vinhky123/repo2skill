# Kế hoạch kiểm chứng pilot repo2skill

## Giả thuyết cần kiểm chứng

Persona chính là maintainer hoặc senior developer ưu tiên repository đa ngôn ngữ và đã dùng coding agent; cohort vẫn gồm repo một ecosystem để kiểm tra collector. Job cốt lõi: tạo hướng dẫn ngắn; truy command/path/rule đến nguồn; cập nhật khi repo đổi mà không ghi đè quyết định của người; và review inference mà không đọc lại toàn repository.

Giả thuyết giá trị: so với viết tay hoặc chạy một lệnh `/init` rồi sửa, generator có provenance giảm thời gian tạo và duy trì `AGENTS.md`/Skill mà không tăng claim sai, context thừa hay xung đột sở hữu. Giả thuyết nhu cầu OSS maintainer hiện **chưa được chứng minh**. Các ngưỡng dưới đây là planning targets để ra quyết định, không phải benchmark hoặc cam kết hiệu năng.

## Cohort và tuyển người

Tuyển 6–10 người qua mạng lưới maintainer/senior developer và cộng đồng kỹ thuật phù hợp. Không gửi tin tự động hoặc scrape liên hệ. Mỗi người dùng một repo họ có quyền chia sẻ; quota theo ecosystem chính là 2 JS/TS, 2 Python, 1 Go, 1 Rust, trong đó cố gắng có ít nhất 2 repo đa ngôn ngữ hoặc generic-only.

Screening 15 phút xác nhận họ từng sửa agent instructions, có thay đổi lặp lại và sẵn sàng làm hai phiên 45–60 phút cách nhau 7–14 ngày. Mặc định chỉ thu note và artifact đã redact; recording cần consent riêng.

## Thiết kế task và baseline

Phiên 1 dùng một change scenario thật hoặc fixture tương đương. Người tham gia tạo/cập nhật hướng dẫn bằng workflow thường dùng (viết tay hoặc `/init` rồi sửa) và repo2skill no-AI, giải thích nguồn của năm claim và đánh dấu claim không chắc. Cohort được phân AB/BA: một nửa dùng baseline trước ở phiên 1 rồi repo2skill trước ở phiên 2; nửa còn lại dùng thứ tự ngược lại.

Phiên 2 tạo thay đổi có kiểm soát: sửa một command, xóa một config và thêm file khớp glob/negative predicate. Mỗi người tiếp tục theo thứ tự AB/BA đã phân; workflow repo2skill dùng `check → generate → review → export`. Với P1, chỉ sau no-AI task, người tham gia có thể opt-in chạy Codex và so sánh candidate bổ sung; no-AI là đối chứng bắt buộc.

Mỗi task lưu duration, output trước/sau, review decisions, claim→evidence audit, diagnostics và phỏng vấn ngắn. Facilitator không sửa output hoặc dẫn người tham gia đến kết quả mong muốn.

## Chỉ số và target đề xuất

| Chỉ số | Cách đo | Planning target chưa kiểm chứng |
|---|---|---|
| Time-to-reviewable generate | Từ bắt đầu đến staged output mà maintainer sẵn sàng review | Median giảm ≥30% so với baseline |
| Maintenance time | Hoàn tất scenario add/change/delete đến output accepted | Median giảm ≥25% |
| Unsupported factual claims | Claim factual live không có evidence hợp lệ hoặc nói quá evidence | 0 trên mọi task |
| Provenance retrieval | 5 claim được truy đúng source+locator trong giới hạn thời gian | ≥90% trong ≤60 giây/claim |
| Useful retention | Claim generated được accept và giữ sau phiên 2 | Median ≥60% |
| Noise | Claim bị reject vì thừa, trùng hoặc quá chung | ≤20% candidate |
| Invalidation recall | Dependency đổi làm đúng claim stale, gồm file mới | 100% scenario kiểm soát |
| Ownership safety | Manual edit tạo conflict; text ngoài marker còn nguyên | 100% scenario |
| Return intent | Người tham gia chọn dùng lại cho lần update kế tiếp | ≥5/8 hoặc ≥60% cohort hợp lệ |

Hỏi thêm điều gì làm họ không tin output, provenance nào giúp review, nhãn observed/inferred có rõ không, và ledger có đáng công không. Báo Codex riêng với no-AI, theo ngôn ngữ và loại repo.

## Measurement và quyền riêng tư

Mặc định không telemetry. Khi người dùng bật `--measure`, CLI P1 ghi JSONL local theo allowlist trong contract và không truyền đi; không có path/ID, source text, secret, prompt, URL hay error text. Facilitator vẫn dùng task template local cho quan sát thủ công. Người dùng xem artifact và chủ động đồng ý gửi; notes có retention date và được redact định danh.

## Cổng quyết định

**Continue** khi invariant an toàn đạt 100%, ít nhất 60% cohort có return intent, cả time-to-reviewable và maintenance đạt target hoặc qualitative evidence giải thích rõ sai số nhỏ, và ít nhất hai người giữ output trong repo qua phiên 2.

**Narrow** khi provenance/safety đạt nhưng chỉ một workflow có giá trị: giữ adaptation nhỏ, no-AI và ecosystem có retention tốt; hoãn Codex, monorepo hoặc collector mới. Cũng narrow nếu output tạo một lần hữu ích nhưng maintenance không tiết kiệm thời gian.

**Pause release/pilot để sửa và retest** nếu factual claim unsupported vào live output hoặc ownership làm mất sửa tay. Đây là safety gate, không tự chứng minh thị trường không có nhu cầu.

**Stop** việc phát triển thành platform nếu sau task hợp lệ, dưới 30% muốn dùng lại hoặc đa số thấy ledger/review tốn hơn giá trị maintenance. Khi dừng, có thể giữ adaptation nhỏ và tài liệu nghiên cứu; không suy diễn kết quả cohort thành kết luận cho toàn bộ thị trường.
