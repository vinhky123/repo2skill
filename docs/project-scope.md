# Phạm vi dự án repo2skill

## Quyết định sản phẩm

repo2skill là một **generator tri thức repository có truy vết**, chạy local-first. Công cụ đọc repository, trích xuất fact, tạo claim hữu ích, rồi dựng các managed block cho `AGENTS.md`, Agent Skill và báo cáo evidence. Generation vẫn là chức năng lõi; `check` và `explain` tồn tại để người dùng biết nội dung đã sinh còn đúng hay không và dựa trên nguồn nào.

Sản phẩm không được bắt đầu như một compiler greenfield lớn. Baseline hiện là **ứng viên fork/adapt có chọn lọc**, không phải cam kết port nguyên [`osmaneb23/agents-md`](https://github.com/osmaneb23/agents-md) tại commit `346b5f78dcbb48d9a700df98ac7f5f8e02f751ea`. Upstream có scanner, dedup với docs, generator ngắn gọn, managed sections, fingerprint, `init/update/diff/explain` và đường chạy không AI; repo2skill chỉ lấy primitive/rule có giá trị, giữ attribution MIT và đặt provenance trước Markdown. Lựa chọn scanner, renderer, fingerprint và các công nghệ khác theo khuyến nghị có điều kiện trong [technology-reuse-decision.md](./technology-reuse-decision.md), dựa trên [audit tái sử dụng](./upstream-reuse-audit.md) và [deep dive runtime](./deep-dive-agents-md.md). Schema và lifecycle vẫn theo [thiết kế provenance](./reuse-and-provenance-design.md).

Người dùng đầu tiên là maintainer hoặc senior developer có repository đa ngôn ngữ và đã dùng coding agent. Họ muốn tạo hướng dẫn vận hành đủ ngắn để agent dùng được, nhưng không chấp nhận command, path hoặc quy tắc không rõ nguồn. Giá trị cần kiểm chứng trong pilot là: generator tiết kiệm công duy trì mà không tạo thêm claim sai hoặc context thừa so với tài liệu do maintainer viết tay.

## Phạm vi đa ngôn ngữ ngày đầu

Yêu cầu ngày đầu là hỗ trợ nhiều ngôn ngữ, không phải đạt semantic parity cho mọi hệ sinh thái. Baseline upstream có collector thực cho bốn nhóm:

- JavaScript/TypeScript: `package.json`, lockfile, scripts, `tsconfig`, framework và test config cùng một số convention nông.
- Python: `pyproject.toml`, package/test configuration và một số phát hiện AST giới hạn.
- Go: `go.mod`, framework hints và template command có nhãn rõ.
- Rust: `Cargo.toml`, test layout và template command có nhãn rõ.

Với ngôn ngữ khác, ngày đầu repo2skill vẫn cung cấp inventory, Git state, CI/workflow, Makefile, Dockerfile, YAML/JSON/TOML/Markdown và diagnostics. Nó phải công bố coverage đã chạy, collector nào không tồn tại và fact nào chưa thể xác nhận. JVM native, Maven/Gradle semantics và extractor sâu theo framework là công việc về sau, chỉ làm khi pilot cho thấy nhu cầu. Không được tiếp tục cam kết năm ecosystem native khi source tái sử dụng chỉ có bốn.

Baseline upstream có ba giới hạn phải loại bỏ mà không mở rộng MVP thành semantic AST: traversal dùng danh sách bỏ qua hard-code thay vì `.gitignore`, quét lặp và dừng theo giới hạn/thứ tự path; renderer chỉ chọn command đầu tiên trong mỗi category nên có bias ở repo đa ngôn ngữ; fingerprint bỏ sót source file dù source có thể thay đổi convention. Managed-marker behavior và rule nông có thể được port riêng, nhưng scanner/fingerprint/renderer không được mang sang nguyên khối.

## Luồng generation và truy vết

Luồng dữ liệu bắt buộc là:

```text
SourceSnapshot
  → Evidence(path + hash + locator)
  → Fact(observed / deterministic-derived / heuristic / template)
  → Claim
  → staged managed output
  → explicit export
```

`SourceSnapshot` ghi commit hoặc worktree state, inventory hash và phiên bản extractor. Dependency ledger phải bao phủ mọi file mà extractor thực sự đọc, cùng inventory/glob/negative predicates có thể làm thay đổi kết quả khi file được thêm hoặc xóa; fingerprint upstream không được reuse nguyên trạng. `Evidence` giữ path chuẩn hóa, content hash và locator như JSON Pointer, TOML key hoặc line hint. `Fact` giữ giá trị có kiểu và evidence IDs. `Claim` là câu hoặc cấu trúc sẽ xuất hiện trong output; nó phải tham chiếu fact/evidence có thật. Claim có `kind = observed | inferred | human_policy` và nguồn tạo độc lập `origin = deterministic | llm | human`; đổi origin không nâng kind. Manifest nối claim với block và span đã render.

Phân loại nguồn là yêu cầu sản phẩm, không phải metadata trang trí:

- `observed`: literal nhìn thấy trong manifest/config, ví dụ giá trị của `scripts.test`.
- `deterministic-derived`: biến đổi tất định từ dữ liệu đã thấy, ví dụ thêm package-manager prefix vào script.
- `heuristic`: kết quả từ pattern hoặc quy tắc nông, có thể không đầy đủ và phải giữ rule ID.
- `template`: command hoặc hướng dẫn mặc định mà repository không khai báo trực tiếp.

Nguồn tạo claim được lưu riêng với derivation của fact: Codex tạo `origin: llm`, maintainer tạo `origin: human`, còn renderer tĩnh tạo `origin: deterministic`. `kind` vẫn do mức support quyết định; origin không biến inference thành observation hay policy.

Upstream hiện trộn literal, deterministic-derived command, heuristic và template defaults; đặc biệt một số Python, Go và Rust commands là template chứ không phải command quan sát trong source. Khi adapt scanner, extractor phải trả evidence và loại derivation ngay tại chỗ phát hiện fact, trước dedup, reasoner và renderer. Default template luôn hiện là “không quan sát trực tiếp”; nó không được render như sự thật của repository.

Đường `--no-ai` phải hoàn chỉnh: scan, fact/evidence ledger, claim tất định, diagnostics, generation vào staging và output có thể review. Nó sinh cả claim `observed` lẫn `inferred`: khai báo literal có thể là observed; recommendation dựa trên deterministic-derived, heuristic hoặc template phải là inferred, hiện nhãn và chờ review. Origin deterministic không tự làm claim thành observed. Claim factual không có evidence hợp lệ thì không được xuất. Người dùng có thể chạy `repo2skill explain <claim-id>` để đi từ output về claim, fact, locator, hash và snapshot.

Dedup chỉ lọc rendering, không xóa fact/evidence. Mỗi `DedupDecision` giữ candidate fact/claim IDs, evidence IDs của docs khớp, rule ID/version, snapshot, suppression reason và result. Docs liên quan đổi thì decision bị invalid; `explain` và report vẫn truy được candidate đã bị suppress.

`repo2skill check` so input hiện tại với ledger. File thay đổi hoặc bị xóa làm stale các claim phụ thuộc; file mới phải đánh thức inventory/glob dependency liên quan. `check` không cố suy luận semantic drift tổng quát. Nó báo `fresh`, `stale`, `conflict`, `pending` hoặc coverage gap và giải thích lý do.

## Codex CLI tùy chọn

Reasoning tùy chọn sử dụng official Codex CLI đã cài và đăng nhập trên máy người dùng. repo2skill không tích hợp nhiều SDK/provider, không quản lý API key và không hardcode một public model alias. Người dùng chọn model/config qua Codex; adapter kiểm tra capability và yêu cầu structured output chứa `claimId`, `factIds` và `evidenceIds`.

Codex chỉ được đề xuất cách diễn đạt hoặc claim suy luận trên evidence pack giới hạn. Reference bịa, sai snapshot hoặc sai scope bị validator từ chối. Timeout, non-zero exit, output partial hay schema invalid không được cập nhật verified snapshot tốt gần nhất. Một lần render thành công cũng không làm evidence cũ trông như vừa được xác minh.

Upstream monorepo mới chỉ đặt cờ và render root note; nested component instructions là coverage gap cho tới khi repo2skill có fixture kiểm chứng.

Trước mỗi lần gọi Codex, CLI hiển thị số fact/claim, kích thước payload, ước tính input/context và output dự kiến. Đây là estimate để người dùng quyết định, không phải quota hay hard cost guarantee. Không có background AI, daemon, hook tự gọi model hoặc ambient reasoning.

## Output, ownership và an toàn

Generation luôn ghi vào staging trước. Export là hành động rõ ràng, atomically cập nhật đúng managed block mà manifest sở hữu. Nội dung ngoài marker thuộc người dùng. Nếu hash của block live khác hash lần export trước, repo2skill tạo conflict/proposed patch thay vì overwrite. Xóa claim chỉ được xóa span mà manifest chứng minh thuộc repo2skill.

Scan không chạy build, test, install, migration hoặc command lấy từ repository. Repository text là dữ liệu không đáng tin; raw snippet được giới hạn và secret-like values được redact trước khi gửi reasoner. Local-first nghĩa là scan, ledger và staging ở local; khi người dùng bật Codex, evidence pack được gửi theo hành vi của CLI đó, nên sản phẩm không quảng bá “offline tuyệt đối”.

## Bao gồm và loại trừ

Pilot bao gồm: adapt có chọn lọc code MIT được quyết định sau so sánh công nghệ; coverage extractor cho bốn ecosystem; generic inventory/CI cho ecosystem khác; evidence/fact/claim ledger versioned; generation không AI; renderer `AGENTS.md` và một Agent Skill portable; managed blocks; staging/export; `explain`; source-change check; optional Codex CLI; coverage report và usage estimate.

Pilot không bao gồm: AST/type/call graph sâu; knowledge graph hoặc vector database; universal policy inference; SaaS; GitHub/GitLab App; nhiều provider; tự chạy command của repo; tự commit hoặc mở PR; hook/daemon tự gọi AI; background learning từ chat; JVM native collector; hay tự động chấp nhận claim quy phạm.

## Lộ trình có điều kiện

1. **Baseline tái sử dụng, 1–2 tuần:** áp dụng khuyến nghị có điều kiện trong `technology-reuse-decision.md`, pin source/license cho phần được chọn, rồi port riêng rule hoặc managed-section primitive có lợi. Không port nguyên scanner/generator/fingerprint; fingerprint/dependency ledger phải bao phủ toàn bộ extractor inputs và inventory/glob dependencies. Giữ hoặc chuyển test liên quan, nhưng việc upstream có test không đồng nghĩa local port đúng.
2. **Generation có provenance, 2–4 tuần:** thêm evidence/fact/claim IDs, derivation labels, staging/export, `explain`, invalidation cho changed/deleted/new inputs và manual-edit conflict. Kết thúc giai đoạn này phải có sản phẩm no-AI dùng được.
3. **Codex và pilot, 2–3 tuần:** structured reasoner adapter, validation, failure isolation, usage estimate và thử trên cohort nhỏ. Chỉ mở extractor sâu hoặc ecosystem mới khi dữ liệu pilot chỉ ra khoảng trống cụ thể.

Các khoảng trên là planning bands, không phải lịch cam kết. Không lập roadmap sáu tháng trước khi có người dùng giữ output trong repository thật.

## Điều kiện tiếp tục

Pilot đạt yêu cầu kỹ thuật khi mọi factual claim trong output đều truy được tới input hash và locator; reference bịa bị chặn; template không quan sát được gắn nhãn; file mới/sửa/xóa invalid đúng claim; LLM thất bại không làm state tiến lên; và chỉnh sửa của con người được bảo toàn hoặc tạo conflict reviewable.

Pilot đạt yêu cầu sản phẩm khi một cohort nhỏ dùng generation trong repository thật, thấy output hữu ích hơn việc chạy `/init` rồi sửa tay, và chấp nhận cơ chế update vì provenance giúp review nhanh hơn. Nếu người dùng chỉ cần tạo một file một lần, hoặc ledger tốn công hơn giá trị review, repo2skill nên dừng ở một adaptation nhỏ của upstream thay vì phát triển thành platform riêng.
