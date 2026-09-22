# 查询串编码：两条流水线，两套规矩

复现：`PYTHONPATH=src .venv/bin/python repro/query-two-engines.py`（进程内 socket 服务抓真实请求行）。
基线：urllib3 2.8.0。本文只描述现状，不改实现。

## 一、八行请求行的确切读数

服务器实际收到的请求行（`repro/query-two-engines.py` 输出）：

| 用例 | 请求行 |
|---|---|
| inline path+query, space | `GET /x?a=b%20c HTTP/1.1` |
| dict fields, space | `GET /x?a=b+c HTTP/1.1` |
| inline query with `#` | `GET /x?a=b HTTP/1.1` |
| dict fields with `#` | `GET /x?a=b%23c HTTP/1.1` |
| inline query stray `%` | `GET /x?a=100%25 HTTP/1.1` |
| dict fields stray `%` | `GET /x?a=100%25 HTTP/1.1` |
| inline query tilde | `GET /x?a=~b HTTP/1.1` |
| dict fields tilde | `GET /x?a=~b HTTP/1.1` |

- **空格**：内联路走 `_encode_invalid_chars`，空格不在 `_QUERY_CHARS` 允许集里，按字节百分号编码成 `%20`；fields 路走标准库 `urlencode`（默认 `quote_via=quote_plus`），表单规则下空格编码成 `+`。
- **`#`**：内联路里 `#` 根本没到编码器——`_TARGET_RE`（`src/urllib3/util/url.py:60`，`^(/[^?#]*)(?:\?([^#]*))?(?:#.*)?$`）先把 `#c` 当片段切掉，片段不上请求行，所以整段丢掉；fields 路里 `#` 只是字典 value 的普通字符，`urlencode` 把它编码成 `%23`，拼进 query 后不再含字面 `#`，`_TARGET_RE` 无从切分。
- **波浪号**：`~` 是 RFC 3986 unreserved 字符，既在 `_QUERY_CHARS` 允许集里（原样放行），也在 `quote_plus` 的 never-quote 名单里（原样放行），两条路结果一致，都是 `~`。

## 二、两条路各走谁的编码

- **fields 字典路（标准库 `urlencode` 那套形状）**：`RequestMethods.request()` 在 `src/urllib3/_request_methods.py:134` 判断 `method in self._encode_url_methods`（第 49 行，集合为 `{"DELETE", "GET", "HEAD", "OPTIONS"}`），命中则进 `request_encode_url()`，在 `src/urllib3/_request_methods.py:180` 执行 `url += "?" + urlencode(fields)`——就是 `urllib.parse.urlencode`，默认 `quote_via=quote_plus`，`application/x-www-form-urlencoded` 形状。触发条件：调用 `request()`/`request_encode_url()` 且传了 `fields`，方法属于上述四个。POST/PUT 等不在集合里的方法走 `request_encode_body()`（`src/urllib3/_request_methods.py:269` 同样用 `urlencode`），但那是写请求体，不上请求行。
- **内联 URL 路（库里自己按允许字符集放行的函数）**：无论 fields 路拼出来的还是用户直接写的 URL，最终都进 `HTTPConnectionPool.urlopen()`，在 `src/urllib3/connectionpool.py:712-714`：`url.startswith("/")` 时调 `_encode_target(url)`（`src/urllib3/util/url.py:453`），它用 `_TARGET_RE` 切出 path/query、丢弃片段，再对 query 调 `_encode_invalid_chars(query, _QUERY_CHARS)`（`src/urllib3/util/url.py:277`，允许集 `_QUERY_CHARS` 定义在第 83 行：unreserved + sub-delims + `:` `@` `/` `?`）。触发条件：任何以 `/` 开头的请求目标，每次 `urlopen` 都会过这道闸——fields 路拼好的 URL 也会再过一遍，只是 `urlencode` 产出的字符全在允许集内、且 `%XX` 被识别为已编码，所以是空操作。

## 三、`100%` 那组为什么两行相同

- fields 路：`urlencode` 无条件把 `%` 编码成 `%25`，得 `a=100%25`。
- 内联路：`_encode_invalid_chars` 里的"整段已编码就放行"判断在 `src/urllib3/util/url.py:303-310`：先用 `_PERCENT_RE`（第 17 行，`%[a-fA-F0-9]{2}`）把已有 `%XX` 序列挑出来并统一大写，再算 `is_percent_encoded = percent_encodings == uri_bytes.count(b"%")`——**组件里每个 `%` 都属于某个 `%XX` 序列**才视为整体已编码，此时 `%` 原样放行；否则所有 `%` 一律编码成 `%25`。`a=100%` 里孤立的 `%` 不构成 `%XX`，判据不成立，编码成 `a=100%25`。两路恰好殊途同归。
- **拿它当判据会不会误判：会，两个方向都会。** 判据是整段全有或全无，不看语义：
  - 双重编码（漏放行）：`a=%41+x%` 混了一个合法 `%41` 和一个孤立 `%`，判据不成立，结果 `%41` 被再编码成 `%2541`（实测 `_encode_target('/x?a=%41+x%')` → `/x?a=%2541+x%25`）。
  - 该编不编（误放行）：用户想发字面文本 `%2f`，`a=%2f` 被判为已编码原样放行（实测 → `/x?a=%2F`，顺带被大写归一），服务器端会解码成 `/`，与本意不符。
- 结论：这个判据只能当"避免重复编码的启发式"，不能当"输入是否已编码"的可靠判据；fields 路根本不做这种判断，所以两组 `100%` 结果相同只是巧合地都编了一次。

## 四、收口：能不能并成一个入口

能。`_encode_target` 本来就是所有请求行出门前的唯一闸口（`connectionpool.py:714`），把 fields 路的产出不经过 `urlencode` 默认形状、改成同一套 RFC 3986 形状即可并轨：具体做法是让 `request_encode_url()` 用 `urlencode(fields, quote_via=quote)`（空格出 `%20` 而非 `+`），拼好的 URL 照旧过 `_encode_target` 兜底。

并完后哪些请求行会变（对照第一节八行）：

- `dict fields, space`：`GET /x?a=b+c` → `GET /x?a=b%20c`。**唯一变化的一行。**
- 其余七行不变：`#` 仍被 `urlencode` 编成 `%23`（`_TARGET_RE` 看不到字面 `#`），`%` 仍出 `%25`，`~` 两边本来就一致，内联四行完全不经过 fields 路。

我选并到 `_encode_target`/`_encode_invalid_chars` 这一边（RFC 3986 形状），理由：它是现有唯一闸口，语义上是"请求目标该长什么样"的正解；`quote_plus` 的 `+` 是 HTML 表单的历史形状，放在 request-target 里属于客串。代价由谁付：改的是 fields 路用户的线上字节——凡是服务器端不做标准 form 解码、按原始字节比对 query 的（自研解析、对 query 串做签名校验的调用方），`+` 变 `%20` 会让签名/比对失效，这批用户要为并轨买单；走标准 form 解码的服务器无感（`+` 和 `%20` 都解码成空格）。内联路用户零成本。

---

验证：`PYTHONPATH=src .venv/bin/python -m pytest test/test_connectionpool.py -q -m "not requires_network"` 连跑两遍，均为 `102 passed`，条数一致。
