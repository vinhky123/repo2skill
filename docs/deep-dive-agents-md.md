# Deep dive: `osmaneb23/agents-md`

## Phạm vi, nguồn và cách kiểm chứng

Phân tích này đi sâu vào mã và runtime của `agents-md` tại commit bất biến [`346b5f78dcbb48d9a700df98ac7f5f8e02f751ea`](https://github.com/osmaneb23/agents-md/tree/346b5f78dcbb48d9a700df98ac7f5f8e02f751ea). Checkout đọc riêng nằm ngoài workspace sản phẩm tại `C:\Users\Kyyy\AppData\Local\Temp\repo2skill-agents-md-audit-805353244d804b209af84fedba0dce34\src`; `git status --short` sạch sau khi đọc và test. Audit license/attribution và ma trận module đã có ở [upstream-reuse-audit.md](./upstream-reuse-audit.md); ở đây chỉ nhắc rằng source là MIT và package `agent-context-md` 0.2.3 đang ở classifier Alpha.

Core yêu cầu Python `>=3.11` và không có runtime dependency. SDK Anthropic, OpenAI, Gemini cùng `build`/`pytest` chỉ là extras ([`pyproject.toml`](https://github.com/osmaneb23/agents-md/blob/346b5f78dcbb48d9a700df98ac7f5f8e02f751ea/pyproject.toml#L1-L37)). Test runtime dùng CPython 3.13.15, Windows 11 `10.0.26300`, pytest 9.1.1 trong virtualenv riêng; không cài package upstream, không gọi provider, không đọc credential.

Lệnh tái hiện suite:

```powershell
$env:PYTHONPATH = "$checkout\src"
& "$venv\Scripts\python.exe" -m pytest -q
```

Kết quả có log tại `C:\Users\Kyyy\AppData\Local\Temp\repo2skill-agents-md-audit-805353244d804b209af84fedba0dce34\pytest-upstream.log`: exit code 1, **53 passed, 3 failed**, 0,89 giây ở lần ghi log. Hai lỗi là `WinError 1314` vì tài khoản không có quyền tạo symlink. Lỗi golden fixture còn lại xuất phát từ fingerprint raw bytes: checkout có `core.autocrlf=true`, `git ls-files --eol` báo `i/lf w/crlf`; đổi CRLF về LF tạo đúng SHA-256 của Git blob và đúng hash golden. Đây là bằng chứng platform sensitivity, không chứng minh suite pass trên môi trường CI Ubuntu. Các test đã đọc không có fixture Go/Rust, provider network hay monorepo package scan.

## Pipeline thực tế và mô hình dữ liệu

`init` chạy tuần tự: kiểm tra overwrite/provider, `scan_repo`, render Markdown tĩnh, tùy chọn gửi **toàn bộ draft** qua LLM, rồi ghi file ([`cli.py`](https://github.com/osmaneb23/agents-md/blob/346b5f78dcbb48d9a700df98ac7f5f8e02f751ea/src/agents_md/cli.py#L95-L159)). `update` không dùng LLM; nó thay nội dung giữa managed markers và refresh fingerprint ([`cli.py`](https://github.com/osmaneb23/agents-md/blob/346b5f78dcbb48d9a700df98ac7f5f8e02f751ea/src/agents_md/cli.py#L160-L197)). Scanner đọc docs trước, lần lượt chạy general, JS, Python, Go, Rust, task files, conventions, exact-dedup dataclass và warnings ([`scanner.py`](https://github.com/osmaneb23/agents-md/blob/346b5f78dcbb48d9a700df98ac7f5f8e02f751ea/src/agents_md/scanner.py#L56-L73)). Không có parallel extractor.

IR chỉ gồm `StackFact`, `CommandFact`, `ConventionFact`, `ScanWarning` và `ScanResult`; `source` là chuỗi, `_markdown_facts` được gắn động vào result ([`types.py`](https://github.com/osmaneb23/agents-md/blob/346b5f78dcbb48d9a700df98ac7f5f8e02f751ea/src/agents_md/types.py#L7-L53)). Renderer nhóm stack, chọn tối đa command đầu tiên cho mỗi category, dựng testing/boundaries/security, rồi gắn fingerprint ([`generator.py`](https://github.com/osmaneb23/agents-md/blob/346b5f78dcbb48d9a700df98ac7f5f8e02f751ea/src/agents_md/generator.py#L15-L56), [`generator.py`](https://github.com/osmaneb23/agents-md/blob/346b5f78dcbb48d9a700df98ac7f5f8e02f751ea/src/agents_md/generator.py#L88-L181)). Trong repo đa ngôn ngữ, thứ tự scanner làm lựa chọn “command đầu tiên” thiên về JS rồi Python; các command hợp lệ còn lại không vào output chính.

## Traversal, parser và coverage thật

Traversal source là DFS có thứ tự tên, bỏ symlink và một tập thư mục hard-code; nó không đọc `.gitignore`. Mỗi lần gọi đi lại cây và dừng ở limit: 1 file để phát hiện ngôn ngữ, 160 file JS/TS cho convention, 80 file Python ([`scanner.py`](https://github.com/osmaneb23/agents-md/blob/346b5f78dcbb48d9a700df98ac7f5f8e02f751ea/src/agents_md/scanner.py#L731-L754)). Vì vậy kết quả bị ảnh hưởng bởi thứ tự path và có thể bỏ sót convention sau ngưỡng. Docs ở root và `docs/**/*.md` không có giới hạn số lượng/kích thước. JSON/TOML dùng stdlib parser nhưng lỗi parse bị đổi thành `None`/`{}` không kèm diagnostic; Make/Just/Taskfile được đọc bằng regex dòng, Taskfile chỉ nhận key thụt đúng hai space ([`scanner.py`](https://github.com/osmaneb23/agents-md/blob/346b5f78dcbb48d9a700df98ac7f5f8e02f751ea/src/agents_md/scanner.py#L707-L788)).

Monorepo chỉ là boolean khi thấy workspace marker hoặc `package.json.workspaces`; generator in note “package-specific instructions not yet configured”, không scan package con ([`scanner.py`](https://github.com/osmaneb23/agents-md/blob/346b5f78dcbb48d9a700df98ac7f5f8e02f751ea/src/agents_md/scanner.py#L138-L149)). JS/TS cần `package.json` ở root; convention dùng regex, không dùng AST. Python dùng AST giới hạn để tìm test function/lớp `*Error`, còn dependency/config là TOML. Go chỉ substring-match framework trong `go.mod`; Rust chỉ kiểm tra `Cargo.toml` và `tests/` ([`scanner.py`](https://github.com/osmaneb23/agents-md/blob/346b5f78dcbb48d9a700df98ac7f5f8e02f751ea/src/agents_md/scanner.py#L208-L266)).

Các default đáng cảnh giác: JS không có lockfile sẽ dùng prefix npm và phát warning; single-test Jest/Vitest/Mocha chứa placeholder. Python ưu tiên Pixi→uv→Poetry→Pipenv→pip, nhưng nhánh install không xử lý Pipenv; repo có `tests/` nhưng chỉ khai báo dependency pytest, không có `[tool.pytest]`, vẫn bị suy ra unittest. Có optional group `dev` thì sinh `python -m build` dù chưa chứng minh `build` tồn tại. Go luôn sinh `go test ./...` và `-run <TestName>`; Rust luôn sinh `cargo build`, `cargo test`, `cargo test <test_name>` ([`scanner.py`](https://github.com/osmaneb23/agents-md/blob/346b5f78dcbb48d9a700df98ac7f5f8e02f751ea/src/agents_md/scanner.py#L576-L657)). Đây là template/heuristic, không phải command quan sát.

## Dedup, freshness và LLM guard

Dedup docs lowercase, bỏ Markdown/punctuation rồi chỉ loại khi **toàn bộ normalized generated line** trùng exact fact; boundary/security luôn được giữ. Log chỉ chứa text và lý do, không có source span hay fact identity ([`dedup.py`](https://github.com/osmaneb23/agents-md/blob/346b5f78dcbb48d9a700df98ac7f5f8e02f751ea/src/agents_md/dedup.py#L7-L74)). Nó không phải semantic dedup và không bảo toàn proof chain.

Fingerprint SHA-256 chỉ bao phủ Markdown docs, danh sách key file root và mọi file workflow; nó không bao phủ source files dù source quyết định convention ([`fingerprint.py`](https://github.com/osmaneb23/agents-md/blob/346b5f78dcbb48d9a700df98ac7f5f8e02f751ea/src/agents_md/fingerprint.py#L10-L105)). Probe runtime tại `...\probe-fingerprint-gap`, log `...\fingerprint-gap.log`, đã đổi `src/api/client.ts` từ named constant sang `fetch('/status')`: scanner thêm convention “HTTP calls appear centralized”, nhưng `compare_fingerprints` trả `added/changed/removed=[]`. Tái hiện bằng cách chạy trước/sau thay đổi:

```powershell
$env:PYTHONPATH = "$checkout\src"
python -c "from pathlib import Path; from agents_md.scanner import scan_repo; from agents_md.fingerprint import fingerprint_repo; r=Path(r'$probe'); print(fingerprint_repo(r)); print([x.text for x in scan_repo(r).conventions])"
```

LLM layer gọi SDK trực tiếp hoặc Ollama HTTP với prompt giữ markers/fingerprint ([`llm.py`](https://github.com/osmaneb23/agents-md/blob/346b5f78dcbb48d9a700df98ac7f5f8e02f751ea/src/agents_md/llm.py#L56-L149)). Guard sau response chỉ kiểm tra có một start marker và một end marker; không xác minh đủ section, fingerprint, command, provenance hay kích thước. Vì vậy transport này không phù hợp để tái sử dụng cho typed Codex subprocess.

## Kết luận tái sử dụng

Nguồn này vẫn là baseline tốt cho CLI behavior, marker replacement, static extractors nhỏ và zero-dependency packaging. Nó không đủ làm provenance/freshness engine: traversal không incremental, fingerprint bỏ source input, IR làm mất bằng chứng, dedup exact-line và LLM guard quá yếu. Nên port chọn lọc ở SHA đã ghim, thêm typed evidence tại extractor và dependency ledger trước renderer. Khuyến nghị này là **tạm thời** cho đến khi so sánh sâu cùng Repomix và Rulesync; không nên chọn upstream này làm dependency/API ổn định vì package Alpha, README chủ yếu mô tả CLI, `__init__.py` chỉ export version và hầu hết seam hữu ích là module nội bộ ([`__init__.py`](https://github.com/osmaneb23/agents-md/blob/346b5f78dcbb48d9a700df98ac7f5f8e02f751ea/src/agents_md/__init__.py#L1-L3)).
