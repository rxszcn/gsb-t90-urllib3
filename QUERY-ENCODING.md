# 查询串两套编码规矩：现状读数

复现：`PYTHONPATH=src .venv/bin/python repro/query-two-engines.py`，脚本在进程内开 socket 服务，下面八行都是服务端 `recv` 到的真实请求行首行，不是客户端视角。

## 一、八行请求行

| 用例 | 服务器收到的请求行 |
| --- | --- |
| inline path+query, space | `GET /x?a=b%20c HTTP/1.1` |
| dict fields, space | `GET /x?a=b+c HTTP/1.1` |
| inline query with '#' | `GET /x?a=b HTTP/1.1` |
| dict fields with '#' | `GET /x?a=b%23c HTTP/1.1` |
| inline query stray '%' | `GET /x?a=100%25 HTTP/1.1` |
| dict fields stray '%' | `GET /x?a=100%25 HTTP/1.1` |
| inline query tilde | `GET /x?a=~b HTTP/1.1` |
| dict fields tilde | `GET /x?a=~b HTTP/1.1` |

逐字符解释：

- **空格**：inline 路把空格按 RFC 3986 query 里的非法字节处理，逐字节百分号编码成 `%20`；dict fields 路走 `urllib.parse.urlencode`（底层 `quote_plus`），它遵循 `application/x-www-form-urlencoded` 形状，空格编码成 `+`。同一个字符，两套规矩。
- **`#`**：inline 的 `/x?a=b#c` 是一个原始 request target，`_TARGET_RE`（`src/urllib3/util/url.py:60`，`^(/[^?#]*)(?:\?([^#]*))?(?:#.*)?$`）把 `#c` 当 fragment 匹配后直接丢弃，query 组只捕获到 `a=b`，请求行里 `#c` 整段消失。fields 字典里的 `b#c` 是**数据值**，没有 fragment 概念，`urlencode` 把 `#` 编成 `%23` 原样进查询串。所以一个被当语法丢掉，一个被当数据转义。
- **`~`**：两路一致地放行。inline 路的允许字符集 `_QUERY_CHARS`（`src/urllib3/util/url.py:83`）经 `_PATH_CHARS` → `_USERINFO_CHARS` 包含 `_UNRESERVED_CHARS`（`src/urllib3/util/url.py:77`），其中有 `~`；`quote_plus` 的 always-safe 集合（字母数字与 `_.-~`）也含 `~`。RFC 3986 起 `~` 属于 unreserved，两边都不编码。
- **`%`**：两行恰好相同（`%25`），原因见第三节——这是唯一一处"结果碰巧一致"，不是"规矩一致"。

## 二、两条路各走谁的编码

**dict fields 路：标准库 `urllib.parse.urlencode`。**

- 入口是 `RequestMethods.request_encode_url`，`src/urllib3/_request_methods.py:180` 一句 `url += "?" + urlencode(fields)`。
- 触发条件：`pool.request(...)` 时方法名在 `_encode_url_methods = {"DELETE", "GET", "HEAD", "OPTIONS"}`（`src/urllib3/_request_methods.py:49`，分派在同文件 134 行）且 `fields` 非空；POST/PUT/PATCH 等走 `request_encode_body`，fields 进 body，与本表无关。
- `urlencode` 默认 `quote_via=quote_plus`：空格→`+`，`#`→`%23`，`%`→`%25`，`~` 保留。拼好的 URL 交给 `urlopen` 后，仍会再过一道下面的库内编码（见 `src/urllib3/connectionpool.py:714`），但本批用例里 `urlencode` 的产物已全是允许字符，第二道走快速路径不改写。

**inline 路：库内自己的"按允许字符集放行"`_encode_invalid_chars`。**

- 同名函数定义在 `src/urllib3/util/url.py:277`，查询串这一段由 `_encode_target`（`src/urllib3/util/url.py:453`）以 `_QUERY_CHARS` 为允许集调用（466 行）。
- 触发条件有两个：`HTTPConnectionPool.urlopen` 收到以 `/` 开头的 target 时直接调 `_encode_target`（`src/urllib3/connectionpool.py:714`）；收到绝对 URL 时走 `parse_url`，query 在 `src/urllib3/util/url.py:555` 用同一个 `_encode_invalid_chars(query, _QUERY_CHARS)` 处理，fragment 单独在 557 行处理，`PoolManager` 再经 `Url.request_uri`（`src/urllib3/util/url.py:172`，调用点 `src/urllib3/poolmanager.py:459`）取出。
- 规矩是逐字节判断：字节在允许字符集里就放行，其余（含空格、`#`、游离 `%`）编成 `%XX`；fragment 在正则阶段就被切掉，不进请求行。

即：fields 路是"表单编码形状（quote_plus）先编码，再被 RFC 3986 允许集校验"，inline 路是"原始 target 直接按 RFC 3986 允许集逐字节编码"。

## 三、`100%` 两行为什么相同，那道判据可不可靠

- fields 路：`%` 对 `quote_plus` 是普通数据字符，编成 `%25`，得 `a=100%25`。
- inline 路：`100%` 不含合法的 `%XX` 转义，`_encode_invalid_chars` 判定它"整体未编码"，于是每个非法字节都编，游离的 `%` 被编成 `%25`，也得 `a=100%25`。两边在"裸 `%`"这一个输入上殊途同归。

那道"整段是否已编码"的判据在 `src/urllib3/util/url.py:308`：

```python
is_percent_encoded = percent_encodings == uri_bytes.count(b"%")
```

`percent_encodings` 是 `_PERCENT_RE`（形如 `%XX`）匹配到的个数，右边是整段里 `%` 字节的总数；两者相等就认为每个 `%` 都属于合法转义、全部放行（313 行），否则把所有 `%` 当普通字节编码。另外 295 行附近有快速路径：不含 `%`、纯 ASCII 且全在允许集内时直接原样返回。

**结论：拿它当判据会误判，双向都会。**实测（同一函数）：

- `_encode_target("/x?a=%41%")` → `/x?a=%2541%25`
- `_encode_target("/x?a=%41 %2")` → `/x?a=%2541%20%252`

依据：段内只要混入一个游离 `%`，计数就不等，**连本来合法的 `%41` 也被整体推翻、二次编码成 `%2541`**（假阴性：已编码部分被破坏）；反过来，inline 路**无法表达"字面量 `100%25`"这个数据**——写 `100%25` 会被计数判为"已编码"原样放行（`_encode_target("/x?a=100%25")` 原样返回），想传六个字面字符反而会被服务器解成 `100%`（假阳性：字面转义文本无法表达）。只有当整段"全部已编码"或"完全不含 `%`"这两种纯净输入时判据才正确；fields 路用 `quote_plus` 统一重编码，不依赖这道启发式，所以 fields 侧没有这个歧义。

## 四、收口：能不能并成一个入口

**物理入口其实已经是一个**：两条路最终都汇到 `urlopen` 里的 `_encode_target` / `_encode_invalid_chars`（`src/urllib3/connectionpool.py:714`），fields 路是 `urlencode` 的产物再过一遍允许集。真正分叉的只有"fields 数据用什么形状预编码"这一处：`quote_plus`（表单形，空格 `+`）对 RFC 3986 允许集（空格 `%20`）。

**我的选择：并到库内 `_encode_invalid_chars`（RFC 3986 query 允许集）这一侧，放弃 `urlencode` 的 `quote_plus` 形状。**理由：请求行最终必须是合法 RFC 3986 target，允许集编码本来就是最后一道闸；`quote_plus` 的 `+` 在 query 里有歧义（可能是空格，也可能是字面加号），`%20` 无歧义。

并完后八行里的变化：

- 只有 `dict fields, space` 一行变：`GET /x?a=b+c HTTP/1.1` → `GET /x?a=b%20c HTTP/1.1`，与 inline 行拉平。
- `#`（`%23`）、游离 `%`（`%25`）、`~`（放行）三对本来就相同，保持不变；inline 侧 `#` 作为 fragment 被丢弃是语法语义，与数据编码无关，合并不影响。

**代价由 fields 调用方付**：线上请求行字节变化（`+`→`%20`），依赖精确报文形状的断言/录制流量/非标准解析器会受影响；标准 query 解析（如 `urllib.parse.parse_qs`）对 `+` 和 `%20` 都按空格解，语义不变。反向选择（让 inline 路改用 `quote_plus`）代价大得多：`&`、`=`、`;`、`/`、`?` 等结构字符会被当数据编成 `%XX`，手写查询串的结构被破坏，已有的 `%20` 这类转义会被二次编码——所以不选。

## 附：测试

`PYTHONPATH=src .venv/bin/python -m pytest test/test_connectionpool.py -q -m "not requires_network"` 连跑两遍，均为 `102 passed`。本文档只新增本文件，未改动 src/ 与 test/ 下任何内容。
