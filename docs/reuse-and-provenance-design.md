# Thiết kế tái sử dụng generator và provenance cho repo2skill

## Mục tiêu

repo2skill phải tiếp tục là một **generator**: đọc repository, tạo tri thức vận hành hữu ích, rồi dựng `AGENTS.md`, Agent Skill và báo cáo evidence. Giá trị mới không nằm ở việc thay generator bằng checker, mà ở chỗ mỗi câu máy sinh ra đều trả lời được: “quan sát từ đâu, ở snapshot nào, qua extractor nào, đang còn đúng không, và đã xuất hiện ở đoạn output nào?”. Đường chạy không AI phải hoàn chỉnh; Codex CLI đã cài chỉ là provider tùy chọn, model do người dùng cấu hình.

Thiết kế chỉ adapt có chọn lọc các primitive, rule và fixture hữu ích từ `agents-md`; không port nguyên scanner, generator hoặc fingerprint. Seam extraction–prose phải đổi vì LLM viết lại toàn draft làm mất ánh xạ fact → claim → span. repo2skill giữ dữ liệu có kiểu đến renderer; LLM chỉ đề xuất claim tham chiếu ID có sẵn.

Đặc tả canonical cho implementation là [MVP design](./superpowers/specs/2026-10-07-repo2skill-mvp-design.md); command, state transition, output và exit code do [CLI contract](./engineering/cli-contract.md) chi phối. Tài liệu này giải thích rationale về reuse/provenance và không ghi đè các interface canonical đó.

## Quyết định tái sử dụng

Có ba lựa chọn:

1. **Adapt có chọn lọc module nguồn**: port từng primitive/rule scanner, managed-marker behavior và fixture có giá trị, giữ attribution, pin commit, ghi phần sửa. Đây là lựa chọn khuyến nghị; không mang nguyên scanner/renderer/fingerprint sang local.
2. **Bọc CLI/package như dependency**: nhanh nhưng chỉ trả prose; parse ngược Markdown không thể chứng minh mapping. Chỉ phù hợp làm baseline.
3. **Viết lại từ đầu**: kiểm soát schema nhưng bỏ phí extractor và dễ lệch hành vi. Chỉ dùng cho module không thể adapt.

Audit xác nhận upstream tại commit [`346b5f78dcbb48d9a700df98ac7f5f8e02f751ea`](https://github.com/osmaneb23/agents-md/tree/346b5f78dcbb48d9a700df98ac7f5f8e02f751ea), giấy phép MIT yêu cầu giữ copyright và permission notice. Chi tiết nằm trong `docs/upstream-reuse-audit.md`. File tái sử dụng ghi upstream path, commit và local modification; bản phân phối giữ license text trong `THIRD_PARTY_NOTICES` hoặc tương đương.

## Kiến trúc dữ liệu và luồng generation

Chuỗi bắt buộc là:

```text
SourceSnapshot → Evidence → Fact → Claim → OutputBlock/Span → OutputManifest
```

`SourceSnapshot` chốt repository identity, commit SHA, inventory hash, worktree mode, scanner/extractor versions và thời điểm scan. Hash nội dung source là SHA-256 trên raw bytes của snapshot/worktree; vì vậy thay đổi chỉ khác EOL vẫn được coi là source drift và conservatively chuyển dependency liên quan sang stale. `Evidence` trỏ tới path, raw-byte content hash, locator ổn định như JSON Pointer/key path, kèm line hint để người đọc mở nhanh; line chỉ là gợi ý vì dễ dịch chuyển. `Fact` chuẩn hóa kết quả extractor và ghi enum `derivation`: `observed` khi giá trị có nguyên văn trong nguồn, `deterministic-derived` khi biến đổi bằng quy tắc tất định, `heuristic` khi nhận diện theo dấu hiệu, hoặc `template` khi sinh default/placeholder. Nó còn mang `ruleId`, rule version và input evidence. `Claim` là đơn vị có thể render, review và theo dõi vòng đời. `OutputBlock` mang claim IDs vào managed section; renderer ghi span sau khi format. `OutputManifest` nối output hash với snapshot, claim revisions và target được quản lý.

Renderer dựng UTF-8 với canonical LF trong staging. Khi export vào target đã có, nó giữ kiểu EOL nhất quán của target (LF hoặc CRLF); mixed EOL là conflict. Manifest hash exact bytes của managed block sau chuyển đổi EOL, nên ownership check so đúng nội dung đã ghi thay vì hash staging canonical.

Claim có `kind = observed | inferred | human_policy` và `origin = deterministic | llm | human`. Kind `observed` chỉ dùng khi toàn bộ fact hỗ trợ là observed; fact dẫn xuất hoặc reasoning tạo `inferred`; policy do maintainer xác nhận là `human_policy`. Origin chỉ ghi ai/cơ chế tạo, không nâng kind. Trạng thái gồm `candidate`, `accepted`, `contested`, `stale`, `rejected`. `lastVerifiedSource` tách khỏi `generatedAt`; render lại không làm nguồn trông mới hơn.

`claimId` phải ổn định qua thay đổi câu chữ. ID được tạo từ claim kind, scope và semantic key, không hash prose. Khi payload ngữ nghĩa đổi, giữ `claimId` và tăng `claimRevision`. Nếu một claim tách thành hai hoặc nhiều claim gộp lại, manifest ghi `supersedes`/`derivedFrom` rõ ràng. Không cho phép nhìn output cũ rồi nhờ LLM đoán citation hồi tố.

## Extractor và coverage đa ngôn ngữ

Baseline Python từ `agents-md` cung cấp scan/render/managed-section cho JavaScript/TypeScript, Python, Go và Rust; upstream chưa hỗ trợ JVM. repo2skill adapt extractor thành interface `extract(snapshot, artifact) -> facts[], evidence[], diagnostics[]`; extractor không được xuất prose. Ngày đầu JVM chỉ nhận inventory và fact generic từ manifest/CI qua collector mới, chưa quảng bá native ecosystem support cho đến khi fixture đạt yêu cầu. Các nguồn chung như GitHub Actions, Makefile, Dockerfile, YAML/JSON/TOML/Markdown đi qua adapter nông. Unsupported syntax, file quá lớn, generated content, submodule hoặc parser lỗi phải tạo diagnostic và coverage gap, không im lặng coi là không có fact.

Mỗi fact mang `factId` ổn định, normalized value, scope, derivation và evidence IDs. Điều này bắt buộc vì upstream hiện trộn command literal với command/default suy ra cho Python, Go và Rust; template hay heuristic không được nâng thành observed truth. Fact thay đổi giá trị tạo revision mới; file bị xóa làm evidence unavailable và mọi claim phụ thuộc chuyển `stale`. File mới phải đánh thức glob dependency; negative dependency như “không có workflow deploy khác” phụ thuộc inventory predicate nên cũng bị invalid khi có file phù hợp. No-AI checker dùng chính dependency ledger này, không gọi model.

Dedup chỉ quyết định candidate nào được render, không xóa fact/evidence. `DedupDecision` lưu candidate fact/claim IDs, evidence IDs của docs khớp, rule ID/version, snapshot, suppression reason và result. Docs khớp thay đổi sẽ invalid decision; `explain` và report vẫn truy được candidate bị suppress.

## Seam LLM có kiểu

Provider nhận EvidencePack giới hạn gồm facts, evidence snippets đã redact, diagnostics, claim hiện tại và schema output. Codex CLI là adapter đầu tiên; adapter capability-check version và cho người dùng chọn model. Model chỉ được trả về cấu trúc như:

```json
{
  "claims": [
    {
      "claimId": "claim.command.test.root",
      "kind": "observed",
      "origin": "llm",
      "text": "Script `test` có giá trị `vitest run`.",
      "factIds": ["fact.script.test.root"],
      "evidenceIds": ["ev.package-json.script-test"],
      "scope": "."
    }
  ]
}
```

Validator từ chối evidence/fact ID không tồn tại, sai snapshot, sai scope hoặc claim `observed` vượt normalized fact. ID hợp lệ một mình không chứng minh phát biểu. Claim observed về command, path hoặc version phải được renderer dựng từ canonical structured payload của fact; model chỉ được diễn đạt lại mà không đổi literal. Ý nghĩa hoặc policy mới phải là `inferred` và chờ review. Sau validation, renderer mới tạo prose và span. Formatting-only regeneration giữ revision; semantic payload khác tăng revision. LLM lỗi, timeout, output partial hoặc reference bịa không cập nhật state tốt gần nhất.

## Ví dụ ledger hoàn chỉnh

Các hash và commit dưới đây là **giá trị minh họa**, không đại diện repository thật:

```json
{
  "sourceSnapshot": {
    "commit": "example-commit-sha",
    "inventoryHash": "sha256:example-inventory-hash",
    "worktreeMode": "head"
  },
  "evidence": {
    "evidenceId": "ev.package-json.script-test",
    "path": "package.json",
    "contentHash": "sha256:example-content-hash",
    "locator": { "type": "json-pointer", "value": "/scripts/test" },
    "lineHint": 12,
    "observedValue": "vitest run"
  },
  "fact": {
    "factId": "fact.script.test.root",
    "kind": "project-script",
    "name": "test",
    "value": "vitest run",
    "scope": ".",
    "derivation": "observed",
    "ruleId": "package-json.scripts.literal",
    "evidenceIds": ["ev.package-json.script-test"]
  },
  "claim": {
    "claimId": "claim.command.test.root",
    "claimRevision": 1,
    "kind": "observed",
    "status": "accepted",
    "origin": "deterministic",
    "text": "Script `test` có giá trị `vitest run`.",
    "factIds": ["fact.script.test.root"],
    "lastVerifiedSource": "example-commit-sha"
  },
  "output": {
    "target": "AGENTS.md",
    "blockId": "commands",
    "span": { "startLine": 18, "endLine": 18 },
    "claimIds": ["claim.command.test.root"]
  }
}
```

## CLI, ownership và lifecycle

Theo CLI contract canonical, generation mặc định chạy no-AI: scan, extract, validate và render vào staging. Nó tạo claim observed từ literal và claim inferred có nhãn/review từ derived, heuristic hoặc template; origin deterministic không làm inference thành observed. Explain in chain từ output span về claim, fact, locator, hash và snapshot. Check so snapshot/inventory/dependencies với ledger mà không sinh prose mới. Export là bước rõ ràng sau review.

Managed markers sở hữu theo `blockId`, không sở hữu toàn file. Manifest lưu hash của block đã install. Nếu người dùng sửa bên trong block và hash khác, lần generate tạo conflict/proposed patch; không overwrite. Nội dung ngoài marker luôn thuộc người dùng. Xóa claim chỉ xóa span mà manifest chứng minh đang sở hữu. Dirty worktree được scan ở chế độ explicit và gắn hashes riêng; không được giả là commit `HEAD`.

Ledger dùng JSON/JSONL versioned cùng index nhẹ. MVP không cần graph database, vector store hay “semantic truth engine”. Provenance là chuỗi kiểm toán được, không phải chứng minh rằng một inference đúng về mặt nghiệp vụ.

## Lộ trình triển khai

**P0 — local generator không AI:** pin upstream và giữ attribution; dựng fixture; triển khai snapshot/inventory cùng evidence/fact/claim IR; adapt có chọn lọc extractor rules; thêm proof-preserving dedup, renderer, staging/review/export; hoàn tất dependency invalidation cho changed/added/deleted, glob/negative predicates, dirty/no-Git snapshots, ownership conflict, `check` và `explain`. P0 chỉ hoàn thành khi generation, drift và ownership chạy end-to-end mà không có model.

**P1 — typed Codex và pilot:** sau P0 mới thêm capability probe, bounded/redacted EvidencePack, structured validation, failure isolation, usage estimate và pilot protocol. Lộ trình task cụ thể nằm trong [P0 local-generator plan](./superpowers/plans/2026-10-07-local-generator.md) và [P1 Codex/pilot plan](./superpowers/plans/2026-10-07-codex-pilot.md).

## Tiêu chí chấp nhận

- Mọi machine-generated factual claim trong output live có ít nhất một fact và evidence hợp lệ tại `lastVerifiedSource`.
- Reference do LLM bịa hoặc thuộc snapshot khác bị từ chối mà không làm hỏng state trước.
- Đổi format không tăng claim revision; đổi nghĩa có tăng revision và lưu quan hệ lịch sử.
- Thêm, sửa, xóa file và dirty snapshot invalid đúng claim, gồm glob và negative dependency.
- No-AI mode generate được output hữu ích và `check` không phát sinh network/model call.
- Sửa tay trong managed block tạo conflict; nội dung ngoài block được bảo toàn.
- Ecosystem chưa hỗ trợ tạo diagnostic/coverage gap thay vì claim rỗng hoặc suy đoán.
- Fixture chứng minh chain `source hash + locator → factId → claimId/revision → output block/span` còn nguyên sau update.

Thiết kế này sao chép phần đáng giá của generator hiện có—extraction, concise rendering và non-destructive update—nhưng chuyển provenance thành cấu trúc lõi. LLM giúp đề xuất cách diễn đạt và inference có kiểm soát; nó không được phép thay thế evidence hoặc tự tạo nguồn.
