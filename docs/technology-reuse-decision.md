# Quyết định công nghệ và tái sử dụng cho repo2skill

## Quyết định

Baseline khuyến nghị cho **pilot hiện tại** là fork/adapt có chọn lọc `agents-md` bằng Python, tại SHA đã audit, rồi thay IR ngay từ extractor để phát `Evidence → Fact → Claim` trước dedup và renderer. Lý do là sản phẩm cần generation có provenance, không chỉ scan rộng: `agents-md` đã có hành vi gần nhất với đầu ra mong muốn—AGENTS.md ngắn, managed sections, update bảo toàn phần viết tay, command/convention extraction và đường no-AI—trong khi core Python không có runtime dependency.

Quyết định này áp dụng [phạm vi dự án](./project-scope.md) và contract trong [thiết kế reuse/provenance](./reuse-and-provenance-design.md).

Quyết định này có điều kiện, không phải ưu tiên Python mặc định. Nếu trước khi bắt đầu implementation nhóm chốt rằng **parser rộng, multi-root/monorepo packaging và phân phối Node đa nền tảng** quan trọng hơn thời gian ra pilot, baseline nên chuyển sang TypeScript: lấy Repomix làm nền scan/process và dùng kiến trúc adapter của Rulesync cho renderer đích. Khi đó phải chấp nhận xây claim generation, managed ownership, proof-preserving dedup và provenance lifecycle mới trên nền đó.

## Bằng chứng quyết định

| Nguồn đã audit | Code/runtime đã xác minh | Boundary ảnh hưởng quyết định |
|---|---|---|
| [`agents-md`](./deep-dive-agents-md.md) | Python ≥3.11, core zero-dependency; 53 test qua, 3 lỗi Windows do symlink/CRLF; scanner JS/TS, Python, Go, Rust; marker update và static renderer có sẵn | IR chỉ có `source: str`; traversal bỏ `.gitignore`; fingerprint bỏ source; monorepo chỉ là cờ; dedup exact normalized line; command template dễ bị hiểu là observed |
| [Repomix](./deep-dive-repomix.md) | Node ≥22; local install lock thành công; subset 5 file/79 test qua; file pipeline, multi-root, secret scan, 16 Tree-sitter WASM configs, tech-stack parser và Skill template | Skill là reference bundle tất định, không phải operational claims; `packSkill` không public; watch full-repack; parser fallback giữ raw text; compression không phát evidence/span |
| [Rulesync](./deep-dive-rulesync.md) | Node ≥22/pnpm ≥10; broad subset: 114 qua, 23 skip, 24 lỗi Windows path/symlink; hai follow-up hẹp convert 24/24 và selected Skill 9/9 qua; public generate/import/convert và registry target | Chỉ chuyển canonical rules/config sang nhiều tool; không đọc repo facts; round-trip có loss; source lock SHA là supply-chain integrity, không phải claim provenance |

Source pin: [`agents-md@346b5f78dcbb48d9a700df98ac7f5f8e02f751ea`](https://github.com/osmaneb23/agents-md/tree/346b5f78dcbb48d9a700df98ac7f5f8e02f751ea), [`repomix@8d6429121e98ed178e4d3a975c2bdbbecc958c4a`](https://github.com/yamadashy/repomix/tree/8d6429121e98ed178e4d3a975c2bdbbecc958c4a), [`rulesync@f14b5bd1273329514c1a0b2762f6f1f1435e089b`](https://github.com/dyoshikawa/rulesync/tree/f14b5bd1273329514c1a0b2762f6f1f1435e089b).

Các kết quả runtime chỉ chứng minh những đường test đã chạy trên Windows. Không xếp hạng dự án bằng số test thô, suy ra Linux sẽ pass, hay coi đó là bằng chứng cho full suite, mọi parser, packaging release hoặc giá trị thị trường.

## Phần tái sử dụng thật và phần chỉ học kiến trúc

Với baseline Python, nên mang sang code MIT có attribution cho: CLI flow `init/update/explain`, managed-marker replacement, renderer section, fingerprint encode/diff làm khung, và từng extractor đã có test. Không giữ nguyên dataclass, fingerprint input set, LLM transport hoặc dedup text. Extractor phải phát path/hash/locator, `derivation`, rule ID/version và dependency ngay tại điểm quan sát; renderer chỉ nhận claim đã validate.

Từ Repomix, pilot Python chỉ nên kế thừa **thiết kế và fixture**: danh sách manifest, expected outputs đa ngôn ngữ, cách phân biệt parser failure với unsupported coverage, multi-root path labeling và security boundary. Gọi Repomix CLI để lấy một blob nén không giải quyết provenance. AST compression chỉ chọn comments/imports/signatures và bỏ phần lớn body; nó giảm context nhưng không chứng minh statement xuất phát từ capture nào sau khi đã merge/dedup chunk. Nếu chuyển sang TypeScript baseline, có thể port trực tiếp file discovery, language registry/query, tech-stack parsers và tests, nhưng phải chèn evidence trước compression.

Từ Rulesync, nên kế thừa kiến trúc registry `feature × target`, adapter có schema, dry-run/check, collision diagnostics, output-root confinement và cách copy companion files. Không phụ thuộc trực tiếp processor nội bộ trong pilot và không dùng Rulesync IR thay Claim IR; hai mô hình giải hai tầng khác nhau.

## Cổng lựa chọn trước implementation

Chọn **Python adaptation** nếu acceptance ngày đầu là bốn ecosystem native đã audit cộng generic inventory/CI cho ecosystem khác, và mục tiêu chính là chứng minh claim-level provenance cùng managed generation sớm. Chọn **TypeScript Repomix + kiến trúc Rulesync** nếu acceptance bắt buộc parser cấu trúc rộng hơn bốn ecosystem, multi-root tốt và một runtime Node thống nhất cho scan lẫn adapters.

Dù chọn nhánh nào, vertical slice phải giống nhau: snapshot → evidence/fact có derivation → observed/inferred claim → proof-preserving dedup → staged AGENTS.md/Skill → explicit export → invalidation khi file thêm/sửa/xóa. Codex CLI vẫn là reasoner tùy chọn; no-AI generation phải dùng được. Đây là tiêu chí chọn nền công nghệ theo lượng code còn phải xây, không phải bằng chứng rằng một upstream hay ngôn ngữ sẽ tạo sản phẩm tốt hơn trên thị trường.
