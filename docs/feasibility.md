# Đánh giá khả thi kỹ thuật: repo2skill

> **Cập nhật phạm vi 2026-10-07:** Tài liệu này giữ nguyên đánh giá kỹ thuật lịch sử cho phương án greenfield rộng với năm ecosystem. Đề xuất scope hiện tại chuyển sang fork/adapt baseline đã audit, có collector native cho JavaScript/TypeScript, Python, Go và Rust; ngôn ngữ khác nhận generic inventory/CI, còn JVM native collector là công việc về sau. Xem [phạm vi dự án hiện tại](./project-scope.md) và [audit tái sử dụng upstream](./upstream-reuse-audit.md). Đây là đề xuất scope, chưa khẳng định code đã được fork hoặc việc triển khai đã được phê duyệt.

**Ngày kiểm chứng:** 2026-10-07  
**Phạm vi:** CLI local-first, dùng coding-agent CLI đã cài và đã đăng nhập; Evidence IR; sinh `AGENTS.md` và Agent Skills; đồng bộ gia tăng theo Git.  
**Kết luận:** **Go có điều kiện cho một MVP hẹp về lời hứa nhưng đa ngôn ngữ ngay từ đầu.** Lõi kỹ thuật khả thi. Không nên hứa “tự động hiểu đúng repository”, “đồng bộ sau mọi thay đổi Git”, hay “giới hạn cứng token/quota”. MVP nên tạo **candidate knowledge có bằng chứng, được kiểm tra cấu trúc và cần review**, rồi export có chủ đích sang đường dẫn mà agent thực sự đọc.

Đánh giá này kiểm chứng tài liệu chính thức và `codex exec --help` trên bản CLI được cài cục bộ. Chưa có adapter, prototype hay thí nghiệm triển khai nào được chạy; các mục ở phần 7 là kế hoạch validation, không phải kết quả test.

## 1. Phán quyết ngắn

| Hạng mục | Phán quyết | Điều kiện để khả thi |
|---|---|---|
| Gọi Codex CLI không tương tác | Khả thi hiện tại | Adapter ghim capability theo phiên bản, dùng `codex exec`, JSONL và JSON Schema; xử lý timeout, exit code và schema failure |
| Dùng phiên đăng nhập hiện có | Khả thi cho local interactive/personal | Không đọc credential; chạy binary chính thức. Không coi subscription là API contract ổn định cho automation quy mô lớn |
| Ước tính và chặn AI usage | Chỉ khả thi một phần | Có thể ước tính kích thước EvidencePack và dừng trước khi gọi; không thể hứa mức trừ quota/token chính xác hoặc hard cap subscription qua CLI |
| Evidence IR và provenance theo claim | Khả thi, là phần lõi | Phân biệt observed fact, inferred procedure và human assertion; lưu hash/range/extractor version |
| “Xác minh” knowledge bằng repository | Khả thi ở mức kiểm tra bằng chứng, không phải chân lý ngữ nghĩa | Kiểm tra nguồn còn tồn tại, không mâu thuẫn và validator chạy được; luôn giữ trạng thái `inferred`/`needs_review` |
| Sinh output dùng được bởi Codex | Khả thi | Export sang `AGENTS.md` và `.agents/skills/<name>/SKILL.md`; `.repo2skill/generated` chỉ là staging, không được tự động discover |
| Đồng bộ bằng commit SHA | Khả thi với fallback | State theo worktree; xử lý baseline bị mất, lịch sử rẽ nhánh, shallow clone, dirty tree và thay đổi version extractor |
| Tự động hóa chỉ bằng ba Git hooks | Không đủ | Hook là wake-up signal; bổ sung `post-commit`, lệnh `sync/status`, và kiểm tra lúc khởi động. Không có hook chuẩn cho `git reset` |
| Đa ngôn ngữ từ ngày đầu | Khả thi ở tầng facts phổ quát | Hỗ trợ JS/TS, Python, Go, Rust, JVM qua inventory, manifest, build/test/CI facts; hoãn deep AST và workflow chuyên framework |
| Claude Code/Gemini CLI adapter | Khả thi như roadmap | Cùng provider contract nhưng capability khác nhau; không đặt vào critical path MVP Codex-first |

## 2. Sự thật đã được xác minh

### 2.1 Codex có giao diện subprocess phù hợp

Tài liệu Codex hiện mô tả `codex exec` là chế độ dành cho scripted/CI run không cần tương tác. Nó nhận prompt qua argument hoặc stdin, có `--json` để phát sự kiện JSONL, `--output-last-message`, `--output-schema` để kiểm tra final response bằng JSON Schema, và `--sandbox` với các mức `read-only`, `workspace-write`, `danger-full-access` ([OpenAI, Developer commands](https://learn.chatgpt.com/docs/developer-commands?surface=cli), truy cập 2026-10-07). Hướng dẫn non-interactive hiện còn mô tả `--ignore-user-config` và `--ignore-rules` cho controlled automation ([OpenAI, Non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode), truy cập 2026-10-07). Bản cài được quan sát trên máy kiểm chứng là `codex-cli 0.160.0`; `codex exec --help` cũng hiện các flag trên. Adapter vẫn phải probe phiên bản/capability trước khi phụ thuộc vào flag cụ thể. Những khả năng này đủ để thiết kế output typed mà không scrape prose; chưa chứng minh adapter đã hoạt động end-to-end.

Codex hỗ trợ đăng nhập bằng ChatGPT cho subscription access hoặc API key cho usage-based access. Với CLI, `codex login` dùng browser flow; `codex login status` cho biết phương thức đang hoạt động. Tài liệu cũng nói API key phù hợp hơn cho programmatic workflow như CI/CD, còn Enterprise có access token riêng cho trusted non-interactive local workflows ([OpenAI, Authentication](https://learn.chatgpt.com/docs/auth), truy cập 2026-10-07). Vì vậy:

- local personal workflow có thể gọi Codex CLI đã đăng nhập mà repo2skill không đọc token;
- nhưng “mọi automation đều được bảo đảm dùng subscription hiện có” là lời hứa quá mạnh, nhất là CI, máy dùng chung và workspace managed;
- adapter phải báo rõ auth mode và không tự chuyển từ subscription sang API key.

### 2.2 Không có hard guarantee cho quota theo số token ước tính

OpenAI nói consumption phụ thuộc model, độ lớn và độ phức tạp task, local/cloud, context, reasoning, tool use, retrieval và caching; prompt length một mình không phải estimator đáng tin. Các con số local messages là estimate, không phải fixed limit, và user phải xem dashboard hoặc `/status` để biết trạng thái hiện tại ([OpenAI, Pricing](https://learn.chatgpt.com/docs/pricing), truy cập 2026-10-07). JSONL của `codex exec` có thể phát `turn.completed.usage` với input, cached input, output và reasoning token counts, nên adapter có thể quan sát usage mà provider báo sau turn; các số này không ánh xạ thành allowance/quota còn lại theo một công thức được tài liệu bảo đảm ([OpenAI, Non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode), truy cập 2026-10-07).

Hệ quả thiết kế: `repo2skill sync --dry-run` có thể báo byte/token estimate của **payload do repo2skill kiểm soát**, số claims và output ceiling nội bộ. Nó không được ghi “sẽ tiêu thụ đúng N token” hoặc cam kết `max_tokens_per_run` là hard cap của allowance. Nếu Codex CLI không expose một giới hạn output/cost có thể kiểm chứng, “budget” chỉ là preflight gate: quá ngưỡng thì không khởi chạy.

### 2.3 Output chỉ có tác dụng khi nằm ở đường dẫn discovery

Codex đọc repository skills từ `.agents/skills` trên chuỗi thư mục từ CWD lên repository root; user skill ở `$HOME/.agents/skills`. Một skill là thư mục có `SKILL.md` và bắt buộc `name`, `description` ([OpenAI, Build skills](https://learn.chatgpt.com/docs/build-skills), truy cập 2026-10-07). Codex đọc `AGENTS.md`/`AGENTS.override.md` từ project root xuống CWD theo precedence và giới hạn tổng kích thước; filename ngoài danh sách discovery bị bỏ qua ([OpenAI, AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md), truy cập 2026-10-07).

Do đó `.repo2skill/generated` không phải consumption path. Thiết kế cần hai lớp:

1. `.repo2skill/generated/`: staging, diff, provenance manifest, rollback.
2. `repo2skill export --target codex`: ghi/link có kiểm soát vào `AGENTS.md` và `.agents/skills/...`; hoặc `publish` để team commit sau review.

Việc export phải phát hiện file hiện hữu, merge theo section marker hoặc tạo patch để review; không overwrite nguyên file. Sau export, chạy smoke test liệt kê instruction/skill đã được agent discover.

### 2.4 Claude Code và Gemini CLI đủ khả năng làm adapter sau MVP

Claude Code có print mode `-p`, output `json`/`stream-json`, giới hạn turns, model và permission mode cho non-interactive use ([Anthropic, CLI reference](https://docs.anthropic.com/en/docs/claude-code/cli-usage), truy cập 2026-10-07). Gemini CLI có headless mode qua `-p`, output JSON hoặc streaming JSON, usage stats và exit codes dành cho automation ([Google, Gemini CLI headless mode](https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/headless.md), truy cập 2026-10-07). Gemini cũng discover workspace skills từ `.gemini/skills/` hoặc `.agents/skills/`, đồng thời có lệnh install/link riêng ([Google, Managing Agent Skills](https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/using-agent-skills.md), truy cập 2026-10-07).

Đây là bằng chứng cho **khả năng làm adapter**, không phải bằng chứng về parity. Codex hiện có JSON Schema validation rõ ràng; adapter khác có thể cần application-side validation/retry. Auth, quota, permission và discovery phải là capability flags theo provider/version, không phải một interface giả định giống hệt nhau.

### 2.5 Git hooks không tạo ra guarantee “sau mọi thay đổi HEAD”

Git chỉ chạy hook executable trong `$GIT_DIR/hooks` hoặc đường dẫn do `core.hooksPath` chỉ định. `post-checkout` chạy sau checkout/switch và khi add worktree; `post-merge` chạy sau merge thành công; `post-rewrite` chỉ được tài liệu nêu cho amend/rebase và không bao phủ mọi history-rewrite tool. `post-commit` là hook riêng cho commit thường ([Git, githooks](https://git-scm.com/docs/githooks), truy cập 2026-10-07). `core.hooksPath` có thể trỏ sang nơi khác hoặc `/dev/null`, nghĩa là installer không được âm thầm thay thế cấu hình hiện hữu ([Git, git-config](https://git-scm.com/docs/git-config), truy cập 2026-10-07).

Các giới hạn cụ thể của proposal ban đầu:

- `post-merge` + `post-checkout` + `post-rewrite` bỏ sót commit thường nếu không cài `post-commit`;
- Git không cung cấp `post-reset` chuẩn trong danh sách hooks, nên `git reset` có thể đổi HEAD mà không đánh thức repo2skill;
- hook có thể bị bypass, vô hiệu hóa, lỗi quyền executable, hoặc bị hook manager khác sở hữu;
- một hook chạy AI đồng bộ sẽ làm `git pull`/checkout chậm và dễ hỏng trải nghiệm;
- linked worktrees có `HEAD`/index riêng nhưng chia sẻ nhiều refs và mặc định chia sẻ repository config; state một file chung dễ race hoặc ghi đè baseline của worktree khác ([Git, git-worktree](https://git-scm.com/docs/git-worktree), truy cập 2026-10-07).

Hooks vì vậy chỉ nên enqueue một `sync-needed` marker và thoát nhanh. Correctness đến từ việc mọi lệnh `repo2skill status/sync/export` đều so sánh state với repository hiện tại, cộng với check lúc khởi động hoặc một watcher tùy chọn.

### 2.6 “Sinh skill” đã là tính năng cạnh tranh, lifecycle vẫn là giả thuyết khác biệt

Repomix đã pack repository sang format phù hợp AI. README của dự án liệt kê watch mode và `--skill-generate` để sinh Claude Agent Skills vào `.claude/skills` hoặc output chỉ định, nhưng cũng nói hai chế độ này không thể kết hợp; đây là hai feature riêng, không phải bằng chứng Repomix đang tự động watch rồi đồng bộ skill ([Repomix, README](https://github.com/yamadashy/repomix/blob/main/README.md), truy cập 2026-10-07). Dù vậy, không nên định vị novelty là “đọc repo rồi tạo SKILL.md”. Giả thuyết khác biệt hợp lý hơn là claim-level evidence, explicit fact/inference boundary, incremental invalidation, reviewable export và drift lifecycle. Đây vẫn chỉ là **giả thuyết sản phẩm**, chưa phải bằng chứng rằng không có đối thủ tương tự hoặc khách hàng sẽ trả tiền.

## 3. Suy luận kỹ thuật từ các sự thật trên

### 3.1 Kiến trúc tập trung, đa ngôn ngữ

```text
Git/worktree snapshot
        |
        v
Universal fact collectors
(inventory, manifests, lockfiles, build files, CI commands)
        |
        +--> language adapters: JS/TS | Python | Go | Rust | JVM
        |
        v
Evidence IR
(observed facts + sources + hashes + relations)
        |
        v
Bounded EvidencePack --> Provider adapter --> Candidate claims/procedures
                              (Codex-first, read-only, schema output)
        |                                      |
        +------------------ verifier <---------+
                               |
                               v
                    Reviewable generated tree
                               |
                         explicit export
                               v
                 AGENTS.md / .agents/skills / adapters
```

**Universal collectors** phải làm việc từ ngày đầu trên JS/TS, Python, Go, Rust và JVM:

- inventory: tracked files, language distribution, module roots, ignore rules;
- manifests/locks: `package.json`, workspace files, `pyproject.toml`, requirements/lock variants, `go.mod`, `Cargo.toml`, `pom.xml`, Gradle settings/build files;
- build/test/lint facts: scripts/targets khai báo trong manifest, Make/Task/Just files và command literal trong CI;
- CI facts: workflow/job/step, working directory, environment reference, command exact string;
- topology thô: package/module boundaries và declared dependencies.

MVP không cần hứa AST chính xác cho năm hệ sinh thái. Deep AST, symbol graph, framework semantics, dbt/Terraform extractors là plugin/pass sau. Điều quan trọng là schema dùng chung không thiên Node: `Component`, `Artifact`, `Command`, `PipelineStep`, `Dependency`, `EvidenceRef`, `ClaimCandidate`.

### 3.2 Ba lớp tri thức không được trộn

1. **Observed fact:** “CI file X, job Y chạy command Z tại commit H.” Có thể kiểm tra bằng parser/hash.
2. **Inferred procedure:** “Sau khi sửa module A, thường chạy Z.” Đây là suy luận từ một hoặc nhiều facts; cần rationale và trạng thái review.
3. **Normative instruction:** “Contributor phải chạy Z.” Chỉ được nâng cấp khi có nguồn quy chuẩn rõ (CI required check, maintainer-approved rule) hoặc con người approve.

Một command xuất hiện trong cấu hình CI chỉ chứng minh command đó **được khai báo ở bối cảnh ấy**, không chứng minh CI đã thực thi thành công và không tự động chứng minh mọi developer phải chạy nó cho mọi thay đổi liên quan. Tương tự, một pattern xuất hiện ở phần lớn file chỉ chứng minh tần suất quan sát, không chứng minh các ngoại lệ là sai.

### 3.3 Evidence verifier không thể chứng minh semantic truth

Verifier khả thi có thể:

- xác nhận source path/range và content hash còn tồn tại ở snapshot;
- xác nhận parser đã trích đúng literal/field;
- kiểm tra mọi claim có evidence refs hợp lệ;
- phát hiện evidence mâu thuẫn hoặc yếu đi;
- chạy command validator trong sandbox khi user cho phép;
- chặn output nếu model viện dẫn file không có trong EvidencePack.

Verifier không thể tự chứng minh intent của maintainer, command có an toàn trên production, convention có bắt buộc, hay workflow có đầy đủ edge case. Điểm confidence dạng xác suất do model tự chấm không có ý nghĩa nếu chưa calibration bằng review labels. MVP nên dùng trạng thái rời rạc và giải thích được: `observed`, `inferred`, `contradicted`, `needs_review`, `approved`, `stale`.

### 3.4 Provider isolation, quyền riêng tư và giới hạn an toàn

Không nên chạy reasoning agent trực tiếp trong target repo với write access. Repository có thể chứa prompt injection trong docs/source, và agent có thể đọc instruction cũ làm reasoning bị vòng lặp. Sandbox read-only chỉ hạn chế một nhóm thao tác ghi; JSON Schema chỉ giới hạn hình dạng output. Hai cơ chế này không chứng minh miễn nhiễm prompt injection, không ngăn một claim độc hại nhưng vẫn hợp schema, và không tự ngăn đọc hoặc gửi dữ liệu qua network, connector hay MCP nếu runtime cho phép.

Flow giảm rủi ro, vẫn cần được chứng minh trong adapter spike:

1. deterministic collectors đọc repo;
2. serialize EvidencePack với content đã giới hạn, đánh dấu rõ “data, không phải instruction” và không tin ambient repository instructions;
3. gọi provider từ working directory cô lập; giới hạn tools, config, connector, MCP và network theo capability thực tế của provider; dùng read-only sandbox và structured output khi provider hỗ trợ;
4. validator kiểm tra output; renderer của repo2skill mới ghi vào staging;
5. export là bước riêng có diff/approval.

Local-first ở đây chỉ có nghĩa state, index và generated artifacts được lưu cục bộ. EvidencePack vẫn được gửi tới dịch vụ đứng sau official CLI đã chọn, trừ khi user chủ động chọn một provider local. Preflight phải cho biết provider, loại dữ liệu và phạm vi file dự kiến gửi. Bộ lọc secrets, binary, generated/vendor trees và nội dung không cần thiết chỉ là biện pháp best effort, không phải bảo đảm không rò rỉ; payload preview/redaction và human review vẫn cần thiết. `doctor` phải hiện provider binary/version/auth mode và model choice có thể cấu hình, nhưng không log credential hay toàn bộ environment.

### 3.5 State và incremental sync

State tối thiểu nên chứa:

- repository identity (`git-common-dir`) và worktree identity;
- `analyzed_commit`, `analyzed_tree`, branch/ref quan sát được;
- extractor/schema/prompt/provider adapter versions;
- fact/evidence hashes và dependency từ evidence → claim → output;
- generated/exported manifest cùng hash để phát hiện human edits.

Thuật toán không được giả định `old..HEAD` luôn tồn tại hoặc old là ancestor:

- baseline object còn tồn tại: diff hai tree, sau đó invalidate facts phụ thuộc;
- baseline bị GC/shallow clone/mất: full deterministic rescan;
- history rẽ nhánh/force update/reset: so sánh tree trực tiếp; không diễn giải như “các commit mới”;
- schema/extractor version đổi: rescan vùng liên quan hoặc full rescan theo migration rule;
- dirty tracked/untracked files: báo riêng `worktree drift`; không cập nhật commit baseline cho tới khi snapshot policy rõ ràng;
- không có commit đầu tiên: scan working tree nhưng state mang loại `unborn`, không giả SHA.

## 4. Các blocker lớn nhất

### Blocker 1: định nghĩa và đo “knowledge đúng”

Generation chạy được không đồng nghĩa output hữu ích. Cần corpus fixture có maintainer labels: fact nào đúng, procedure nào hợp lý, instruction nào không được suy ra. Nếu không có eval này, semantic drift engine chỉ là LLM rewrite có provenance đẹp.

**Giảm thiểu:** MVP tập trung claims dễ kiểm tra (build/test commands, component map, CI constraints); mọi inferred procedure cần review; lưu accepted/edited/rejected làm calibration data.

### Blocker 2: delivery vào agent mà không phá file người dùng

Staging file không được agent dùng; export thẳng có nguy cơ overwrite/duplication và xung đột với instruction hiện hữu.

**Giảm thiểu:** managed section markers, three-way merge theo output manifest, `export --check`, patch preview và rollback; repo-shared publish mặc định không tự commit.

### Blocker 3: trigger không đầy đủ và race giữa worktrees

Hooks không bao phủ mọi thay đổi, có thể bị thay thế, và nhiều worktree có HEAD riêng.

**Giảm thiểu:** hook chỉ đánh dấu; state theo worktree; kiểm tra HEAD/tree tại mọi entry point; lock theo worktree; full-rescan fallback; không tuyên bố realtime guarantee.

### Blocker 4: provider/quota là dependency bên ngoài thay đổi

CLI flags, auth policy, model availability và quota thay đổi theo version/account. Subscription consumption không suy ra chắc chắn từ token count local.

**Giảm thiểu:** provider capability handshake, supported-version range, explicit auth display, bounded payload, preflight gate, retry policy hạn chế, cache theo EvidencePack hash và đường API-key opt-in cho CI. Adapter spike phải xác minh cấu hình kiểm soát tools, ambient instructions, connector/MCP và network thực sự có hiệu lực; nếu không kiểm soát được thì capability phải báo unsupported thay vì suy đoán an toàn.

### Blocker 5: breadth đa ngôn ngữ dễ tạo coverage giả

Năm ecosystem có manifest và monorepo semantics khác nhau. “Hỗ trợ Python” không thể chỉ là đếm `.py`.

**Giảm thiểu:** công bố capability theo fact family; cùng acceptance suite cho từng ecosystem; nếu adapter không hiểu field thì giữ raw evidence thay vì suy luận. Deep AST không nằm trong MVP claim.

## 5. Rủi ro và biện pháp

| Rủi ro | Tác động | Biện pháp MVP |
|---|---|---|
| Prompt injection từ repository | Model có thể sinh claim độc hại nhưng vẫn hợp schema, hoặc đọc/gửi dữ liệu qua capability đang mở | CWD cô lập; giới hạn tools/config/connectors/MCP/network theo provider; payload preview; review nội dung ngữ nghĩa; output staging. Read-only và schema không phải security proof |
| Hallucinated provenance | Claim trỏ nguồn không tồn tại | Chỉ cho phép evidence IDs do compiler cấp; reject unknown IDs |
| Command được nâng sai thành policy | Agent áp dụng workflow quá rộng | Tách observed/inferred/normative; approval để promote |
| Existing hooks bị ghi đè | Phá workflow developer | Detect `core.hooksPath` và hook hiện hữu; chain qua installer adapter hoặc từ chối cài, không overwrite |
| Hook làm Git chậm | UX tệ, re-entrancy/race | Hook chỉ touch/enqueue marker; AI chạy ngoài critical path |
| Baseline commit mất | Incremental diff lỗi hoặc bỏ sót | `cat-file`/object existence check; full rescan fallback |
| Hai worktree ghi cùng state | Drift sai và output lẫn branch | Namespace + lock theo worktree; common cache chỉ immutable/content-addressed |
| File generated sửa tay | Export ghi đè contribution | Hash manifest, three-way diff, conflict yêu cầu review |
| Quota hết giữa run | Partial output/state sai | Atomic staging; chỉ commit state sau validate; giữ previous good generation |
| Skills quá nhiều/description mơ hồ | Agent không chọn đúng | Router nhỏ, trigger rõ, giới hạn số skill candidate; eval discovery |

## 6. Thiết kế MVP đề xuất

### Có trong MVP

- `repo2skill doctor`: detect Codex binary/version/auth status, model preference và capability flags.
- `repo2skill scan`: deterministic scan đa ngôn ngữ cho JS/TS, Python, Go, Rust, JVM ở mức inventory/manifest/build/CI facts.
- Evidence IR versioned, content-addressed, mỗi fact có extractor và source location/hash.
- `repo2skill reason --provider codex --model <provider-supported-model>`: model choice có thể cấu hình và được capability probe; invocation cô lập, quyền tối thiểu và structured output theo khả năng provider.
- Claim verifier và contradiction report; không dùng scalar confidence giả chính xác.
- `repo2skill sync --dry-run`: diff facts, claims bị ảnh hưởng, payload estimate, không gọi AI.
- Commit/tree-based incremental engine với full-rescan fallback và state theo worktree.
- Git hook installer an toàn cho `post-merge`, `post-checkout`, `post-rewrite`, `post-commit`; hook chỉ tạo marker.
- Renderer tạo candidate root `AGENTS.md` và `.agents/skills`; explicit `export --check/apply` với diff.
- Human review trạng thái accept/edit/reject; không auto-commit.

### Hoãn sau MVP

- Deep AST/symbol graph đồng đều cho mọi ngôn ngữ.
- Tự động suy ra workflow chuyên dbt, Terraform, Kubernetes, Rails/Spring nếu chưa có extractor riêng.
- Claude/Gemini production adapters; trước mắt chỉ spike contract.
- PR mining và hosted GitHub/GitLab app.
- Autonomous publish/commit hoặc background AI không có preflight policy.
- Numeric confidence cho tới khi có calibration dataset.

## 7. Thí nghiệm xác nhận trước khi xây rộng

1. **Codex adapter contract.** Trên một fixture vô hại, gọi `codex exec` bằng stdin, JSONL, output schema và `read-only`; kiểm tra exit handling, schema violation, timeout, auth exhausted và atomic retry. Pass khi parser không phụ thuộc prose và failure không cập nhật state.

2. **Consumption path.** Sinh một `AGENTS.md` managed section và một skill trong `.agents/skills`, mở phiên Codex mới rồi kiểm tra instruction source/skill discovery. Đồng thời chứng minh file chỉ nằm trong `.repo2skill/generated` không được dùng. Pass khi export/check phát hiện được cả install và removal.

3. **Git event matrix.** Fixture thực hiện commit, merge, checkout/switch, rebase/amend, reset, shallow clone, baseline deletion giả lập, unborn branch và hai linked worktrees. Pass khi mỗi lần chạy `status` đều nhận ra drift; hook coverage được báo như telemetry, không phải correctness dependency.

4. **Five-ecosystem fact suite.** Mỗi fixture JS/TS, Python, Go, Rust, JVM có monorepo/module, manifest, build/test command và CI working directory. Golden files chỉ chấm observed facts. Pass khi không biến command ngoài context thành repository-wide policy.

5. **Evidence epistemics.** Cài các tình huống có command mâu thuẫn giữa README, CI và manifest; convention phổ biến nhưng có ngoại lệ; stale docs. Pass khi hệ thống giữ mâu thuẫn, hạ trạng thái về `needs_review`, không chọn một nguồn như chân lý mà không có rule.

6. **Prompt-injection fixture.** Đặt instruction độc hại trong README/source comment, gồm cả trường hợp model có thể tạo một claim nguy hiểm nhưng dùng evidence ID hợp lệ và hoàn toàn đúng schema. Thí nghiệm phải quan sát riêng quyền đọc, ghi, network và connector/MCP trong cấu hình adapter. Pass chỉ chứng minh cấu hình fixture đã chặn capability dự kiến và review/validator bắt được các mẫu độc hại đã định nghĩa; không được diễn giải là miễn nhiễm prompt injection.

7. **Usage transparency.** Ghi kích thước/token estimate của EvidencePack, provider-reported usage nếu có, thời gian và trạng thái allowance trước/sau mà không tuyên bố quan hệ nhân quả quá mức. Pass khi preflight threshold luôn ngăn process khởi chạy và UI gọi con số là estimate.

8. **Maintainer utility test.** Cho maintainer của các fixture/repo thật review từng claim theo accept/edit/reject và lý do. Go tiếp chỉ khi claim classes nào hữu ích và ít sửa đã được xác định; không dùng một điểm “confidence” tự chấm làm thay thế.

## 8. Điều chưa biết cần giữ nguyên là chưa biết

- Mức consumption subscription cụ thể của một `codex exec` và khả năng còn lại của từng tài khoản tại thời điểm chạy; tài liệu công khai không cho hard token guarantee.
- Điều khoản/behavior lâu dài khi một công cụ bên thứ ba liên tục shell-out vào CLI đã đăng nhập; thiết kế phải theo tài liệu và version hiện hành, không coi đây là giao thức bất biến.
- Chất lượng inference workflow trên repository thật, đặc biệt monorepo lớn và convention không được ghi thành văn.
- Mức willingness-to-pay cho lifecycle/evidence so với Repomix hoặc instruction generation miễn phí.
- Tỷ lệ review burden đủ thấp để continuous sync đáng dùng.
- Parity thực tế của Claude/Gemini adapter về schema enforcement, permission và quota trên các plan khác nhau.

## 9. Quyết định go/no-go đã hiệu chỉnh

**Go** nếu sản phẩm được mô tả là: *compiler tạo candidate repository knowledge có provenance, phát hiện evidence drift và cung cấp bản export reviewable cho coding agents*. Bắt đầu Codex-first nhưng IR/provider contract trung lập; hỗ trợ năm ecosystem ở tầng deterministic facts ngay từ ngày đầu.

**No-go cho lời hứa hiện tại** nếu “continuous sync” nghĩa là chắc chắn chạy sau mọi Git operation, nếu “validated knowledge” được hiểu là đúng về intent/ngữ nghĩa, nếu `.repo2skill/generated` được coi là tự động có hiệu lực, hoặc nếu token budget được quảng bá là hard cap trên subscription.

Mốc quyết định tiếp theo không phải thêm extractor hay renderer. Mốc đó là hoàn thành tám thí nghiệm trên và chứng minh ba điều: adapter thất bại an toàn, output thực sự được agent consume sau explicit export, và maintainer đánh giá các candidate claims có ích hơn chi phí review. Khi ba điều này chưa có bằng chứng, phần “compiler” khả thi về kỹ thuật nhưng product loop vẫn chưa được xác nhận.
