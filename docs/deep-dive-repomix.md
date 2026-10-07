# Deep dive Repomix: source, runtime và khả năng tái sử dụng

## Mốc audit và kết luận

Audit này pin HEAD công khai tại commit [`8d6429121e98ed178e4d3a975c2bdbbecc958c4a`](https://github.com/yamadashy/repomix/tree/8d6429121e98ed178e4d3a975c2bdbbecc958c4a), ngày 2026-10-03. Đây là 48 commit sau tag `v1.18.1` (`80b4280…`), dù `package.json` vẫn mang version `1.18.1`. Repomix là thư viện TypeScript/Node ESM, yêu cầu Node `>=22`; build chỉ chạy `tsc`, còn hai export condition `import` và `require` cùng trỏ tới `lib/index.js`, không có CJS build riêng đã được xác minh ([package manifest](https://github.com/yamadashy/repomix/blob/8d6429121e98ed178e4d3a975c2bdbbecc958c4a/package.json#L1-L129)).

Kết luận kỹ thuật: Repomix là **repository packer và code-reference Skill generator tất định**, không phải hệ thống dùng LLM để hiểu repo hay sinh policy. Nó mạnh hơn `agents-md` ở file discovery, multi-root, AST compression, token/security pipeline và số ngôn ngữ parser. Nó yếu hơn cho mục tiêu repo2skill ở claim vận hành, lọc trùng với docs theo heuristic, managed `AGENTS.md`, provenance và drift. Phần đáng tái sử dụng nhất là thuật toán/fixture ở file pipeline, Tree-sitter và tech-stack detection; lấy nguyên Skill output làm IR sẽ mất dấu vết cần thiết.

## Runtime và dependency theo vai trò

Lock install thực tế dùng Node `24.19.0`, npm `11.17.0`, lockfile v3. Các dependency chính phân vai như sau:

| Vai trò | Công nghệ thực tế |
|---|---|
| CLI, prompt, watch | `commander`, `@clack/prompts`, `chokidar`, `picocolors`, `picospinner` |
| Tìm và lọc file | `globby` cho include/ignore và ignore files; `minimatch` cho pattern; `is-binary-path`/`isbinaryfile`, `iconv-lite`, `jschardet` cho binary/encoding |
| Biến đổi code | `@repomix/strip-comments`; `web-tree-sitter` + bundle grammar WASM `@repomix/tree-sitter-wasms`; không có native addon/node-gyp |
| Bảo mật | `@secretlint/core` + preset recommend, chạy qua worker `tinypool`; file bị phát hiện secret bị loại khỏi processed output ([packager](https://github.com/yamadashy/repomix/blob/8d6429121e98ed178e4d3a975c2bdbbecc958c4a/src/core/packager.ts#L233-L253)) |
| Metrics và output | `gpt-tokenizer` đếm token; `Handlebars` dựng Markdown/Skill; `fast-xml-builder` dựng XML; `tinyclip` copy clipboard |
| Config và schema | `valibot` validate config; `jiti` nạp config TS/JS, `json5` cho config; `zod` định nghĩa MCP tool input |
| Remote/MCP | `git-url-parse`, `tar`, Git subprocess; `@modelcontextprotocol/sdk` cho server/tools stdio |

Không có OpenAI/Anthropic/Gemini SDK trong runtime dependency. “AI-friendly” chỉ mô tả consumer của output.

## Pipeline pack và ranh giới

Public API chính là `pack(rootDirs, config, …)`; package còn export file search/process, security, token counter và `parseFile`, nhưng không export trực tiếp `packSkill` hay tech-stack parser ([public exports](https://github.com/yamadashy/repomix/blob/8d6429121e98ed178e4d3a975c2bdbbecc958c4a/src/index.ts#L4-L69)). Pipeline thực tế:

1. Chuẩn hóa nhiều root, tìm file bằng glob/ignore và đọc nội dung; có root label để tránh nhập nhằng path ở multi-root.
2. Lấy Git diff/log song song với file collection.
3. Chạy secret scan và file processing song song; processing có thể strip comment hoặc compress.
4. Loại file suspicious, sort, rồi rẽ sang Skill hoặc output thường.
5. Dựng tree/content, đồng thời tính file/output/git metrics và token; ghi output/cache.

Các boundary thể hiện rõ trong [packager](https://github.com/yamadashy/repomix/blob/8d6429121e98ed178e4d3a975c2bdbbecc958c4a/src/core/packager.ts#L161-L352). Multi-root là danh sách checkout/root được gắn nhãn, còn “monorepo understanding” chủ yếu đến từ tree và việc nhóm dependency manifest theo thư mục package; không có workspace dependency graph hay module ownership semantics.

## AST compression: có gì và mất gì

Compression dùng Tree-sitter WASM cho 16 cấu hình: JavaScript, TypeScript, Python, Go, Rust, Java, C#, Ruby, PHP, Swift, C, C++, CSS, Solidity, Vue và Dart ([registry](https://github.com/yamadashy/repomix/blob/8d6429121e98ed178e4d3a975c2bdbbecc958c4a/src/core/treeSitter/languageConfig.ts#L28-L159)). JavaScript/TypeScript, Python, Go, CSS và Vue có strategy riêng; các ngôn ngữ còn lại dùng strategy mặc định cùng query riêng.

Query bắt comments/imports và nhiều declaration như class, interface, type, enum, function/method, module hoặc macro tùy ngôn ngữ. Strategy TypeScript/Python cố giữ decorator/doc và signature rồi bỏ body. Strategy mặc định chỉ giữ capture dạng name/comment/import. Vì vậy output hữu ích để tìm cấu trúc, nhưng làm mất phần lớn statement, control flow, literal trong body và quan hệ runtime. Nó không type-check, resolve symbol, expand macro, dựng call graph hay chứng minh semantic behavior.

`parseFile` là best-effort: extension không hỗ trợ, parser/WASM lỗi hoặc parse failure trả `undefined`; caller dùng nội dung không nén. Nó dedup capture theo dòng, merge chunk liền kề và chèn separator, nhưng không phát provenance span hay semantic ID ([compression implementation](https://github.com/yamadashy/repomix/blob/8d6429121e98ed178e4d3a975c2bdbbecc958c4a/src/core/treeSitter/parseFile.ts#L36-L123)). Do đó compressed text không phù hợp làm evidence chuẩn nếu không bổ sung anchor trước khi nén.

## Skill generation, tech stack và watch

Skill pipeline nhận `ProcessedFile[]`, tạo `summary.md`, `project-structure.md`, `files.md`, `tech-stacks.md`, tính token rồi render `SKILL.md` bằng Handlebars ([pipeline](https://github.com/yamadashy/repomix/blob/8d6429121e98ed178e4d3a975c2bdbbecc958c4a/src/core/skill/packSkill.ts#L70-L236), [template](https://github.com/yamadashy/repomix/blob/8d6429121e98ed178e4d3a975c2bdbbecc958c4a/src/core/skill/skillStyle.ts#L15-L112)). Đây là codebase reference bundle: `files.md` chứa nội dung để grep; SKILL hướng agent tìm file và trace import. Không có LLM synthesis, claim validation hoặc policy inference.

Tech-stack detector parse theo rule các manifest `package.json`, `requirements.txt`, `pyproject.toml`, Pipfile, `go.mod`, `Cargo.toml`, Composer, Gemfile, Maven và Gradle; nó nhóm kết quả theo thư mục package ([detector](https://github.com/yamadashy/repomix/blob/8d6429121e98ed178e4d3a975c2bdbbecc958c4a/src/core/skill/skillTechStack.ts#L24-L36), [grouping](https://github.com/yamadashy/repomix/blob/8d6429121e98ed178e4d3a975c2bdbbecc958c4a/src/core/skill/skillTechStack.ts#L470-L540)). Đây là regex/parser nông, không cùng nghĩa với 16-language AST coverage.

Watch dùng Chokidar với debounce và guard chống pack chồng nhau, nhưng mỗi event add/change/unlink gọi lại `runPack` cho toàn target ([watch rebuild](https://github.com/yamadashy/repomix/blob/8d6429121e98ed178e4d3a975c2bdbbecc958c4a/src/cli/actions/watchAction.ts#L130-L178)). Watch còn không tương thích `--skill-generate`. Cache bền vững chỉ lưu token count theo encoding + byte length + content digest, tối đa 100.000 entry; không cache AST/fact hay dependency invalidation. Vì vậy đây là full repack có cache metrics, không phải incremental knowledge compiler.

## Kiểm chứng runtime có giới hạn

Checkout tạm nằm tại `C:\Users\Kyyy\AppData\Local\Temp\repo2skill-repomix-audit-8d642912`. Sau khi đọc scripts, audit dùng `npm ci --ignore-scripts --no-audit --no-fund`; cờ ignore-scripts tránh chạy `prepare: npm run build`. Install local thành công: 310 package trong 15 giây.

Lệnh hẹp đã chạy:

```text
npx vitest run tests/core/treeSitter/parseFile.typescript.test.ts tests/core/treeSitter/parseFile.python.test.ts tests/core/skill/packSkill.test.ts tests/core/skill/skillStyle.test.ts tests/core/skill/skillTechStack.test.ts
```

Kết quả exit code 0: 5 test files, 79 tests pass, 3,43 giây. Nó kiểm chứng parser WASM TypeScript/Python, template Skill, complete-skill unit path và tech-stack fixtures Node/Python/Go/Rust/multi-package. Audit không chạy full suite, build, CLI end-to-end, 14 parser còn lại, remote/MCP/watch/security integration hoặc generation trên repo thật.

## Reuse cho repo2skill

Ba seam khả thi:

- **Dependency/library:** gọi public `pack()` hoặc `parseFile`; ít copy nhưng Skill internals không public, compressed output mất span và processed file chưa có evidence/claim IR.
- **Fork/adapt có chọn lọc:** port file discovery, language registry/query/fixtures, tech-stack parsers và multi-root conventions; thêm evidence tại file/parser capture trước template. Đây là lựa chọn mạnh nếu repo2skill dùng Node/TypeScript.
- **CLI subprocess:** phù hợp làm baseline/export reference bundle, không phù hợp làm lõi provenance vì chỉ nhận file output.

So với Python `agents-md`, Repomix cho nền polyglot và packaging sâu hơn nhưng kéo theo khoảng 30 runtime dependency và pipeline lớn. `agents-md` nhẹ hơn, có concise operational sections, docs dedup và managed update. Lựa chọn hợp lý có thể là giữ claim/provenance lifecycle từ thiết kế repo2skill, rồi tái sử dụng có chọn lọc extractor/test của Repomix thay vì thay toàn bộ baseline. Repomix dùng [MIT](https://github.com/yamadashy/repomix/blob/8d6429121e98ed178e4d3a975c2bdbbecc958c4a/LICENSE); source copy đáng kể phải giữ copyright và permission notice.
