# Đánh giá hai đầu vào nghiên cứu cho repo2skill

## Kết luận

Hai tài liệu thống nhất ở một thesis đáng theo đuổi: repo2skill không nên là công cụ “đưa repository cho LLM rồi sinh `SKILL.md`”, mà là một hệ thống thu thập bằng chứng từ repository, suy luận các quy trình có thể tái sử dụng, rồi xuất chúng thành tri thức cho coding agent. Điểm khác biệt có khả năng tạo giá trị là khả năng trả lời ba câu hỏi: instruction này dựa vào đâu, còn đúng ở commit hiện tại không, và thay đổi nào của repository đã làm nó mất hiệu lực.

**Đánh giá của người review:** khả thi để làm một CLI hữu ích; chưa có đủ bằng chứng để khẳng định khả thi như một “compiler” tổng quát hoặc có moat mạnh. Rủi ro chính không nằm ở việc viết Markdown mà ở precision: một instruction sai nhưng được trình bày như quy tắc chính thức có thể gây hại lặp lại. Vì vậy MVP nên tối ưu cho “ít claim nhưng giải thích được”, đo false authoritative instruction rate, và luôn để con người duyệt trước khi publish.

## Nội dung trực tiếp từ hai tài liệu

Các điểm sau là đề xuất hoặc nhận định có trong đầu vào, chưa phải kết luận đã được kiểm chứng độc lập:

- Repository là nguồn bằng chứng chính; AI chỉ làm lớp suy luận và diễn đạt.
- Pipeline nên tách deterministic analysis, Repository IR, Knowledge IR và renderer. Output gồm context luôn bật (`AGENTS.md`, `CLAUDE.md`, `GEMINI.md`) và procedure theo tác vụ dưới dạng Agent Skills.
- Provenance nên gắn tới từng claim, không chỉ tới cả skill. Một claim có evidence, scope, extractor/version, commit, trạng thái và confidence.
- Incremental sync nên hoạt động trên fact delta và reverse evidence lookup, thay vì file đổi là sinh lại mọi skill liên quan.
- Local-first và gọi CLI Codex/Claude/Gemini như subprocess giúp giữ credential ở provider hiện hữu. Các đầu vào có cấu trúc như CI, manifests, dbt artifacts và Terraform JSON phù hợp hơn raw repository dump.
- `explain`, dry run, budget guard, review PR và benchmark được xem là chức năng cốt lõi cho trust.
- Nội dung repository phải được coi là untrusted data; scan mặc định không nên chạy code của repo. Dữ liệu Terraform có thể chứa secret và cần allowlist/redaction.

## Những insight thực sự hữu ích

**Claim là đơn vị sản phẩm đúng.** Nếu chỉ quản lý file `SKILL.md`, hệ thống không thể biết một câu cụ thể bị stale khi evidence biến mất. Claim-level provenance cho phép giải thích, recompute support và chỉ sửa phần bị ảnh hưởng.

**Authority khác frequency.** CI/config hiện hành, tài liệu policy, code generator và lịch sử thay đổi có mức thẩm quyền khác nhau. Một pattern lặp lại nhiều lần có thể chỉ là technical debt. Knowledge model cần biểu diễn contradiction và các trạng thái như `supported`, `contested`, `superseded`, `stale`, `needs_confirmation` thay vì ép thành đúng/sai.

**Semantic drift phải tương đối với claim.** Đổi timeout có thể không ảnh hưởng procedure, trừ khi một claim ghi rõ timeout bắt buộc. Vì thế classification phải bắt đầu từ fact delta rồi tra claims sử dụng fact đó; không thể chỉ phân loại theo loại file.

**Progressive disclosure quyết định chất lượng output.** Context luôn bật chỉ nên chứa repo map, boundary và lệnh phổ quát. Procedure có trigger, nhiều bước và validation mới đáng thành skill. Audit metadata nên ở sidecar hoặc lệnh `explain`.

**Benchmark phải đi trước lời hứa “thông minh”.** So sánh raw-repo-to-LLM với deterministic facts, IR có/không history và hệ thống đầy đủ là cách kiểm tra phần kiến trúc phức tạp có tạo giá trị thật hay không. Precision, evidence precision, abstention quality, false regeneration rate và agent task success quan trọng hơn demo đẹp.

## Điểm chưa đồng ý hoặc đang bị nói quá

“Repository là canonical truth” là cách nói quá mạnh. Repository phản ánh trạng thái hiện tại nhưng có thể chứa policy mâu thuẫn, code chết, generated files, migration tạm, secret, và quy trình vận hành nằm ngoài repo. Cách chính xác hơn là: repository là nguồn bằng chứng mặc định, có thứ bậc thẩm quyền và có thể chưa đầy đủ.

Từ “deterministic” cũng dễ tạo cảm giác chắc chắn giả. Parser có thể xác định một command tồn tại, nhưng không tự chứng minh command đó là quy trình bắt buộc, áp dụng cho scope nào, hoặc còn được dùng. Phần chuyển fact thành normative claim vẫn là suy luận và phải có abstention/review.

Confidence kiểu `0.96` chưa có ý nghĩa nếu chưa được hiệu chỉnh trên tập label. MVP nên hiển thị evidence dimensions và nhãn `high/medium/low` có rule rõ ràng; chưa nên trình bày xác suất. Tương tự, “semantic drift là moat” hiện là hypothesis. Chỉ benchmark và dữ liệu accept/edit/reject qua thời gian mới chứng minh được.

Tự chạy AI trong hook sau `git pull` xung đột với nguyên tắc không tiêu quota vô hình, làm chậm Git và khó debug. Hook chỉ nên đánh dấu trạng thái dirty hoặc in lệnh tiếp theo; analysis nên chạy chủ động trong CLI ở MVP.

Việc gọi subscription-backed CLI như subprocess có thể khác giữa provider về chế độ non-interactive, schema output, quota, điều khoản và độ ổn định. Đây là dependency sản phẩm cần spike sớm, không nên coi là abstraction đã giải quyết.

## Đường cắt MVP đề xuất

Yêu cầu sản phẩm là **đa ngôn ngữ từ ngày đầu**. Điều đó nên được đáp ứng bằng coverage rộng ở tầng artifact và ecosystem, không bằng lời hứa deep semantic AST cho mọi ngôn ngữ.

MVP nên nhận repository viết bằng bất kỳ ngôn ngữ phổ biến nào và phân tích được file tree, manifests, lockfiles, scripts, Make/Task targets, CI, test/config/docs, Git diff và quan hệ path-level. Một registry nhỏ cho phép thêm ecosystem packs. Tree-sitter có thể cung cấp import/symbol outline cho nhiều grammar, nhưng precise cross-file resolution, type analysis và SCIP chỉ là capability tùy chọn; output phải ghi rõ giới hạn coverage.

MVP không cần extractor sâu riêng cho dbt hay Terraform để chứng minh lõi đa ngôn ngữ. Hai ecosystem này có thể được nhận diện qua metadata/config generic; extractor dựa trên artifact chính thức chỉ nên thêm sau khi benchmark cho thấy depth đem lại precision đủ lớn. Một provider CLI adapter hoàn chỉnh cộng protocol trung lập tốt hơn ba adapter nửa vời; output renderer vẫn có thể hỗ trợ Agent Skills và một context target trước.

Luồng tối thiểu nên là:

1. `init`: detect artifacts, chọn provider/output, tạo config.
2. `scan`: static-only extraction, lưu facts/evidence và generation manifest.
3. `discover`: đề xuất ít workflow candidates kèm evidence/contradiction; chưa ghi output.
4. `explain`: cho thấy claim, scope, nguồn, giới hạn và lý do xếp hạng.
5. `generate`: chỉ render candidates được chấp nhận.
6. `validate`: kiểm tra schema, stale/missing evidence và cấu trúc output.

Hoãn Git hooks tự động, daemon, GitHub App/PR bot, semantic auto-sync, embeddings/vector DB, graph DB, multi-repo, production execution và auto-approval. Incremental scan có thể chỉ là “diff changed artifacts rồi re-extract”; selective semantic regeneration là giai đoạn sau khi full scan có benchmark.

## Các giả định cần kiểm chứng trước khi mở rộng

- Người dùng có đau vì agent thiếu repository procedure đủ nhiều để cài thêm CLI và duyệt knowledge proposals.
- Skills sinh ra cải thiện tỷ lệ hoàn thành task so với `AGENTS.md` được viết tốt, với token overhead chấp nhận được.
- Static evidence đủ để đạt precision cao trên nhiều ngôn ngữ; hệ thống biết abstain khi coverage thấp.
- Có thể gọi ít nhất một coding-agent CLI ổn định, lấy structured output, báo chi phí hợp lý và không chạm credential.
- Claim/evidence schema đủ nhỏ để maintainers hiểu và sửa tay; provenance không biến thành dữ liệu audit quá nặng.
- Người review có thể quyết định accept/edit/reject trong vài phút. Nếu review tốn ngang viết skill, value proposition yếu.
- Fact delta thực sự giảm đáng kể context/cost mà không làm drift recall giảm nguy hiểm.
- Output format và discovery semantics giữa các agent đủ ổn định để renderer adapters đáng duy trì.

Gate nên là benchmark trên 8–12 repository đa ngôn ngữ và 30–50 workflow candidates được maintainer label. Chỉ mở rộng semantic sync khi precision cao, false authoritative instruction gần bằng 0, và output giúp agent tốt hơn baseline.

## Nguồn được nhắc trong nghiên cứu, chưa xác minh

Hai file đính kèm không lưu literal URL/link target, nên không thể chép lại “source URLs” mà không tự đoán. Các nguồn/tên dự án được viện dẫn gồm OpenAI Agent Skills và hướng dẫn `AGENTS.md`; Anthropic Agent Skills/open standard và cảnh báo security; Gemini CLI Skills, workspace discovery và auto-memory; chuẩn AGENTS.md; nghiên cứu GitSkills (công bố 08/2026); Rulesync; Tree-sitter; SCIP; dbt `manifest.json`; Terraform JSON output/cảnh báo sensitive values; GitHub Actions token permissions và GitHub App permissions. Tất cả số liệu, ngày tháng, mức hỗ trợ và tuyên bố tương thích từ các nguồn này phải được coi là **unverified** cho tới khi đối chiếu tài liệu chính thức/primary source.

Đặc biệt cần xác minh các claim dễ thay đổi: quy mô “hơn 60.000 repository” dùng AGENTS.md; 3.797.117 `SKILL.md` occurrences/282.200 repositories của GitSkills; vị trí discovery và precedence của từng agent; khả năng gọi CLI qua subprocess bằng subscription; và tình trạng “open standard”/portability thực tế.
