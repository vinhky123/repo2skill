# Khởi động dự án repo2skill

Tài liệu này đưa repo2skill từ quyết định sản phẩm sang các thay đổi nhỏ, có thể review. Nguồn điều hướng là [đặc tả MVP](./superpowers/specs/2026-10-07-repo2skill-mvp-design.md), [CLI contract](./engineering/cli-contract.md), [catalog tính năng](./product/feature-catalog.md), [kế hoạch pilot](./product/pilot-validation.md), và các kế hoạch implementation [P0 local generator](./superpowers/plans/2026-10-07-local-generator.md), [P1 Codex + pilot](./superpowers/plans/2026-10-07-codex-pilot.md). Spec và CLI contract là nguồn canonical cho interface.

## Cổng phạm vi

Baseline trước khi viết code:

- Python `>=3.11`; CI hỗ trợ có chủ đích Python 3.11 và 3.13.
- Adapt có chọn lọc code MIT từ `osmaneb23/agents-md` tại SHA `346b5f78dcbb48d9a700df98ac7f5f8e02f751ea`; không port nguyên scanner, generator hoặc fingerprint.
- Public alpha đầu tiên phải có collector nông cho JavaScript/TypeScript, Python, Go và Rust, cộng generic inventory/config/CI. Vertical slice nội bộ có thể bắt đầu bằng JS/TS nhưng chưa được gọi là alpha.
- Pipeline bắt buộc là `SourceSnapshot → Evidence → Fact → Claim → staged output → explicit export`; provenance hình thành trước Markdown.
- P0 phải hữu ích hoàn chỉnh khi không có AI. P1 chỉ dùng official Codex CLI đã cài và cấu hình bởi người dùng; không SDK đa provider, model hard-code hoặc credential do repo2skill quản lý.
- Scanner không chạy command trong repository. Không SaaS, graph/vector database, daemon, background AI, auto-commit hoặc auto-PR.

Thay đổi P0 phải phục vụ generation no-AI, provenance, drift, ownership hoặc khả năng phân phối. P1 chỉ phục vụ adapter Codex giới hạn và đo pilot. Candidate P2 phải có bằng chứng nhu cầu; ý tưởng khác vào backlog thay vì implementation.

## Chuẩn bị repository

Repository cần package `src/`, tests, fixture đa hệ sinh thái, tài liệu schema, notice giấy phép và build Python tối thiểu. Chỉ thêm dependency khi standard library không đủ hoặc dependency giảm rủi ro đáng kể; dependency runtime cần lý do, license và boundary. File/rule adapt phải ghi upstream path, commit pin và sửa đổi cục bộ. Artifact phát hành phải chứa MIT notice trong `THIRD_PARTY_NOTICES` hoặc tương đương.

Contribution guide cần nêu cách tạo môi trường, chạy formatter/type check/test, thêm fixture, đổi schema và kiểm notice. PR nên bám feature ID, nêu contract bị tác động và fixture chứng minh, không trộn cleanup ngoài phạm vi.

CI chạy Windows, Linux và macOS, gồm unit/integration tests, static checks, packaging build và smoke install trong môi trường sạch. Matrix Python là 3.11 và 3.13, không đoán “latest”. Release đầu ở pre-1.0; chỉ quảng bá ecosystem/platform đã có fixture chạy qua CI.

## P0 — local no-AI alpha

Thứ tự phụ thuộc: khóa schema/CLI; inventory và snapshot; extractor; fact/claim; dedup; renderer và staging; review/ownership/export; drift, explain, packaging và CI.

Checklist nghiệm thu:

- Inventory tất định, có cap và diagnostic, tôn trọng ignore; path ổn định trên Windows/Unix; symlink không được follow; case collision, CRLF/LF và path thoát root được xử lý rõ.
- Snapshot phân biệt commit sạch, dirty worktree và no-Git; lưu inventory hash và phiên bản policy/extractor.
- Evidence có normalized path, raw-byte hash và locator. Fact có typed value, rule/version và derivation `observed | deterministic-derived | heuristic | template`. Claim giữ riêng `kind`, `origin`, status, semantic ID, revision và lineage.
- Registry phát hiện đồng thời nhiều ecosystem. JS/TS, Python, Go, Rust và generic collector không được thiên lệch “command đầu tiên”. Unsupported, capped hoặc parse failure tạo coverage diagnostic.
- Dependency ledger bao phủ file đã đọc, glob/inventory và negative predicate. File thêm, sửa, xóa invalid đúng claim; docs đổi invalid quyết định dedup liên quan.
- Dedup chỉ suppress rendering, không xóa fact/evidence/candidate; `explain` vẫn truy được proof.
- `generate` mặc định no-AI, zero network, tạo staged `AGENTS.md`, portable Skill và report tất định. Factual claim thiếu chain hợp lệ không được render.
- Review chỉ batch `--all-observed`; inferred cần quyết định theo claim. Live output chỉ đổi qua `export` explicit.
- Ownership theo block; text ngoài marker luôn được giữ. Edit trong block tạo conflict/proposed patch, không overwrite.
- Mỗi file export dùng atomic replace. Export nhiều file không globally atomic: journal ghi tiến độ và trạng thái incomplete phục hồi được; resume/retry kiểm hash live và không đè manual edit mới.
- `check` chỉ đọc và báo fresh/pending/stale/conflict/coverage; `explain` đi từ span qua claim/fact/evidence đến snapshot. JSON schema và exit code tuân CLI contract.
- Artifact cài được trên matrix hỗ trợ và giữ attribution.

Alpha chỉ đạt khi fixture đa ngôn ngữ đi hết scan → generate → review → export → check/explain, gồm manual conflict, crash recovery và add/change/delete. Đây là milestone kỹ thuật, chưa chứng minh nhu cầu thị trường.

## P1 — Codex tùy chọn và pilot beta

P1 bắt đầu sau P0 và một capability spike trên Codex CLI thật. Spike kiểm version/help, structured output, stdin/stdout, timeout/exit và config selection; không hứa flag theo trí nhớ.

Adapter gửi EvidencePack giới hạn, đã redact, và nhận structured proposal tham chiếu ID có thật. Validator chặn unknown reference, sai snapshot/scope và canonical payload bị đổi. Timeout, partial/schema-invalid hoặc non-zero không advance verified state. Trước invocation, CLI hiển thị counts, payload size, estimate input/output và xin opt-in; estimate không phải quota hay giá. Không telemetry tự động.

Pilot tuyển 6–10 maintainer theo [protocol](./product/pilot-validation.md), nhưng tuyển dụng hiện chỉ là đề xuất: chưa liên hệ, ghi danh hoặc gửi dữ liệu. Trước pilot phải có consent, rubric review, measurement allowlist, retention/redaction và stop/narrow/continue gate. No-AI là đối chứng; Codex chỉ được opt-in sau đó.

## P2 — demand-gated

Nested scopes/monorepo, JVM native, extractor sâu và target mới chỉ mở khi pilot chỉ ra coverage gap lặp lại, tác động đến claim hữu ích và chi phí workaround. Nếu người dùng chỉ cần one-shot generation hoặc ledger tốn hơn giá trị review, dự án thu hẹp thành adaptation nhỏ thay vì thành platform.

## Milestone, phụ thuộc và hành động đầu tiên

Planning bands cho một maintainer là baseline reuse 1–2 tuần, provenance generation 2–4 tuần, Codex/pilot 2–3 tuần. Đây không phải cam kết giao hàng. P0 qua khi no-AI, drift và ownership pass; P1 qua khi adapter fail-safe và pilot protocol sẵn sàng; P2 chỉ mở bằng dữ liệu.

Fixture ecosystem, attribution, CI/package và docs có thể làm song song sau khi contract ổn định. Schema phải trước extractor; evidence/fact trước claim; validation trước renderer; staging trước export; ownership trước conflict; dependency ledger trước `check`; capability spike trước Codex; P0 trước pilot.

Ba hành động cụ thể đều **planned**, chưa thực thi:

1. **Planned — khóa contract:** review spec, CLI và catalog theo feature ID; giải quyết khác biệt schema, exit code, state transition và path trước khi tạo package.
2. **Planned — vertical slice:** cho fixture JS/TS đi trọn pipeline, rồi mở rộng Python, Go, Rust và generic trước public alpha.
3. **Planned — release gate:** dựng fixture matrix, CI ba OS trên Python 3.11/3.13, packaging smoke test và MIT attribution check.

## Risk register và điểm dừng

Scope có thể phình thành compiler; giữ extraction nông và cổng P2. Renderer/LLM có thể nói quá canonical payload; dùng typed validation và proof-chain fixture. Drift dễ bỏ sót glob/negative dependency; kiểm cả add/change/delete. Marker/write lỗi có thể mất nội dung; dùng block hash, proposed patch, per-file atomic replace và export journal recovery. Symlink, case/path, EOL, inventory cap, secret trong EvidencePack, Codex capability đổi và thiếu MIT attribution đều cần owning task cùng test hoặc release gate.

Release bị chặn nếu factual claim live không truy được đến hash+locator, AI failure advance state, hoặc export có thể overwrite manual edit. Roadmap dừng/thu hẹp nếu pilot không chứng minh công duy trì giảm hoặc ledger/review tạo nhiều chi phí hơn giá trị.

Hai implementation plan chỉ ở trạng thái **ready for review** sau khi viết; command trong đó mô tả phiên implementation tương lai và chưa chạy được trên repository hiện chỉ có tài liệu.
