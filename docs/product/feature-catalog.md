# Danh mục năng lực sản phẩm repo2skill

## Mục đích và quy ước

repo2skill là **generator tri thức repository có truy vết**, chạy local-first. Luồng lõi là `SourceSnapshot → Evidence → Fact → Claim → staged output → explicit export`; `check` và `explain` phục vụ việc tin tưởng và bảo trì output. Baseline làm việc là Python `>=3.11`, adapt có chọn lọc code MIT từ `agents-md@346b5f78dcbb48d9a700df98ac7f5f8e02f751ea`, không port nguyên upstream và không xem module nội bộ upstream là stable API.

Phase gồm `P0 — local no-AI alpha`, `P1 — optional Codex + pilot beta`, `P2 — demand-gated later`, và `Excluded — ngoài định hướng đã chốt`. Priority gồm `Must`, `Should`, `Could`, `Won't`. P0 phải hữu ích khi không có model; P1 chỉ mở sau khi P0 đạt acceptance kỹ thuật; P2 cần bằng chứng nhu cầu từ pilot. “Đủ mọi chức năng” trong tài liệu này nghĩa là inventory đầy đủ trong phạm vi sản phẩm đã chốt, không phải lời hứa xây mọi tính năng có thể tưởng tượng.

Ba chiều nguồn phải độc lập. `Fact.derivation` là `observed | deterministic-derived | heuristic | template`; `Claim.kind` là `observed | inferred | human_policy`; `Claim.origin` là `deterministic | llm | human`. `origin: llm` không biến inference thành observation; `origin: human` không tự biến nội dung thành policy. Template luôn là nguồn không quan sát trực tiếp.

## Catalog

| ID | Phase / priority | Hành vi và acceptance criterion | Phụ thuộc |
|---|---|---|---|
| F001 | P0 / Must | Pin đúng SHA upstream, lưu mapping code được adapt và nguyên văn MIT notice; release artifact luôn chứa attribution. | — |
| F002 | P0 / Must | CLI nhận repository root, staging root và target rõ ràng; path thoát khỏi root bị từ chối với lỗi có hành động sửa. | — |
| F003 | P0 / Must | Inventory tất định, tôn trọng `.gitignore`, loại generated/vendor theo rule công bố; cùng snapshot cho cùng danh sách path bất kể thứ tự filesystem. | F002 |
| F004 | P0 / Must | `SourceSnapshot` ghi commit, dirty-worktree hoặc no-Git mode, schema và extractor versions; mỗi scan có UUID mới, còn cùng inputs/settings giữ cùng inventory hash/input digest. | F003 |
| F005 | P0 / Must | Dependency ledger ghi mọi file đã đọc, inventory/glob và negative predicate; hash policy cùng giới hạn EOL-only change được công bố, report chỉ ra dependency ảnh hưởng claim. | F004 |
| F006 | P0 / Must | Mỗi `Evidence` có path chuẩn hóa, content hash, locator và excerpt/value đã giới hạn; locator trỏ lại đúng nguồn. | F004 |
| F007 | P0 / Must | Mỗi `Fact` có kiểu, scope, normalized value, evidence IDs, rule/version và derivation đúng; template/heuristic không được ghi `observed`. | F006 |
| F008 | P0 / Must | `Claim` lưu riêng `kind`, `origin`, status và fact IDs; validator chặn `observed` nếu bất kỳ support fact nào không observed. | F007 |
| F009 | P0 / Must | Stable IDs không hash prose; đổi format giữ revision, đổi nghĩa tăng revision; split/merge lưu `supersedes`/`derivedFrom`. | F007, F008 |
| F010 | P0 / Must | Extractor registry trả facts, evidence và diagnostics, không trả prose; parser lỗi, unsupported syntax, file lớn/submodule tạo coverage gap thay vì im lặng. | F003–F007 |
| F011 | P0 / Must | JS/TS shallow collector đọc package manifest, lockfile, scripts, `tsconfig` và config framework/test đã audit; literal và command được bọc prefix có derivation khác nhau. | F010 |
| F012 | P0 / Must | Python shallow collector đọc `pyproject.toml`, package/test config và AST giới hạn; lệnh mặc định được gắn template hoặc derived, không observed. | F010 |
| F013 | P0 / Must | Go shallow collector đọc `go.mod`, framework hints và test layout; command cố định luôn hiện là template/inferred. | F010 |
| F014 | P0 / Must | Rust shallow collector đọc `Cargo.toml` và test layout; command mặc định luôn hiện là template/inferred. | F010 |
| F015 | P0 / Must | Ngôn ngữ khác vẫn có inventory, Git state, CI/workflow, Makefile, Dockerfile, YAML/JSON/TOML/Markdown facts generic; không quảng bá native support. | F010 |
| F016 | P0 / Must | Coverage report liệt kê collector đã chạy, artifact đã/không đọc, diagnostic và khoảng trống; một repo JVM chỉ được báo generic coverage. | F010–F015 |
| F017 | P0 / Must | `generate` mặc định no-AI, tạo candidate observed và inferred, ledger và staging hoàn chỉnh, không network/model call. | F007–F016 |
| F018 | P0 / Must | Factual claim chỉ được render khi chain claim→fact→evidence cùng snapshot hợp lệ; dangling/sai scope bị reject với diagnostic. | F008, F017 |
| F019 | P0 / Must | Dedup chỉ suppress render; `DedupDecision` giữ candidate, evidence docs, rule/version, snapshot và lý do, nên `explain` vẫn thấy proof bị suppress. | F006–F009 |
| F020 | P0 / Must | Renderer ngắn gọn nhưng không chọn duy nhất command đầu tiên theo category; mọi heuristic/template luôn hiện nhãn inferred và trạng thái cần review. | F018, F019 |
| F021 | P0 / Must | Sinh `AGENTS.md` vào staging, không sửa file live; manifest ánh xạ block/span về claim IDs. | F020 |
| F022 | P0 / Must | Sinh một Agent Skill portable vào staging với cùng claim ledger và provenance, không chép raw repository vượt giới hạn. | F020 |
| F023 | P0 / Must | Review accept/reject/contest theo từng claim; chỉ observed deterministic hợp lệ được batch. Quyết định chỉ đổi status, không tự đổi `kind`/`origin`; inferred chưa accept không vào live output. | F021, F022 |
| F024 | P0 / Must | Managed markers sở hữu theo `blockId`; content ngoài marker luôn được bảo toàn và manifest lưu hash block đã export. | F021 |
| F025 | P0 / Must | Export explicit chỉ đưa accepted claims vào live output; pending inferred còn lại không chặn export. Mỗi file atomic; export nhiều file dùng journal và có thể resume khi incomplete. | F023, F024 |
| F026 | P0 / Must | Sửa tay trong managed block tạo conflict và proposed patch; không overwrite. Journal/conflict report giữ hash và trạng thái cần thiết để recovery có thể review. | F024, F025 |
| F027 | P0 / Must | `check` read-only phát hiện file dependency sửa/xóa và báo effective status `stale` cho đúng claim; không persist mutation, sinh prose hay gọi model. | F005, F009 |
| F028 | P0 / Must | Thêm file mới invalid glob/negative dependency phù hợp, gồm trường hợp “không tồn tại workflow khác”; test fixture chứng minh wake-up. | F005, F027 |
| F029 | P0 / Must | `explain <claim-id>` đi từ output span qua revision, fact, derivation, evidence locator/hash đến snapshot, kể cả claim bị suppress/stale. | F009, F019–F022 |
| F030 | P0 / Must | `generate/check/explain/review/export` có JSON schema versioned, output máy đọc ổn định và lỗi schema không trộn vào stdout JSON. | F017, F023–F029 |
| F031 | P0 / Must | Human report và exit codes phân biệt success, pending review, stale, conflict, coverage gap và fatal error; CI có thể gate theo trạng thái. | F030 |
| F032 | P0 / Must | Scan coi repo là dữ liệu không tin cậy: không chạy build/test/install/migration/command, giới hạn snippet và redact secret-like values. | F003, F006 |
| F033 | P0 / Must | Parse/render/write failure giữ verified snapshot tốt gần nhất; staging tạm có thể dọn an toàn và lần chạy lại cho kết quả xác định. | F004, F025 |
| F034 | P0 / Must | Fixtures phủ JS/TS, Python, Go, Rust, generic-only, no-Git/dirty, add/change/delete, glob/negative, CRLF và conflict; chain proof còn nguyên sau update. | F011–F033 |
| F035 | P0 / Should | Contributor guide mô tả extractor/rule/fixture; docs cài đặt, threat model, schema migration và release checklist; P0 ghi rõ kết quả kiểm trên Windows, Linux, macOS. | F001, F030, F034 |
| F036 | P1 / Must | Adapter phát hiện official Codex CLI đã cài/đăng nhập và capability structured output; thiếu capability thì no-AI vẫn hoạt động. Không hardcode model alias. | P0 |
| F037 | P1 / Must | Trước mỗi call, hiển thị fact/claim count, payload size và estimate input/output; chỉ gọi sau opt-in cho lần chạy hoặc cấu hình rõ. | F036 |
| F038 | P1 / Must | Tạo bounded `EvidencePack` chỉ gồm facts, snippet đã redact, diagnostics và claims liên quan; report chính xác dữ liệu được gửi. | F032, F037 |
| F039 | P1 / Must | Validate structured result: ID tồn tại, cùng snapshot/scope và payload observed khớp canonical fact; hallucinated reference bị reject. | F018, F038 |
| F040 | P1 / Must | Timeout, non-zero, partial/invalid output không advance verified state; candidate hợp lệ còn lại được báo rõ, không chấp nhận ngầm. | F033, F039 |
| F041 | P1 / Should | Pilot có task packet/review log và `--measure` opt-in ghi JSONL local theo allowlist, không truyền đi; maintainer chủ động gửi artifact/feedback nếu muốn. | F030, F035 |
| F042 | P2 / Should | Multi-root/monorepo tạo scope và nested instructions chỉ khi fixture pilot chứng minh ownership, path labeling và conflict đúng. | Bằng chứng pilot |
| F043 | P2 / Could | JVM/Maven/Gradle native collector chỉ mở khi cohort có nhu cầu lặp lại; trước đó vẫn báo generic coverage gap. | Bằng chứng pilot |
| F044 | P2 / Could | Thêm extractor domain/artifact có cấu trúc như dbt, Terraform hoặc convention framework khi gap lặp lại; không mở rộng thành full semantic AST. | Bằng chứng pilot |
| F045 | P2 / Could | Output adapter ngoài `AGENTS.md` và một portable Skill dùng registry `feature × target`, chỉ khi người dùng giữ output đó trong repo thật. | Bằng chứng pilot |
| F046 | Excluded / Won't | Deep AST/type/call graph, knowledge graph và vector database không thuộc roadmap hiện tại. | — |
| F047 | Excluded / Won't | SaaS, hosted sync, GitHub/GitLab App và dashboard tổ chức không thuộc pilot. | — |
| F048 | Excluded / Won't | Multi-provider SDK, API-key manager và alias model hardcode bị loại; P1 chỉ dùng official installed Codex CLI. | — |
| F049 | Excluded / Won't | Không chạy command lấy từ repository, kể cả test/build “để xác minh” tự động. | — |
| F050 | Excluded / Won't | Không auto-commit, mở PR, hook, daemon hoặc background AI. | — |
| F051 | Excluded / Won't | Không học nền từ chat hoặc tự tích lũy policy ngoài ledger/review explicit. | — |
| F052 | Excluded / Won't | Không tự accept claim quy phạm; `human_policy` cần quyết định có chủ ý của maintainer. | — |

## Trình tự delivery và definition of done

P0 nên đi theo bốn increment reviewable: (1) reuse/license + inventory/snapshot; (2) evidence/fact/claim và bốn shallow collector; (3) no-AI render, staging/review/export; (4) invalidation, explain, JSON/CI và hardening. Một increment chỉ xong khi fixture của nó chứng minh acceptance tương ứng, docs/schema được cập nhật và không làm suy giảm chain provenance.

Alpha đủ để pilot khi mọi factual claim live truy được tới hash+locator, add/change/delete invalid đúng dependency, output ngoài marker được bảo toàn, sửa tay tạo conflict, unsupported ecosystem tạo coverage gap, và toàn bộ luồng chạy không AI. Beta chỉ thêm Codex sau các invariant đó; kết quả pilot quyết định tiếp tục, thu hẹp hay dừng theo [pilot-validation.md](./pilot-validation.md).
