# TODO

## Chat Tool Budget

- Issue: Chat can return `（工具调用次数已达上限，请缩小问题范围后重试。）` when Claude keeps requesting read-only knowledge tools instead of producing a final answer.
- Likely causes: broad user questions, scattered search results, or tool-call loops across `kb_search`, `kb_get_node`, `kb_get_neighbors`, and `kb_get_sources`.
- Current mitigation: `chat.max_tool_rounds` is configured in `config/system.yaml` and the chat system prompt now tells the model to stay within a fixed tool budget.
- Future improvement: add lightweight observability for per-message tool rounds and tool names, then tune the budget/prompt based on real usage.

## Chat Citation Rendering

- Issue: Some assistant messages still render raw citation text such as `[art_5539136a191a] "Rebels in our own time"` instead of turning it into a clickable knowledge-node link.
- Known attempted mitigation: the frontend chat Markdown renderer recognizes standard Markdown links, bare `[art_*]` / `[ent_*]` / `[sum_*]` / `[idx_*]` node ids, source lines with `来源:` / `Sources:`, and Markdown tables.
- Gap: real model output can still contain citation formats that bypass the current line-level parser, especially mixed prose plus quoted node titles.
- Future improvement: replace the ad hoc renderer with a proper Markdown pipeline plus a citation transform plugin, or normalize citations server-side before streaming/saving assistant messages.
